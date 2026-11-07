"""End-to-end behaviour through the HTTP layer."""
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.integrations.tally import TallyError, TallyResult
from app.models import AuditLog, OutboxStatus, SalesOrder, SOStatus, TallyOutbox, TallyStatus, User
from app.models.base import utcnow
from app.services import outbox as outbox_service
from tests.conftest import PASSWORD, csrf, extract_id, login, order_form, post


def _create(client, d, **over):
    r = post(client, "/orders/new", order_form(d, **over))
    assert r.status_code == 303, r.text[:2000]
    return extract_id(r.headers["location"])


# ----------------------------------------------------------------------------- auth
def test_login_success_and_generic_failure(client, data):
    assert login(client, "sales@example.com").status_code == 303
    bad = login(client.__class__(client.app, base_url="http://testserver"), "sales@example.com", "nope")
    unknown = login(client.__class__(client.app, base_url="http://testserver"), "ghost@example.com", "nope")
    assert bad.status_code == unknown.status_code == 401
    assert "The email or password is incorrect." in bad.text and "The email or password is incorrect." in unknown.text


def test_account_locks_after_repeated_failures(client, data, db):
    for _ in range(get_settings().max_failed_logins):
        assert login(client, "sales@example.com", "wrong-pass").status_code == 401
    # Even the right password is refused while locked, with the same message.
    r = login(client, "sales@example.com")
    assert r.status_code == 401
    db.expire_all()
    assert db.execute(select(User).where(User.email == "sales@example.com")).scalar_one().locked_until is not None


def test_post_without_csrf_token_is_rejected(client, data):
    login(client, "sales@example.com")
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/logout", data={"csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403


