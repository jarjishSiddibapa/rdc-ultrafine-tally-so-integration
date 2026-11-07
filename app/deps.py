"""Request-scoped dependencies: auth, role guards, CSRF."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import csrf_valid
from app.models import Role, SalesOrder, SOStatus, User
from app.services import auth as auth_service
from app.services import notify as notify_service
from app.services.audit import Ctx
from app.web import CSRF_COOKIE, SESSION_COOKIE

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class LoginRequired(Exception):
    def __init__(self, next_url: str | None = None) -> None:
        self.next_url = next_url


class PasswordChangeRequired(Exception):
    pass


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def get_ctx(request: Request) -> Ctx:
    ctx = getattr(request.state, "ctx", None)
    if ctx is None:
        ctx = Ctx(
            user=getattr(request.state, "user", None),
            ip=client_ip(request),
            request_id=getattr(request.state, "request_id", None),
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        )
        request.state.ctx = ctx
    return ctx


async def csrf_protect(request: Request) -> None:
    """Applied globally. Signed double-submit token + Origin check on every state-changing request."""
    if request.method not in UNSAFE:
        return
    origin = request.headers.get("origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "Cross-site request blocked")
    elif origin == "null":
        raise HTTPException(403, "Cross-site request blocked")
    submitted = request.headers.get("x-csrf-token")
    if not submitted:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            submitted = form.get("csrf_token") if isinstance(form.get("csrf_token"), str) else None
    if not csrf_valid(request.cookies.get(CSRF_COOKIE), submitted):
        raise HTTPException(403, "Your form expired. Reload the page and try again.")


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    cached = getattr(request.state, "_user_loaded", False)
    if cached:
        return request.state.user
    found = auth_service.resolve_session(db, request.cookies.get(SESSION_COOKIE))
    user = found[1] if found else None
    request.state.user = user
    # Templates get a plain snapshot: the ORM object can be expired by a rollback (e.g. on a 403)
    # before the error page renders.
    request.state.view_user = (
        SimpleNamespace(id=user.id, full_name=user.full_name, email=user.email, role=user.role) if user else None
    )
    request.state.nav_pending = 0
    request.state.nav_unread = notify_service.unread_count(db, user.id) if user else 0
    if user and user.role in (Role.ACCOUNTS, Role.ADMIN):
        request.state.nav_pending = db.execute(
            select(func.count()).select_from(SalesOrder).where(
                SalesOrder.deleted_at.is_(None), SalesOrder.status == SOStatus.PENDING_APPROVAL,
                SalesOrder.created_by_id != user.id)
        ).scalar_one()
    request.state._user_loaded = True
    get_ctx(request).user = user
    return user


def passive_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Who is signed in, WITHOUT counting this request as activity (the bell polls in the background and
    must not keep an abandoned session alive past the idle timeout)."""
    found = auth_service.resolve_session(db, request.cookies.get(SESSION_COOKIE), touch=False)
    return found[1] if found else None


def current_user(request: Request, user: User | None = Depends(optional_user)) -> User:
    if user is None:
        nxt = request.url.path if request.method == "GET" else None
        raise LoginRequired(nxt)
    if user.must_change_password and request.url.path not in ("/account/password", "/logout"):
        raise PasswordChangeRequired()
    return user


def require_roles(*roles: Role):
    allowed = set(roles)

    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, "You do not have access to this page.")
        return user

    return checker
