"""Notification inbox, the bell's live summary, and each person's notification settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.utils import to_ist
from app.deps import current_user, get_ctx, passive_user
from app.models import Notification, User
from app.models.base import utcnow
from app.routers._lists import run_list
from app.services import notify
from app.services.audit import Ctx, audit
from app.services.specs import notification_spec
from app.web import redirect, render, safe_next

router = APIRouter()


def _mine(db: Session, user: User, nid: int) -> Notification:
    n = db.execute(select(Notification).where(Notification.id == nid, Notification.user_id == user.id, Notification.deleted_at.is_(None))).scalar_one_or_none()
    if n is None:
        raise HTTPException(404, "That notification was not found.")
    return n


@router.get("/notifications")
def inbox(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    base = select(Notification).where(Notification.user_id == user.id, Notification.deleted_at.is_(None))
    st, page, download = run_list(request, db, user, ctx, base, notification_spec(), "My notifications")
    if download:
        return download
    return render(request, "notifications/inbox.html", {"st": st, "page": page, "unread": notify.unread_count(db, user.id)})


# Declared before the /{id} routes so "summary" and "read-all" are never mistaken for an id.
@router.get("/notifications/summary")
def summary(db: Session = Depends(get_db), user: User | None = Depends(passive_user)):
    """Small JSON for the bell. Does not extend the session's idle timer."""
    if user is None:
        return JSONResponse({"error": "signed_out"}, status_code=401)
    rows = db.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.deleted_at.is_(None))
        .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(8)
    ).scalars().all()
    return JSONResponse({
        "unread": notify.unread_count(db, user.id),
        "items": [{
            "id": n.id, "title": n.title, "body": n.body, "level": n.level, "read": n.read_at is not None,
            "href": f"/notifications/{n.id}/open", "when": to_ist(n.created_at).strftime("%d %b, %I:%M %p"),
        } for n in rows],
    })


@router.post("/notifications/read-all")
def read_all(next: str = Form("/notifications"), db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    n = notify.mark_all_read(db, user.id)
    audit(db, ctx, "notifications_read_all", entity_type="user", entity_id=user.id, details={"count": n})
    db.commit()
    return redirect(safe_next(next, "/notifications"), f"Marked {n} notification{'s' if n != 1 else ''} as read." if n else "Nothing to mark: you are all caught up.")


@router.get("/notifications/{nid}/open")
def open_one(nid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _mine(db, user, nid)
    if n.read_at is None:
        n.read_at = utcnow()
        db.commit()
    return redirect(safe_next(n.url, "/notifications"))


@router.post("/notifications/{nid}/read")
def mark_read(nid: int, next: str = Form("/notifications"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _mine(db, user, nid)
    if n.read_at is None:
        n.read_at = utcnow()
        db.commit()
    return redirect(safe_next(next, "/notifications"))


@router.post("/notifications/{nid}/unread")
def mark_unread(nid: int, next: str = Form("/notifications"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _mine(db, user, nid)
    n.read_at = None
    db.commit()
    return redirect(safe_next(next, "/notifications"))


@router.post("/notifications/{nid}/delete")
def delete_one(nid: int, next: str = Form("/notifications"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    n = _mine(db, user, nid)
    n.deleted_at = utcnow()
    db.commit()
    return redirect(safe_next(next, "/notifications"), "Notification removed.")


# --------------------------------------------------------------------------- settings
def _rows(db: Session, user: User):
    prefs = notify.prefs_for(db, [user.id])[user.id]
    return [{"key": k, "label": c["label"], "help": c["help"], "locked": bool(c.get("locked")),
             "in_app": prefs[k][0] or bool(c.get("locked")), "email": prefs[k][1] or bool(c.get("locked"))}
            for k, c in notify.categories_for(user.role)]


@router.get("/account/notifications")
def prefs_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "notifications/settings.html", {"rows": _rows(db, user)})


@router.post("/account/notifications")
async def prefs_save(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    before = notify.prefs_for(db, [user.id])[user.id]
    chosen = {k: (form.get(f"app_{k}") == "on", form.get(f"mail_{k}") == "on") for k, _ in notify.categories_for(user.role)}
    after = notify.save_prefs(db, user, chosen)
    audit(db, ctx, "notification_prefs_changed", entity_type="user", entity_id=user.id,
          details={"before": {k: list(v) for k, v in before.items()}, "after": {k: list(v) for k, v in after.items()}})
    db.commit()
    return redirect("/account/notifications", "Notification settings saved.")
