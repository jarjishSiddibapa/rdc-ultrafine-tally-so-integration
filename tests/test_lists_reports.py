"""Search, filters, sorting, paging, column choice, downloads and the report catalogue."""
import csv
import io
import re
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import select

from app.models import AuditLog, SalesOrder, TallyOutbox
from app.services import reports as rep
from app.services.listing import ListSpec, Col, parse
from tests.conftest import extract_id, login, order_form, post


def _make(client, d, n, **over):
    ids = []
    for i in range(n):
        r = post(client, "/orders/new", order_form(d, **{"customer_po_number": f"PO-{i:03d}", **over}))
        assert r.status_code == 303, r.text[:500]
        ids.append(extract_id(r.headers["location"]))
    return ids


def _approve_all(client, ids):
    for i in ids:
        assert post(client, f"/orders/{i}/approve").status_code == 303


def _seed(client, data):
    """Sales submits 5 orders; accounts approves 2 and sends 1 back."""
    login(client, "sales@example.com")
    ids = _make(client, data, 5)
    # one for the other customer so customer filters have something to separate
    r = post(client, "/orders/new", order_form(data, customer_po_number="PO-MH", party_id=str(data["party_mh"].id),
                                                plant_id=str(data["plant_mh"].id), billing_state_code="27", shipping_state_code="27"))
    ids.append(extract_id(r.headers["location"]))
    client.cookies.clear()
    login(client, "accounts@example.com")
    _approve_all(client, ids[:2])
    post(client, f"/orders/{ids[2]}/reject", {"remarks": "Wrong rate on line 1"})
    return ids


# ----------------------------------------------------------------------------- engine
def test_parse_ignores_junk_and_clamps():
    spec = ListSpec([Col("a", "A", None, search=True)], default_sort="a")
    st = parse({"per_page": "9999", "page": "-4", "dir": "sideways", "sort": "drop table", "q": "x" * 500, "f_a": "zzz"}, spec)
    assert st.per_page == 20 and st.page == 1 and st.dir == "desc" and st.sort == "a" and len(st.q) == 80


def test_search_filter_sort_page(client, data):
    _seed(client, data)
    # search by PO number
    r = client.get("/orders?q=PO-003")
    assert r.status_code == 200 and "PO-003" in r.text and "PO-004" not in r.text
    # filter by customer (text contains) and by billing state (choice)
    r = client.get("/orders?f_status=all&f_party_name=Beta")
    assert "Beta Industries" in r.text and "Alpha Traders" not in r.text
    # date range with nothing in it
    r = client.get("/orders?f_status=all&f_order_date_from=2001-01-01&f_order_date_to=2001-12-31")
    assert "No orders match" in r.text
    # number range on total (all 5 are 12,50,000 + GST)
    r = client.get("/orders?f_status=all&f_total_amount_from=99999999")
    assert "No orders match" in r.text
    # sorting does not error for any sortable column
    from app.services.specs import order_spec
    from app.web import templates  # noqa: F401
    for c in order_spec().cols:
        if c.can_sort:
            assert client.get(f"/orders?f_status=all&sort={c.key}&dir=asc").status_code == 200, c.key
            assert client.get(f"/orders?f_status=all&sort={c.key}&dir=desc&per_page=10").status_code == 200, c.key


def test_pagination_controls(client, data):
    login(client, "sales@example.com")
    _make(client, data, 12)
    r = client.get("/orders?per_page=10")
    assert "Showing <b>1</b> to <b>10</b> of <b>12</b>" in r.text and "Go to page" in r.text
    r2 = client.get("/orders?per_page=10&page=2")
    assert "Showing <b>11</b> to <b>12</b> of <b>12</b>" in r2.text
    # a page past the end lands on the last page instead of an empty screen
    r3 = client.get("/orders?per_page=10&page=99")
    assert "Showing <b>11</b> to <b>12</b>" in r3.text


def test_column_choice(client, data):
    login(client, "sales@example.com")
    _make(client, data, 1)
    plain = client.get("/orders").text
    assert "Pushed to Tally at" not in plain.split("<table")[1].split("</table>")[0]
    shown = client.get("/orders?cols=so_number&cols=sent_at&cols=push_attempts").text.split("<table")[1].split("</table>")[0]
    assert "Pushed to Tally at" in shown and "Customer" not in shown


def test_accounts_default_view_is_pending_but_all_is_available(client, data):
    _seed(client, data)
    default = client.get("/orders").text
    assert "Awaiting approval" in default
    allv = client.get("/orders?f_status=all")
    assert allv.status_code == 200 and "Approved" in allv.text


