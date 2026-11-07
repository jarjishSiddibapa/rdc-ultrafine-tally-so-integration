"""Administration: users, settings, audit log, Tally outbox."""
from __future__ import annotations

import re
import secrets
import string

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import GST_STATES
from app.core.database import get_db
from app.core.utils import clean_text
from app.deps import get_ctx, require_roles
from app.integrations.tally import get_gateway
from app.models import AuditLog, EmailStatus, OutboxStatus, Plant, QueuedEmail, Role, TallyOutbox, User
from app.services import auth as auth_service
from app.services import emailing, notify
from app.services import outbox as outbox_service
from app.services import settings_service
from app.routers._lists import run_list
from app.services import specs
from app.services.audit import Ctx, audit
from app.services.specs import audit_spec, user_spec
from app.web import redirect, render
from app.models.base import utcnow

router = APIRouter(prefix="/admin", dependencies=[Depends(require_roles(Role.ADMIN))])
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9 .%()/&{}\-]{3,100}$")


def temp_password() -> str:
    """Random, policy-compliant one-time password."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "lIO01o")   # nothing easily misread when typed from an email
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


# --------------------------------------------------------------------------- users
@router.get("/users")
def users_list(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    base = select(User)
    if request.query_params.get("show_deleted") != "1" and request.query_params.get("f_status") != "removed":
        base = base.where(User.deleted_at.is_(None))
    st, page, download = run_list(request, db, ctx.user, ctx, base, user_spec(), "Users")
    if download:
        return download
    return render(request, "admin/users.html", {"st": st, "page": page, "show_deleted": request.query_params.get("show_deleted") == "1",
                                                "mail_ok": get_settings().mail_configured})


def _plants(db: Session) -> list[Plant]:
    return db.execute(select(Plant).where(Plant.deleted_at.is_(None), Plant.is_active.is_(True)).order_by(Plant.name)).scalars().all()


def _form_ctx(db: Session, target, values: dict, errors: dict, plant_ids: list[int] | None = None) -> dict:
    chosen = {p.id for p in target.plants} if (target is not None and plant_ids is None) else set(plant_ids or [])
    return {"target": target, "values": values, "errors": errors, "roles": list(Role), "all_plants": _plants(db), "chosen_plants": chosen,
            "mail_ok": get_settings().mail_configured}


def _pick_plants(db: Session, plant_ids: list[int]) -> list[Plant]:
    wanted = set(plant_ids)
    return [p for p in _plants(db) if p.id in wanted]


@router.get("/users/new")
def user_new(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/user_form.html", _form_ctx(db, None, {"email_login": "on"}, {}))


def _validate_user(db: Session, email: str, name: str, role: str, existing: User | None) -> tuple[dict, dict]:
    errors: dict[str, str] = {}
    values = {"email": email, "full_name": name, "role": role}
    if existing is None:
        try:
            values["email"] = validate_email(email.strip(), check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            errors["email"] = "Enter a valid email address."
        else:
            if auth_service.get_user_by_email(db, values["email"], include_deleted=True):
                errors["email"] = "A user with this email already exists (it may be deleted; restore it instead)."
    if not (2 <= len(name) <= 120):
        errors["full_name"] = "Enter the full name (2 to 120 characters)."
    try:
        Role(role)
    except ValueError:
        errors["role"] = "Choose a role."
    return values, errors


@router.post("/users/new")
def user_create(request: Request, email: str = Form("", max_length=254), full_name: str = Form("", max_length=120),
                role: str = Form(""), plant_ids: list[int] = Form([]), email_login: str | None = Form(None),
                db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    values, errors = _validate_user(db, email, clean_text(full_name, 120), role, None)
    values["email_login"] = "on" if email_login == "on" else ""
    if errors:
        return render(request, "admin/user_form.html", _form_ctx(db, None, values, errors, plant_ids), status_code=422)
    send = email_login == "on" and get_settings().mail_configured
    pw = temp_password()
    user = User(email=values["email"], full_name=values["full_name"], role=Role(role), password_hash="", is_active=True)
    auth_service.set_password(db, user, pw, must_change=True)
    user.plants = _pick_plants(db, plant_ids) if Role(role) is Role.SALES else []
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        errors["email"] = "A user with this email already exists."
        return render(request, "admin/user_form.html", _form_ctx(db, None, values, errors, plant_ids), status_code=422)
    audit(db, ctx, "user_created", entity_type="user", entity_id=user.id,
          details={"email": user.email, "role": role, "plants": [p.name for p in user.plants]})
    if send:
        auth_service.queue_welcome_email(db, user, pw, ctx, by_admin=ctx.user)   # the welcome mail replaces the generic "account ready" notice
    else:
        notify.account_created(db, user)
    db.commit()
    return render(request, "admin/user_credentials.html", {"target": user, "temp_password": None if send else pw, "reason": "created", "emailed": send,
                                                         "temp_hours": get_settings().temp_password_hours})


def _target(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "User not found.")
    return u


@router.get("/users/{user_id}")
def user_edit_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    u = _target(db, user_id)
    return render(request, "admin/user_form.html", _form_ctx(db, u, {"email": u.email, "full_name": u.full_name, "role": u.role.value}, {}))


@router.post("/users/{user_id}")
def user_update(user_id: int, request: Request, full_name: str = Form("", max_length=120), role: str = Form(""),
                plant_ids: list[int] = Form([]), is_active: str | None = Form(None), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    u = _target(db, user_id)
    me = ctx.user
    values, errors = _validate_user(db, u.email, clean_text(full_name, 120), role, u)
    active = is_active == "on"
    if not errors and me and u.id == me.id and (not active or Role(role) is not Role.ADMIN):
        errors["_"] = "You cannot deactivate or demote your own account."
    if not errors and u.role is Role.ADMIN and (not active or Role(role) is not Role.ADMIN) and auth_service.count_active_admins(db) <= 1:
        errors["_"] = "At least one active administrator is required."
    if errors:
        return render(request, "admin/user_form.html", _form_ctx(db, u, values, errors, plant_ids), status_code=422)
    before = {"role": u.role.value, "active": u.is_active, "plants": [p.name for p in u.plants]}
    new_plants = _pick_plants(db, plant_ids) if Role(role) is Role.SALES else []
    u.full_name = values["full_name"]
    u.role = Role(role)
    u.is_active = active
    u.plants = new_plants
    changes = []
    if before["role"] != role:
        changes.append(f"Your role is now {Role(role).label}.")
    if before["plants"] != [p.name for p in new_plants] and Role(role) is Role.SALES:
        changes.append("Your plants are now: " + (", ".join(p.name for p in new_plants) or "none") + ".")
    if before["active"] != active:
        changes.append("Your account was enabled." if active else "Your account was disabled.")
    notify.account_changed(db, u, changes)
    if before["active"] and not active or before["role"] != role:
        auth_service.revoke_all_sessions(db, u.id)   # new rights / disabled -> force re-login
    audit(db, ctx, "user_updated", entity_type="user", entity_id=u.id,
          details={"before": before, "after": {"role": role, "active": active, "plants": [p.name for p in new_plants]}})
    db.commit()
    return redirect("/admin/users", "User updated.")


@router.post("/users/{user_id}/reset-password")
def user_reset(user_id: int, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    u = _target(db, user_id)
    pw = temp_password()
    auth_service.set_password(db, u, pw, must_change=True)
    auth_service.revoke_all_sessions(db, u.id)
    audit(db, ctx, "user_password_reset", entity_type="user", entity_id=u.id)
    notify.password_reset_by_admin(db, u, ctx.user)
    db.commit()
    return render(request, "admin/user_credentials.html", {"target": u, "temp_password": pw, "reason": "reset", "emailed": False,
                                                         "temp_hours": get_settings().temp_password_hours})


@router.post("/users/{user_id}/resend-welcome")
def user_resend_welcome(user_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Issue a fresh temporary password and email it. For people who have not signed in yet (lost or expired mail)."""
    u = _target(db, user_id)
    if not u.can_sign_in or not get_settings().mail_configured:
        return redirect("/admin/users", "That account is disabled, or email is not set up.", "error")
    pw = temp_password()
    if not auth_service.queue_welcome_email(db, u, pw, ctx, by_admin=ctx.user, resend=True):
        db.rollback()
        return redirect("/admin/users", f"A welcome email was already sent to {u.email} less than a minute ago.", "info")
    auth_service.set_password(db, u, pw, must_change=True)
    auth_service.revoke_all_sessions(db, u.id)
    audit(db, ctx, "user_password_reset", entity_type="user", entity_id=u.id, details={"emailed": True})
    db.commit()
    return redirect("/admin/users", f"A new temporary password is on its way to {u.email}. The old one no longer works.")


