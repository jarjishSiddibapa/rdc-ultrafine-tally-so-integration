from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.datastructures import FormData

from app.core.database import get_db
from app.deps import current_user, get_ctx, require_roles
from app.models import (
    AuditLog,
    Item,
    Party,
    Role,
    Plant,
    SalesOrder,
    SOStatus,
    SOType,
    TallyOutbox,
    User,
)
from app.services import orders as svc
from app.services.audit import Ctx
from app.core.utils import today_ist
from app.routers._lists import run_list
from app.services.listing import fmt_duration
from app.services.specs import order_spec
from app.web import redirect, render

router = APIRouter(prefix="/orders")


async def form_data(request: Request) -> FormData:
    return await request.form()


_visible = svc.visible_orders


def _get_order(db: Session, user: User, order_id: int, *, lock: bool = False) -> SalesOrder:
    order = svc.load_for_update(db, order_id) if lock else db.get(SalesOrder, order_id)
    # Same response for "missing" and "not yours": do not reveal which ids exist (IDOR / enumeration).
    if order is None or not svc.can_view(user, order):
        raise HTTPException(404, "Order not found.")
    return order


def _form_context(db: Session, form: svc.OrderForm, errors: dict[str, str], order: SalesOrder | None, user: User) -> dict[str, Any]:
    parties = db.execute(select(Party).where(Party.deleted_at.is_(None), Party.is_active.is_(True)).order_by(Party.name)).scalars().all()
    items = db.execute(select(Item).where(Item.deleted_at.is_(None), Item.is_active.is_(True)).order_by(Item.name)).scalars().all()
    plants = svc.allowed_plants(db, user)  # sales people only see the regions assigned to them
    any_plants = bool(db.execute(select(Plant.id).where(Plant.deleted_at.is_(None), Plant.is_active.is_(True)).limit(1)).first())
    return {
        "any_plants": any_plants,
        "form": form, "errors": errors, "order": order, "parties": parties, "items": items, "ledger_name": svc.SALES_LEDGER_NAME,
        "plants": plants, "today": today_ist().isoformat(),
        "so_types": list(SOType),
    }


def _form_from_order(o: SalesOrder) -> svc.OrderForm:
    return svc.OrderForm(
        so_type=o.so_type.value, order_date=o.order_date.isoformat(), party_id=str(o.party_id), payment_term=o.payment_term,
        customer_po_number=o.customer_po_number, billing_address=o.billing_address, billing_state_code=o.billing_state_code,
        shipping_address=o.shipping_address, shipping_state_code=o.shipping_state_code or "",
        plant_id=str(o.plant_id or ""), remarks=o.remarks,
        lines=[svc.LineInput(item_id=i.item_id, quantity_raw=format(i.quantity.normalize(), "f"), rate_raw=str(i.rate)) for i in o.items],
    )


# --------------------------------------------------------------------------- list
@router.get("")
def list_orders(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    ctx: Ctx = Depends(get_ctx),
):
    defaults = {"status": "pending_approval"} if user.role is Role.ACCOUNTS else {}
    spec = order_spec(db, defaults=defaults)
    st, page, download = run_list(request, db, user, ctx, _visible(select(SalesOrder), user), spec, "Sales orders")
    if download:
        return download
    tab_counts = {s.value: 0 for s in SOStatus}
    for status, n in db.execute(_visible(select(SalesOrder.status, func.count()), user).group_by(SalesOrder.status)).all():
        tab_counts[status.value] = n
    return render(request, "orders_list.html", {
        "st": st, "page": page, "statuses": list(SOStatus),
        "tab_counts": tab_counts, "all_count": sum(tab_counts.values()), "tab": st.raw.get("f_status", ""),
    })


# --------------------------------------------------------------------------- create / edit
@router.get("/new")
def new_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.SALES))):
    form = svc.OrderForm(order_date=today_ist().isoformat(), lines=[svc.LineInput()])
    return render(request, "order_form.html", _form_context(db, form, {}, None, user))


@router.post("/new")
def create(request: Request, data: FormData = Depends(form_data), db: Session = Depends(get_db),
           user: User = Depends(require_roles(Role.SALES)), ctx: Ctx = Depends(get_ctx)):
    form = svc.parse_form(data)
    action = data.get("action")
    v, errors = svc.validate(db, form, user=user)
    if v is not None:
        try:
            order = svc.create_order(db, user, v, ctx)
            if action == "submit":
                svc.submit(db, user, order, ctx)
            db.commit()
        except svc.OrderError as exc:
            db.rollback()
            errors = {"_": str(exc)}
        else:
            if action == "submit":
                return redirect(f"/orders/{order.id}?printed=1", "Order submitted for approval.")
            return redirect(f"/orders/{order.id}", "Draft saved.")
    if not form.lines:
        form.lines.append(svc.LineInput())
    return render(request, "order_form.html", _form_context(db, form, errors, None, user), status_code=422)


@router.get("/{order_id}/edit")
def edit_form(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.SALES))):
    order = _get_order(db, user, order_id)
    if not svc.can_edit(user, order):
        return redirect(f"/orders/{order.id}", "This order can no longer be edited.", "error")
    return render(request, "order_form.html", _form_context(db, _form_from_order(order), {}, order, user))


