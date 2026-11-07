"""Create sample orders through the running DEMO instance (port 1402). Dev only."""
import re
import sys
from datetime import date

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:1402"
PW = "Demo-Pass-2026"


def session(email):
    c = httpx.Client(base_url=BASE, follow_redirects=False)
    html = c.get("/login").text
    tok = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)
    assert c.post("/login", data={"email": email, "password": PW, "csrf_token": tok, "next": "/"}).status_code == 303
    return c


def tok(c):
    html = c.get("/orders/new").text if False else c.get("/").text
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def create(c, po, party, state, qty, rate, item, action, so_type="sales_order", narr=None):
    html = c.get("/orders/new").text
    pid = re.search(r'<option value="(\d+)"[^>]*>%s</option>' % party, html).group(1)
    iid = re.search(r'<option value="(\d+)"[^>]*>%s \(' % item, html).group(1)
    plid = re.search(r'<option value="(\d+)"[^>]*>Gujarat plant', html).group(1)
    data = {
        "csrf_token": tok(c), "so_type": so_type, "order_date": date.today().isoformat(), "party_id": pid, "plant_id": plid,
        "payment_term": "30 days from invoice", "customer_po_number": po, "billing_address": "1 Mill Road, Ahmedabad 380001",
        "billing_state_code": state, "shipping_address": "1 Mill Road, Ahmedabad 380001", "shipping_state_code": state,
        "remarks": narr or "OK",
        "item_id": [iid], "quantity": [qty], "rate": [rate], "action": action,
    }
    r = c.post("/orders/new", data=data)
    assert r.status_code == 303, r.text[:500]
    return int(re.search(r"/orders/(\d+)", r.headers["location"]).group(1))


sales = session("sales@demo.test")
acc = session("accounts@demo.test")
a = create(sales, "PO-24-0118", "Alpha Traders", "24", "100", "12500", "Ultrafine", "submit")
b = create(sales, "PO-24-0119", "Beta Industries", "27", "250.5", "4800", "Fly Ash Brick Grade", "submit", "trading_sales_order")
c = create(sales, "PO-24-0120", "Alpha Traders", "24", "60", "13200", "Ultrafine", "submit")
d = create(sales, "PO-24-0121", "Beta Industries", "27", "40", "4950", "Fly Ash Brick Grade", "submit")
create(sales, "PO-24-0122", "Alpha Traders", "24", "75", "12500", "Ultrafine", "save")
for oid, action, data in ((a, "approve", {"remarks": "Verified against PO"}), (d, "reject", {"remarks": "Rate differs from the customer PO (4900)"})):
    r = acc.post(f"/orders/{oid}/{action}", data={"csrf_token": tok(acc), **data})
    assert r.status_code == 303
print("orders", a, b, c, d)
