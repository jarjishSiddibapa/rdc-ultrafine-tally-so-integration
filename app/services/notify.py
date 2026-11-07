"""In-app notifications + their email twins, per-person preferences, and the events that raise them.

Every ``notify_*`` function is called from inside the business transaction (submit, approve, ...), so a
notification exists if and only if the event really happened. Emails are only queued here; the worker sends them.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.utils import format_inr, to_ist
from app.models import Notification, NotificationPref, Role, SalesOrder, SOStatus, User
from app.models.base import utcnow
from app.services import emailing

log = logging.getLogger("app.notify")

# category -> (label, help, roles that can ever receive it, default in-app, default email)
CATEGORIES: dict[str, dict] = {
    "orders": {"label": "Order approvals", "help": "Orders waiting for your decision, and the decision on orders you created.",
               "roles": (Role.SALES, Role.ACCOUNTS, Role.ADMIN), "in_app": True, "email": True},
    "delivery": {"label": "Delivered to Tally", "help": "An approved order has reached Tally.",
                 "roles": (Role.SALES, Role.ACCOUNTS, Role.ADMIN), "in_app": True, "email": False},
    "problems": {"label": "Problems that need attention", "help": "An order could not be delivered to Tally.",
                 "roles": (Role.ADMIN,), "in_app": True, "email": True},
    "reminders": {"label": "Daily reminder", "help": "Orders that have been waiting for approval for too long.",
                  "roles": (Role.ACCOUNTS, Role.ADMIN), "in_app": True, "email": True},
    "account": {"label": "Account and security", "help": "Password changes, sign-in alerts and changes to your access. Always on.",
                "roles": (Role.SALES, Role.ACCOUNTS, Role.ADMIN), "in_app": True, "email": True, "locked": True},
}

# notification kind -> category (also the list of kinds the inbox can filter by)
KINDS: dict[str, tuple[str, str]] = {
    "order_pending": ("orders", "Order waiting for approval"),
    "order_approved": ("orders", "Order approved"),
    "order_rejected": ("orders", "Order sent back"),
    "order_withdrawn": ("orders", "Order withdrawn"),
    "order_deleted": ("orders", "Order deleted"),
    "tally_delivered": ("delivery", "Delivered to Tally"),
    "tally_failed": ("problems", "Tally delivery failed"),
    "pending_reminder": ("reminders", "Waiting too long"),
    "password_changed": ("account", "Password changed"),
    "password_reset_by_admin": ("account", "Password reset by administrator"),
    "account_locked": ("account", "Account locked"),
    "account_changed": ("account", "Access changed"),
    "account_created": ("account", "Account created"),
}
KIND_CHOICES = tuple((k, v[1]) for k, v in KINDS.items())


def categories_for(role: Role) -> list[tuple[str, dict]]:
    return [(k, c) for k, c in CATEGORIES.items() if role in c["roles"]]


# --------------------------------------------------------------------------- preferences
def prefs_for(db: Session, user_ids: Iterable[int]) -> dict[int, dict[str, tuple[bool, bool]]]:
    """{user_id: {category: (in_app, email)}} with defaults filled in."""
    ids = list(user_ids)
    out: dict[int, dict[str, tuple[bool, bool]]] = {
        uid: {k: (c["in_app"], c["email"]) for k, c in CATEGORIES.items()} for uid in ids
    }
    if ids:
        for p in db.execute(select(NotificationPref).where(NotificationPref.user_id.in_(ids))).scalars():
            if p.category in CATEGORIES and not CATEGORIES[p.category].get("locked"):
                out[p.user_id][p.category] = (p.in_app, p.email)
    return out


def save_prefs(db: Session, user: User, chosen: dict[str, tuple[bool, bool]]) -> dict[str, tuple[bool, bool]]:
    """Store the person's choices for the categories they can receive. Returns what is now stored."""
    current = prefs_for(db, [user.id])[user.id]
    for cat, c in categories_for(user.role):
        if c.get("locked") or cat not in chosen:
            continue
        in_app, email = chosen[cat]
        row = db.get(NotificationPref, (user.id, cat))
        if row is None:
            db.add(NotificationPref(user_id=user.id, category=cat, in_app=in_app, email=email))
        else:
            row.in_app, row.email = in_app, email
        current[cat] = (in_app, email)
    return current


