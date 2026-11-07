"""Column definitions for every list and report. One place, so the screen and the download always agree."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.core.constants import GST_STATES, UNITS
from app.models import (
    AuditLog,
    EmailStatus,
    Notification,
    QueuedEmail,
    Item,
    OutboxStatus,
    Party,
    Plant,
    Role,
    SalesOrder,
    SalesOrderItem,
    SOStatus,
    SOType,
    TallyOutbox,
    TallyStatus,
    TaxMode,
    User,
    UserPlant,
)
from app.models.base import utcnow
from app.services.audit_labels import action_label, describe
from app.services.listing import Col, ListSpec

STATE_CHOICES = tuple((code, f"{name} ({code})") for code, name in sorted(GST_STATES.items(), key=lambda kv: kv[1]))


def _enum_choices(e: type, attr: str = "label") -> tuple[tuple[str, str], ...]:
    return tuple((m.value, getattr(m, attr, m.value)) for m in e)  # type: ignore[attr-defined]


def _uname(fk: Any) -> Any:
    """Correlated 'name of the user with this id' (works in WHERE and ORDER BY without joins)."""
    return select(User.full_name).where(User.id == fk).correlate_except(User).scalar_subquery()


def _secs(a: Any, b: Any) -> Any:
    return func.timestampdiff(literal_column("SECOND"), a, b)


def _state_name(code: str | None) -> str:
    return GST_STATES.get(code or "", "") if code else ""


def pick(spec: ListSpec, keys: list[str], **kw: Any) -> ListSpec:
    """A report that reuses some columns of another spec, in the order given, all shown."""
    cols = [replace(spec.col(k), default=True) for k in keys]  # type: ignore[arg-type]
    return ListSpec(cols, **kw)


def show_all(spec: ListSpec) -> ListSpec:
    return replace(spec, cols=[replace(c, default=True) for c in spec.cols])


# --------------------------------------------------------------------------- sales orders
def _sent_back(order: SalesOrder) -> int:
    """How many times accounts sent this order back (one small query per row shown)."""
    from sqlalchemy.orm import object_session

    s = object_session(order)
    return s.execute(select(func.count(AuditLog.id)).where(
        AuditLog.entity_type == "sales_order", AuditLog.action == "so_rejected", AuditLog.entity_id == order.id)).scalar_one() if s else 0


def order_spec(db: Session | None = None, *, defaults: dict[str, str] | None = None) -> ListSpec:
    O = SalesOrder

    def ob(col: Any) -> Any:
        return select(col).where(TallyOutbox.order_id == O.id).correlate_except(TallyOutbox).scalar_subquery()

    def push_secs(o: SalesOrder) -> Any:
        ob_ = o.outbox
        return (ob_.sent_at - ob_.created_at).total_seconds() if ob_ and ob_.sent_at else None

    def ob_attr(name: str):
        return lambda o: getattr(o.outbox, name) if o.outbox else None

    cols = [
        Col("so_number", "Order no.", O.so_number, search=True, value=lambda o: o.so_number or f"Draft #{o.id}"),
        Col("so_type", "Type", O.so_type, "choice", _enum_choices(SOType, "short"), coerce=SOType, default=False, value=lambda o: o.so_type.short),
        Col("party_name", "Customer", O.party_name, search=True, quick=True),
        Col("customer_po_number", "PO number", O.customer_po_number, search=True, default=False),
        Col("plant_name", "Plant", O.plant_name, search=True, default=False, quick=True),
        Col("order_date", "Order date", O.order_date, "date", quick=True),
        Col("billing_state_code", "Billing state", O.billing_state_code, "choice", STATE_CHOICES, default=False, value=lambda o: _state_name(o.billing_state_code)),
        Col("tax_mode", "Tax", O.tax_mode, "choice", (("cgst_sgst", "CGST + SGST"), ("igst", "IGST")), default=False,
            coerce=TaxMode, value=lambda o: o.tax_mode.label),
        Col("subtotal", "Subtotal (Rs)", O.subtotal, "money", total=True, default=False),
        Col("cgst_amount", "CGST (Rs)", O.cgst_amount, "money", total=True, default=False),
        Col("sgst_amount", "SGST (Rs)", O.sgst_amount, "money", total=True, default=False),
        Col("igst_amount", "IGST (Rs)", O.igst_amount, "money", total=True, default=False),
        Col("total_amount", "Total (Rs)", O.total_amount, "money", total=True, quick=True),
        Col("status", "Status", O.status, "choice", _enum_choices(SOStatus), coerce=SOStatus, value=lambda o: o.status.label, quick=True),
        Col("tally_status", "Tally", O.tally_status, "choice", _enum_choices(TallyStatus), coerce=TallyStatus, value=lambda o: o.tally_status.label, quick=True),
        Col("tally_reference", "Tally reference", O.tally_reference, search=True, default=False),
        Col("created_by", "Created by", _uname(O.created_by_id), search=True, value=lambda o: o.created_by.full_name, quick=True, default=False),
        Col("created_at", "Created at", O.created_at, "datetime", default=False),
        Col("submitted_at", "Submitted at", O.submitted_at, "datetime", default=False),
        Col("reviewed_by", "Decision by", _uname(O.reviewed_by_id), search=True, default=False, value=lambda o: o.reviewed_by.full_name if o.reviewed_by else None),
        Col("reviewed_at", "Approved / sent back at", O.reviewed_at, "datetime", default=False),
        Col("decision_time", "Time to decision", _secs(O.submitted_at, O.reviewed_at), "duration", default=False,
            value=lambda o: (o.reviewed_at - o.submitted_at).total_seconds() if o.reviewed_at and o.submitted_at else None),
        Col("review_remarks", "Decision remarks", O.review_remarks, search=True, default=False),
        Col("queued_at", "Queued for Tally at", ob(TallyOutbox.created_at), "datetime", default=False, value=ob_attr("created_at")),
        Col("first_attempt_at", "First push at", ob(TallyOutbox.first_attempt_at), "datetime", default=False, value=ob_attr("first_attempt_at")),
        Col("last_attempt_at", "Last push attempt at", ob(TallyOutbox.last_attempt_at), "datetime", default=False, value=ob_attr("last_attempt_at")),
        Col("sent_at", "Pushed to Tally at", ob(TallyOutbox.sent_at), "datetime", default=False, value=ob_attr("sent_at")),
        Col("push_attempts", "Push attempts", ob(TallyOutbox.attempts), "number", default=False, value=ob_attr("attempts")),
        Col("push_time", "Approval to Tally", _secs(O.reviewed_at, ob(TallyOutbox.sent_at)), "duration", default=False,
            value=lambda o: (o.outbox.sent_at - o.reviewed_at).total_seconds() if o.outbox and o.outbox.sent_at and o.reviewed_at else None),
        Col("sent_back_count", "Times sent back", select(func.count(AuditLog.id)).where(
            AuditLog.entity_type == "sales_order", AuditLog.action == "so_rejected", AuditLog.entity_id == O.id)
            .correlate_except(AuditLog).scalar_subquery(), "number", default=False,
            value=_sent_back),
        Col("total_time", "Start to finish", _secs(O.created_at, ob(TallyOutbox.sent_at)), "duration", default=False,
            value=lambda o: (o.outbox.sent_at - o.created_at).total_seconds() if o.outbox and o.outbox.sent_at else None),
        Col("remarks", "Remarks", O.remarks, search=True, default=False),
        Col("narration", "Narration", O.narration, search=True, default=False, sortable=False),
        Col("updated_at", "Last modified", O.updated_at, "datetime", default=False),
    ]
    return ListSpec(cols, default_sort="created_at", default_dir="desc", tiebreak=O.id.desc(), defaults=defaults or {})


# --------------------------------------------------------------------------- tally queue
def outbox_spec() -> ListSpec:
    O, Q = SalesOrder, TallyOutbox

    def label(r: TallyOutbox) -> str:
        return r.status.value.replace("_", " ").capitalize()

    cols = [
        Col("so_number", "Order no.", O.so_number, search=True, value=lambda r: r.order.so_number),
        Col("party_name", "Customer", O.party_name, search=True, value=lambda r: r.order.party_name),
        Col("total_amount", "Total (Rs)", O.total_amount, "money", total=True, value=lambda r: r.order.total_amount),
        Col("status", "Queue status", Q.status, "choice", tuple((m.value, m.value.replace("_", " ").capitalize()) for m in OutboxStatus),
            coerce=OutboxStatus, value=label),
        Col("attempts", "Attempts", Q.attempts, "number"),
        Col("created_at", "Queued at", Q.created_at, "datetime"),
        Col("first_attempt_at", "First attempt at", Q.first_attempt_at, "datetime", default=False),
        Col("last_attempt_at", "Last attempt at", Q.last_attempt_at, "datetime"),
        Col("next_attempt_at", "Next attempt at", Q.next_attempt_at, "datetime", default=False,
            value=lambda r: r.next_attempt_at if r.status in (OutboxStatus.PENDING, OutboxStatus.FAILED) else None),
        Col("sent_at", "Delivered at", Q.sent_at, "datetime"),
        Col("push_time", "Queue to delivery", _secs(Q.created_at, Q.sent_at), "duration", default=False,
            value=lambda r: (r.sent_at - r.created_at).total_seconds() if r.sent_at else None),
        Col("response_reference", "Tally reference", Q.response_reference, search=True, default=False),
        Col("last_error", "Last error", Q.last_error, search=True),
        Col("approved_at", "Approved at", O.reviewed_at, "datetime", default=False, value=lambda r: r.order.reviewed_at),
    ]
    return ListSpec(cols, default_sort="created_at", tiebreak=Q.id.desc())


def outbox_base() -> Any:
    return select(TallyOutbox).join(SalesOrder, SalesOrder.id == TallyOutbox.order_id)


# --------------------------------------------------------------------------- users
USER_STATUS = (("active", "Active"), ("must_change", "Must change password"), ("locked", "Locked"), ("disabled", "Disabled"), ("removed", "Removed"))


def user_status(u: User, now: datetime) -> str:
    if u.deleted_at:
        return "removed"
    if not u.is_active:
        return "disabled"
    if u.locked_until and u.locked_until > now:
        return "locked"
    if u.must_change_password:
        return "must_change"
    return "active"


def _user_status_where(v: str) -> Any:
    now = utcnow()
    live = User.deleted_at.is_(None)
    locked = and_(User.locked_until.is_not(None), User.locked_until > now)
    return {
        "removed": User.deleted_at.is_not(None),
        "disabled": and_(live, User.is_active.is_(False)),
        "locked": and_(live, User.is_active.is_(True), locked),
        "must_change": and_(live, User.is_active.is_(True), ~locked, User.must_change_password.is_(True)),
        "active": and_(live, User.is_active.is_(True), ~locked, User.must_change_password.is_(False)),
    }[v]


def user_spec() -> ListSpec:
    plants = (
        select(func.group_concat(Plant.name))
        .select_from(UserPlant.__table__.join(Plant.__table__, Plant.id == UserPlant.plant_id))
        .where(UserPlant.user_id == User.id)
        .correlate_except(UserPlant, Plant)
        .scalar_subquery()
    )
    now = utcnow()
    cols = [
        Col("full_name", "Name", User.full_name, search=True),
        Col("email", "Email", User.email, search=True),
        Col("role", "Role", User.role, "choice", _enum_choices(Role), coerce=Role, value=lambda u: u.role.label),
        Col("plants", "Plants", plants, search=True, value=lambda u: ", ".join(p.name for p in u.plants) if u.role is Role.SALES else ""),
        Col("status", "Status", None, "choice", USER_STATUS, where=_user_status_where, sortable=False, value=lambda u: dict(USER_STATUS)[user_status(u, now)]),
        Col("last_login_at", "Last sign-in", User.last_login_at, "datetime"),
        Col("created_at", "Created at", User.created_at, "datetime", default=False),
        Col("updated_at", "Last modified", User.updated_at, "datetime", default=False),
        Col("password_changed_at", "Password changed at", User.password_changed_at, "datetime", default=False),
        Col("failed_login_count", "Failed sign-ins", User.failed_login_count, "number", default=False),
        Col("deleted_at", "Removed at", User.deleted_at, "datetime", default=False),
    ]
    return ListSpec(cols, default_sort="full_name", default_dir="asc", extras=("show_deleted",), tiebreak=User.id)


# --------------------------------------------------------------------------- master data
MASTER_STATUS = (("active", "Active"), ("inactive", "Inactive"), ("removed", "Removed"))


def _master_status_where(model: type):
    def build(v: str) -> Any:
        return {
            "removed": model.deleted_at.is_not(None),
            "inactive": and_(model.deleted_at.is_(None), model.is_active.is_(False)),
            "active": and_(model.deleted_at.is_(None), model.is_active.is_(True)),
        }[v]
    return build


def master_spec(slug: str, db: Session) -> ListSpec:
    M = {"parties": Party, "plants": Plant, "items": Item}[slug]
    status = Col("status", "Status", None, "choice", MASTER_STATUS, where=_master_status_where(M), sortable=False,
                 value=lambda r: "Removed" if r.deleted_at else ("Active" if r.is_active else "Inactive"))
    stamps = [
        Col("created_at", "Created at", M.created_at, "datetime", default=False),
        Col("updated_at", "Last modified", M.updated_at, "datetime", default=False),
        Col("deleted_at", "Removed at", M.deleted_at, "datetime", default=False),
    ]
    if slug == "parties":
        cols = [
            Col("name", "Name", M.name, search=True),
            Col("gstin", "GSTIN", M.gstin, search=True),
            Col("state_code", "State", M.state_code, "choice", STATE_CHOICES, value=lambda r: _state_name(r.state_code)),
            Col("city", "City", M.city, search=True, default=False),
            Col("pincode", "Pincode", M.pincode, search=True, default=False),
            Col("default_payment_term", "Usual payment term", M.default_payment_term, search=True, default=False),
            Col("address", "Address", M.address_line1, search=True, default=False,
                value=lambda r: ", ".join(x for x in (r.address_line1, r.address_line2) if x)),
            status, *stamps,
        ]
    elif slug == "plants":
        used = (
            select(func.count()).select_from(SalesOrder).where(SalesOrder.plant_id == M.id, SalesOrder.deleted_at.is_(None))
            .correlate_except(SalesOrder).scalar_subquery()
        )
        counts = dict(db.execute(select(SalesOrder.plant_id, func.count()).where(SalesOrder.deleted_at.is_(None), SalesOrder.plant_id.is_not(None)).group_by(SalesOrder.plant_id)).all())
        cols = [
            Col("name", "Name", M.name, search=True),
            Col("state_code", "State", M.state_code, "choice", STATE_CHOICES, value=lambda r: _state_name(r.state_code)),
            Col("orders", "Orders", used, "number", total=True, value=lambda r: counts.get(r.id, 0)),
            status, *stamps,
        ]
    else:
        cols = [
            Col("name", "Name", M.name, search=True),
            Col("hsn_code", "HSN", M.hsn_code, search=True),
            Col("unit", "Unit", M.unit, "choice", tuple((u, f"{u} ({d})") for u, d in UNITS.items())),
            Col("gst_rate", "GST %", M.gst_rate, "number", value=lambda r: r.gst_rate.normalize()),
            status, *stamps,
        ]
    return ListSpec(cols, default_sort="name", default_dir="asc", extras=("show_deleted",), tiebreak=M.id)


# --------------------------------------------------------------------------- audit log
def audit_spec(db: Session) -> ListSpec:
    actions = [r[0] for r in db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()]
    kinds = [r[0] for r in db.execute(select(AuditLog.entity_type).where(AuditLog.entity_type.is_not(None)).distinct().order_by(AuditLog.entity_type)).all()]
    A = AuditLog
    cols = [
        Col("at", "When", A.at, "datetime"),
        Col("actor_email", "Who", A.actor_email, search=True, value=lambda r: r.actor_email or "System"),
        Col("action", "Event", A.action, "choice", tuple((a, action_label(a)) for a in actions), value=lambda r: action_label(r.action)),
        Col("entity_type", "Record type", A.entity_type, "choice", tuple((k, k.replace("_", " ")) for k in kinds), default=False),
        Col("entity_id", "Record id", A.entity_id, "number", default=False),
        Col("record", "Record", None, sortable=False, filterable=False,
            value=lambda r: f"{r.entity_type.replace('_', ' ')} {r.entity_id or ''}".strip() if r.entity_type else ""),
        Col("details", "Details", cast(A.details, Text), search=True, sortable=False,
            value=lambda r: describe(r.details)),
        Col("ip_address", "IP address", A.ip_address, search=True),
        Col("request_id", "Request id", A.request_id, search=True, default=False),
    ]
    return ListSpec(cols, default_sort="at", tiebreak=A.id.desc())


# --------------------------------------------------------------------------- notifications + email log
def notification_spec() -> ListSpec:
    from app.services.notify import KIND_CHOICES

    cols = [
        Col("created_at", "Received", Notification.created_at, "datetime"),
        Col("title", "Notification", Notification.title, search=True),
        Col("body", "Details", Notification.body, search=True),
        Col("kind", "Type", Notification.kind, "choice", KIND_CHOICES, value=lambda n: dict(KIND_CHOICES).get(n.kind, n.kind)),
        Col("status", "Status", Notification.read_at, "choice", (("unread", "Unread"), ("read", "Read")),
            where=lambda v: Notification.read_at.is_(None) if v == "unread" else Notification.read_at.is_not(None),
            value=lambda n: "Read" if n.read_at else "Unread"),
        Col("read_at", "Read at", Notification.read_at, "datetime", default=False),
        Col("email", "Email", None, value=lambda n: (n.email.status.label if n.email else "Not emailed"), sortable=False, filterable=False, default=False),
    ]
    return ListSpec(cols, default_sort="created_at", default_dir="desc", tiebreak=Notification.id)


def mail_spec() -> ListSpec:
    from app.services.notify import KIND_CHOICES

    kinds = KIND_CHOICES + (("password_reset", "Password reset link"), ("test_email", "Test email"))
    cols = [
        Col("created_at", "Queued at", QueuedEmail.created_at, "datetime"),
        Col("to_email", "To", QueuedEmail.to_email, search=True),
        Col("subject", "Subject", QueuedEmail.subject, search=True),
        Col("kind", "Type", QueuedEmail.kind, "choice", kinds, value=lambda e: dict(kinds).get(e.kind, e.kind)),
        Col("status", "Status", QueuedEmail.status, "choice", _enum_choices(EmailStatus), coerce=EmailStatus),
        Col("attempts", "Attempts", QueuedEmail.attempts, "number"),
        Col("first_attempt_at", "First attempt", QueuedEmail.first_attempt_at, "datetime"),
        Col("last_attempt_at", "Last attempt", QueuedEmail.last_attempt_at, "datetime"),
        Col("sent_at", "Sent at", QueuedEmail.sent_at, "datetime"),
        Col("took", "Queue to sent", _secs(QueuedEmail.created_at, QueuedEmail.sent_at), "duration",
            value=lambda e: int((e.sent_at - e.created_at).total_seconds()) if e.sent_at else None),
        Col("last_error", "Last error", QueuedEmail.last_error, search=True),
        Col("requested_by", "Requested by", _uname(QueuedEmail.requested_by_id), default=False,
            value=lambda e: e.requested_by.full_name if e.requested_by else ""),
        Col("message_id", "Message ID", QueuedEmail.message_id, default=False),
    ]
    return ListSpec(cols, default_sort="created_at", default_dir="desc", tiebreak=QueuedEmail.id)
