"""Audit trail + request context. Audit rows are INSERT-only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, User


@dataclass(slots=True)
class Ctx:
    """Who is doing what, from where. Built once per request."""

    user: User | None = None
    ip: str | None = None
    request_id: str | None = None
    user_agent: str | None = None


def audit(
    db: Session,
    ctx: Ctx,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
    actor_email: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=ctx.user.id if ctx.user else None,
            actor_email=(ctx.user.email if ctx.user else actor_email),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ctx.ip,
            request_id=ctx.request_id,
            details=_scrub(details) if details else None,
        )
    )


_SENSITIVE = ("password", "token", "secret", "hash")


def _scrub(d: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if any(s in k.lower() for s in _SENSITIVE) else v) for k, v in d.items()}
