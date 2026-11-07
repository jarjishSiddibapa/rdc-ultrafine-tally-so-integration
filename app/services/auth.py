"""Authentication: login with lockout, server-side sessions, password changes."""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    burn_password_check,
    hash_password,
    hash_token,
    needs_rehash,
    new_session_token,
    password_problems,
    verify_password,
)
from app.models import PasswordReset, QueuedEmail, User, UserSession
from app.models.base import utcnow
from app.services import emailing, notify
from app.services.audit import Ctx, audit

GENERIC_FAIL = "The email or password is incorrect."


class AuthError(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(db: Session, email: str, *, include_deleted: bool = False) -> User | None:
    stmt = select(User).where(User.email == normalize_email(email))
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    return db.execute(stmt).scalar_one_or_none()


def authenticate(db: Session, email: str, password: str, ctx: Ctx) -> tuple[User, str]:
    """Return (user, raw_session_token) or raise AuthError with a deliberately generic message."""
    s = get_settings()
    now = utcnow()
    user = get_user_by_email(db, email)

    if user is None:
        burn_password_check(password)
        audit(db, ctx, "login_failed", details={"reason": "unknown_user"}, actor_email=normalize_email(email)[:254])
        db.commit()
        raise AuthError(GENERIC_FAIL)

    if user.locked_until and user.locked_until > now:
        burn_password_check(password)
        audit(db, ctx, "login_blocked", entity_type="user", entity_id=user.id, details={"reason": "locked"}, actor_email=user.email)
        db.commit()
        raise AuthError(GENERIC_FAIL)

    if not verify_password(password, user.password_hash) or not user.can_sign_in:
        # Atomic increment so concurrent guesses can't dodge the counter.
        db.execute(update(User).where(User.id == user.id).values(failed_login_count=User.failed_login_count + 1))
        count = db.execute(select(User.failed_login_count).where(User.id == user.id)).scalar_one()
        locked = count >= s.max_failed_logins
        if locked:
            db.execute(
                update(User).where(User.id == user.id).values(
                    locked_until=now + timedelta(minutes=s.lockout_minutes), failed_login_count=0
                )
            )
        audit(db, ctx, "login_failed", entity_type="user", entity_id=user.id,
              details={"locked": locked, "active": user.can_sign_in}, actor_email=user.email)
        if locked and user.can_sign_in:
            notify.account_locked(db, user, s.lockout_minutes, now.strftime("%Y%m%d%H%M"))
        db.commit()
        raise AuthError(GENERIC_FAIL)

    if user.must_change_password and user.password_changed_at and now - user.password_changed_at > timedelta(hours=s.temp_password_hours):
        # Right password, but a temporary one that sat unused too long (it may be sitting in an inbox).
        audit(db, ctx, "login_failed", entity_type="user", entity_id=user.id, details={"reason": "temp_password_expired"}, actor_email=user.email)
        db.commit()
        raise AuthError("Your temporary password has expired. Ask your administrator to send a new one, or use 'Forgot your password?'.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    token = create_session(db, user, ctx)
    ctx.user = user
    audit(db, ctx, "login", entity_type="user", entity_id=user.id)
    db.commit()
    return user, token


def create_session(db: Session, user: User, ctx: Ctx) -> str:
    ua = ctx.user_agent
    s = get_settings()
    token = new_session_token()
    now = utcnow()
    db.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=s.session_absolute_hours),
            ip_address=ctx.ip,
            user_agent=(ua or "")[:255] or None,
        )
    )
    return token


def resolve_session(db: Session, token: str | None, *, touch: bool = True) -> tuple[UserSession, User] | None:
    """Validate the cookie token: not revoked, within idle + absolute limits, user still allowed."""
    if not token:
        return None
    s = get_settings()
    sess = db.execute(select(UserSession).where(UserSession.token_hash == hash_token(token))).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return None
    now = utcnow()
    if sess.expires_at <= now or sess.last_seen_at + timedelta(minutes=s.session_idle_minutes) <= now:
        return None
    user = sess.user
    if not user.can_sign_in:
        return None
    # Touch at most once a minute to avoid a write on every request.
    if touch and now - sess.last_seen_at > timedelta(seconds=60):
        sess.last_seen_at = now
        db.commit()
    return sess, user


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    db.execute(update(UserSession).where(UserSession.token_hash == hash_token(token), UserSession.revoked_at.is_(None))
               .values(revoked_at=utcnow()))


def revoke_all_sessions(db: Session, user_id: int, except_token: str | None = None) -> None:
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_token:
        stmt = stmt.where(UserSession.token_hash != hash_token(except_token))
    db.execute(stmt.values(revoked_at=utcnow()))


def purge_expired_sessions(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=7)
    res = db.execute(UserSession.__table__.delete().where(UserSession.expires_at < cutoff))
    return res.rowcount or 0


def change_password(db: Session, user: User, current: str | None, new: str, ctx: Ctx, *, keep_token: str | None) -> list[str]:
    """Self-service change. Returns a list of problems (empty on success)."""
    if current is not None and not verify_password(current, user.password_hash):
        audit(db, ctx, "password_change_failed", entity_type="user", entity_id=user.id)
        db.commit()
        return ["The current password is incorrect."]
    problems = password_problems(new, user.email, user.full_name)
    if verify_password(new, user.password_hash):
        problems.append("Choose a password you have not used just now.")
    if problems:
        return problems
    set_password(db, user, new, must_change=False)
    revoke_all_sessions(db, user.id, except_token=keep_token)
    audit(db, ctx, "password_changed", entity_type="user", entity_id=user.id)
    notify.password_changed(db, user, "from your account page")
    db.commit()
    return []