def test_sales_only_sees_and_exports_own_orders(client, data):
    login(client, "sales@example.com")
    _make(client, data, 2)
    client.cookies.clear()
    login(client, "sales2@example.com")
    _make(client, data, 1, customer_po_number="OTHER-1")
    r = client.get("/orders?export=csv")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    body = [x for x in rows[1:] if x and x[0].startswith(("Draft", "SO", "TSO"))]
    assert len(body) == 1 and body[0][3] == "OTHER-1"


# ----------------------------------------------------------------------------- downloads
def test_orders_csv_download_has_every_timestamp_and_is_audited(client, data, db):
    ids = _seed(client, data)
    r = client.get("/orders?f_status=all&export=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="ultrafine-sales-orders-' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    text = r.text.lstrip("﻿")
    header = next(csv.reader(io.StringIO(text)))
    for needed in ("Created at", "Submitted at", "Approved / sent back at", "Queued for Tally at", "First push at", "Last push attempt at",
                   "Pushed to Tally at", "Push attempts", "Time to decision", "Decision remarks", "Total (Rs)"):
        assert needed in header, needed
    assert "Wrong rate on line 1" in text
    db.expire_all()
    log = db.execute(select(AuditLog).where(AuditLog.action == "report_downloaded").order_by(AuditLog.id.desc())).scalars().first()
    assert log is not None and log.details["rows"] == len(ids) and log.details["format"] == "csv"


def test_xlsx_download_opens_and_has_info_sheet(client, data):
    _seed(client, data)
    r = client.get("/orders?f_status=approved&export=xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.worksheets[0]
    assert ws.max_row == 3                       # header + the 2 approved orders
    assert ws["A1"].value == "Order no."
    info = {row[0].value: row[1].value for row in wb["Report info"].iter_rows()}
    assert info["Rows"] == "2" and "Generated on" in info and "Status" in " ".join(str(k) for k in info)


def test_export_neutralises_spreadsheet_formulas(client, data):
    login(client, "sales@example.com")
    _make(client, data, 1, remarks="=HYPERLINK(\"http://evil\")")
    text = client.get("/orders?export=csv").text
    assert "'=HYPERLINK" in text


def test_export_ignores_paging_but_respects_filters(client, data):
    login(client, "sales@example.com")
    _make(client, data, 12)
    r = client.get("/orders?per_page=10&page=2&q=PO-00&export=csv")
    body = [x for x in csv.reader(io.StringIO(r.text.lstrip("﻿"))) if x and x[0].startswith(("Draft", "SO", "TSO"))]
    assert len(body) == 10      # PO-000..PO-009 match "PO-00"; not cut to one page of 10 starting at row 11


# ----------------------------------------------------------------------------- other lists
def test_masters_users_audit_tally_lists_filter_sort_and_export(client, data):
    _seed(client, data)
    client.cookies.clear()
    login(client, "admin@example.com")
    for url in ("/masters/parties", "/masters/plants", "/masters/items", "/admin/users", "/admin/audit", "/admin/tally"):
        assert client.get(url).status_code == 200, url
        assert client.get(url + "?per_page=10&dir=asc&q=a").status_code == 200, url
        assert client.get(url + "?export=csv").status_code == 200, url
        assert client.get(url + "?export=xlsx").status_code == 200, url
    assert "Alpha Traders" in client.get("/masters/parties?q=alpha").text
    assert "Beta Industries" not in client.get("/masters/parties?q=alpha").text
    assert "Maharashtra" in client.get("/masters/parties?f_state_code=27").text
    assert "Alpha Traders" not in client.get("/masters/parties?f_state_code=27").text
    assert "Ada Admin" in client.get("/admin/users?f_role=admin").text
    assert "Sam Sales" not in client.get("/admin/users?f_role=admin").text
    assert "Active" in client.get("/admin/users?f_status=active").text
    assert "Anil Accounts" in client.get("/admin/users?q=anil").text
    audit = client.get("/admin/audit?f_action=so_approved").text
    assert "Order approved" in audit and "Order submitted" not in audit.split("<tbody>")[1]
    # the queue shows when each order was queued; sorting by each timestamp works
    for key in ("created_at", "last_attempt_at", "sent_at", "push_time", "attempts"):
        assert client.get(f"/admin/tally?sort={key}&dir=desc").status_code == 200, key


def test_non_admin_cannot_open_admin_lists_or_their_exports(client, data):
    login(client, "sales@example.com")
    for url in ("/admin/audit?export=csv", "/admin/users?export=csv", "/admin/tally?export=csv", "/masters/parties?export=csv"):
        assert client.get(url).status_code == 403, url


# ----------------------------------------------------------------------------- reports
def test_every_report_renders_sorts_and_downloads_for_every_allowed_role(client, data):
    _seed(client, data)
    roles = {"sales@example.com": "sales", "accounts@example.com": "accounts", "admin@example.com": "admin"}
    for email, role in roles.items():
        client.cookies.clear()
        login(client, email)
        assert client.get("/reports").status_code == 200
        for r in rep.REPORTS:
            url = f"/reports/{r.slug}"
            allowed = any(x.value == role for x in r.roles)
            resp = client.get(url)
            assert resp.status_code == (200 if allowed else 404), (email, r.slug, resp.status_code)
            if not allowed:
                continue
            assert client.get(url + "?date_from=2020-01-01&date_to=2099-12-31&per_page=50").status_code == 200, r.slug
            assert client.get(url + "?export=csv").status_code == 200, r.slug
            x = client.get(url + "?export=xlsx&date_from=2020-01-01")
            assert x.status_code == 200 and load_workbook(io.BytesIO(x.content)).worksheets, r.slug
    # sorting every column of every report (accounts sees all but admin-only ones)
    client.cookies.clear()
    login(client, "admin@example.com")
    for r in rep.REPORTS:
        page = client.get(f"/reports/{r.slug}")
        block = re.findall(r'name="sort".*?</select>', page.text, flags=re.S)[0]
        for k in re.findall(r'<option value="([^"]+)"', block):
            assert client.get(f"/reports/{r.slug}?sort={k}&dir=asc").status_code == 200, (r.slug, k)


def test_report_content_is_correct(client, data):
    ids = _seed(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    # sent-back report lists the reason
    r = client.get("/reports/rejections")
    assert "Wrong rate on line 1" in r.text
    # approval trail shows created, submitted, approved, sent back with who and when
    trail = client.get("/reports/approval-trail").text
    for word in ("Created", "Submitted", "Approved", "Sent back", "accounts@example.com", "sales@example.com"):
        assert word in trail
    # customer summary: Alpha has 5 submitted orders (2 approved, 1 sent back, 2 waiting)
    summary = client.get("/reports/by-customer?f_customer=Alpha").text
    assert "Alpha Traders" in summary and "Beta Industries" not in summary
    # totals row adds up all matching rows
    reg = client.get("/reports/order-register?f_status=approved").text
    assert "Total of all 2 matching" in reg
    assert "25,00,000.00" in reg or "29,50,000.00" in reg        # 2 x 12,50,000 (+ GST on top)
    # GST summary only counts approved orders
    assert "Approved orders" in client.get("/reports/monthly-gst").text


def test_report_period_filter_and_presets(client, data):
    _seed(client, data)
    client.cookies.clear()
    login(client, "accounts@example.com")
    assert "Nothing was recorded" in client.get("/reports/order-register?date_from=2001-01-01&date_to=2001-02-01").text
    assert "Last financial year" in client.get("/reports/order-register").text
    # reversed dates are swapped, not an error
    assert client.get("/reports/order-register?date_from=2099-01-01&date_to=2001-01-01").status_code == 200
    # garbage dates are ignored
    assert client.get("/reports/order-register?date_from=oops&date_to=%27").status_code == 200


def test_tally_push_timestamps_are_recorded(client, data, db):
    from app.integrations.tally import TallyResult
    from app.services import outbox as outbox_service

    ids = _seed(client, data)
    row = db.execute(select(TallyOutbox).order_by(TallyOutbox.id)).scalars().first()
    assert row.first_attempt_at is None and row.last_attempt_at is None

    class Gateway:
        configured = True

        def send(self, payload, key):
            return TallyResult(reference="V-1")

    outbox_service.process_batch(db, Gateway())
    db.expire_all()
    row = db.execute(select(TallyOutbox).order_by(TallyOutbox.id)).scalars().first()
    assert row.first_attempt_at and row.last_attempt_at and row.sent_at and row.attempts == 1
    client.cookies.clear()
    login(client, "admin@example.com")
    detail = client.get(f"/orders/{row.order_id}").text
    for label in ("Queued for Tally", "First push attempt", "Latest push attempt", "Delivered to Tally", "Ref V-1"):
        assert label in detail, label
    timing = client.get("/reports/tally-timing").text
    assert "Delivered at" in timing and "Queue to delivery" in timing
