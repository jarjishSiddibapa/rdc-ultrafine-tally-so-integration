"""Plain-English wording for audit log entries, so the screen and the download read like sentences, not code."""
from __future__ import annotations

import json
from typing import Any

ACTIONS = {
    "login": "Signed in", "login_failed": "Failed sign-in", "login_blocked": "Sign-in blocked (account locked)", "logout": "Signed out",
    "so_created": "Order created", "so_updated": "Order edited", "so_submitted": "Order submitted", "so_withdrawn": "Order withdrawn",
    "so_rejected": "Order sent back", "so_approved": "Order approved", "so_deleted": "Order deleted",
    "tally_push_ok": "Delivered to Tally", "tally_push_failed": "Tally delivery failed", "tally_retry": "Tally retry requested",
    "user_created": "User created", "user_updated": "User updated", "user_deleted": "User removed", "user_restored": "User restored",
    "user_unlocked": "User unlocked", "user_password_reset": "Temporary password issued",
    "password_changed": "Password changed", "password_reset_requested": "Password reset link requested",
    "password_reset_completed": "Password reset completed", "welcome_email_queued": "Welcome email queued",
    "notifications_read_all": "Notifications marked read", "notification_prefs_changed": "Notification settings changed",
    "settings_updated": "System settings changed", "mail_test_sent": "Test email queued",
}
KEYS = {
    "so_number": "Order", "party": "Customer", "total": "Total (Rs)", "remarks": "Remarks", "email": "Email", "role": "Role",
    "plants": "Plants", "reason": "Reason", "attempt": "Attempt", "error": "Error", "reference": "Tally reference", "count": "Count",
    "to": "Sent to", "resend": "Resent", "by_admin": "Requested by admin", "emailed": "Emailed", "expires_minutes": "Expires in (min)",
    "locked": "Locked", "active": "Active", "format": "Format", "rows": "Rows",
}


def action_label(action: str) -> str:
    return ACTIONS.get(action) or action.replace("_", " ").capitalize()


def _show(v: Any) -> str:
    if v is None or v == "":
        return "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(_show(x) for x in v) or "none"
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))[:140]
    return str(v)


def describe(details: dict[str, Any] | None) -> str:
    """'Order SO/26-27/00002 · Customer Beta Industries · Total (Rs) 514500.00'"""
    if not details:
        return ""
    return " · ".join(f"{KEYS.get(k, k.replace('_', ' ').capitalize())} {_show(v)}" for k, v in details.items())
