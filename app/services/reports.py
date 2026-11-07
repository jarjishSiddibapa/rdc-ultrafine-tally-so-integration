"""The report catalogue. Every report is a SQL select plus a column spec, so each one gets the same
search, filters, sorting, paging, column choice and CSV / Excel download as the normal lists."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.orm import Session

from app.core.utils import financial_year
from app.models import AuditLog, Role, SalesOrder, SalesOrderItem, SOStatus, User
from app.services import specs
from app.services.listing import Col, ListSpec, range_conditions
from app.services.orders import visible_orders

O = SalesOrder
ALL = (Role.SALES, Role.ACCOUNTS, Role.ADMIN)
STAFF = (Role.ACCOUNTS, Role.ADMIN)


@dataclass
class Built:
    stmt: Any
    spec: ListSpec
    entities: bool = False


@dataclass(frozen=True)
class Report:
    slug: str
    title: str
    blurb: str
    roles: tuple[Role, ...]
    period_on: str                                  # what the From / To dates are applied to
    group: str                                      # heading on the catalogue page
    build: Callable[[Session, User, date | None, date | None], Built]


def _sum_if(cond: Any, value: Any) -> Any:
    return func.coalesce(func.sum(case((cond, value), else_=0)), 0)


# --------------------------------------------------------------------------- order based
def _register(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    stmt = visible_orders(select(O), user)
    for c in range_conditions(O.order_date, "date", lo, hi):
        stmt = stmt.where(c)
    return Built(stmt, specs.show_all(specs.order_spec(db)), True)


def _turnaround(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    stmt = visible_orders(select(O), user).where(O.submitted_at.is_not(None))
    for c in range_conditions(O.submitted_at, "datetime", lo, hi):
        stmt = stmt.where(c)
    base = specs.order_spec(db)
    spec = specs.pick(base, ["so_number", "party_name", "total_amount", "created_by", "submitted_at", "status", "reviewed_by",
                             "reviewed_at", "decision_time", "review_remarks"], default_sort="submitted_at", tiebreak=O.id.desc())
    return Built(stmt, spec, True)


def _lifecycle(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    """One row per order that has left draft, with each milestone; a row opens that order's full journey."""
    stmt = visible_orders(select(O), user).where(O.submitted_at.is_not(None))
    for c in range_conditions(O.created_at, "datetime", lo, hi):
        stmt = stmt.where(c)
    spec = specs.pick(specs.order_spec(db), ["so_number", "party_name", "total_amount", "status", "tally_status", "created_by", "created_at",
                                             "submitted_at", "sent_back_count", "reviewed_by", "reviewed_at", "decision_time",
                                             "sent_at", "push_time", "total_time"], default_sort="created_at", tiebreak=O.id.desc())
    return Built(stmt, spec, True)