# --------------------------------------------------------------------------- raising notifications
def _first_name(user: User) -> str:
    return (user.full_name or "there").split()[0]


def notify(db: Session, users: Iterable[User], kind: str, title: str, body: str, *, url: str | None = None,
           level: str = "info", entity: tuple[str, int] | None = None, dedupe: str | None = None,
           heading: str | None = None, paragraphs: list[str] | None = None, details: list[tuple[str, str]] | None = None,
           button_label: str = "Open in the app", footnote: str | None = None, subject: str | None = None) -> int:
    """Raise one notification (and queue one email) for each user who wants it. Returns how many people were notified."""
    category = KINDS[kind][0]
    locked = bool(CATEGORIES[category].get("locked"))
    people = {u.id: u for u in users if u.can_sign_in or kind == "account_changed"}
    if not people:
        return 0
    prefs = prefs_for(db, people)
    count = 0
    for uid, u in people.items():
        want_app, want_mail = prefs[uid][category]
        want_app, want_mail = want_app or locked, want_mail or locked
        if not (want_app or want_mail):
            continue
        if dedupe and db.execute(select(Notification.id).where(Notification.user_id == uid, Notification.dedupe_key == dedupe)).first():
            continue
        mail_row = None
        if want_mail:
            html, text = emailing.compose(
                heading or title, paragraphs or [body], greeting=f"Hi {_first_name(u)},", details=details,
                button=(button_label, emailing.abs_url(url)) if url else None, footnote=footnote)
            try:
                mail_row = emailing.queue_email(
                    db, to_email=u.email, to_name=u.full_name, subject=subject or f"[{get_settings().app_name}] {title}",
                    html=html, text=text, kind=kind, user_id=uid)
            except ValueError:
                log.warning("not emailing %s: invalid address", uid)
        if want_app:
            db.add(Notification(
                user_id=uid, kind=kind, level=level, title=emailing.one_line(title, 200), body=emailing.one_line(body, 500),
                url=url, entity_type=entity[0] if entity else None, entity_id=entity[1] if entity else None,
                dedupe_key=dedupe, email_id=mail_row.id if mail_row else None))
        count += 1
    return count


def approvers(db: Session, exclude_id: int | None = None) -> list[User]:
    stmt = select(User).where(User.role.in_([Role.ACCOUNTS, Role.ADMIN]), User.is_active.is_(True), User.deleted_at.is_(None))
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return list(db.execute(stmt.order_by(User.id)).scalars())


def admins(db: Session) -> list[User]:
    return list(db.execute(select(User).where(User.role == Role.ADMIN, User.is_active.is_(True), User.deleted_at.is_(None))).scalars())


# --------------------------------------------------------------------------- order events
def _order_details(o: SalesOrder) -> list[tuple[str, str]]:
    return [("Order", o.so_number or f"Draft #{o.id}"), ("Customer", o.party_name), ("Customer PO", o.customer_po_number),
            ("Total", f"Rs {format_inr(o.total_amount)}")]


def _label(o: SalesOrder) -> str:
    return o.so_number or f"draft #{o.id}"


def order_submitted(db: Session, order: SalesOrder, by: User) -> None:
    notify(db, approvers(db, exclude_id=by.id), "order_pending",
           f"Order {_label(order)} is waiting for approval",
           f"{by.full_name} submitted {_label(order)} for {order.party_name}, Rs {format_inr(order.total_amount)}.",
           url=f"/orders/{order.id}", level="warning", entity=("sales_order", order.id),
           heading="An order is waiting for your approval",
           paragraphs=[f"{by.full_name} has submitted a sales order. Please review it and approve it or send it back with a reason."],
           details=_order_details(order) + [("Submitted by", by.full_name)], button_label="Review the order",
           subject=f"[{get_settings().app_name}] Approval needed: {_label(order)} - {order.party_name}")


def order_withdrawn(db: Session, order: SalesOrder, by: User) -> None:
    notify(db, approvers(db, exclude_id=by.id), "order_withdrawn", f"Order {_label(order)} was withdrawn",
           f"{by.full_name} took {_label(order)} for {order.party_name} back to draft. Nothing to approve for now.",
           url=f"/orders/{order.id}", entity=("sales_order", order.id))


