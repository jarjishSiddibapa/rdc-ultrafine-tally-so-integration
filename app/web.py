"""Template engine, flash messages, CSRF cookie plumbing and response helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.core.config import get_settings
from app.core.constants import GST_STATES, STATE_CHOICES
from app.core.security import csrf_token_for, new_csrf_seed
from app.core.utils import format_inr, to_ist
from app.models import Role, SOStatus, SOType, TallyStatus
from app.models.base import utcnow
from app.services.listing import fmt_duration

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CSRF_COOKIE = "csrf_seed"
SESSION_COOKIE = "sid"
FLASH_COOKIE = "flash"


# --------------------------------------------------------------------------- jinja filters
def _fmt_dt(dt: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    local = to_ist(dt)
    return local.strftime(fmt) if local else ""


def _fmt_date(d) -> str:  # noqa: ANN001
    return d.strftime("%d %b %Y") if d else ""


def _qty(v) -> str:  # noqa: ANN001
    return format_inr(v, 3).rstrip("0").rstrip(".") if v is not None else ""


templates.env.filters.update(
    {"inr": format_inr, "dt": _fmt_dt, "date": _fmt_date, "qty": _qty, "duration": fmt_duration,
     "state_name": lambda c: GST_STATES.get(c or "", "")}
)
templates.env.globals.update(
    {"STATE_CHOICES": STATE_CHOICES, "GST_STATES": GST_STATES, "Role": Role, "SOStatus": SOStatus,
     "SOType": SOType, "TallyStatus": TallyStatus, "app_name": get_settings().app_name}
)


# --------------------------------------------------------------------------- flash (signed cookie)
def _sign(raw: bytes) -> str:
    mac = hmac.new(get_settings().secret_key.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return f"{base64.urlsafe_b64encode(raw).decode()}.{mac}"


def _unsign(value: str) -> bytes | None:
    try:
        b64, mac = value.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64.encode())
    except Exception:
        return None
    good = hmac.new(get_settings().secret_key.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return raw if hmac.compare_digest(good, mac) else None


def set_flash(response: Response, message: str, level: str = "success") -> None:
    response.set_cookie(FLASH_COOKIE, _sign(json.dumps({"m": message[:300], "l": level}).encode()),
                        max_age=60, httponly=True, samesite="lax", secure=get_settings().cookie_secure, path="/")


def pop_flash(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    data = _unsign(raw)
    if data is None:
        return None
    try:
        obj = json.loads(data)
        return {"message": str(obj["m"]), "level": obj["l"] if obj["l"] in ("success", "error", "info") else "info"}
    except Exception:
        return None


# --------------------------------------------------------------------------- csrf cookie
def csrf_seed(request: Request) -> tuple[str, bool]:
    """Return (seed, is_new). A new seed must be set on the outgoing response."""
    seed = request.cookies.get(CSRF_COOKIE)
    if seed and 20 <= len(seed) <= 64:
        return seed, False
    return new_csrf_seed(), True


def set_csrf_cookie(response: Response, seed: str) -> None:
    s = get_settings()
    response.set_cookie(CSRF_COOKIE, seed, max_age=s.session_absolute_hours * 3600, httponly=True,
                        samesite="lax", secure=s.cookie_secure, path="/")


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(SESSION_COOKIE, token, max_age=s.session_absolute_hours * 3600, httponly=True,
                        samesite="lax", secure=s.cookie_secure, path="/")


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# --------------------------------------------------------------------------- responses
def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    seed, is_new = csrf_seed(request)
    ctx: dict[str, Any] = {
        "request": request,
        "user": getattr(request.state, "view_user", None),
        "csrf_token": csrf_token_for(seed),
        "flash": pop_flash(request),
        "now_utc": utcnow(),
        "nav_pending": getattr(request.state, "nav_pending", 0),
        "nav_unread": getattr(request.state, "nav_unread", 0),
    }
    ctx.update(context or {})
    resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if is_new:
        set_csrf_cookie(resp, seed)
    if request.cookies.get(FLASH_COOKIE):
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def redirect(url: str, message: str | None = None, level: str = "success") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if message:
        set_flash(resp, message, level)
    return resp


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths (prevents open redirects)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url and "\n" not in url and "\r" not in url:
        return url
    return default


__all__ = ["Markup", "render", "redirect", "templates", "safe_next"]
