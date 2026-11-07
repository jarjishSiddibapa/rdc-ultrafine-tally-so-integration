"""Outgoing email: compose branded messages, queue them in the database, deliver with retries.

Why a queue: a request must never wait for (or fail because of) the mail server. Every email is a row
in ``email_queue`` written in the same transaction as the event that caused it, and a background worker
sends it. Each attempt is timed and kept, so "was it sent, and when?" always has an answer.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import EmailStatus, QueuedEmail
from app.models.base import utcnow

log = logging.getLogger("app.mail")

MAX_ATTEMPTS = 6
STALE_LOCK = timedelta(minutes=10)
SENSITIVE_MAX_AGE = timedelta(hours=1)   # a reset link that could not be sent within an hour is useless
SENSITIVE_MAX_AGE_BY_KIND = {"welcome": timedelta(hours=24)}   # a temporary password outlives a 30-minute link
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_ADDR = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")

Transport = Callable[[EmailMessage], None]


def one_line(text: str | None, limit: int = 200) -> str:
    """Strip control characters (so a name or customer can never inject a header) and cap the length."""
    return _CTRL.sub(" ", text or "").strip()[:limit]


def abs_url(path: str) -> str:
    """Absolute link for use inside an email. Built from APP_BASE_URL, never from the incoming request."""
    return get_settings().app_base_url + (path if path.startswith("/") else "/" + path)


# --------------------------------------------------------------------------- composing
_GREEN, _INK, _SOFT, _WELL = "#089604", "#16241a", "#55655a", "#f2f6f2"


def compose(heading: str, paragraphs: list[str], *, greeting: str | None = None,
            details: list[tuple[str, str]] | None = None, button: tuple[str, str] | None = None,
            footnote: str | None = None) -> tuple[str, str]:
    """Return (html, text). Every value is escaped; ``button`` is (label, absolute url)."""
    s = get_settings()
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:{_SOFT};font-size:14px;vertical-align:top;white-space:nowrap">{escape(k)}</td>'
        f'<td style="padding:6px 0;color:{_INK};font-size:14px;font-weight:600">{escape(v)}</td></tr>'
        for k, v in (details or [])
    )
    paras = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:{_INK}">{escape(p)}</p>' for p in paragraphs)
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0"><a href="{escape(button[1], quote=True)}" style="display:inline-block;background:{_GREEN};'
               f'color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;padding:12px 22px;border-radius:8px">{escape(button[0])}</a></p>'
               f'<p style="margin:0 0 14px;font-size:12px;color:{_SOFT};word-break:break-all">If the button does not work, copy this address into your browser:<br>{escape(button[1])}</p>')
    html = (
        '<!doctype html><html><body style="margin:0;padding:0;background:#eef2ee">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2ee;padding:24px 12px"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;font-family:Segoe UI,Arial,sans-serif">'
        f'<tr><td style="background:{_GREEN};padding:18px 28px;color:#ffffff;font-size:18px;font-weight:700;letter-spacing:-0.01em">{escape(s.app_name)}</td></tr>'
        '<tr><td style="padding:28px">'
        f'<h1 style="margin:0 0 16px;font-size:20px;line-height:1.3;color:{_INK}">{escape(heading)}</h1>'
        + (f'<p style="margin:0 0 14px;font-size:15px;color:{_INK}">{escape(greeting)}</p>' if greeting else "")
        + paras
        + (f'<table role="presentation" cellpadding="0" cellspacing="0" style="background:{_WELL};border-radius:8px;padding:8px 14px;margin:6px 0 10px;width:100%">{rows}</table>' if rows else "")
        + btn
        + (f'<p style="margin:14px 0 0;font-size:13px;line-height:1.5;color:{_SOFT}">{escape(footnote)}</p>' if footnote else "")
        + '</td></tr>'
        f'<tr><td style="padding:16px 28px;background:{_WELL};font-size:12px;line-height:1.5;color:{_SOFT}">'
        f'This is an automated message from {escape(s.mail_from or "the system")}. Please do not reply to it.<br>Made with ❤️ by Jarjish</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    lines = [heading, ""]
    if greeting:
        lines += [greeting, ""]
    for p in paragraphs:
        lines += [p, ""]
    for k, v in details or []:
        lines.append(f"{k}: {v}")
    if details:
        lines.append("")
    if button:
        lines += [f"{button[0]}: {button[1]}", ""]
    if footnote:
        lines += [footnote, ""]
    lines += ["--", f"This is an automated message from {s.mail_from or 'the system'}. Please do not reply to it."]
    return html, "\n".join(lines)


# --------------------------------------------------------------------------- queueing
def queue_email(db: Session, *, to_email: str, to_name: str | None, subject: str, html: str, text: str, kind: str,
                user_id: int | None = None, sensitive: bool = False, requested_by_id: int | None = None) -> QueuedEmail:
    """Add an email to the queue (flushed so it has an id). The caller commits with the rest of its work."""
    to_email = one_line(to_email, 254).lower()
    if not _ADDR.match(to_email):
        raise ValueError("invalid recipient address")
    row = QueuedEmail(
        to_email=to_email, to_name=one_line(to_name, 120) or None, subject=one_line(subject, 200), kind=kind[:40],
        user_id=user_id, html_body=html, text_body=text, sensitive=sensitive, status=EmailStatus.PENDING,
        requested_by_id=requested_by_id, next_attempt_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- delivery
def build_message(row: QueuedEmail) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = row.subject
    msg["From"] = formataddr((one_line(s.mail_from_name, 80), s.mail_from))
    msg["To"] = formataddr((row.to_name or "", row.to_email))
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=s.mail_from.split("@")[-1] or "localhost")
    msg["Auto-Submitted"] = "auto-generated"          # keeps auto-responders from replying
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(row.text_body or "")
    if row.html_body:
        msg.add_alternative(row.html_body, subtype="html")
    return msg


def _connect() -> smtplib.SMTP:
    s = get_settings()
    local = (s.mail_from.split("@")[-1] or "localhost")
    ctx = ssl.create_default_context()
    if s.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=ctx, local_hostname=local)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20, local_hostname=local)
        if s.smtp_security == "starttls":
            server.starttls(context=ctx)
    server.login(s.smtp_user, s.smtp_password.get_secret_value())
    return server


def smtp_send(msg: EmailMessage) -> None:
    server = _connect()
    try:
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - the message is already accepted or the error is raised above
            pass


def check_connection() -> tuple[bool, str]:
    """Sign in to the mail server without sending anything. Used by the CLI and the admin page."""
    s = get_settings()
    if not s.mail_configured:
        return False, "Email is not configured (SMTP_HOST, SMTP_USER, SMTP_PASSWORD and MAIL_FROM are needed)."
    try:
        server = _connect()
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"The mail server refused the sign-in ({exc.smtp_code}). Check SMTP_USER and the app password."
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"Could not reach the mail server: {type(exc).__name__}: {str(exc)[:160]}"
    try:
        server.quit()
    except Exception:  # noqa: BLE001
        pass
    return True, f"Signed in to {s.smtp_host}:{s.smtp_port} as {s.smtp_user}."


def deliver_pending(db: Session, transport: Transport | None = None, limit: int = 10) -> int:
    """Send up to ``limit`` due emails. Returns how many were attempted. No-op when email isn't configured."""
    s = get_settings()
    if transport is None:
        if s.app_env == "test" or not s.mail_configured:
            return 0                     # tests never reach a real mail server; unconfigured mail just waits in the queue
        transport = smtp_send
    now = utcnow()
    rows = db.execute(
        select(QueuedEmail)
        .where(QueuedEmail.status.in_([EmailStatus.PENDING, EmailStatus.FAILED, EmailStatus.SENDING]), QueuedEmail.next_attempt_at <= now)
        .order_by(QueuedEmail.id).limit(limit)
        .with_for_update(skip_locked=True, of=QueuedEmail)
    ).scalars().all()
    rows = [r for r in rows if r.status is not EmailStatus.SENDING or (r.locked_at and now - r.locked_at > STALE_LOCK)]
    for r in rows:
        r.status = EmailStatus.SENDING
        r.locked_at = now
    db.commit()

    done = 0
    for row_id in [r.id for r in rows]:
        row = db.get(QueuedEmail, row_id)
        row.locked_at = None
        if row.sensitive and utcnow() - row.created_at > SENSITIVE_MAX_AGE_BY_KIND.get(row.kind, SENSITIVE_MAX_AGE):
            _finish_dead(row, "Not sent: the secret in it expired before the mail server accepted it.")
            db.commit()
            continue
        row.attempts += 1
        row.last_attempt_at = utcnow()
        if row.first_attempt_at is None:
            row.first_attempt_at = row.last_attempt_at
        try:
            msg = build_message(row)
            transport(msg)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the others
            row.last_error = f"{type(exc).__name__}: {one_line(str(exc), 400)}"
            if row.attempts >= MAX_ATTEMPTS:
                _finish_dead(row, row.last_error)
            else:
                row.status = EmailStatus.FAILED
                row.next_attempt_at = utcnow() + timedelta(seconds=min(60 * 2 ** (row.attempts - 1), 3600))
            log.warning("email %s to %s failed (attempt %s): %s", row.id, row.to_email, row.attempts, row.last_error)
        else:
            row.status = EmailStatus.SENT
            row.sent_at = utcnow()
            row.last_error = None
            row.message_id = one_line(str(msg["Message-ID"]), 160)
            if row.sensitive:
                row.html_body = row.text_body = None     # the secret link is not kept once delivered
            log.info("email %s (%s) sent to %s", row.id, row.kind, row.to_email)
        db.commit()
        done += 1
    return done


def _finish_dead(row: QueuedEmail, error: str) -> None:
    row.status = EmailStatus.DEAD
    row.last_error = error[:500]
    if row.sensitive:
        row.html_body = row.text_body = None


def retry(row: QueuedEmail) -> bool:
    """Admin action: put a failed email back in the queue. Emails whose body was wiped cannot be resent."""
    if row.status in (EmailStatus.SENT, EmailStatus.SENDING) or not row.text_body:
        return False
    row.status = EmailStatus.PENDING
    row.attempts = 0
    row.next_attempt_at = utcnow()
    row.last_error = None
    return True


def purge_old(db: Session, days: int = 180) -> int:
    from app.models import Notification  # noqa: F401  (keeps FK order obvious)

    cutoff = utcnow() - timedelta(days=days)
    res = db.execute(QueuedEmail.__table__.delete().where(QueuedEmail.created_at < cutoff, QueuedEmail.status.in_([EmailStatus.SENT, EmailStatus.DEAD])))
    return res.rowcount or 0
