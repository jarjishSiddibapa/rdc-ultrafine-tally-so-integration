"""Notifications, email queue/delivery, forgot-password, and the pages around them."""
import re
from datetime import timedelta

from sqlalchemy import select

from app.integrations.tally import TallyError, TallyResult
from app.models import (
    AuditLog,
    EmailStatus,
    Notification,
    PasswordReset,
    QueuedEmail,
    SalesOrder,
    UserSession,
)
from app.models.base import utcnow
from app.services import emailing, notify
from app.services import outbox as outbox_service
from tests.conftest import PASSWORD, extract_id, login, order_form, post

NEW_PASSWORD = "Brand-New-Pass-77"


class Sink:
    """Stands in for the mail server."""

    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    def __call__(self, msg):
        if self.fail:
            raise OSError("mail server unreachable")
        self.sent.append(msg)


def _submit(client, d, **over):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(d, **over))
    assert r.status_code == 303, r.text[:300]
    return extract_id(r.headers["location"])


def _notes(db, user, kind=None):
    db.expire_all()
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.execute(stmt).scalars())


def _mails(db, to=None, kind=None):
    db.expire_all()
    stmt = select(QueuedEmail).order_by(QueuedEmail.id)
    if to:
        stmt = stmt.where(QueuedEmail.to_email == to)
    if kind:
        stmt = stmt.where(QueuedEmail.kind == kind)
    return list(db.execute(stmt).scalars())


# ----------------------------------------------------------------------------- order events
def test_submit_notifies_approvers_not_the_submitter(client, data, db):
    oid = _submit(client, data)
    for who in ("accounts", "admin"):
        n = _notes(db, data[who], "order_pending")
        assert len(n) == 1 and n[0].url == f"/orders/{oid}" and n[0].read_at is None and n[0].email_id
    assert not _notes(db, data["sales"]) and not _notes(db, data["sales2"])
    mail = _mails(db, "accounts@example.com", "order_pending")[0]
    assert "Approval needed" in mail.subject and "Alpha Traders" in mail.text_body and mail.status is EmailStatus.PENDING


def test_decisions_reach_the_creator(client, data, db):
    ok = _submit(client, data)
    bad = extract_id(post(client, "/orders/new", order_form(data, customer_po_number="PO-2")).headers["location"])
    client.cookies.clear()
    login(client, "accounts@example.com")
    post(client, f"/orders/{ok}/approve")
    post(client, f"/orders/{bad}/reject", {"remarks": "Rate is wrong on line 1"})
    approved = _notes(db, data["sales"], "order_approved")
    sent_back = _notes(db, data["sales"], "order_rejected")
    assert len(approved) == 1 and approved[0].level == "success"
    assert len(sent_back) == 1 and "Rate is wrong" in sent_back[0].body and sent_back[0].level == "danger"
    assert not _notes(db, data["accounts"], "order_approved")        # you are not told about your own decision


def test_withdraw_and_tally_delivery_and_failure(client, data, db):
    oid = _submit(client, data)
    post(client, f"/orders/{oid}/withdraw")
    assert len(_notes(db, data["accounts"], "order_withdrawn")) == 1
    post(client, f"/orders/{oid}/submit")
    client.cookies.clear()
    login(client, "accounts@example.com")
    post(client, f"/orders/{oid}/approve")

    class Ok:
        configured = True
        def send(self, payload, key): return TallyResult(reference="V-42")

    outbox_service.process_batch(db, Ok())
    done = _notes(db, data["sales"], "tally_delivered")
    assert len(done) == 1 and "V-42" in done[0].body
    assert len(_notes(db, data["accounts"], "tally_delivered")) == 1                 # the approver hears too
    # emailing for this category is off by default, so no email, but the in-app one exists
    assert not _mails(db, "sales@example.com", "tally_delivered")