def test_cross_origin_post_is_rejected(client, data):
    login(client, "sales@example.com")
    r = client.post("/logout", data={"csrf_token": csrf(client)}, headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_anonymous_is_sent_to_login(client, data):
    r = client.get("/orders", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_logout_revokes_session_server_side(client, data):
    login(client, "sales@example.com")
    stolen = client.cookies.get("sid")
    assert post(client, "/logout").status_code == 303
    client.cookies.set("sid", stolen)
    assert client.get("/orders", follow_redirects=False).status_code == 303  # old cookie is dead


def test_security_headers_present(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" in r.headers and "httponly" in r.headers["set-cookie"].lower()


def test_oversized_body_rejected(client):
    r = client.post("/login", content=b"x" * 2_000_000, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_footer_credit_is_rendered(client):
    assert "Made with ❤️ by Jarjish" in client.get("/login").text


# ----------------------------------------------------------------------------- RBAC
def test_role_boundaries(client, data):
    login(client, "sales@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/masters/items").status_code == 403
    assert client.get("/orders/new").status_code == 200


def test_accounts_cannot_create_orders(client, data):
    login(client, "accounts@example.com")
    assert client.get("/orders/new").status_code == 403
    assert post(client, "/orders/new", order_form(data)).status_code == 403


def test_sales_cannot_see_other_users_orders(client, data):
    login(client, "sales@example.com")
    oid = _create(client, data)
    other = client.__class__(client.app, base_url="http://testserver")
    login(other, "sales2@example.com")
    assert other.get(f"/orders/{oid}").status_code == 404
    assert "PO-1001" not in other.get("/orders").text


# ----------------------------------------------------------------------------- workflow
def test_full_approval_flow_queues_tally_payload(client, data, db):
    login(client, "sales@example.com")
    oid = _create(client, data)
    o = db.get(SalesOrder, oid)
    assert o.status is SOStatus.PENDING_APPROVAL and o.so_number.startswith("SO/") and o.so_number.endswith("/00001")
    assert (o.subtotal, o.cgst_amount, o.sgst_amount, o.igst_amount, o.total_amount) == tuple(
        Decimal(x) for x in ("1250000.00", "112500.00", "112500.00", "0.00", "1475000.00"))

    # Creator cannot approve their own order
    assert post(client, f"/orders/{oid}/approve").status_code == 403

    acc = client.__class__(client.app, base_url="http://testserver")
    login(acc, "accounts@example.com")
    assert "PO-1001" in acc.get("/orders").text  # default view = pending queue
    r = post(acc, f"/orders/{oid}/approve", {"remarks": "ok"})
    assert r.status_code == 303
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.status is SOStatus.APPROVED and o.tally_status is TallyStatus.QUEUED and o.reviewed_by_id == data["accounts"].id
    box = db.execute(select(TallyOutbox).where(TallyOutbox.order_id == oid)).scalar_one()
    p = box.payload
    assert p["voucher_type"] == "Sales Order" and p["order_no"] == "PO-1001" and p["party_ledger"] == "Alpha Traders"
    assert p["items"][0]["quantity"] == "100.000" and p["items"][0]["rate"] == "12500.00"
    assert [t["ledger"] for t in p["taxes"]] == ["Output CGST", "Output SGST"]
    assert p["sales_ledger"] == "Sales GST"
    assert p["narration"] == "We sold 100 MT of Ultrafine to Alpha Traders at Rs 12,500.00 per MT. OK"

    # Approved orders are frozen
    post(client, f"/orders/{oid}/edit", order_form(data))
    db.expire_all()
    assert db.get(SalesOrder, oid).status is SOStatus.APPROVED
    # Approving again is a no-op error, not a second outbox row
    post(acc, f"/orders/{oid}/approve")
    assert db.execute(select(TallyOutbox)).scalars().all().__len__() == 1


def test_interstate_order_uses_igst_and_trading_numbering(client, data, db):
    login(client, "sales@example.com")
    oid = _create(client, data, so_type="trading_sales_order", party_id=str(data["party_mh"].id),
                  billing_state_code="27", customer_po_number="PO-MH-1")
    o = db.get(SalesOrder, oid)
    assert o.tax_mode.value == "igst" and o.igst_amount == Decimal("225000.00") and o.cgst_amount == 0
    assert o.so_number.startswith("TSO/")


def test_reject_needs_reason_then_creator_can_fix_and_resubmit(client, data, db):
    login(client, "sales@example.com")
    oid = _create(client, data)
    acc = client.__class__(client.app, base_url="http://testserver")
    login(acc, "accounts@example.com")
    post(acc, f"/orders/{oid}/reject", {"remarks": ""})
    db.expire_all()
    assert db.get(SalesOrder, oid).status is SOStatus.PENDING_APPROVAL
    post(acc, f"/orders/{oid}/reject", {"remarks": "Rate differs from PO"})
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.status is SOStatus.REJECTED and o.review_remarks == "Rate differs from PO"
    assert db.execute(select(TallyOutbox)).first() is None

    r = post(client, f"/orders/{oid}/edit", order_form(data, rate=["12000"], action="submit"))
    assert r.status_code == 303
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.status is SOStatus.PENDING_APPROVAL and o.items[0].rate == Decimal("12000.00") and o.review_remarks is None
    assert o.so_number.endswith("/00001")  # number is kept across resubmits


def test_withdraw_and_soft_delete(client, data, db):
    login(client, "sales@example.com")
    oid = _create(client, data)
    post(client, f"/orders/{oid}/withdraw")
    db.expire_all()
    assert db.get(SalesOrder, oid).status is SOStatus.DRAFT
    post(client, f"/orders/{oid}/delete")
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.deleted_at is not None and o.deleted_by_id == data["sales"].id  # row kept
    assert client.get(f"/orders/{oid}").status_code == 404
    assert db.execute(select(AuditLog).where(AuditLog.action == "so_deleted")).scalar_one()


def test_duplicate_customer_po_is_blocked(client, data):
    login(client, "sales@example.com")
    _create(client, data)
    r = post(client, "/orders/new", order_form(data))
    assert r.status_code == 422 and "already has an order for this PO" in r.text


def test_validation_errors_are_reported_per_field(client, data):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, quantity=["-5"], billing_state_code="", remarks="  ", order_date="2999-01-01"))
    assert r.status_code == 422
    for needle in ("Quantity must be", "Choose the billing state", "Enter the remarks", "cannot be in the future"):
        assert needle in r.text


def test_nan_and_overflow_quantities_rejected(client, data):
    login(client, "sales@example.com")
    for q in ("NaN", "Infinity", "1e999", "99999999999"):
        assert post(client, "/orders/new", order_form(data, quantity=[q])).status_code == 422


def test_stored_xss_is_escaped(client, data):
    login(client, "sales@example.com")
    evil = "<script>alert(1)</script>"
    oid = _create(client, data, remarks=evil, payment_term=evil)
    html = client.get(f"/orders/{oid}").text
    assert evil not in html and "&lt;script&gt;" in html


def test_order_needs_a_plant(client, data, db):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, plant_id=""))
    assert r.status_code == 422 and "Select the plant" in r.text
    assert db.query(SalesOrder).count() == 0


