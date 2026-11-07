from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import login_limiter, new_csrf_seed
from app.deps import current_user, get_ctx, optional_user
from app.models import User
from app.services import auth as auth_service
from app.services.audit import Ctx, audit
from app.web import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_auth_cookies,
    redirect,
    render,
    safe_next,
    set_csrf_cookie,
    set_session_cookie,
)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, user: User | None = Depends(optional_user), next: str = "/"):
    if user:
        return redirect("/")
    return render(request, "login.html", {"next": safe_next(next), "email": ""})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form("", max_length=254),
    password: str = Form("", max_length=256),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    ctx: Ctx = Depends(get_ctx),
):
    s = get_settings()
    ip = ctx.ip or "?"
    # Per-IP and per-account throttles before any expensive hashing.
    if not login_limiter.allow(f"ip:{ip}", limit=20, window_seconds=300) or not login_limiter.allow(
        f"acct:{email.strip().lower()}", limit=10, window_seconds=300
    ):
        return render(request, "login.html",
                      {"next": safe_next(next), "email": email, "error": "Too many attempts. Wait a few minutes and try again."},
                      status_code=429)
    try:
        user, token = auth_service.authenticate(db, email, password, ctx)
    except auth_service.AuthError as exc:
        return render(request, "login.html", {"next": safe_next(next), "email": email, "error": str(exc)}, status_code=401)

    resp = redirect("/account/password" if user.must_change_password else safe_next(next))
    set_session_cookie(resp, token)
    set_csrf_cookie(resp, new_csrf_seed())  # rotate on privilege change (login)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    auth_service.revoke_session(db, request.cookies.get(SESSION_COOKIE))
    audit(db, ctx, "logout", entity_type="user", entity_id=user.id)
    db.commit()
    resp = redirect("/login", "You have been signed out.", "info")
    clear_auth_cookies(resp)
    return resp


@router.get("/account/password")
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, "password.html", {"forced": user.must_change_password, "problems": []})


@router.post("/account/password")
def password_submit(
    request: Request,
    current: str = Form("", max_length=256),
    new_password: str = Form("", max_length=256),
    confirm: str = Form("", max_length=256),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    ctx: Ctx = Depends(get_ctx),
):
    problems: list[str] = []
    if new_password != confirm:
        problems.append("The new passwords do not match.")
    else:
        problems = auth_service.change_password(db, user, current, new_password, ctx,
                                                keep_token=request.cookies.get(SESSION_COOKIE))
    if problems:
        return render(request, "password.html", {"forced": user.must_change_password, "problems": problems}, status_code=400)
    return redirect("/", "Password updated. Other devices were signed out.")


# --------------------------------------------------------------------------- forgot / reset password
SENT_MESSAGE = "If that email belongs to an active account, a reset link is on its way. It works once and expires soon."


@router.get("/forgot-password")
def forgot_page(request: Request, user: User | None = Depends(optional_user)):
    if user:
        return redirect("/account/password")
    return render(request, "forgot_password.html", {"email": "", "sent": False})


@router.post("/forgot-password")
def forgot_submit(
    request: Request,
    email: str = Form("", max_length=254),
    db: Session = Depends(get_db),
    ctx: Ctx = Depends(get_ctx),
):
    ip = ctx.ip or "?"
    addr = auth_service.normalize_email(email)
    if not login_limiter.allow(f"fp-ip:{ip}", limit=10, window_seconds=900):
        return render(request, "forgot_password.html",
                      {"email": email, "sent": False, "error": "Too many requests. Wait a few minutes and try again."}, status_code=429)
    # Same answer whether or not the account exists, so this page can't be used to find out who has one.
    if login_limiter.allow(f"fp-acct:{addr}", limit=3, window_seconds=900):
        user = auth_service.get_user_by_email(db, addr) if addr else None
        if user is not None and user.can_sign_in:
            auth_service.request_password_reset(db, user, ctx)
        else:
            audit(db, ctx, "password_reset_requested", details={"known": False}, actor_email=addr[:254] or None)
        db.commit()
    return render(request, "forgot_password.html", {"email": "", "sent": True, "message": SENT_MESSAGE})


@router.get("/reset-password")
def reset_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    # Viewing the page never uses up the link (mail scanners open links), only saving a new password does.
    found = auth_service.find_valid_reset(db, token)
    if found is None:
        return render(request, "reset_password.html", {"invalid": True, "token": "", "problems": []}, status_code=400)
    return render(request, "reset_password.html", {"invalid": False, "token": token, "problems": [], "who": found[1].email})


@router.post("/reset-password")
def reset_submit(
    request: Request,
    token: str = Form("", max_length=100),
    new_password: str = Form("", max_length=256),
    confirm: str = Form("", max_length=256),
    db: Session = Depends(get_db),
    ctx: Ctx = Depends(get_ctx),
):
    ip = ctx.ip or "?"
    if not login_limiter.allow(f"rp-ip:{ip}", limit=20, window_seconds=900):
        return render(request, "reset_password.html", {"invalid": True, "token": "", "problems": []}, status_code=429)
    found = auth_service.find_valid_reset(db, token)
    if found is None:
        audit(db, ctx, "password_reset_failed", details={"reason": "invalid_or_expired_link"})
        db.commit()
        return render(request, "reset_password.html", {"invalid": True, "token": "", "problems": []}, status_code=400)
    reset, user = found
    problems = auth_service.complete_password_reset(db, reset, user, new_password, confirm, ctx)
    if problems:
        return render(request, "reset_password.html",
                      {"invalid": False, "token": token, "problems": problems, "who": user.email}, status_code=400)
    return redirect("/login", "Your password was changed. Sign in with the new one.")
