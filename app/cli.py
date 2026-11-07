"""Operational commands:  python -m app.cli <command>

  seed-admin   create the first administrator from ADMIN_EMAIL / ADMIN_PASSWORD (.env). Idempotent.
  mail-check   sign in to the mail server with the .env settings, without sending anything
  serve        run the server on HOST:PORT (default 127.0.0.1:1401)
"""
from __future__ import annotations

import argparse
import sys

from app.core.config import get_settings
from app.core.database import session_factory
from app.models import Role, User
from app.services import auth as auth_service
from app.services.audit import Ctx, audit


def seed_admin() -> int:
    s = get_settings()
    if not s.admin_email or not s.admin_password:
        print("ADMIN_EMAIL and ADMIN_PASSWORD must be set in .env", file=sys.stderr)
        return 1
    db = session_factory()()
    try:
        email = auth_service.normalize_email(s.admin_email)
        if auth_service.get_user_by_email(db, email, include_deleted=True):
            print(f"Admin {email} already exists - nothing to do.")
            return 0
        user = User(email=email, full_name=s.admin_name, role=Role.ADMIN, password_hash="", is_active=True)
        auth_service.set_password(db, user, s.admin_password, must_change=False)  # operator-chosen credentials
        db.add(user)
        db.flush()
        audit(db, Ctx(), "admin_seeded", entity_type="user", entity_id=user.id, actor_email="cli")
        db.commit()
        print(f"Created administrator {email}. Change the password after first sign-in (Account > Password).")
        return 0
    finally:
        db.close()


def mail_check() -> int:
    from app.services import emailing

    ok, message = emailing.check_connection()
    print(("OK: " if ok else "FAILED: ") + message)
    return 0 if ok else 1


def serve() -> int:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, server_header=False, date_header=True,
                proxy_headers=True, access_log=False, log_level="info")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    p.add_argument("command", choices=["seed-admin", "mail-check", "serve"])
    args = p.parse_args()
    return {"seed-admin": seed_admin, "mail-check": mail_check, "serve": serve}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
