"""Fill a throwaway DEMO database with fictional data, for screenshots and local exploration.

Two steps, both refuse to run unless the database name ends in ``_demo``:

  1. python scripts/seed_demo_database.py masters
        Adds users, customers, items, a sales ledger and plant assignments to the database named in .env.
  2. python scripts/seed_demo_database.py orders http://127.0.0.1:1402
        With the app running against that demo database, creates sample orders through the real HTTP
        forms (so numbering, tax, audit log, notifications and the Tally queue are all exercised).

Everything here is invented: no real customer, GSTIN, rate or person appears in this file.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "Demo-Passw0rd-2026!"
USERS = [
    ("riya.sales@demo.example", "Riya Sales", "SALES", ["Maharashtra", "Gujarat"]),
    ("neil.sales@demo.example", "Neil Sales", "SALES", ["Karnataka", "Tamil Nadu", "Telangana"]),
    ("asha.accounts@demo.example", "Asha Accounts", "ACCOUNTS", []),
]
PARTIES = [
    ("Sample Readymix Traders", "24AAAAA0000A1Z5", "24", "12 Demo Industrial Estate", "Ahmedabad", "380001", "30 days from invoice"),
    ("Example Infra Projects", "27AAAAA0000B1Z5", "27", "4 Sample Business Park", "Pune", "411001", "45 days from invoice"),
    ("Demo Precast Works", "29AAAAA0000C1Z5", "29", "88 Specimen Layout", "Bengaluru", "560001", "Advance"),
    ("Specimen Blocks & Pavers", "33AAAAA0000D1Z5", "33", "7 Example Main Road", "Chennai", "600001", "30 days from invoice"),
    ("Sample Roadways Ltd", "36AAAAA0000E1Z5", "36", "21 Demo Circle", "Hyderabad", "500001", "60 days from invoice"),
]
ITEMS = [
    ("Slag powder (bulk)", "2618", 18, "MT"),
    ("Fly ash (bulk)", "2621", 5, "MT"),
    ("Micro silica (bagged)", "2811", 18, "MT"),
]


def _require_demo(name: str) -> None:
    if not name.endswith("_demo"):
        sys.exit(f"Refusing to run: database '{name}' does not end in _demo.")


def seed_masters() -> None:
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.database import init_engine, session_factory
    from app.models import Item, Party, PartyAddress, Plant, Role, SalesLedger, User
    from app.services import auth as auth_service

    name = get_settings().db_name
    _require_demo(name)
    init_engine(name)
    db = session_factory()()
    plants = {p.name: p for p in db.execute(select(Plant)).scalars()}
    for email, full_name, role, plant_names in USERS:
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
            continue
        user = User(email=email, full_name=full_name, role=Role[role], password_hash="", is_active=True)
        auth_service.set_password(db, user, PASSWORD, must_change=False)
        user.plants = [plants[n] for n in plant_names]
        db.add(user)
    for n, gstin, state, addr, city, pin, term in PARTIES:
        if not db.execute(select(Party).where(Party.name == n)).scalar_one_or_none():
            db.add(Party(name=n, gstin=gstin, state_code=state, default_payment_term=term, addresses=[
                PartyAddress(label="Head office", address_line1=addr, city=city, state_code=state, pincode=pin, is_default=True)]))
    for n, hsn, rate, unit in ITEMS:
        if not db.execute(select(Item).where(Item.name == n)).scalar_one_or_none():
            db.add(Item(name=n, hsn_code=hsn, gst_rate=rate, unit=unit))
    if not db.execute(select(SalesLedger).where(SalesLedger.name == "Sales GST")).scalar_one_or_none():
        db.add(SalesLedger(name="Sales GST"))
    db.commit()
    print(f"Masters seeded in {name}. Users: {', '.join(u[0] for u in USERS)} (password {PASSWORD})")


# --- step 2: orders through the real forms -------------------------------------------------------------------------

def _client(base: str, email: str):
    import httpx

    c = httpx.Client(base_url=base, follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token, "next": "/"})
    assert r.status_code == 303, f"login failed for {email}: {r.status_code}"
    return c


def _csrf(c) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', c.get("/").text).group(1)


def _option(html: str, label: str) -> str:
    """Value of the <option> whose visible text starts with ``label`` (or whose data-name equals it, for items)."""
    import html as htmllib

    text = htmllib.unescape(html)
    m = re.search(r'<option value="(\d+)"[^>]*data-name="%s"' % re.escape(label), text) or re.search(
        r'<option value="(\d+)"[^>]*>\s*%s\s*(?:\(|<)' % re.escape(label), text, re.S
    )
    assert m, f"option not found: {label}"
    return m.group(1)


def _default_address(html: str, party_id: str) -> str:
    """Id of the customer's default saved address, as the order form offers it."""
    import html as htmllib
    import json

    m = re.search(r"<option value=\"%s\"[^>]*?data-addresses='([^']*)'" % party_id, html, re.S)
    assert m, f"no addresses for customer {party_id}"
    addresses = json.loads(htmllib.unescape(m.group(1)))
    assert addresses, f"customer {party_id} has no saved address"
    return str(next((a for a in addresses if a["default"]), addresses[0])["id"])


