"""Sales-order domain logic: validation, tax, workflow transitions, Tally payload.

State machine
-------------
    DRAFT --submit--> PENDING_APPROVAL --approve--> APPROVED  (+ outbox row, same transaction)
      ^                    |   |
      +----withdraw--------+   +--reject--> REJECTED --edit/submit--> PENDING_APPROVAL
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import GST_STATES
from app.core.utils import clean_text, financial_year, format_inr, parse_decimal, q2, q3, today_ist
from app.models import (
    Item,
    NumberSequence,
    OutboxStatus,
    Party,
    Role,
    Plant,
    SalesLedger,
    SalesOrder,
    SalesOrderItem,
    SOStatus,
    SOType,
    TallyOutbox,
    TallyStatus,
    User,
)
from app.models.base import utcnow
from app.services import notify, settings_service
from app.services.audit import Ctx, audit
from app.services.tax import compute_totals, ledger_name, tax_mode_for

MAX_LINES = 50
MAX_QTY = Decimal("100000")        # MT per line
MAX_RATE = Decimal("100000000")    # Rs per MT
BACKDATE_DAYS = 400
SALES_LEDGER_NAME = "Sales GST"   # every sales order uses this ledger; it is shown, never chosen


class OrderError(Exception):
    """Business-rule violation that is safe to show to the user."""


class NotAllowed(OrderError):
    pass


# --------------------------------------------------------------------------- input parsing
@dataclass(slots=True)
class LineInput:
    item_id: int | None = None
    quantity_raw: str = ""
    rate_raw: str = ""
    quantity: Decimal | None = None
    rate: Decimal | None = None


@dataclass(slots=True)
class OrderForm:
    so_type: str = ""
    order_date: str = ""
    party_id: str = ""
    payment_term: str = ""
    customer_po_number: str = ""
    billing_address: str = ""
    billing_state_code: str = ""
    shipping_address: str = ""
    shipping_state_code: str = ""
    plant_id: str = ""
    remarks: str = ""
    lines: list[LineInput] = field(default_factory=list)


def parse_form(data) -> OrderForm:  # data: starlette FormData
    f = OrderForm(
        so_type=clean_text(data.get("so_type"), 32),
        order_date=clean_text(data.get("order_date"), 10),
        party_id=clean_text(data.get("party_id"), 10),
        payment_term=clean_text(data.get("payment_term"), 200),
        customer_po_number=clean_text(data.get("customer_po_number"), 60),
        billing_address=clean_text(data.get("billing_address"), 600),
        billing_state_code=clean_text(data.get("billing_state_code"), 2),
        shipping_address=clean_text(data.get("shipping_address"), 600),
        shipping_state_code=clean_text(data.get("shipping_state_code"), 2),
        plant_id=clean_text(data.get("plant_id"), 10),
        remarks=clean_text(data.get("remarks"), 500),
    )
    ids = data.getlist("item_id")[: MAX_LINES + 1]
    qtys = data.getlist("quantity")[: MAX_LINES + 1]
    rates = data.getlist("rate")[: MAX_LINES + 1]
    for i in range(max(len(ids), len(qtys), len(rates))):
        raw_id = clean_text(ids[i], 10) if i < len(ids) else ""
        qty = clean_text(qtys[i], 20) if i < len(qtys) else ""
        rate = clean_text(rates[i], 20) if i < len(rates) else ""
        if not (raw_id or qty or rate):
            continue  # fully blank row
        f.lines.append(LineInput(item_id=int(raw_id) if raw_id.isdigit() else None, quantity_raw=qty, rate_raw=rate))
    return f


@dataclass(slots=True)
class Validated:
    so_type: SOType
    order_date: date
    party: Party
    payment_term: str
    customer_po_number: str
    billing_address: str
    billing_state_code: str
    shipping_address: str
    shipping_state_code: str | None
    plant: Plant
    ledger: SalesLedger
    narration: str
    remarks: str
    lines: list[tuple[Item, Decimal, Decimal]]  # (item, qty, rate)


def sales_ledger(db: Session) -> SalesLedger:
    """The one sales ledger every order uses ("Sales GST"). Created on first use if it does not exist yet."""
    row = db.execute(
        select(SalesLedger).where(SalesLedger.name == SALES_LEDGER_NAME, SalesLedger.deleted_at.is_(None)).order_by(SalesLedger.id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = SalesLedger(name=SALES_LEDGER_NAME, is_active=True)
        db.add(row)
        db.flush()
    return row


def _qty_text(q: Decimal) -> str:
    return format(q.normalize(), "f")


def build_narration(party_name: str, lines: list[tuple[Item, Decimal, Decimal]]) -> str:
    """"We sold 100 MT of Ultrafine to Alpha Traders at Rs 12,500.00 per MT." (one sentence per item)."""
    return " ".join(
        f"We sold {_qty_text(q)} {it.unit} of {it.name} to {party_name} at Rs {format_inr(r)} per {it.unit}." for it, q, r in lines
    )


def tally_narration(order: SalesOrder) -> str:
    """What goes into Tally's narration field: the generated narration followed by the sales person's remarks."""
    return f"{order.narration} {order.remarks}".strip()


def allowed_plants(db: Session, user: User) -> list[Plant]:
    """Active plants this user may raise orders for. Sales people only get the plants an admin assigned to them."""
    plants = db.execute(select(Plant).where(Plant.deleted_at.is_(None), Plant.is_active.is_(True)).order_by(Plant.name)).scalars().all()
    if user.role is Role.SALES:
        mine = {p.id for p in user.plants}
        return [p for p in plants if p.id in mine]
    return list(plants)


def validate(db: Session, f: OrderForm, *, user: User, exclude_order_id: int | None = None) -> tuple[Validated | None, dict[str, str]]:
    errors: dict[str, str] = {}

    try:
        so_type = SOType(f.so_type)
    except ValueError:
        so_type = None  # type: ignore[assignment]
        errors["so_type"] = "Choose the type of sales order."

    od: date | None = None
    try:
        od = date.fromisoformat(f.order_date)
        today = today_ist()
        if od > today:
            errors["order_date"] = "The date cannot be in the future."
        elif od < today - timedelta(days=BACKDATE_DAYS):
            errors["order_date"] = "The date is too far in the past."
    except ValueError:
        errors["order_date"] = "Enter a valid date."

    party = None
    if f.party_id.isdigit():
        party = db.execute(select(Party).where(Party.id == int(f.party_id), Party.deleted_at.is_(None), Party.is_active.is_(True))).scalar_one_or_none()
    if party is None:
        errors["party_id"] = "Select a customer from the list."

    if not f.payment_term:
        errors["payment_term"] = "Enter the payment term from the customer PO."
    if not f.customer_po_number:
        errors["customer_po_number"] = "Enter the customer PO number."
    if len(f.billing_address) < 5:
        errors["billing_address"] = "Enter the billing address from the customer PO."
    if f.billing_state_code not in GST_STATES:
        errors["billing_state_code"] = "Choose the billing state."
    if len(f.shipping_address) < 5:
        errors["shipping_address"] = "Enter the shipping address from the customer PO."
    if f.shipping_state_code and f.shipping_state_code not in GST_STATES:
        errors["shipping_state_code"] = "Choose a valid shipping state."

    plant = None
    if f.plant_id.isdigit():
        plant = next((p for p in allowed_plants(db, user) if p.id == int(f.plant_id)), None)
    if plant is None:
        errors["plant_id"] = ("Select the plant this order is supplied from." if allowed_plants(db, user)
                              else "No plant is assigned to you yet. Ask an administrator to assign your regions.")

    if not f.remarks.strip():
        errors["remarks"] = "Enter the remarks. Even a single word such as OK is fine."

    lines: list[tuple[Item, Decimal, Decimal]] = []
    if not f.lines:
        errors["lines"] = "Add at least one item."
    elif len(f.lines) > MAX_LINES:
        errors["lines"] = f"An order can have at most {MAX_LINES} items."
    else:
        wanted = {ln.item_id for ln in f.lines if ln.item_id}
        items = {}
        if wanted:
            items = {i.id: i for i in db.execute(select(Item).where(Item.id.in_(wanted), Item.deleted_at.is_(None), Item.is_active.is_(True))).scalars()}
        for idx, ln in enumerate(f.lines):
            key = f"line_{idx}"
            item = items.get(ln.item_id or -1)
            qty = parse_decimal(ln.quantity_raw, places=3)
            rate = parse_decimal(ln.rate_raw, places=2)
            if item is None:
                errors[key] = "Select the item as named in the customer PO."
            elif qty is None or qty <= 0 or qty > MAX_QTY:
                errors[key] = f"Quantity must be a number above 0 and up to {MAX_QTY:,} {item.unit} (max 3 decimals)."
            elif rate is None or rate <= 0 or rate > MAX_RATE:
                errors[key] = f"Rate per {item.unit} must be a positive amount (max 2 decimals)."
            else:
                lines.append((item, q3(qty), q2(rate)))

    if party is not None and f.customer_po_number and not errors.get("customer_po_number"):
        dup = db.execute(
            select(SalesOrder.id, SalesOrder.so_number).where(
                SalesOrder.party_id == party.id,
                SalesOrder.customer_po_number == f.customer_po_number,
                SalesOrder.deleted_at.is_(None),
                SalesOrder.status != SOStatus.REJECTED,
                SalesOrder.id != (exclude_order_id or 0),
            ).limit(1)
        ).first()
        if dup:
            ref = dup.so_number or f"draft #{dup.id}"
            errors["customer_po_number"] = f"This customer already has an order for this PO number ({ref})."

    if errors:
        return None, errors
    assert so_type and od and party and plant
    return (
        Validated(so_type, od, party, f.payment_term, f.customer_po_number, f.billing_address, f.billing_state_code,
                  f.shipping_address, f.shipping_state_code or None, plant, sales_ledger(db),
                  build_narration(party.name, lines), f.remarks.strip(), lines),
        {},
    )


# --------------------------------------------------------------------------- permissions
def visible_orders(stmt: Any, user: User) -> Any:
    """Restrict any select over SalesOrder to the rows this user may see."""
    stmt = stmt.where(SalesOrder.deleted_at.is_(None))
    if user.role is Role.SALES:
        stmt = stmt.where(SalesOrder.created_by_id == user.id)
    elif user.role is Role.ACCOUNTS:
        stmt = stmt.where(SalesOrder.status != SOStatus.DRAFT)
    return stmt


def can_view(user: User, order: SalesOrder) -> bool:
    if order.deleted_at is not None and user.role is not Role.ADMIN:
        return False
    if user.role is Role.ADMIN:
        return True
    if user.role is Role.ACCOUNTS:
        return order.status is not SOStatus.DRAFT
    return order.created_by_id == user.id


def can_edit(user: User, order: SalesOrder) -> bool:
    return user.role is Role.SALES and order.created_by_id == user.id and order.is_editable and order.deleted_at is None


def can_review(user: User, order: SalesOrder) -> bool:
    return (
        user.role in (Role.ACCOUNTS, Role.ADMIN)
        and order.status is SOStatus.PENDING_APPROVAL
        and order.created_by_id != user.id  # segregation of duties
        and order.deleted_at is None
    )


def can_delete(user: User, order: SalesOrder) -> bool:
    if order.deleted_at is not None or order.status is SOStatus.APPROVED:
        return False
    if user.role is Role.ADMIN:
        return True
    return user.role is Role.SALES and order.created_by_id == user.id and order.is_editable


def can_withdraw(user: User, order: SalesOrder) -> bool:
    return user.role is Role.SALES and order.created_by_id == user.id and order.status is SOStatus.PENDING_APPROVAL and order.deleted_at is None


# --------------------------------------------------------------------------- persistence
def _apply(db: Session, order: SalesOrder, v: Validated) -> None:
    if v.plant.state_code not in GST_STATES:
        raise OrderError(f"The plant {v.plant.name} has no valid GST state. Ask an administrator to fix it under Master data > Plants.")
    mode = tax_mode_for(v.plant.state_code, v.billing_state_code)
    totals = compute_totals([(q2(q * r), it.gst_rate) for it, q, r in v.lines], mode)

    order.so_type = v.so_type
    order.order_date = v.order_date
    order.party_id = v.party.id
    order.party_name = v.party.name
    order.party_gstin = v.party.gstin
    order.payment_term = v.payment_term
    order.customer_po_number = v.customer_po_number
    order.billing_address = v.billing_address
    order.billing_state_code = v.billing_state_code
    order.shipping_address = v.shipping_address
    order.shipping_state_code = v.shipping_state_code
    order.plant_id = v.plant.id
    order.plant_name = v.plant.name
    order.plant_state_code = v.plant.state_code
    order.sales_ledger_id = v.ledger.id
    order.sales_ledger_name = v.ledger.name
    order.narration = v.narration
    order.remarks = v.remarks
    order.tax_mode = mode
    order.subtotal = totals.subtotal
    order.cgst_amount = totals.cgst
    order.sgst_amount = totals.sgst
    order.igst_amount = totals.igst
    order.total_amount = totals.total

    order.items.clear()
    db.flush()
    for n, (it, q, r) in enumerate(v.lines, start=1):
        order.items.append(
            SalesOrderItem(line_no=n, item_id=it.id, item_name=it.name, hsn_code=it.hsn_code, unit=it.unit,
                           quantity=q, rate=r, gst_rate=it.gst_rate, amount=q2(q * r))
        )


def create_order(db: Session, user: User, v: Validated, ctx: Ctx) -> SalesOrder:
    order = SalesOrder(created_by_id=user.id, status=SOStatus.DRAFT)
    _apply(db, order, v)
    db.add(order)
    db.flush()
    audit(db, ctx, "so_created", entity_type="sales_order", entity_id=order.id,
          details={"party": order.party_name, "total": str(order.total_amount)})
    return order


def update_order(db: Session, user: User, order: SalesOrder, v: Validated, ctx: Ctx) -> None:
    if not can_edit(user, order):
        raise NotAllowed("This order can no longer be edited.")
    _apply(db, order, v)
    audit(db, ctx, "so_updated", entity_type="sales_order", entity_id=order.id,
          details={"party": order.party_name, "total": str(order.total_amount)})


def load_for_update(db: Session, order_id: int) -> SalesOrder | None:
    return db.execute(select(SalesOrder).where(SalesOrder.id == order_id).with_for_update()).scalar_one_or_none()


def _next_number(db: Session, so_type: SOType, d: date) -> str:
    key = f"{so_type.number_prefix}/{financial_year(d)}"
    seq = db.execute(select(NumberSequence).where(NumberSequence.key == key).with_for_update()).scalar_one_or_none()
    if seq is None:
        # Two concurrent first-of-year submits could both insert; the PK makes one fail -> caller retries.
        seq = NumberSequence(key=key, last_value=0)
        db.add(seq)
        db.flush()
    seq.last_value += 1
    return f"{key}/{seq.last_value:05d}"


def submit(db: Session, user: User, order: SalesOrder, ctx: Ctx) -> None:
    if not can_edit(user, order):
        raise NotAllowed("Only the creator can submit an editable order.")
    if order.plant_id is not None and order.plant_id not in {p.id for p in allowed_plants(db, user)}:
        raise NotAllowed("You are no longer assigned to this order's plant. Edit the order and choose one of your plants.")
    if order.so_number is None:
        order.so_number = _next_number(db, order.so_type, order.order_date)
    order.status = SOStatus.PENDING_APPROVAL
    order.submitted_at = utcnow()
    order.reviewed_by_id = None
    order.reviewed_at = None
    order.review_remarks = None
    audit(db, ctx, "so_submitted", entity_type="sales_order", entity_id=order.id, details={"so_number": order.so_number})
    notify.order_submitted(db, order, user)


def withdraw(db: Session, user: User, order: SalesOrder, ctx: Ctx) -> None:
    if not can_withdraw(user, order):
        raise NotAllowed("This order cannot be withdrawn.")
    order.status = SOStatus.DRAFT
    audit(db, ctx, "so_withdrawn", entity_type="sales_order", entity_id=order.id)
    notify.order_withdrawn(db, order, user)


def approve(db: Session, user: User, order: SalesOrder, ctx: Ctx, remarks: str | None = None) -> None:
    if not can_review(user, order):
        raise NotAllowed("You cannot approve this order. Orders you created must be approved by someone else.")
    order.status = SOStatus.APPROVED
    order.reviewed_by_id = user.id
    order.reviewed_at = utcnow()
    order.review_remarks = remarks or None
    queue_for_tally(db, order)
    audit(db, ctx, "so_approved", entity_type="sales_order", entity_id=order.id, details={"so_number": order.so_number})
    notify.order_approved(db, order, user)


def reject(db: Session, user: User, order: SalesOrder, remarks: str, ctx: Ctx) -> None:
    if not can_review(user, order):
        raise NotAllowed("You cannot reject this order.")
    if len(remarks.strip()) < 3:
        raise OrderError("Give a reason so the sales person knows what to fix.")
    order.status = SOStatus.REJECTED
    order.reviewed_by_id = user.id
    order.reviewed_at = utcnow()
    order.review_remarks = remarks.strip()[:500]
    audit(db, ctx, "so_rejected", entity_type="sales_order", entity_id=order.id,
          details={"so_number": order.so_number, "remarks": order.review_remarks})
    notify.order_rejected(db, order, user)


def delete(db: Session, user: User, order: SalesOrder, ctx: Ctx) -> None:
    if not can_delete(user, order):
        raise NotAllowed("This order cannot be deleted.")
    order.soft_delete(user.id)
    audit(db, ctx, "so_deleted", entity_type="sales_order", entity_id=order.id, details={"so_number": order.so_number})
    notify.order_deleted(db, order, user)


# --------------------------------------------------------------------------- Tally payload / outbox
def build_tally_payload(db: Session, order: SalesOrder) -> dict[str, Any]:
    cfg = settings_service.get_all(db)
    totals = compute_totals([(i.amount, i.gst_rate) for i in order.items], order.tax_mode)
    return {
        "voucher_type": order.so_type.short,           # "Sales Order" | "Trading Sales Order"
        "so_number": order.so_number,
        "date": order.order_date.isoformat(),
        "party_ledger": order.party_name,
        "party_gstin": order.party_gstin,
        "payment_term": order.payment_term,
        "billing_address": order.billing_address,
        "billing_state": GST_STATES.get(order.billing_state_code),
        "shipping_address": order.shipping_address,
        "shipping_state": GST_STATES.get(order.shipping_state_code or ""),
        "plant": order.plant_name,
        "plant_state": GST_STATES.get(order.plant_state_code or ""),
        "order_no": order.customer_po_number,          # "PO Number as per Customer PO in Order No."
        "sales_ledger": order.sales_ledger_name,
        "items": [
            {"name": i.item_name, "hsn": i.hsn_code, "quantity": str(i.quantity), "unit": i.unit,
             "rate": str(i.rate), "rate_per": i.unit, "amount": str(i.amount), "gst_rate": str(i.gst_rate)}
            for i in order.items
        ],
        "taxes": [
            {"ledger": ledger_name(cfg[f"tax_ledger_{t.ledger_key}"], t.rate), "rate": str(t.rate), "amount": str(t.amount)}
            for t in totals.lines
        ],
        "subtotal": str(order.subtotal),
        "total": str(order.total_amount),
        "narration": tally_narration(order),          # generated narration + remarks
    }


def queue_for_tally(db: Session, order: SalesOrder) -> TallyOutbox:
    existing = db.execute(select(TallyOutbox).where(TallyOutbox.order_id == order.id)).scalar_one_or_none()
    if existing:
        return existing
    row = TallyOutbox(
        order_id=order.id,
        idempotency_key=hashlib.sha256(f"so:{order.id}:{order.so_number}".encode()).hexdigest(),
        payload=build_tally_payload(db, order),
        status=OutboxStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    db.add(row)
    order.tally_status = TallyStatus.QUEUED
    return row