def test_no_plants_configured_blocks_orders_and_warns(client, data, db):
    from app.models import Plant

    db.query(Plant).update({Plant.deleted_at: utcnow()})
    db.commit()
    login(client, "sales@example.com")
    assert "No plants have been set up" in client.get("/orders/new").text
    assert post(client, "/orders/new", order_form(data)).status_code == 422


def test_deleted_plant_is_refused(client, data, db):
    data["plant_gj"].soft_delete(None)
    db.commit()
    login(client, "sales@example.com")
    assert post(client, "/orders/new", order_form(data)).status_code == 422


def test_plant_state_decides_the_tax(client, data, db):
    login(client, "sales@example.com")
    # Maharashtra plant, Gujarat customer -> outside the plant's state -> IGST
    a = _create(client, data, plant_id=str(data["plant_mh"].id), customer_po_number="PO-A")
    # Maharashtra plant, Maharashtra customer -> same state -> CGST + SGST
    b = _create(client, data, plant_id=str(data["plant_mh"].id), customer_po_number="PO-B", billing_state_code="27")
    db.expire_all()
    oa, ob = db.get(SalesOrder, a), db.get(SalesOrder, b)
    assert oa.tax_mode.value == "igst" and oa.plant_name == "Maharashtra plant" and oa.plant_state_code == "27"
    assert ob.tax_mode.value == "cgst_sgst" and ob.igst_amount == 0 and ob.cgst_amount == ob.sgst_amount > 0


def test_plant_snapshot_survives_renaming_and_is_in_tally_payload(client, data, db):
    from app.services.orders import build_tally_payload

    login(client, "sales@example.com")
    oid = _create(client, data)
    data["plant_gj"].name = "Renamed plant"
    db.commit()
    db.expire_all()
    order = db.get(SalesOrder, oid)
    assert order.plant_name == "Gujarat plant"
    p = build_tally_payload(db, order)
    assert p["plant"] == "Gujarat plant" and p["plant_state"] == "Gujarat"


def test_items_from_deleted_masters_are_refused(client, data, db):
    data["item"].soft_delete(None)
    db.commit()
    login(client, "sales@example.com")
    assert post(client, "/orders/new", order_form(data)).status_code == 422


# ----------------------------------------------------------------------------- admin
def test_admin_creates_user_with_forced_password_change(client, data, db):
    login(client, "admin@example.com")
    r = post(client, "/admin/users/new", {"email": "New.User@Example.com", "full_name": "New User", "role": "sales"})
    assert r.status_code == 200 and "one-time password" in r.text
    import re
    tmp = re.search(r'class="cred"[^>]*>([^<]+)<', r.text).group(1)
    fresh = client.__class__(client.app, base_url="http://testserver")
    resp = login(fresh, "new.user@example.com", tmp)
    assert resp.status_code == 303 and resp.headers["location"] == "/account/password"
    assert fresh.get("/orders", follow_redirects=False).headers["location"] == "/account/password"
    assert post(fresh, "/account/password", {"current": tmp, "new_password": "weak", "confirm": "weak"}).status_code == 400
    ok = post(fresh, "/account/password", {"current": tmp, "new_password": "A-Better-Pass-2026", "confirm": "A-Better-Pass-2026"})
    assert ok.status_code == 303
    assert fresh.get("/orders", follow_redirects=False).status_code == 200


def test_admin_cannot_lock_themselves_out(client, data):
    login(client, "admin@example.com")
    uid = data["admin"].id
    r = post(client, f"/admin/users/{uid}", {"full_name": "Ada", "role": "sales", "is_active": "on"})
    assert r.status_code == 422 and "cannot deactivate or demote" in r.text
    assert "cannot delete your own" in client.get(post(client, f"/admin/users/{uid}/delete").headers["location"], follow_redirects=True).text