@router.post("/{order_id}/edit")
def edit(order_id: int, request: Request, data: FormData = Depends(form_data), db: Session = Depends(get_db),
         user: User = Depends(require_roles(Role.SALES)), ctx: Ctx = Depends(get_ctx)):
    order = _get_order(db, user, order_id, lock=True)
    form = svc.parse_form(data)
    action = data.get("action")
    errors: dict[str, str] = {}
    try:
        if not svc.can_edit(user, order):
            raise svc.NotAllowed("This order can no longer be edited.")
        v, errors = svc.validate(db, form, user=user, exclude_order_id=order.id)
        if v is not None:
            svc.update_order(db, user, order, v, ctx)
            if action == "submit":
                svc.submit(db, user, order, ctx)
            db.commit()
            if action == "submit":
                return redirect(f"/orders/{order.id}?printed=1", "Order submitted for approval.")
            return redirect(f"/orders/{order.id}", "Changes saved.")
    except svc.OrderError as exc:
        db.rollback()
        errors = {"_": str(exc)}
    except StaleDataError:
        db.rollback()
        errors = {"_": "Someone else changed this order while you were editing. Reload and try again."}
    db.rollback()
    if not form.lines:
        form.lines.append(svc.LineInput())
    order = db.get(SalesOrder, order_id)
    return render(request, "order_form.html", _form_context(db, form, errors, order, user), status_code=422)


# --------------------------------------------------------------------------- detail + actions
@router.get("/{order_id}")
def detail(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    order = _get_order(db, user, order_id)
    history = db.execute(
        select(AuditLog).where(AuditLog.entity_type == "sales_order", AuditLog.entity_id == order.id).order_by(AuditLog.id.desc()).limit(200)
    ).scalars().all()
    outbox = db.execute(select(TallyOutbox).where(TallyOutbox.order_id == order.id)).scalar_one_or_none()
    def gap(a, b):
        return fmt_duration((b - a).total_seconds()) if a and b and b >= a else None

    gaps = {
        "submitted": gap(order.created_at, order.submitted_at),
        "decision": gap(order.submitted_at, order.reviewed_at),
        "queued": gap(order.reviewed_at, outbox.created_at) if outbox else None,
        "delivered": gap(order.reviewed_at, outbox.sent_at) if outbox else None,
    }
    return render(request, "order_detail.html", {
        "order": order, "history": history, "outbox": outbox, "gaps": gaps,
        "can_edit": svc.can_edit(user, order), "can_review": svc.can_review(user, order),
        "can_delete": svc.can_delete(user, order), "can_withdraw": svc.can_withdraw(user, order),
        "own_pending": order.created_by_id == user.id and order.status is SOStatus.PENDING_APPROVAL,
        # One-off receipt animation right after the creator submits (only the creator, only while it is waiting).
        "print_receipt": request.query_params.get("printed") == "1" and order.created_by_id == user.id
                        and order.status is SOStatus.PENDING_APPROVAL and order.so_number is not None,
    })


def _transition(order_id: int, db: Session, user: User, fn, ok_message: str, redirect_to: str | None = None):
    order = _get_order(db, user, order_id, lock=True)
    try:
        fn(order)
        db.commit()
    except svc.OrderError as exc:
        db.rollback()
        return redirect(f"/orders/{order_id}", str(exc), "error")
    except (StaleDataError, IntegrityError):
        db.rollback()
        return redirect(f"/orders/{order_id}", "This order was just changed by someone else. Check its current status.", "error")
    return redirect(redirect_to or f"/orders/{order_id}", ok_message)


@router.post("/{order_id}/submit")
def submit(order_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.SALES)), ctx: Ctx = Depends(get_ctx)):
    return _transition(order_id, db, user, lambda o: svc.submit(db, user, o, ctx), "Order submitted for approval.",
                       f"/orders/{order_id}?printed=1")


@router.post("/{order_id}/withdraw")
def withdraw(order_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles(Role.SALES)), ctx: Ctx = Depends(get_ctx)):
    return _transition(order_id, db, user, lambda o: svc.withdraw(db, user, o, ctx), "Order withdrawn. You can edit it again.")


@router.post("/{order_id}/approve")
def approve(order_id: int, remarks: str = Form("", max_length=500), db: Session = Depends(get_db),
            user: User = Depends(require_roles(Role.ACCOUNTS, Role.ADMIN)), ctx: Ctx = Depends(get_ctx)):
    return _transition(order_id, db, user, lambda o: svc.approve(db, user, o, ctx, remarks.strip() or None),
                       "Order approved and queued for Tally.")


@router.post("/{order_id}/reject")
def reject(order_id: int, remarks: str = Form("", max_length=500), db: Session = Depends(get_db),
           user: User = Depends(require_roles(Role.ACCOUNTS, Role.ADMIN)), ctx: Ctx = Depends(get_ctx)):
    return _transition(order_id, db, user, lambda o: svc.reject(db, user, o, remarks, ctx), "Order sent back to the sales person.")


@router.post("/{order_id}/delete")
def delete(order_id: int, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    return _transition(order_id, db, user, lambda o: svc.delete(db, user, o, ctx), "Order deleted.", "/orders")
