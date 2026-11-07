"""Outbox delivery worker: claims due rows with SKIP LOCKED, retries with exponential backoff."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import session_factory
from app.integrations.tally import TallyError, TallyGateway, get_gateway
from app.models import OutboxStatus, TallyOutbox, TallyStatus
from app.models.base import utcnow
from app.services import notify
from app.services.audit import Ctx, audit

log = logging.getLogger("app.outbox")
MAX_ATTEMPTS = 8
STALE_LOCK = timedelta(minutes=10)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** (attempts - 1), 3600))


def process_batch(db: Session, gateway: TallyGateway, limit: int = 10) -> int:
    """Deliver up to `limit` due rows. Returns number processed. No-op if Tally isn't configured."""
    if not gateway.configured:
        return 0
    now = utcnow()
    rows = db.execute(
        select(TallyOutbox)
        .where(
            TallyOutbox.status.in_([OutboxStatus.PENDING, OutboxStatus.FAILED, OutboxStatus.IN_PROGRESS]),
            TallyOutbox.next_attempt_at <= now,
        )
        .order_by(TallyOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=TallyOutbox)
    ).scalars().all()
    # Rows left IN_PROGRESS by a crashed worker become eligible again after STALE_LOCK.
    rows = [r for r in rows if r.status is not OutboxStatus.IN_PROGRESS or (r.locked_at and now - r.locked_at > STALE_LOCK)]
    for row in rows:
        row.status = OutboxStatus.IN_PROGRESS
        row.locked_at = now
    db.commit()

    done = 0
    for row_id in [r.id for r in rows]:
        row = db.get(TallyOutbox, row_id)
        order = row.order
        row.attempts += 1
        row.last_attempt_at = utcnow()
        if row.first_attempt_at is None:
            row.first_attempt_at = row.last_attempt_at
        try:
            result = gateway.send(row.payload, row.idempotency_key)
        except TallyError as exc:
            row.last_error = str(exc)[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = OutboxStatus.DEAD
                order.tally_status = TallyStatus.FAILED
            else:
                row.status = OutboxStatus.FAILED
                row.next_attempt_at = utcnow() + _backoff(row.attempts)
            audit(db, Ctx(), "tally_push_failed", entity_type="sales_order", entity_id=order.id,
                  details={"attempt": row.attempts, "error": row.last_error})
            if row.status is OutboxStatus.DEAD:
                notify.tally_failed(db, order, row.attempts, row.last_error or "", str(int(row.last_attempt_at.timestamp())))
        except Exception as exc:  # never let one bad row kill the worker
            log.exception("unexpected outbox error for row %s", row_id)
            row.status = OutboxStatus.FAILED
            row.last_error = f"Unexpected error: {type(exc).__name__}"
            row.next_attempt_at = utcnow() + _backoff(row.attempts)
        else:
            row.status = OutboxStatus.SENT
            row.sent_at = utcnow()
            row.last_error = None
            row.response_reference = result.reference
            order.tally_status = TallyStatus.SYNCED
            order.tally_reference = result.reference
            audit(db, Ctx(), "tally_push_ok", entity_type="sales_order", entity_id=order.id,
                  details={"reference": result.reference})
            notify.tally_delivered(db, order, result.reference)
        row.locked_at = None
        db.commit()
        done += 1
    return done


def retry_now(db: Session, row: TallyOutbox, ctx: Ctx) -> None:
    """Admin action: put a failed / dead row back in the queue."""
    row.status = OutboxStatus.PENDING
    row.attempts = 0
    row.next_attempt_at = utcnow()
    row.last_error = None
    row.order.tally_status = TallyStatus.QUEUED
    audit(db, ctx, "tally_retry", entity_type="sales_order", entity_id=row.order_id)


async def worker_loop(stop: asyncio.Event, interval: float = 15.0) -> None:
    log.info("outbox worker started")
    while not stop.is_set():
        try:
            await asyncio.to_thread(_tick)
        except Exception:
            log.exception("outbox tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("outbox worker stopped")


def _tick() -> None:
    db = session_factory()()
    try:
        process_batch(db, get_gateway())
    finally:
        db.close()