def test_tally_giving_up_alerts_admins_by_email(client, data, db):
    oid = _submit(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    post(client, f"/orders/{oid}/approve")

    class Down:
        configured = True
        def send(self, payload, key): raise TallyError("Tally is offline")

    row = db.execute(select(SalesOrder).where(SalesOrder.id == oid)).scalar_one().outbox
    for _ in range(outbox_service.MAX_ATTEMPTS):
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
        outbox_service.process_batch(db, Down())
        db.expire_all()
        row = db.get(type(row), row.id)
    n = _notes(db, data["admin"], "tally_failed")
    assert len(n) == 1 and "Tally is offline" in n[0].body and n[0].level == "danger"
    assert _mails(db, "admin@example.com", "tally_failed")
    assert not _notes(db, data["sales"], "tally_failed")                              # only admins see problems


# ----------------------------------------------------------------------------- preferences
def test_preferences_control_app_and_email_but_not_security(client, data, db):
    login(client, "sales@example.com")
    r = client.get("/account/notifications")
    assert r.status_code == 200 and "Account and security" in r.text and "Daily reminder" not in r.text   # role-aware
    # sales turns off the email for order decisions and keeps the in-app one
    assert post(client, "/account/notifications", {"app_orders": "on"}).status_code == 303
    oid = extract_id(post(client, "/orders/new", order_form(data)).headers["location"])
    client.cookies.clear()
    login(client, "accounts@example.com")
    post(client, f"/orders/{oid}/approve")
    assert len(_notes(db, data["sales"], "order_approved")) == 1
    assert not _mails(db, "sales@example.com", "order_approved")
    # switching everything off still lets a security notice through
    client.cookies.clear()
    login(client, "sales@example.com")
    post(client, "/account/notifications", {})
    client.cookies.clear()
    for _ in range(5):
        login(client, "sales@example.com", "wrong-password")
    assert len(_notes(db, data["sales"], "account_locked")) == 1
    assert _mails(db, "sales@example.com", "account_locked")
    assert db.execute(select(AuditLog).where(AuditLog.action == "notification_prefs_changed")).scalars().first()


def test_password_change_is_announced(client, data, db):
    login(client, "sales@example.com")
    r = post(client, "/account/password", {"current": PASSWORD, "new_password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert r.status_code == 303
    assert len(_notes(db, data["sales"], "password_changed")) == 1
    assert _mails(db, "sales@example.com", "password_changed")


# ----------------------------------------------------------------------------- inbox
def test_inbox_read_unread_open_delete_and_isolation(client, data, db):
    _submit(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    nid = _notes(db, data["accounts"])[0].id
    page = client.get("/notifications")
    assert page.status_code == 200 and "is waiting for approval" in page.text
    summary = client.get("/notifications/summary").json()
    assert summary["unread"] == 1 and summary["items"][0]["href"] == f"/notifications/{nid}/open" and not summary["items"][0]["read"]
    assert "(unread)" in page.text
    # opening marks it read and lands on the order
    r = client.get(f"/notifications/{nid}/open", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/orders/")
    assert client.get("/notifications/summary").json()["unread"] == 0
    # unread again, filter works
    post(client, f"/notifications/{nid}/unread")
    assert "is waiting for approval" in client.get("/notifications?f_status=unread").text
    assert "is waiting for approval" not in client.get("/notifications?f_status=read").text
    # someone else cannot touch it
    client.cookies.clear()
    login(client, "sales2@example.com")
    assert client.get(f"/notifications/{nid}/open").status_code == 404
    assert post(client, f"/notifications/{nid}/delete").status_code == 404
    # the owner can remove it, and it disappears
    client.cookies.clear()
    login(client, "accounts@example.com")
    assert post(client, f"/notifications/{nid}/delete").status_code == 303
    assert "is waiting for approval" not in client.get("/notifications").text


def test_read_all_and_open_redirect_is_safe(client, data, db):
    _submit(client, data)
    _submit(client, data, customer_po_number="PO-9")
    client.cookies.clear()
    login(client, "admin@example.com")
    assert client.get("/notifications/summary").json()["unread"] == 2
    r = post(client, "/notifications/read-all", {"next": "https://evil.example/x"})
    assert r.status_code == 303 and r.headers["location"] == "/notifications"
    assert client.get("/notifications/summary").json()["unread"] == 0
    # a notification pointing off-site is never followed
    n = _notes(db, data["admin"])[0]
    n.url = "//evil.example/phish"
    db.commit()
    assert client.get(f"/notifications/{n.id}/open", follow_redirects=False).headers["location"] == "/notifications"


def test_summary_requires_login_and_does_not_extend_the_session(client, data, db):
    assert client.get("/notifications/summary").status_code == 401
    login(client, "sales@example.com")
    sess = db.execute(select(UserSession)).scalars().one()
    old = utcnow() - timedelta(minutes=5)
    sess.last_seen_at = old
    db.commit()
    assert client.get("/notifications/summary").status_code == 200
    db.expire_all()
    assert db.execute(select(UserSession)).scalars().one().last_seen_at == old       # polling is not activity
    client.get("/notifications")
    db.expire_all()
    assert db.execute(select(UserSession)).scalars().one().last_seen_at > old         # real page views are


def test_bell_is_on_every_page_with_the_count(client, data, db):
    _submit(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    html = client.get("/").text
    assert "data-bell" in html and 'data-bell-count' in html and "1 unread" in html


def test_inbox_download_and_sorting(client, data):
    _submit(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    for c in ("created_at", "title", "kind", "status"):
        assert client.get(f"/notifications?sort={c}&dir=asc").status_code == 200
    r = client.get("/notifications?export=csv")
    assert r.status_code == 200 and "is waiting for approval" in r.text


# ----------------------------------------------------------------------------- email queue + delivery
def test_delivery_marks_times_and_wipes_secret_bodies(client, data, db):
    row = emailing.queue_email(db, to_email="a@example.com", to_name="A", subject="Hello", html="<p>x</p>", text="x", kind="test_email")
    secret = emailing.queue_email(db, to_email="b@example.com", to_name="B", subject="Reset", html="<p>t</p>", text="token=abc", kind="password_reset", sensitive=True)
    db.commit()
    sink = Sink()
    assert emailing.deliver_pending(db, sink) == 2
    db.expire_all()
    a, b = db.get(QueuedEmail, row.id), db.get(QueuedEmail, secret.id)
    assert a.status is EmailStatus.SENT and a.sent_at and a.first_attempt_at and a.last_attempt_at and a.attempts == 1 and a.message_id
    assert a.text_body == "x"                                                 # ordinary mail is kept for the log
    assert b.status is EmailStatus.SENT and b.text_body is None and b.html_body is None   # the secret link is not
    m = sink.sent[0]
    assert m["Auto-Submitted"] == "auto-generated" and "no-reply@rdc.in" in m["From"] and m["To"] == "A <a@example.com>"
    assert m.get_body(("plain",)).get_content().strip() == "x" and "<p>x</p>" in m.get_body(("html",)).get_content()
    assert emailing.deliver_pending(db, sink) == 0                            # nothing is sent twice


def test_failures_back_off_then_give_up_and_can_be_retried(client, data, db):
    row = emailing.queue_email(db, to_email="a@example.com", to_name=None, subject="S", html="<p>x</p>", text="x", kind="test_email")
    db.commit()
    rid = row.id
    down = Sink(fail=True)
    for n in range(1, emailing.MAX_ATTEMPTS + 1):
        assert emailing.deliver_pending(db, down) == 1
        db.expire_all()
        r = db.get(QueuedEmail, rid)
        assert r.attempts == n and "unreachable" in r.last_error
        if n < emailing.MAX_ATTEMPTS:
            assert r.status is EmailStatus.FAILED and r.next_attempt_at > utcnow()
            assert emailing.deliver_pending(db, down) == 0                    # backing off: not due yet
            r.next_attempt_at = utcnow() - timedelta(seconds=1)
            db.commit()
    assert r.status is EmailStatus.DEAD
    assert emailing.retry(r) and r.status is EmailStatus.PENDING and r.attempts == 0
    db.commit()
    ok = Sink()
    emailing.deliver_pending(db, ok)
    db.expire_all()
    assert db.get(QueuedEmail, rid).status is EmailStatus.SENT and len(ok.sent) == 1


def test_stale_reset_mail_is_not_sent(db):
    row = emailing.queue_email(db, to_email="a@example.com", to_name=None, subject="S", html="h", text="t", kind="password_reset", sensitive=True)
    row.created_at = utcnow() - timedelta(hours=2)
    db.commit()
    sink = Sink()
    emailing.deliver_pending(db, sink)
    db.expire_all()
    r = db.get(QueuedEmail, row.id)
    assert not sink.sent and r.status is EmailStatus.DEAD and r.text_body is None and not emailing.retry(r)


def test_nothing_reaches_a_real_server_under_test(db):
    emailing.queue_email(db, to_email="a@example.com", to_name=None, subject="S", html="h", text="t", kind="test_email")
    db.commit()
    assert emailing.deliver_pending(db) == 0         # no transport given: the guard refuses to touch SMTP


def test_composed_mail_escapes_everything_and_blocks_header_injection(db):
    html, text = emailing.compose("Hi <script>alert(1)</script>", ["<b>bold</b>"], details=[("K", "<img src=x>")], button=("Go", "https://x.example/?a=1&b=2"))
    assert "<script>" not in html and "&lt;script&gt;" in html and "<img" not in html and "&amp;b=2" in html
    assert emailing.one_line("Evil\r\nBcc: x@evil.example") == "Evil Bcc: x@evil.example"
    row = emailing.queue_email(db, to_email="a@example.com", to_name="Name\r\nBcc: x@evil.example", subject="Sub\r\nBcc: x@evil.example", html=html, text=text, kind="x")
    msg = emailing.build_message(row)
    assert msg["Bcc"] is None and "\n" not in msg["Subject"]
    for bad in ("a@example.com\r\nBcc: x@evil.example", "no-at-sign", "a b@example.com", "a@b"):
        try:
            emailing.queue_email(db, to_email=bad, to_name=None, subject="S", html="h", text="t", kind="x")
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_links_use_the_configured_address_not_the_request_host(client, data, db):
    client.get("/login")
    client.post("/forgot-password", data={"email": "sales@example.com", "csrf_token": __import__("tests.conftest", fromlist=["csrf"]).csrf(client)},
                headers={"host": "testserver", "x-forwarded-host": "evil.example"}, follow_redirects=False)
    body = _mails(db, "sales@example.com", "password_reset")[0].text_body
    assert "http://localhost:1401/reset-password?token=" in body and "evil.example" not in body


# ----------------------------------------------------------------------------- forgot / reset
def _ask(client, email):
    client.get("/forgot-password")
    return post(client, "/forgot-password", {"email": email})


def _token(db, email="sales@example.com"):
    m = _mails(db, email, "password_reset")[-1]
    return re.search(r"token=([\w-]+)", m.text_body).group(1)


def test_forgot_page_is_public_and_linked_from_login(client):
    assert "/forgot-password" in client.get("/login").text
    assert client.get("/forgot-password").status_code == 200


def test_unknown_and_known_emails_get_the_same_answer(client, data, db):
    a = _ask(client, "nobody@example.com")
    b = _ask(client, "sales@example.com")
    assert a.status_code == b.status_code == 200
    assert re.sub(r"csrf_token\"[^>]*", "", a.text) == re.sub(r"csrf_token\"[^>]*", "", b.text)
    assert not _mails(db, "nobody@example.com")
    mail = _mails(db, "sales@example.com", "password_reset")
    assert len(mail) == 1 and mail[0].sensitive and "expires in 30 minutes" in mail[0].text_body
    acts = [a.action for a in db.execute(select(AuditLog)).scalars()]
    assert acts.count("password_reset_requested") == 2        # both attempts are on record


def test_disabled_users_get_no_link(client, data, db):
    data["sales"].is_active = False
    db.commit()
    _ask(client, "sales@example.com")
    assert not _mails(db, "sales@example.com")


def test_reset_flow_end_to_end(client, data, db):
    login(client, "sales@example.com")                          # an existing session must die with the reset
    client.cookies.clear()
    _ask(client, "sales@example.com")
    token = _token(db)
    assert client.get(f"/reset-password?token={token}").status_code == 200
    assert client.get(f"/reset-password?token={token}").status_code == 200       # looking never uses the link up
    weak = post(client, "/reset-password", {"token": token, "new_password": "short", "confirm": "short"})
    assert weak.status_code == 400 and "at least 10" in weak.text.lower()
    mismatch = post(client, "/reset-password", {"token": token, "new_password": NEW_PASSWORD, "confirm": NEW_PASSWORD + "x"})
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    ok = post(client, "/reset-password", {"token": token, "new_password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert ok.status_code == 303 and ok.headers["location"] == "/login"
    # new password works, old one does not, sessions are gone, the link is spent
    client.cookies.clear()
    assert login(client, "sales@example.com").status_code == 401
    assert login(client, "sales@example.com", NEW_PASSWORD).status_code == 303
    again = post(client, "/reset-password", {"token": token, "new_password": NEW_PASSWORD + "9", "confirm": NEW_PASSWORD + "9"})
    assert again.status_code == 400 and "no longer works" in again.text
    assert len(_notes(db, data["sales"], "password_changed")) == 1
    acts = [a.action for a in db.execute(select(AuditLog)).scalars()]
    assert "password_reset_completed" in acts and "password_reset_failed" in acts


def test_reset_clears_a_lockout_and_signs_out_other_devices(client, data, db):
    login(client, "sales@example.com")
    old_cookie = client.cookies.get("sid")
    client.cookies.clear()
    _ask(client, "sales@example.com")
    post(client, "/reset-password", {"token": _token(db), "new_password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    client.cookies.set("sid", old_cookie)
    assert client.get("/", follow_redirects=False).status_code == 303          # the old session no longer works


def test_expired_revoked_and_garbage_links(client, data, db):
    _ask(client, "sales@example.com")
    t1 = _token(db)
    row = db.execute(select(PasswordReset)).scalars().one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.get(f"/reset-password?token={t1}").status_code == 400
    # a newer request cancels the older link (after the 60 second cool-down)
    row.expires_at = utcnow() + timedelta(minutes=30)
    row.created_at = utcnow() - timedelta(minutes=5)
    db.commit()
    _ask(client, "sales@example.com")
    t2 = _token(db)
    assert t1 != t2
    assert client.get(f"/reset-password?token={t1}").status_code == 400
    assert client.get(f"/reset-password?token={t2}").status_code == 200
    for junk in ("", "x" * 500, "../../etc/passwd", "' OR 1=1 --"):
        assert client.get("/reset-password", params={"token": junk}).status_code == 400
        assert post(client, "/reset-password", {"token": junk, "new_password": NEW_PASSWORD, "confirm": NEW_PASSWORD}).status_code in (400, 422)


def test_requests_are_rate_limited(client, data, db):
    for _ in range(5):
        _ask(client, "sales@example.com")
    assert len(_mails(db, "sales@example.com", "password_reset")) == 1          # cool-down: one link per minute
    for i in range(12):
        r = _ask(client, f"user{i}@example.com")
    assert r.status_code == 429


def test_the_token_is_stored_only_as_a_hash(client, data, db):
    _ask(client, "sales@example.com")
    token = _token(db)
    row = db.execute(select(PasswordReset)).scalars().one()
    assert token not in row.token_hash and len(row.token_hash) == 64
    assert all(token not in (str(a.details or "")) for a in db.execute(select(AuditLog)).scalars())


# ----------------------------------------------------------------------------- admin
def test_admin_mail_log_test_email_retry_and_access(client, data, db):
    login(client, "sales@example.com")
    assert client.get("/admin/mail").status_code == 403
    client.cookies.clear()
    login(client, "admin@example.com")
    assert client.get("/admin/mail").status_code == 200
    r = post(client, "/admin/mail/test")
    assert r.status_code == 303
    row = _mails(db, kind="test_email")[0]
    assert row.to_email == "admin@example.com" and row.requested_by_id == data["admin"].id    # only ever to the admin's own address
    page = client.get("/admin/mail")
    assert "Test email" in page.text and "admin@example.com" in page.text
    for c in ("created_at", "to_email", "subject", "kind", "status", "attempts", "sent_at", "took"):
        assert client.get(f"/admin/mail?sort={c}").status_code == 200
    assert client.get("/admin/mail?export=csv").status_code == 200
    assert client.get("/admin/mail?f_status=sent").status_code == 200
    row.status = EmailStatus.DEAD
    db.commit()
    assert "Retry now" in client.get("/admin/mail").text
    assert post(client, f"/admin/mail/{row.id}/retry").status_code == 303
    db.expire_all()
    assert db.get(QueuedEmail, row.id).status is EmailStatus.PENDING
    assert post(client, "/admin/mail/999999/retry").status_code == 404


def test_admin_can_email_a_reset_link_and_events_notify_the_user(client, data, db):
    login(client, "admin@example.com")
    uid = data["sales"].id
    r = post(client, f"/admin/users/{uid}/send-reset-link")
    assert r.status_code == 303 and len(_mails(db, "sales@example.com", "password_reset")) == 1
    # a second click within the minute is refused politely
    post(client, f"/admin/users/{uid}/send-reset-link")
    assert len(_mails(db, "sales@example.com", "password_reset")) == 1
    # temp-password reset tells the user, without ever mailing a password
    post(client, f"/admin/users/{uid}/reset-password")
    n = _notes(db, data["sales"], "password_reset_by_admin")
    assert len(n) == 1
    m = _mails(db, "sales@example.com", "password_reset_by_admin")[0]
    assert "temporary password" in m.text_body and not re.search(r"[A-Za-z0-9]{14}", m.text_body.split("\n")[-3])
    # changing a role is announced
    post(client, f"/admin/users/{uid}", {"full_name": "Sam Sales", "role": "accounts", "is_active": "on"})
    assert any("Accounts in-charge" in x.body for x in _notes(db, data["sales"], "account_changed"))
    # creating a user sends a welcome notice
    post(client, "/admin/users/new", {"email": "new.person@example.com", "full_name": "New Person", "role": "accounts"})
    assert _mails(db, "new.person@example.com", "account_created")


# ----------------------------------------------------------------------------- reminders
def test_pending_reminders_are_sent_once_a_day(client, data, db):
    oid = _submit(client, data)
    assert notify.send_pending_reminders(db) == 0                                  # too fresh
    o = db.get(SalesOrder, oid)
    o.submitted_at = utcnow() - timedelta(hours=30)
    db.commit()
    assert notify.send_pending_reminders(db) == 2                                  # accounts + admin
    db.commit()
    assert notify.send_pending_reminders(db) == 0                                  # same day: no second reminder
    n = _notes(db, data["accounts"], "pending_reminder")
    assert len(n) == 1 and "waiting over 24 hours" in n[0].title
    assert _mails(db, "admin@example.com", "pending_reminder")


def test_old_read_notifications_are_purged_but_unread_are_kept(client, data, db):
    _submit(client, data)
    rows = _notes(db, data["accounts"])
    rows[0].created_at = utcnow() - timedelta(days=100)
    db.commit()
    assert notify.purge_old(db) == 0                                               # old but unread: kept
    rows[0].read_at = utcnow() - timedelta(days=99)
    db.commit()
    assert notify.purge_old(db) == 1