def test_deactivated_user_session_stops_working(client, data):
    login(client, "sales@example.com")
    admin = client.__class__(client.app, base_url="http://testserver")
    login(admin, "admin@example.com")
    post(admin, f"/admin/users/{data['sales'].id}", {"full_name": "Sam", "role": "sales"})  # is_active omitted = off
    assert client.get("/orders", follow_redirects=False).status_code == 303


def test_audit_log_records_key_events(client, data, db):
    login(client, "sales@example.com")
    _create(client, data)
    actions = {a for (a,) in db.execute(select(AuditLog.action)).all()}
    assert {"login", "so_created", "so_submitted"} <= actions


# ----------------------------------------------------------------------------- outbox
class FakeGateway:
    configured = True

    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send(self, payload, key):
        if self.fail:
            raise TallyError("boom")
        self.sent.append((payload, key))
        return TallyResult(reference="TLY-1")


def _approved_order(client, data):
    login(client, "sales@example.com")
    oid = _create(client, data)
    acc = client.__class__(client.app, base_url="http://testserver")
    login(acc, "accounts@example.com")
    post(acc, f"/orders/{oid}/approve")
    return oid


def test_outbox_delivers_and_marks_order_synced(client, data, db):
    oid = _approved_order(client, data)
    gw = FakeGateway()
    assert outbox_service.process_batch(db, gw) == 1
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.tally_status is TallyStatus.SYNCED and o.tally_reference == "TLY-1" and len(gw.sent) == 1
    assert outbox_service.process_batch(db, gw) == 0  # nothing left, no duplicate send


def test_outbox_failure_backs_off_then_dies(client, data, db):
    oid = _approved_order(client, data)
    gw = FakeGateway(fail=True)
    outbox_service.process_batch(db, gw)
    db.expire_all()
    row = db.execute(select(TallyOutbox)).scalar_one()
    assert row.status is OutboxStatus.FAILED and row.attempts == 1 and row.next_attempt_at > row.updated_at
    assert outbox_service.process_batch(db, gw) == 0  # not due yet
    row.attempts = outbox_service.MAX_ATTEMPTS - 1
    row.next_attempt_at = row.created_at
    db.commit()
    outbox_service.process_batch(db, gw)
    db.expire_all()
    assert db.execute(select(TallyOutbox)).scalar_one().status is OutboxStatus.DEAD
    assert db.get(SalesOrder, oid).tally_status is TallyStatus.FAILED


def test_outbox_idle_when_tally_not_configured(client, data, db):
    _approved_order(client, data)

    class Off(FakeGateway):
        configured = False

    assert outbox_service.process_batch(db, Off()) == 0
    assert db.execute(select(TallyOutbox)).scalar_one().status is OutboxStatus.PENDING


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


# ----------------------------------------------------------------------------- page smoke tests
def test_every_page_renders_for_its_role(client, data, db):
    login(client, "sales@example.com")
    oid = _create(client, data)
    for url in ("/", "/orders", "/orders?status=pending_approval&q=PO", f"/orders/{oid}", "/orders/new", "/account/password"):
        assert client.get(url).status_code == 200, url

    acc = client.__class__(client.app, base_url="http://testserver")
    login(acc, "accounts@example.com")
    for url in ("/", "/orders", f"/orders/{oid}", "/masters/parties", "/masters/items",
                "/masters/parties/new", f"/masters/parties/{data['party_gj'].id}", f"/masters/items/{data['item'].id}"):
        assert acc.get(url).status_code == 200, url
    assert acc.get("/admin/users").status_code == 403

    adm = client.__class__(client.app, base_url="http://testserver")
    login(adm, "admin@example.com")
    for url in ("/", "/orders", f"/orders/{oid}", "/admin/users", "/admin/users?show_deleted=1", "/admin/users/new",
                f"/admin/users/{data['sales'].id}", "/admin/settings", "/admin/audit", "/admin/audit?action=login&q=sales",
                "/admin/tally", "/masters/parties?show_deleted=1&q=Alpha"):
        assert adm.get(url).status_code == 200, url
    assert adm.get("/nope").status_code == 404
    assert adm.get("/orders/999999").status_code == 404