def order_approved(db: Session, order: SalesOrder, by: User) -> None:
    if order.created_by_id == by.id:
        return
    notify(db, [order.created_by], "order_approved", f"Order {_label(order)} was approved",
           f"{by.full_name} approved it. It is now queued for Tally.", url=f"/orders/{order.id}", level="success",
           entity=("sales_order", order.id), heading="Your order was approved",
           paragraphs=[f"{by.full_name} approved your sales order. It has been queued and will be delivered to Tally automatically."],
           details=_order_details(order) + [("Approved by", by.full_name)] + ([("Remarks", order.review_remarks)] if order.review_remarks else []),
           button_label="View the order", subject=f"[{get_settings().app_name}] Approved: {_label(order)} - {order.party_name}")


def order_rejected(db: Session, order: SalesOrder, by: User) -> None:
    if order.created_by_id == by.id:
        return
    reason = order.review_remarks or ""
    notify(db, [order.created_by], "order_rejected", f"Order {_label(order)} was sent back",
           f"{by.full_name}: {reason}", url=f"/orders/{order.id}", level="danger", entity=("sales_order", order.id),
           heading="Your order was sent back",
           paragraphs=[f"{by.full_name} sent your sales order back. Fix what is mentioned below, then submit it again."],
           details=_order_details(order) + [("Sent back by", by.full_name), ("Reason", reason)], button_label="Edit the order",
           subject=f"[{get_settings().app_name}] Sent back: {_label(order)} - {order.party_name}")


def order_deleted(db: Session, order: SalesOrder, by: User) -> None:
    if order.created_by_id == by.id:
        return
    notify(db, [order.created_by], "order_deleted", f"Order {_label(order)} was deleted",
           f"{by.full_name} deleted it. The record is kept for audit.", url=f"/orders/{order.id}", level="warning", entity=("sales_order", order.id))


def tally_delivered(db: Session, order: SalesOrder, reference: str | None) -> None:
    people = {order.created_by_id: order.created_by}
    if order.reviewed_by is not None:
        people[order.reviewed_by_id] = order.reviewed_by
    notify(db, people.values(), "tally_delivered", f"Order {_label(order)} is now in Tally",
           f"Delivered{' with reference ' + reference if reference else ''}.", url=f"/orders/{order.id}", level="success",
           entity=("sales_order", order.id), dedupe=f"tally-ok:{order.id}", heading="Your order has reached Tally",
           paragraphs=["The approved sales order was delivered to Tally."],
           details=_order_details(order) + ([("Tally reference", reference)] if reference else []), button_label="View the order")


def tally_failed(db: Session, order: SalesOrder, attempts: int, error: str, dedupe_stamp: str) -> None:
    notify(db, admins(db), "tally_failed", f"Order {_label(order)} could not be sent to Tally",
           f"Gave up after {attempts} attempts: {error}", url="/admin/tally", level="danger", entity=("sales_order", order.id),
           dedupe=f"tally-dead:{order.id}:{dedupe_stamp}", heading="An order could not be delivered to Tally",
           paragraphs=[f"The system tried {attempts} times and has stopped. Fix the cause, then press Retry on the Tally queue page."],
           details=_order_details(order) + [("Last error", error)], button_label="Open the Tally queue",
           subject=f"[{get_settings().app_name}] Tally delivery failed: {_label(order)}")


# --------------------------------------------------------------------------- account events
def password_changed(db: Session, user: User, how: str) -> None:
    when = to_ist(utcnow())
    notify(db, [user], "password_changed", "Your password was changed", f"Changed {how}.", url="/account/notifications",
           level="warning", heading="Your password was changed",
           paragraphs=[f"The password for your account was changed {how} at {when:%d %b %Y, %I:%M %p} (India time). You were signed out on your other devices."],
           footnote="If this was not you, contact your administrator immediately.", button_label="Notification settings")