def _create(c, *, party: str, plant: str, po: str, lines: list[tuple[str, str, str]], action: str, days_ago: int, remarks: str, so_type: str = "sales_order") -> int:
    html = c.get("/orders/new").text
    term = {p[0]: p[6] for p in PARTIES}[party]
    party_id = _option(html, party)
    address_id = _default_address(html, party_id)
    data = {
        "csrf_token": _csrf(c), "so_type": so_type, "order_date": (date.today() - timedelta(days=days_ago)).isoformat(),
        "party_id": party_id, "plant_id": _option(html, plant), "payment_term": term, "customer_po_number": po,
        "billing_address_id": address_id, "shipping_address_id": address_id,
        "remarks": remarks, "item_id": [_option(html, i[0]) for i in lines],
        "quantity": [i[1] for i in lines], "rate": [i[2] for i in lines], "action": action,
    }
    r = c.post("/orders/new", data=data)
    assert r.status_code == 303, r.text[:400]
    return int(re.search(r"/orders/(\d+)", r.headers["location"]).group(1))


def seed_orders(base: str) -> None:
    riya, neil, asha = _client(base, USERS[0][0]), _client(base, USERS[1][0]), _client(base, USERS[2][0])
    plan = [
        # (client, party, plant, po, lines, action, days_ago, remarks, review)
        (riya, "Sample Readymix Traders", "Gujarat", "PO-0412", [("Slag powder (bulk)", "120", "3150")], "submit", 40, "Dispatch in two lots", "approve"),
        (riya, "Example Infra Projects", "Maharashtra", "PO-0413", [("Fly ash (bulk)", "300", "1180"), ("Slag powder (bulk)", "60", "3200")], "submit", 36, "Site delivery", "approve"),
        (neil, "Demo Precast Works", "Karnataka", "PO-0414", [("Micro silica (bagged)", "20", "14500")], "submit", 33, "Bagged, palletised", "approve"),
        (neil, "Specimen Blocks & Pavers", "Tamil Nadu", "PO-0415", [("Fly ash (bulk)", "180", "1210")], "submit", 30, "Weekly supply", "approve"),
        (riya, "Sample Readymix Traders", "Gujarat", "PO-0416", [("Slag powder (bulk)", "90", "3150")], "submit", 27, "Repeat order", "approve"),
        (neil, "Sample Roadways Ltd", "Telangana", "PO-0417", [("Fly ash (bulk)", "500", "1150")], "submit", 22, "Project phase 2", "approve"),
        (riya, "Example Infra Projects", "Maharashtra", "PO-0418", [("Micro silica (bagged)", "12", "14800")], "submit", 18, "Trial lot", "approve"),
        (neil, "Demo Precast Works", "Karnataka", "PO-0419", [("Slag powder (bulk)", "75", "3300")], "submit", 14, "Customer asked for early dispatch", "approve"),
        (riya, "Sample Readymix Traders", "Gujarat", "PO-0420", [("Fly ash (bulk)", "220", "1190")], "submit", 11, "", "approve"),
        (riya, "Example Infra Projects", "Maharashtra", "PO-0421", [("Slag powder (bulk)", "40", "3600")], "submit", 9, "Rate to be confirmed", "reject:Rate is above the agreed price list (3200). Please correct and resubmit."),
        (neil, "Sample Roadways Ltd", "Telangana", "PO-0422", [("Micro silica (bagged)", "8", "15500")], "submit", 7, "", "reject:Customer GSTIN missing on the PO copy. Attach and resubmit."),
        (riya, "Sample Readymix Traders", "Gujarat", "PO-0423", [("Slag powder (bulk)", "150", "3150")], "submit", 4, "Urgent", None),
        (neil, "Specimen Blocks & Pavers", "Tamil Nadu", "PO-0424", [("Fly ash (bulk)", "260", "1215"), ("Micro silica (bagged)", "6", "14900")], "submit", 3, "", None),
        (riya, "Example Infra Projects", "Maharashtra", "PO-0425", [("Fly ash (bulk)", "400", "1175")], "submit", 2, "Delivery to site 3", None),
        (neil, "Demo Precast Works", "Karnataka", "PO-0426", [("Slag powder (bulk)", "110", "3250")], "submit", 1, "", None),
        (riya, "Sample Roadways Ltd", "Gujarat", "PO-0427", [("Micro silica (bagged)", "10", "14600")], "save", 1, "Waiting for the final PO", None),
        (neil, "Sample Readymix Traders", "Telangana", "PO-0428", [("Fly ash (bulk)", "90", "1200")], "save", 0, "", None),
    ]
    made = 0
    for client, party, plant, po, lines, action, ago, remarks, review in plan:
        try:
            oid = _create(client, party=party, plant=plant, po=po, lines=lines, action=action, days_ago=ago, remarks=remarks or "-")
        except AssertionError as exc:
            print("skipped", po, exc)
            continue
        made += 1
        if review == "approve":
            r = asha.post(f"/orders/{oid}/approve", data={"csrf_token": _csrf(asha), "remarks": "Checked against the customer PO"})
            assert r.status_code == 303, r.text[:300]
        elif review and review.startswith("reject:"):
            r = asha.post(f"/orders/{oid}/reject", data={"csrf_token": _csrf(asha), "remarks": review.split(":", 1)[1]})
            assert r.status_code == 303, r.text[:300]
    print(f"{made} orders created")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "masters":
        seed_masters()
    elif step == "orders":
        seed_orders(sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:1402")
    else:
        sys.exit(__doc__)