def set_password(db: Session, user: User, new_password: str, *, must_change: bool) -> None:
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = must_change
    user.failed_login_count = 0
    user.locked_until = None


def count_active_admins(db: Session) -> int:
    from app.models.enums import Role

    return db.execute(
        select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active.is_(True), User.deleted_at.is_(None))
    ).scalar_one()


# --------------------------------------------------------------------------- forgot / reset password
RESET_COOLDOWN = timedelta(seconds=60)


def request_password_reset(db: Session, user: User, ctx: Ctx, *, by_admin: User | None = None) -> bool:
    """Email a one-time reset link. Returns False (and does nothing) if one was requested within the last minute."""
    s = get_settings()
    now = utcnow()
    last = db.execute(select(func.max(PasswordReset.created_at)).where(PasswordReset.user_id == user.id)).scalar_one()
    if last and now - last < RESET_COOLDOWN:
        return False
    # Only the newest link works: older unused ones are cancelled.
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None),
                                           PasswordReset.revoked_at.is_(None)).values(revoked_at=now))
    token = secrets.token_urlsafe(32)
    db.add(PasswordReset(token_hash=hash_token(token), user_id=user.id, created_at=now,
                         expires_at=now + timedelta(minutes=s.reset_token_minutes),
                         requested_by_admin_id=by_admin.id if by_admin else None,
                         ip_address=ctx.ip, user_agent=(ctx.user_agent or "")[:255] or None))
    link = emailing.abs_url(f"/reset-password?token={token}")
    html, text = emailing.compose(
        "Reset your password",
        [f"We received a request to reset the password for your {s.app_name} account"
         + (f", made by administrator {by_admin.full_name}." if by_admin else "."),
         f"The link below works once and expires in {s.reset_token_minutes} minutes."],
        greeting=f"Hi {(user.full_name or 'there').split()[0]},", button=("Choose a new password", link),
        footnote="If you did not ask for this, ignore this email. Your password stays as it is.")
    emailing.queue_email(db, to_email=user.email, to_name=user.full_name, subject=f"[{s.app_name}] Reset your password",
                         html=html, text=text, kind="password_reset", user_id=user.id, sensitive=True,
                         requested_by_id=by_admin.id if by_admin else None)
    audit(db, ctx, "password_reset_requested", entity_type="user", entity_id=user.id,
          details={"by_admin": by_admin.email if by_admin else None, "expires_minutes": s.reset_token_minutes}, actor_email=user.email)
    return True


def queue_welcome_email(db: Session, user: User, password: str, ctx: Ctx, *, by_admin: User | None, resend: bool = False) -> bool:
    """Email the person their temporary password and sign-in link. Returns False if one went out within the last minute.

    The message body is wiped as soon as it is delivered (see emailing.deliver_pending); the password only
    ever lives hashed in the users table after that. The person must choose their own password at first sign-in.
    """
    s = get_settings()
    cutoff = utcnow() - RESET_COOLDOWN
    recent = db.execute(select(func.count()).select_from(QueuedEmail).where(
        QueuedEmail.user_id == user.id, QueuedEmail.kind == "welcome", QueuedEmail.created_at > cutoff)).scalar_one()
    if recent:
        return False
    html, text = emailing.compose(
        f"Welcome to {s.app_name}" if not resend else "Your new temporary password",
        ([f"An account was created for you by {by_admin.full_name if by_admin else 'your administrator'}."] if not resend
         else [f"{by_admin.full_name if by_admin else 'Your administrator'} issued you a new temporary password."])
        + ["Sign in with the details below. You will be asked to choose your own password straight away."],
        greeting=f"Hi {(user.full_name or 'there').split()[0]},",
        details=[("Email", user.email), ("Role", user.role.label), ("Temporary password", password)],
        button=("Sign in", emailing.abs_url("/login")),
        footnote=f"The temporary password stops working after {s.temp_password_hours} hours and can be used only to set your own. "
                 "Do not share it, and delete this email once you have signed in.")
    emailing.queue_email(db, to_email=user.email, to_name=user.full_name,
                         subject=f"[{s.app_name}] " + ("Your account is ready" if not resend else "Your new temporary password"),
                         html=html, text=text, kind="welcome", user_id=user.id, sensitive=True,
                         requested_by_id=by_admin.id if by_admin else None)
    audit(db, ctx, "welcome_email_queued", entity_type="user", entity_id=user.id, details={"resend": resend, "to": user.email})
    return True


def find_valid_reset(db: Session, token: str | None) -> tuple[PasswordReset, User] | None:
    """The reset row and its user if the token is genuine, unused, unexpired and the user may still sign in."""
    if not token or len(token) > 100:
        return None
    row = db.execute(select(PasswordReset).where(PasswordReset.token_hash == hash_token(token))).scalar_one_or_none()
    if row is None or row.used_at or row.revoked_at or row.expires_at <= utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.can_sign_in:
        return None
    return row, user


def complete_password_reset(db: Session, reset: PasswordReset, user: User, new_password: str, confirm: str, ctx: Ctx) -> list[str]:
    """Set the new password. Returns problems (empty on success). Signs the user out everywhere."""
    if new_password != confirm:
        return ["The new passwords do not match."]
    problems = password_problems(new_password, user.email, user.full_name)
    if verify_password(new_password, user.password_hash):
        problems.append("Choose a password you have not used just now.")
    if problems:
        return problems
    now = utcnow()
    set_password(db, user, new_password, must_change=False)
    reset.used_at = now
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None),
                                           PasswordReset.revoked_at.is_(None)).values(revoked_at=now))
    revoke_all_sessions(db, user.id)
    ctx.user = user
    audit(db, ctx, "password_reset_completed", entity_type="user", entity_id=user.id)
    notify.password_changed(db, user, "using a reset link")
    db.commit()
    return []
