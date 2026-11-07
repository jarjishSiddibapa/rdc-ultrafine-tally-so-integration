"""The life of one sales order, told in order: who did what, when, and how long each wait took.

Built only from records that already exist (the audit trail and the Tally queue row), so it can never
disagree with the rest of the app.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, SalesOrder, SOStatus, TallyOutbox, TallyStatus, User
from app.models.base import utcnow
from app.services.listing import fmt_duration

# audit action -> (title, tone). Tones drive the colour of the marker: ok, warn, bad, info, plain.
ACTIONS: dict[str, tuple[str, str]] = {
    "so_created": ("Order created", "plain"),
    "so_updated": ("Order edited", "plain"),
    "so_submitted": ("Submitted for approval", "info"),
    "so_withdrawn": ("Withdrawn to edit", "warn"),
    "so_rejected": ("Sent back to sales", "bad"),
    "so_approved": ("Approved", "ok"),
    "tally_push_failed": ("Tally push failed", "bad"),
    "tally_retry": ("Retry requested", "warn"),
    "tally_push_ok": ("Delivered to Tally", "ok"),
    "so_deleted": ("Order deleted", "bad"),
}


@dataclass
class Step:
    title: str
    tone: str
    at: datetime
    who: str
    note: str | None = None
    since_prev: str | None = None          # "3 h 12 min later"
    action: str = ""


@dataclass
class Lifecycle:
    steps: list[Step] = field(default_factory=list)
    stage: str = ""
    stage_tone: str = "info"
    total: str = "-"
    total_open: bool = False               # still running: the clock has not stopped
    waits: list[tuple[str, str, bool]] = field(default_factory=list)   # (label, duration, still running)
    sent_back: int = 0
    attempts: int = 0


def _dur(a: datetime | None, b: datetime | None) -> str | None:
    return fmt_duration((b - a).total_seconds()) if a and b and b >= a else None


def current_stage(order: SalesOrder) -> tuple[str, str]:
    if order.deleted_at:
        return "Deleted", "bad"
    if order.status is SOStatus.DRAFT:
        return "Draft with sales", "plain"
    if order.status is SOStatus.PENDING_APPROVAL:
        return "Waiting for accounts to approve", "info"
    if order.status is SOStatus.REJECTED:
        return "Sent back, waiting for sales to fix it", "bad"
    if order.tally_status is TallyStatus.SYNCED:
        return "Done: posted in Tally", "ok"
    if order.tally_status is TallyStatus.FAILED:
        return "Approved, but Tally push failed", "bad"
    return "Approved, queued for Tally", "info"


def build(db: Session, order: SalesOrder, outbox: TallyOutbox | None, now: datetime | None = None) -> Lifecycle:
    now = now or utcnow()
    rows = db.execute(
        select(AuditLog).where(AuditLog.entity_type == "sales_order", AuditLog.entity_id == order.id,
                               AuditLog.action.in_(list(ACTIONS))).order_by(AuditLog.at, AuditLog.id)
    ).scalars().all()
    names = {u.id: u.full_name for u in db.execute(select(User).where(User.id.in_({r.actor_id for r in rows if r.actor_id}))).scalars()} if rows else {}

    steps: list[Step] = []
    for r in rows:
        title, tone = ACTIONS[r.action]
        d = r.details or {}
        note = None
        if r.action == "so_rejected":
            note = d.get("remarks")
        elif r.action == "tally_push_failed":
            note = f"Attempt {d.get('attempt', '?')}: {d.get('error', 'unknown error')}"
        elif r.action == "tally_push_ok" and d.get("reference"):
            note = f"Tally reference {d['reference']}"
        steps.append(Step(title, tone, r.at, names.get(r.actor_id) or r.actor_email or "System", note, action=r.action))
    if outbox is not None:
        steps.append(Step("Queued for Tally", "info", outbox.created_at, "System", None, action="tally_queued"))
    steps.sort(key=lambda s: s.at)                       # stable: audit order is kept for equal times
    prev = None
    for s in steps:
        s.since_prev = _dur(prev, s.at) if prev else None
        prev = s.at

    lc = Lifecycle(steps=steps)
    lc.stage, lc.stage_tone = current_stage(order)
    lc.sent_back = sum(1 for s in steps if s.action == "so_rejected")
    lc.attempts = outbox.attempts if outbox else 0

    # Where the time went. Each wait runs from one step to the next that ends it; an unfinished wait runs to now.
    created = next((s.at for s in steps if s.action == "so_created"), order.created_at)
    first_submit = next((s.at for s in steps if s.action == "so_submitted"), None)
    with_accounts = with_sales = 0.0
    accounts_open = sales_open = False
    waiting_since = None          # set by a submit, cleared by approve / send back / withdraw
    fixing_since = None           # set by a send back, cleared by the next submit
    for s in steps:
        if s.action == "so_submitted":
            if fixing_since:
                with_sales += (s.at - fixing_since).total_seconds()
                fixing_since = None
            waiting_since = s.at
        elif s.action in ("so_approved", "so_rejected", "so_withdrawn") and waiting_since:
            with_accounts += (s.at - waiting_since).total_seconds()
            waiting_since = None
            if s.action == "so_rejected":
                fixing_since = s.at
    if waiting_since and order.status is SOStatus.PENDING_APPROVAL:
        with_accounts += (now - waiting_since).total_seconds()
        accounts_open = True
    if fixing_since and order.status is SOStatus.REJECTED:
        with_sales += (now - fixing_since).total_seconds()
        sales_open = True

    if first_submit:
        lc.waits.append(("Sales preparing the order", _dur(created, first_submit) or "-", False))
    else:
        lc.waits.append(("Sales preparing the order", fmt_duration((now - created).total_seconds()), order.status is SOStatus.DRAFT))
    if first_submit:
        lc.waits.append(("Waiting for accounts", fmt_duration(with_accounts), accounts_open))
    if lc.sent_back:
        lc.waits.append(("With sales after being sent back", fmt_duration(with_sales), sales_open))
    approved = next((s.at for s in reversed(steps) if s.action == "so_approved"), None)
    delivered = outbox.sent_at if outbox and outbox.sent_at else None
    if approved:
        lc.waits.append(("Approval to Tally", _dur(approved, delivered) if delivered else fmt_duration(max((now - approved).total_seconds(), 0)), delivered is None and order.tally_status is not TallyStatus.FAILED))

    end = delivered or (order.deleted_at if order.deleted_at else None)
    lc.total_open = end is None
    lc.total = fmt_duration(((end or now) - created).total_seconds())
    return lc