def test_masters_crud_with_soft_delete(client, data, db):
    from app.models import Party

    login(client, "accounts@example.com")
    r = post(client, "/masters/parties/new", {"name": "Gamma Ltd", "gstin": "27abcde1234f1z5", "state_code": "27", "pincode": "400001"})
    assert r.status_code == 303
    p = db.query(Party).filter_by(name="Gamma Ltd").one()
    assert p.gstin == "27ABCDE1234F1Z5"
    assert post(client, "/masters/parties/new", {"name": "Gamma Ltd"}).status_code == 422          # duplicate
    assert post(client, "/masters/parties/new", {"name": "Bad", "gstin": "XYZ"}).status_code == 422  # invalid GSTIN
    assert post(client, "/masters/parties/new", {"name": "Bad2", "gstin": "27ABCDE1234F1Z5", "state_code": "24"}).status_code == 422
    post(client, f"/masters/parties/{p.id}/delete")
    db.expire_all()
    assert db.get(Party, p.id).deleted_at is not None
    post(client, f"/masters/parties/{p.id}/restore")
    db.expire_all()
    assert db.get(Party, p.id).deleted_at is None


def test_plants_can_be_added_edited_hidden_removed_and_restored(client, data, db):
    from app.models import Plant

    login(client, "admin@example.com")
    assert post(client, "/masters/plants/new", {"name": "Odisha plant", "state_code": "21"}).status_code == 303
    pl = db.query(Plant).filter_by(name="Odisha plant").one()
    assert pl.state_code == "21"
    assert post(client, "/masters/plants/new", {"name": "Odisha plant", "state_code": "21"}).status_code == 422   # duplicate name
    assert post(client, "/masters/plants/new", {"name": "No state"}).status_code == 422                         # state required
    assert post(client, "/masters/plants/new", {"name": "Bad", "state_code": "99"}).status_code == 422          # not a GST state
    assert post(client, "/masters/plants/new", {"name": "Odisha unit 2", "state_code": "21"}).status_code == 303  # same state twice is fine

    assert post(client, f"/masters/plants/{pl.id}", {"name": "Cuttack plant", "state_code": "21", "is_active": "on"}).status_code == 303
    db.expire_all()
    assert db.get(Plant, pl.id).name == "Cuttack plant"

    data["sales"].plants.append(db.get(Plant, pl.id))   # sales people only see plants assigned to them
    db.commit()
    login(client, "sales@example.com")
    assert "Cuttack plant" in client.get("/orders/new").text
    login(client, "admin@example.com")
    post(client, f"/masters/plants/{pl.id}", {"name": "Cuttack plant", "state_code": "21"})        # untick Active
    login(client, "sales@example.com")
    assert "Cuttack plant" not in client.get("/orders/new").text
    login(client, "admin@example.com")
    post(client, f"/masters/plants/{pl.id}/delete")
    db.expire_all()
    assert db.get(Plant, pl.id).deleted_at is not None
    client.get("/masters/plants")  # first load shows the "Removed" flash message
    assert "Cuttack plant" not in client.get("/masters/plants").text
    assert "Cuttack plant" in client.get("/masters/plants?show_deleted=1").text
    post(client, f"/masters/plants/{pl.id}/restore")
    db.expire_all()
    assert db.get(Plant, pl.id).deleted_at is None


def test_item_gst_slab_validated(client, data):
    login(client, "admin@example.com")
    assert post(client, "/masters/items/new", {"name": "Odd", "gst_rate": "17", "unit": "MT"}).status_code == 422
    assert post(client, "/masters/items/new", {"name": "Fine", "gst_rate": "12", "unit": "MT"}).status_code == 303


def test_narration_is_generated_ledger_is_fixed_and_remarks_are_appended(client, data, db):
    login(client, "sales@example.com")
    # Whatever a client tries to send for narration / ledger is ignored.
    oid = _create(client, data, narration="typed by hand", sales_ledger_id="999", remarks="x",
                  item_id=[str(data["item"].id)], quantity=["250.5"], rate=["12500.5"])
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert o.narration == "We sold 250.5 MT of Ultrafine to Alpha Traders at Rs 12,500.50 per MT."
    assert o.sales_ledger_name == "Sales GST" and o.remarks == "x"
    from app.services.orders import tally_narration
    assert tally_narration(o) == o.narration + " x"