def _tally_timing(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    from app.models import TallyOutbox

    stmt = specs.outbox_base()
    for c in range_conditions(TallyOutbox.created_at, "datetime", lo, hi):
        stmt = stmt.where(c)
    return Built(stmt, specs.show_all(specs.outbox_spec()), True)


def _approval_trail(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    """Every submission, approval and send-back as its own row, with who did it and when."""
    A = AuditLog
    stmt = (
        select(A.at.label("at"), A.action.label("action"), O.so_number.label("so_number"), O.party_name.label("party_name"),
               O.total_amount.label("total_amount"), A.actor_email.label("actor"),
               func.json_unquote(func.json_extract(A.details, "$.remarks")).label("remarks"), A.ip_address.label("ip"))
        .select_from(A).join(O, O.id == A.entity_id)
        .where(A.entity_type == "sales_order", A.action.in_(["so_created", "so_submitted", "so_withdrawn", "so_approved", "so_rejected", "so_deleted"]))
    )
    stmt = visible_orders(stmt, user)
    for c in range_conditions(A.at, "datetime", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.subquery()
    names = (("so_created", "Created"), ("so_submitted", "Submitted"), ("so_withdrawn", "Withdrawn"),
             ("so_approved", "Approved"), ("so_rejected", "Sent back"), ("so_deleted", "Deleted"))
    cols = [
        Col("at", "When", sub.c.at, "datetime"),
        Col("action", "Action", sub.c.action, "choice", names, value=lambda r: dict(names).get(r.action, r.action)),
        Col("so_number", "Order no.", sub.c.so_number, search=True, value=lambda r: r.so_number or ""),
        Col("party_name", "Customer", sub.c.party_name, search=True),
        Col("total_amount", "Total (Rs)", sub.c.total_amount, "money", total=True),
        Col("actor", "Done by", sub.c.actor, search=True, value=lambda r: r.actor or "System"),
        Col("remarks", "Remarks", sub.c.remarks, search=True),
        Col("ip", "IP address", sub.c.ip, search=True),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="at", tiebreak=sub.c.at.desc()))


def _rejections(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    A = AuditLog
    stmt = (
        select(A.at.label("at"), O.so_number.label("so_number"), O.party_name.label("party_name"), O.total_amount.label("total_amount"),
               A.actor_email.label("rejected_by"), func.json_unquote(func.json_extract(A.details, "$.remarks")).label("reason"),
               specs._uname(O.created_by_id).label("created_by"), O.status.label("status"))
        .select_from(A).join(O, O.id == A.entity_id)
        .where(A.entity_type == "sales_order", A.action == "so_rejected")
    )
    stmt = visible_orders(stmt, user)
    for c in range_conditions(A.at, "datetime", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.subquery()
    cols = [
        Col("at", "Sent back at", sub.c.at, "datetime"),
        Col("so_number", "Order no.", sub.c.so_number, search=True),
        Col("party_name", "Customer", sub.c.party_name, search=True),
        Col("total_amount", "Total (Rs)", sub.c.total_amount, "money", total=True),
        Col("rejected_by", "Sent back by", sub.c.rejected_by, search=True),
        Col("reason", "Reason", sub.c.reason, search=True),
        Col("created_by", "Sales person", sub.c.created_by, search=True),
        Col("status", "Order status now", sub.c.status, "choice", specs._enum_choices(SOStatus), coerce=SOStatus, value=lambda r: r.status.label),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="at", tiebreak=sub.c.at.desc()))


def _lines(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    L = SalesOrderItem
    stmt = (
        select(O.so_number.label("so_number"), O.order_date.label("order_date"), O.party_name.label("party_name"),
               O.plant_name.label("plant_name"), O.status.label("status"), L.line_no.label("line_no"), L.item_name.label("item_name"),
               L.hsn_code.label("hsn_code"), L.unit.label("unit"), L.quantity.label("quantity"), L.rate.label("rate"),
               L.gst_rate.label("gst_rate"), L.amount.label("amount"))
        .select_from(L).join(O, O.id == L.order_id)
    )
    stmt = visible_orders(stmt, user)
    for c in range_conditions(O.order_date, "date", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.subquery()
    cols = [
        Col("so_number", "Order no.", sub.c.so_number, search=True, value=lambda r: r.so_number or ""),
        Col("order_date", "Order date", sub.c.order_date, "date"),
        Col("party_name", "Customer", sub.c.party_name, search=True),
        Col("plant_name", "Plant", sub.c.plant_name, search=True),
        Col("status", "Status", sub.c.status, "choice", specs._enum_choices(SOStatus), coerce=SOStatus, value=lambda r: r.status.label),
        Col("line_no", "Line", sub.c.line_no, "number"),
        Col("item_name", "Item", sub.c.item_name, search=True),
        Col("hsn_code", "HSN", sub.c.hsn_code, search=True),
        Col("unit", "Unit", sub.c.unit),
        Col("quantity", "Quantity", sub.c.quantity, "qty"),
        Col("rate", "Rate (Rs)", sub.c.rate, "money"),
        Col("gst_rate", "GST %", sub.c.gst_rate, "number", value=lambda r: r.gst_rate.normalize()),
        Col("amount", "Amount (Rs)", sub.c.amount, "money", total=True),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="order_date", tiebreak=sub.c.so_number.desc()))


# --------------------------------------------------------------------------- summaries
def _order_summary(user: User, lo: date | None, hi: date | None, group: Any, key: str, label: str, *, join_user: bool = False) -> Built:
    stmt = select(
        group.label(key),
        func.count(O.id).label("orders"),
        _sum_if(O.status == SOStatus.APPROVED, 1).label("approved"),
        _sum_if(O.status == SOStatus.PENDING_APPROVAL, 1).label("pending"),
        _sum_if(O.status == SOStatus.REJECTED, 1).label("sent_back"),
        _sum_if(O.status == SOStatus.APPROVED, O.total_amount).label("approved_value"),
        _sum_if(O.status == SOStatus.PENDING_APPROVAL, O.total_amount).label("pending_value"),
        func.max(O.order_date).label("last_order"),
    ).select_from(O)
    if join_user:
        stmt = stmt.join(User, User.id == O.created_by_id)
    stmt = visible_orders(stmt, user).where(O.status != SOStatus.DRAFT)
    for c in range_conditions(O.order_date, "date", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.group_by(group).subquery()
    cols = [
        Col(key, label, sub.c[key], search=True, value=lambda r, k=key: getattr(r, k) or "(not set)"),
        Col("orders", "Orders", sub.c.orders, "number", total=True),
        Col("approved", "Approved", sub.c.approved, "number", total=True),
        Col("pending", "Awaiting approval", sub.c.pending, "number", total=True),
        Col("sent_back", "Sent back", sub.c.sent_back, "number", total=True),
        Col("approved_value", "Approved value (Rs)", sub.c.approved_value, "money", total=True),
        Col("pending_value", "Awaiting value (Rs)", sub.c.pending_value, "money", total=True),
        Col("last_order", "Last order date", sub.c.last_order, "date"),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="approved_value", tiebreak=sub.c[key].asc()))


def _by_customer(db, user, lo, hi): return _order_summary(user, lo, hi, O.party_name, "customer", "Customer")
def _by_plant(db, user, lo, hi): return _order_summary(user, lo, hi, O.plant_name, "plant", "Plant")
def _by_person(db, user, lo, hi): return _order_summary(user, lo, hi, User.full_name, "salesperson", "Sales person", join_user=True)


def _by_item(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    L = SalesOrderItem
    stmt = select(
        L.item_name.label("item"), L.unit.label("unit"),
        func.count(func.distinct(O.id)).label("orders"),
        _sum_if(O.status == SOStatus.APPROVED, L.quantity).label("approved_qty"),
        _sum_if(O.status == SOStatus.APPROVED, L.amount).label("approved_value"),
        _sum_if(O.status == SOStatus.PENDING_APPROVAL, L.quantity).label("pending_qty"),
        _sum_if(O.status == SOStatus.PENDING_APPROVAL, L.amount).label("pending_value"),
    ).select_from(L).join(O, O.id == L.order_id)
    stmt = visible_orders(stmt, user).where(O.status != SOStatus.DRAFT)
    for c in range_conditions(O.order_date, "date", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.group_by(L.item_name, L.unit).subquery()
    cols = [
        Col("item", "Item", sub.c.item, search=True),
        Col("unit", "Unit", sub.c.unit),
        Col("orders", "Orders", sub.c.orders, "number"),
        Col("approved_qty", "Approved quantity", sub.c.approved_qty, "qty"),
        Col("approved_value", "Approved value (Rs)", sub.c.approved_value, "money", total=True),
        Col("pending_qty", "Awaiting quantity", sub.c.pending_qty, "qty"),
        Col("pending_value", "Awaiting value (Rs)", sub.c.pending_value, "money", total=True),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="approved_value", tiebreak=sub.c.item.asc()))


def _monthly_gst(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    month = func.date_format(O.order_date, literal_column("'%Y-%m'"))
    stmt = select(
        month.label("month"), func.count(O.id).label("orders"),
        func.coalesce(func.sum(O.subtotal), 0).label("subtotal"), func.coalesce(func.sum(O.cgst_amount), 0).label("cgst"),
        func.coalesce(func.sum(O.sgst_amount), 0).label("sgst"), func.coalesce(func.sum(O.igst_amount), 0).label("igst"),
        func.coalesce(func.sum(O.total_amount), 0).label("total"),
    ).select_from(O)
    stmt = visible_orders(stmt, user).where(O.status == SOStatus.APPROVED)
    for c in range_conditions(O.order_date, "date", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.group_by(month).subquery()
    cols = [
        Col("month", "Month", sub.c.month, search=True, value=lambda r: datetime.strptime(r.month, "%Y-%m").strftime("%b %Y") if r.month else ""),
        Col("orders", "Approved orders", sub.c.orders, "number", total=True),
        Col("subtotal", "Taxable value (Rs)", sub.c.subtotal, "money", total=True),
        Col("cgst", "CGST (Rs)", sub.c.cgst, "money", total=True),
        Col("sgst", "SGST (Rs)", sub.c.sgst, "money", total=True),
        Col("igst", "IGST (Rs)", sub.c.igst, "money", total=True),
        Col("total", "Invoice value (Rs)", sub.c.total, "money", total=True),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="month", tiebreak=sub.c.month.desc()))


def _user_activity(db: Session, user: User, lo: date | None, hi: date | None) -> Built:
    A = AuditLog
    stmt = select(
        func.coalesce(A.actor_email, "System").label("who"), func.count(A.id).label("events"),
        _sum_if(A.action == "login", 1).label("sign_ins"), _sum_if(A.action == "login_failed", 1).label("failed"),
        _sum_if(A.action == "so_created", 1).label("created"), _sum_if(A.action == "so_submitted", 1).label("submitted"),
        _sum_if(A.action == "so_approved", 1).label("approved"), _sum_if(A.action == "so_rejected", 1).label("sent_back"),
        func.min(A.at).label("first_at"), func.max(A.at).label("last_at"),
    )
    for c in range_conditions(A.at, "datetime", lo, hi):
        stmt = stmt.where(c)
    sub = stmt.group_by(func.coalesce(A.actor_email, "System")).subquery()
    cols = [
        Col("who", "User", sub.c.who, search=True),
        Col("events", "Actions", sub.c.events, "number", total=True),
        Col("sign_ins", "Sign-ins", sub.c.sign_ins, "number", total=True),
        Col("failed", "Failed sign-ins", sub.c.failed, "number", total=True),
        Col("created", "Orders created", sub.c.created, "number", total=True),
        Col("submitted", "Submitted", sub.c.submitted, "number", total=True),
        Col("approved", "Approved", sub.c.approved, "number", total=True),
        Col("sent_back", "Sent back", sub.c.sent_back, "number", total=True),
        Col("first_at", "First activity", sub.c.first_at, "datetime"),
        Col("last_at", "Last activity", sub.c.last_at, "datetime"),
    ]
    return Built(select(sub), ListSpec(cols, default_sort="last_at", tiebreak=sub.c.who.asc()))


REPORTS: list[Report] = [
    Report("order-lifecycle", "Order lifecycle", "Follow each sales order from creation to Tally: when it was submitted, approved or sent back, delivered, and how long each stage took. Open a row for its full story.",
           ALL, "creation time", "Orders", _lifecycle),
    Report("order-register", "Order register", "Every order with all its details and every timestamp: created, submitted, approved or sent back, queued, pushed to Tally.",
           ALL, "order date", "Orders", _register),
    Report("approval-trail", "Approval trail", "One row per action on an order: created, submitted, withdrawn, approved, sent back, deleted, with who did it, when and from where.",
           ALL, "action time", "Orders", _approval_trail),
    Report("approval-turnaround", "Approval turnaround", "How long accounts took to decide each submitted order.",
           ALL, "submitted time", "Orders", _turnaround),
    Report("rejections", "Sent-back orders", "Every time an order was sent back, with the reason and who did it.",
           ALL, "sent-back time", "Orders", _rejections),
    Report("line-items", "Item lines", "Every line of every order: item, quantity, rate, GST and amount.",
           ALL, "order date", "Orders", _lines),
    Report("tally-timing", "Tally push timing", "When each approved order was queued, tried, retried and delivered to Tally, and how long it took.",
           STAFF, "queued time", "Tally", _tally_timing),
    Report("by-customer", "Sales by customer", "Orders and value per customer.", ALL, "order date", "Summaries", _by_customer),
    Report("by-item", "Sales by item", "Quantity and value per item.", ALL, "order date", "Summaries", _by_item),
    Report("by-plant", "Sales by plant", "Orders and value per plant.", ALL, "order date", "Summaries", _by_plant),
    Report("by-person", "Sales by sales person", "Orders and value per sales person.", STAFF, "order date", "Summaries", _by_person),
    Report("monthly-gst", "Monthly GST summary", "Approved orders by month: taxable value, CGST, SGST, IGST and invoice value.",
           ALL, "order date", "Summaries", _monthly_gst),
    Report("user-activity", "User activity", "What each user did and when: sign-ins, orders created, submitted, approved and sent back.",
           (Role.ADMIN,), "action time", "Administration", _user_activity),
]
BY_SLUG = {r.slug: r for r in REPORTS}


# --------------------------------------------------------------------------- periods
def presets(today: date) -> list[tuple[str, date | None, date | None]]:
    m0 = today.replace(day=1)
    last_m_end = m0 - timedelta(days=1)
    q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    fy = today.year if today.month >= 4 else today.year - 1
    return [
        ("Today", today, today),
        ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
        ("Last 7 days", today - timedelta(days=6), today),
        ("Last 30 days", today - timedelta(days=29), today),
        ("This month", m0, today),
        ("Last month", last_m_end.replace(day=1), last_m_end),
        ("This quarter", q_start, today),
        (f"This financial year ({financial_year(today)})", date(fy, 4, 1), today),
        ("Last financial year", date(fy - 1, 4, 1), date(fy, 3, 31)),
        ("All time", None, None),
    ]