@router.post("/users/{user_id}/send-reset-link")
def user_send_reset_link(user_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Email the person a one-time link to choose their own password (no password is ever emailed)."""
    u = _target(db, user_id)
    if not u.can_sign_in:
        return redirect("/admin/users", "That account is disabled or removed.", "error")
    if not auth_service.request_password_reset(db, u, ctx, by_admin=ctx.user):
        db.rollback()
        return redirect("/admin/users", f"A reset link was already sent to {u.email} less than a minute ago.", "info")
    db.commit()
    return redirect("/admin/users", f"A reset link is on its way to {u.email}. It works once and expires in {get_settings().reset_token_minutes} minutes.")


@router.post("/users/{user_id}/unlock")
def user_unlock(user_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    u = _target(db, user_id)
    u.locked_until = None
    u.failed_login_count = 0
    audit(db, ctx, "user_unlocked", entity_type="user", entity_id=u.id)
    db.commit()
    return redirect("/admin/users", "Account unlocked.")


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    u = _target(db, user_id)
    if ctx.user and u.id == ctx.user.id:
        return redirect("/admin/users", "You cannot delete your own account.", "error")
    if u.role is Role.ADMIN and u.is_active and auth_service.count_active_admins(db) <= 1:
        return redirect("/admin/users", "At least one active administrator is required.", "error")
    u.soft_delete(ctx.user.id if ctx.user else None)
    auth_service.revoke_all_sessions(db, u.id)
    audit(db, ctx, "user_deleted", entity_type="user", entity_id=u.id)
    db.commit()
    return redirect("/admin/users", "User removed. Their history is kept.")


@router.post("/users/{user_id}/restore")
def user_restore(user_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    u = _target(db, user_id)
    u.restore()
    audit(db, ctx, "user_restored", entity_type="user", entity_id=u.id)
    db.commit()
    return redirect("/admin/users?show_deleted=1", "User restored.")


# --------------------------------------------------------------------------- settings
@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", {"cfg": settings_service.get_all(db), "errors": {}, "states": GST_STATES})


@router.post("/settings")
def settings_save(request: Request, company_name: str = Form("", max_length=120),
                  tax_ledger_cgst: str = Form("", max_length=100), tax_ledger_sgst: str = Form("", max_length=100),
                  tax_ledger_igst: str = Form("", max_length=100), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    vals = {"company_name": clean_text(company_name, 120),
            "tax_ledger_cgst": clean_text(tax_ledger_cgst, 100), "tax_ledger_sgst": clean_text(tax_ledger_sgst, 100),
            "tax_ledger_igst": clean_text(tax_ledger_igst, 100)}
    errors: dict[str, str] = {}
    for k in ("tax_ledger_cgst", "tax_ledger_sgst", "tax_ledger_igst"):
        if not _TEMPLATE_RE.match(vals[k]):
            errors[k] = "Use 3 to 100 letters, numbers or . % ( ) / & - and optionally {rate}."
    if errors:
        return render(request, "admin/settings.html", {"cfg": vals, "errors": errors, "states": GST_STATES}, status_code=422)
    old = settings_service.get_all(db)
    for k, v in vals.items():
        settings_service.set_value(db, k, v, ctx.user.id if ctx.user else None)
    audit(db, ctx, "settings_changed", details={"before": {k: old[k] for k in vals}, "after": vals})
    db.commit()
    return redirect("/admin/settings", "Settings saved.")


# --------------------------------------------------------------------------- audit log
@router.get("/audit")
def audit_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    st, page, download = run_list(request, db, ctx.user, ctx, select(AuditLog), audit_spec(db), "Audit log")
    if download:
        return download
    return render(request, "admin/audit.html", {"st": st, "page": page})


# --------------------------------------------------------------------------- tally outbox
@router.get("/tally")
def tally_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    st, page, download = run_list(request, db, ctx.user, ctx, specs.outbox_base(), specs.outbox_spec(), "Tally queue")
    if download:
        return download
    s = get_settings()
    return render(request, "admin/tally.html", {"st": st, "page": page, "configured": get_gateway().configured,
                                                 "url_host": s.tally_api_url.split("//")[-1].split("/")[0] if s.tally_api_url else ""})


@router.post("/tally/{row_id}/retry")
def tally_retry(row_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    row = db.get(TallyOutbox, row_id)
    if row is None:
        raise HTTPException(404, "Not found.")
    if row.status in (OutboxStatus.SENT, OutboxStatus.IN_PROGRESS):
        return redirect("/admin/tally", "That order is already in Tally or being sent.", "error")
    outbox_service.retry_now(db, row, ctx)
    db.commit()
    return redirect("/admin/tally", "Queued for another attempt.")


# --------------------------------------------------------------------------- email log
@router.get("/mail")
def mail_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    st, page, download = run_list(request, db, ctx.user, ctx, select(QueuedEmail), specs.mail_spec(), "Email log")
    if download:
        return download
    s = get_settings()
    waiting = db.execute(select(func.count()).select_from(QueuedEmail).where(
        QueuedEmail.status.in_([EmailStatus.PENDING, EmailStatus.FAILED, EmailStatus.SENDING]))).scalar_one()
    return render(request, "admin/mail.html", {
        "st": st, "page": page, "configured": s.mail_configured, "enabled": s.mail_enabled, "host": f"{s.smtp_host}:{s.smtp_port}",
        "sender": s.mail_from, "base_url": s.app_base_url, "waiting": waiting, "EmailStatus": EmailStatus})


@router.post("/mail/test")
def mail_test(db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Queue a test message to the administrator's own address (never to an address typed into a form)."""
    me = ctx.user
    html, text = emailing.compose("Test email", ["If you can read this, the system can send email and the settings are right."],
                                  greeting=f"Hi {me.full_name.split()[0]},",
                                  details=[("Sent at", utcnow().strftime("%d %b %Y %H:%M:%S UTC")), ("Requested by", me.full_name)])
    row = emailing.queue_email(db, to_email=me.email, to_name=me.full_name, subject=f"[{get_settings().app_name}] Test email",
                               html=html, text=text, kind="test_email", user_id=me.id, requested_by_id=me.id)
    audit(db, ctx, "test_email_queued", entity_type="email", entity_id=row.id, details={"to": me.email})
    db.commit()
    if not get_settings().mail_configured:
        return redirect("/admin/mail", "Queued, but email is not configured yet, so it will wait.", "info")
    return redirect("/admin/mail", f"Test email queued for {me.email}. It is sent within a few seconds.")


@router.post("/mail/{row_id}/retry")
def mail_retry(row_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    row = db.get(QueuedEmail, row_id)
    if row is None:
        raise HTTPException(404, "Not found.")
    if not emailing.retry(row):
        return redirect("/admin/mail", "That email cannot be sent again (already sent, or its contents were removed for safety).", "error")
    audit(db, ctx, "email_retry", entity_type="email", entity_id=row.id)
    db.commit()
    return redirect("/admin/mail", "Queued for another attempt.")