def password_reset_by_admin(db: Session, user: User, admin: User) -> None:
    notify(db, [user], "password_reset_by_admin", "An administrator reset your password",
           f"{admin.full_name} gave you a temporary password. You must change it when you sign in.", level="warning",
           heading="Your password was reset by an administrator",
           paragraphs=[f"{admin.full_name} reset your password. Ask them for the temporary password; you will choose a new one when you sign in."],
           footnote="For your safety, passwords are never sent by email.", url="/login", button_label="Go to sign in")


def account_locked(db: Session, user: User, minutes: int, stamp: str) -> None:
    notify(db, [user], "account_locked", "Your account was locked",
           f"Too many wrong passwords. It unlocks by itself in {minutes} minutes.", level="danger", dedupe=f"locked:{stamp}",
           heading="Your account was temporarily locked",
           paragraphs=[f"There were several failed sign-in attempts on your account, so it is locked for {minutes} minutes."],
           footnote="If it was not you, tell your administrator. You can also use 'Forgot your password?' on the sign-in page.",
           url="/login", button_label="Go to sign in")


def account_created(db: Session, user: User) -> None:
    notify(db, [user], "account_created", "Your account is ready", "An administrator created your account.", level="success",
           heading="Your account is ready",
           paragraphs=[f"An account was created for you on {get_settings().app_name}. Your administrator will give you a temporary password; you will choose your own when you first sign in."],
           url="/login", button_label="Go to sign in", subject=f"[{get_settings().app_name}] Your account is ready")


def account_changed(db: Session, user: User, changes: list[str]) -> None:
    if not changes:
        return
    notify(db, [user], "account_changed", "Your access was changed", "; ".join(changes), level="info", url="/",
           heading="Your access was changed", paragraphs=["An administrator changed your account:"] + changes, button_label="Open the app")


# --------------------------------------------------------------------------- reminders
def send_pending_reminders(db: Session) -> int:
    """Once a day, tell approvers about orders waiting longer than PENDING_REMINDER_HOURS. Idempotent per day."""
    hours = get_settings().pending_reminder_hours
    if hours <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=hours)
    old = db.execute(
        select(SalesOrder).where(SalesOrder.deleted_at.is_(None), SalesOrder.status == SOStatus.PENDING_APPROVAL,
                                 SalesOrder.submitted_at.is_not(None), SalesOrder.submitted_at < cutoff)
        .order_by(SalesOrder.submitted_at)
    ).scalars().all()
    if not old:
        return 0
    day = to_ist(utcnow()).strftime("%Y-%m-%d")
    n = len(old)
    sample = ", ".join(f"{_label(o)} ({o.party_name})" for o in old[:5]) + (f" and {n - 5} more" if n > 5 else "")
    sent = 0
    for person in approvers(db):
        mine = [o for o in old if o.created_by_id != person.id]
        if not mine:
            continue
        sent += notify(db, [person], "pending_reminder",
                       f"{len(mine)} order{'s have' if len(mine) != 1 else ' has'} been waiting over {hours} hours",
                       sample, url="/orders?f_status=pending_approval", level="warning", dedupe=f"reminder:{day}",
                       heading="Orders are waiting for approval",
                       paragraphs=[f"{len(mine)} sales order{'s have' if len(mine) != 1 else ' has'} been waiting for a decision for more than {hours} hours."],
                       details=[(_label(o), f"{o.party_name}, Rs {format_inr(o.total_amount)}") for o in mine[:8]],
                       button_label="Open the approvals", subject=f"[{get_settings().app_name}] {len(mine)} order(s) waiting for approval")
    return sent


# --------------------------------------------------------------------------- reading + housekeeping
def unread_count(db: Session, user_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None), Notification.deleted_at.is_(None))
    ).scalar_one()


def mark_all_read(db: Session, user_id: int) -> int:
    res = db.execute(update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None), Notification.deleted_at.is_(None))
                     .values(read_at=utcnow()))
    return res.rowcount or 0


def purge_old(db: Session, days: int = 90) -> int:
    """Read (or deleted) notifications older than ``days`` are removed for good; unread ones are kept for a year."""
    now = utcnow()
    res = db.execute(Notification.__table__.delete().where(
        ((Notification.read_at.is_not(None)) | (Notification.deleted_at.is_not(None))) & (Notification.created_at < now - timedelta(days=days))
        | (Notification.created_at < now - timedelta(days=365))))
    return res.rowcount or 0