def test_remarks_are_mandatory_but_one_character_is_enough(client, data):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, remarks=""))
    assert r.status_code == 422 and "Enter the remarks" in r.text
    assert post(client, "/orders/new", order_form(data, remarks="k")).status_code == 303


def test_item_unit_drives_narration_and_validation_messages(client, data, db):
    data["item"].unit = "KG"
    db.commit()
    login(client, "sales@example.com")
    oid = _create(client, data, quantity=["40"], rate=["55"])
    db.expire_all()
    o = db.get(SalesOrder, oid)
    assert "40 KG of Ultrafine" in o.narration and "per KG" in o.narration and o.items[0].unit == "KG"
    assert "Rate per KG" in post(client, "/orders/new", order_form(data, rate=["0"], customer_po_number="PO-2")).text


# --------------------------------------------------------------------------- region (plant) restriction for sales
def test_sales_dropdown_lists_only_assigned_plants(client, data, db):
    data["sales"].plants = [data["plant_mh"]]
    db.commit()
    login(client, "sales@example.com")
    page = client.get("/orders/new").text
    assert "Maharashtra plant" in page and "Gujarat plant" not in page
    login(client, "sales2@example.com")  # still has both
    page = client.get("/orders/new").text
    assert "Maharashtra plant" in page and "Gujarat plant" in page


def test_sales_cannot_post_an_unassigned_plant(client, data, db):
    data["sales"].plants = [data["plant_mh"]]
    db.commit()
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, plant_id=str(data["plant_gj"].id)))
    assert r.status_code == 422 and "Select the plant" in r.text
    r = post(client, "/orders/new", order_form(data, plant_id=str(data["plant_mh"].id), action="draft"))
    assert r.status_code == 303


def test_sales_with_no_plants_cannot_create_orders(client, data, db):
    data["sales"].plants = []
    db.commit()
    login(client, "sales@example.com")
    assert "No plant assigned to you yet" in client.get("/orders/new").text
    r = post(client, "/orders/new", order_form(data))
    assert r.status_code == 422 and "No plant is assigned to you" in r.text


def test_removing_a_plant_blocks_submitting_an_old_draft(client, data, db):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, plant_id=str(data["plant_gj"].id), action="draft"))
    oid = int(r.headers["location"].split("/orders/")[1].split("?")[0])
    data["sales"].plants = [data["plant_mh"]]
    db.commit()
    r = post(client, f"/orders/{oid}/submit")
    assert r.status_code == 303
    db.expire_all()
    assert db.get(SalesOrder, oid).status is SOStatus.DRAFT


def test_admin_assigns_regions_to_a_sales_user(client, data, db):
    login(client, "admin@example.com")
    sales = data["sales"]
    page = client.get(f"/admin/users/{sales.id}").text
    assert "Gujarat plant" in page and "Maharashtra plant" in page
    r = post(client, f"/admin/users/{sales.id}", {"full_name": sales.full_name, "role": "sales", "is_active": "on",
                                                  "plant_ids": [str(data["plant_mh"].id)]})
    assert r.status_code == 303
    db.expire_all()
    assert [p.name for p in db.get(User, sales.id).plants] == ["Maharashtra plant"]
    assert "Maharashtra plant" in client.get("/admin/users").text
    # changing the role away from sales clears the assignment
    post(client, f"/admin/users/{sales.id}", {"full_name": sales.full_name, "role": "accounts", "is_active": "on",
                                              "plant_ids": [str(data["plant_mh"].id)]})
    db.expire_all()
    assert db.get(User, sales.id).plants == []


def test_unassigned_sales_user_sees_assignment_warning_not_setup_warning(client, data, db):
    data["sales"].plants = []
    db.commit()
    login(client, "sales@example.com")
    page = client.get("/orders/new").text
    assert "No region is assigned to you yet" in page and "No plants have been set up" not in page
