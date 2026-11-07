"""Dashboard + health endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.utils import IST
from app.deps import current_user
from app.integrations.tally import get_gateway
from app.models import (
    Item,
    OutboxStatus,
    Party,
    Role,
    Plant,
    SalesLedger,
    SalesOrder,
    SOStatus,
    TallyOutbox,
    TallyStatus,
    User,
)
from app.web import render

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


def _scoped(stmt, user: User):
    stmt = stmt.where(SalesOrder.deleted_at.is_(None))
    if user.role is Role.SALES:
        stmt = stmt.where(SalesOrder.created_by_id == user.id)
    elif user.role is Role.ACCOUNTS:
        stmt = stmt.where(SalesOrder.status != SOStatus.DRAFT)
    return stmt


def _count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model).where(model.deleted_at.is_(None))).scalar_one()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    counts = {s: 0 for s in SOStatus}
    rows = db.execute(_scoped(select(SalesOrder.status, func.count()), user).group_by(SalesOrder.status)).all()
    for st, n in rows:
        counts[st] = n
    total = sum(counts.values())

    now = datetime.now(IST)
    greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    ctx: dict = {
        "counts": counts, "total": total, "greeting": greeting, "today": now.strftime("%A, %d %B %Y"),
        "recent": db.execute(_scoped(select(SalesOrder), user).order_by(SalesOrder.updated_at.desc()).limit(6)).scalars().all(),
        "waiting": [], "setup": None,
    }

    if user.role in (Role.ACCOUNTS, Role.ADMIN):
        ctx["waiting"] = db.execute(
            _scoped(select(SalesOrder), user).where(SalesOrder.status == SOStatus.PENDING_APPROVAL, SalesOrder.created_by_id != user.id)
            .order_by(SalesOrder.submitted_at).limit(5)
        ).scalars().all()
        parties, items, plants = _count(db, Party), _count(db, Item), _count(db, Plant)
        ctx["setup"] = [
            {"label": f"Add plants ({plants} so far)", "done": plants > 0, "href": "/masters/plants/new"},
            {"label": f"Add customers ({parties} so far)", "done": parties > 0, "href": "/masters/parties/new"},
            {"label": f"Add items ({items} so far)", "done": items > 0, "href": "/masters/items/new"},
            {"label": "Connect Tally", "done": get_gateway().configured, "href": "/admin/tally", "admin": True},
        ]
        if user.role is not Role.ADMIN:
            ctx["setup"] = [s for s in ctx["setup"] if not s.get("admin")]
        ctx["setup_done"] = all(s["done"] for s in ctx["setup"])
        ctx["plants_missing"] = plants == 0

    if user.role is Role.ADMIN:
        ctx["outbox_waiting"] = db.execute(
            select(func.count()).select_from(TallyOutbox).where(TallyOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED]))
        ).scalar_one()
        ctx["outbox_dead"] = db.execute(
            select(func.count()).select_from(TallyOutbox).where(TallyOutbox.status == OutboxStatus.DEAD)
        ).scalar_one()
        ctx["users_count"] = _count(db, User)

    ctx["synced"] = db.execute(
        _scoped(select(func.count()).select_from(SalesOrder), user).where(SalesOrder.tally_status == TallyStatus.SYNCED)
    ).scalar_one()
    return render(request, "dashboard.html", ctx)
