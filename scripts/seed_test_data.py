"""Add sample data to the configured database so the approval workflow can be tried out.

Usage:  venv\\Scripts\\python.exe scripts\\seed_test_data.py          (add sample data)
        venv\\Scripts\\python.exe scripts\\seed_test_data.py --remove  (take it out again)

It only ADDS rows (never edits or deletes your existing users, plants or orders), is safe to run twice, and every row it
creates is recognisable: demo users end in @demo.ultrafine.test, customers/orders carry "[DEMO]" / "DEMO-" markers.
"""
import os
import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import session_factory  # noqa: E402
from app.models import Item, Party, Plant, Role, SalesOrder, SOType, User  # noqa: E402
from app.services import auth as auth_service  # noqa: E402
from app.services import orders as so  # noqa: E402
from app.services.audit import Ctx  # noqa: E402

PASSWORD = "Demo-Pass-2026"
DEMO_DOMAIN = "@demo.ultrafine.test"
USERS = [
    ("sales.demo" + DEMO_DOMAIN, Role.SALES, "Demo Sales Person"),
    ("sales2.demo" + DEMO_DOMAIN, Role.SALES, "Demo Sales Person 2"),
    ("accounts.demo" + DEMO_DOMAIN, Role.ACCOUNTS, "Demo Accounts Head"),
]
PARTIES = [  # name, gstin, state, address, city, pincode, payment term
    ("[DEMO] Shree Cement Traders", "24AAACS1234F1Z5", "24", "12 GIDC Estate, Vatva", "Ahmedabad", "382445", "30 days from invoice"),
    ("[DEMO] Mumbai Infra Build Pvt Ltd", "27AABCM5678K1Z2", "27", "Plot 9, MIDC Andheri East", "Mumbai", "400093", "45 days from invoice"),
    ("[DEMO] Hyderabad Concrete Works", "36AAECH4321L1Z8", "36", "Survey 44, Medchal Road", "Hyderabad", "501401", "Advance"),
    ("[DEMO] Karnataka Ready Mix", "29AAGCK8765P1Z1", "29", "88 Peenya Industrial Area", "Bengaluru", "560058", "15 days from invoice"),
    ("[DEMO] Raipur Steel & Alloys", "22AABCR2468Q1Z9", "22", "Urla Industrial Complex", "Raipur", "493221", "60 days from invoice"),
]
ITEMS = [("[DEMO] Ultrafine Cement", "2523", 28), ("[DEMO] Fly Ash Brick Grade A", "6901", 5), ("[DEMO] Ground Granulated Slag", "2618", 18)]


def get_or_add(db, model, **where):
    row = db.execute(select(model).where(*[getattr(model, k) == v for k, v in where.items()])).scalars().first()
    if row is None:
        row = model(**where)
        db.add(row)
    return row


def remove(db) -> None:
    """Delete the demo orders, then any [DEMO] master / demo user that no remaining order still refers to."""
    skipped = []
    with db.get_bind().begin() as c:
        ids = [r[0] for r in c.execute(text("SELECT id FROM sales_orders WHERE customer_po_number LIKE 'DEMO-%'"))]
        for oid in ids:
            c.execute(text("DELETE FROM tally_outbox WHERE order_id = :i"), {"i": oid})
            c.execute(text("DELETE FROM sales_order_items WHERE order_id = :i"), {"i": oid})
            c.execute(text("DELETE FROM sales_orders WHERE id = :i"), {"i": oid})
        for table, ref_table, ref_col in (("parties", "sales_orders", "party_id"), ("items", "sales_order_items", "item_id"),
                                          ("sales_ledgers", "sales_orders", "sales_ledger_id")):
            n = c.execute(text(f"DELETE FROM {table} WHERE LEFT(name, 6) = '[DEMO]' AND id NOT IN (SELECT {ref_col} FROM {ref_table} WHERE {ref_col} IS NOT NULL)")).rowcount
            left = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE LEFT(name, 6) = '[DEMO]'")).scalar()
            if left:
                skipped.append(f"{left} [DEMO] {table} still used by other orders")
        c.execute(text("DELETE FROM user_sessions WHERE user_id IN (SELECT id FROM users WHERE email LIKE :d)"), {"d": "%" + DEMO_DOMAIN})
        c.execute(text(f"DELETE FROM users WHERE email LIKE :d AND id NOT IN (SELECT created_by_id FROM sales_orders) "
                       f"AND id NOT IN (SELECT reviewed_by_id FROM sales_orders WHERE reviewed_by_id IS NOT NULL)"), {"d": "%" + DEMO_DOMAIN})
        remaining = c.execute(text("SELECT COUNT(*) FROM sales_orders")).scalar()
        if remaining == 0:  # no orders left at all, so restart numbering at 00001
            c.execute(text("DELETE FROM number_sequences"))
    print(f"Removed {len(ids)} demo orders and the unused demo users and masters." + (" Numbering restarts at 00001." if remaining == 0 else ""))
    for note in skipped:
        print("  kept:", note)


def main() -> int:
    db = session_factory()()
    if "--remove" in sys.argv:
        remove(db)
        return 0

    if db.execute(select(SalesOrder.id).where(SalesOrder.customer_po_number.like("DEMO-%"))).first():
        print("Demo data is already in place. Run with --remove first if you want a fresh set.")
        return 0

    users = {}
    for email, role, name in USERS:
        u = auth_service.get_user_by_email(db, email, include_deleted=True)
        if u is None:
            u = User(email=email, full_name=name, role=role, password_hash="", is_active=True)
            auth_service.set_password(db, u, PASSWORD, must_change=False)
            db.add(u)
        users[email] = u

    parties = {}
    for name, gstin, st, addr, city, pin, term in PARTIES:
        p = get_or_add(db, Party, name=name)
        p.gstin, p.state_code, p.address_line1, p.city, p.pincode, p.default_payment_term = gstin, st, addr, city, pin, term
        parties[name] = p
    items = {}
    for name, hsn, rate in ITEMS:
        i = get_or_add(db, Item, name=name)
        i.hsn_code, i.gst_rate, i.unit = hsn, Decimal(rate), "MT"
        items[name] = i
    db.commit()

    plants = {p.name: p for p in db.execute(select(Plant).where(Plant.deleted_at.is_(None))).scalars()}
    plant_gj, plant_mh, plant_tg = plants.get("Gujarat"), plants.get("Maharashtra"), plants.get("Telangana")
    if not (plant_gj and plant_mh and plant_tg):  # fall back to whichever plants exist
        by_state = {p.state_code: p for p in plants.values()}
        plant_gj, plant_mh, plant_tg = by_state.get("24"), by_state.get("27"), by_state.get("36")
    if not (plant_gj and plant_mh and plant_tg):
        print("Add Gujarat, Maharashtra and Telangana plants first (Master data > Plants).", file=sys.stderr)
        return 1

    sales1, sales2 = users["sales.demo" + DEMO_DOMAIN], users["sales2.demo" + DEMO_DOMAIN]
    for u in (sales1, sales2):   # sales people may only sell from their assigned plants
        u.plants = [plant_gj, plant_mh, plant_tg]
    db.commit()
    acct = users["accounts.demo" + DEMO_DOMAIN]
    ctx = lambda u: Ctx(user=u, ip="127.0.0.1", request_id="seed", user_agent="seed_test_data")  # noqa: E731
    today = date.today()
    P = [parties[p[0]] for p in PARTIES]
    I = list(items.values())  # noqa: E741
    ledger = so.sales_ledger(db)

    def make(user, n, party, plant, lines, *, days_ago=0, trading=False, state=None, term=None, action="submit", remarks="OK"):
        st = state or party.state_code
        addr = f"{party.address_line1}, {party.city} {party.pincode}"
        v = so.Validated(
            so_type=SOType.TRADING_SALES_ORDER if trading else SOType.SALES_ORDER,
            order_date=today - timedelta(days=days_ago), party=party, payment_term=term or party.default_payment_term or "30 days",
            customer_po_number=f"DEMO-PO-{n:03d}", billing_address=addr, billing_state_code=st,
            shipping_address=addr, shipping_state_code=st, plant=plant, ledger=ledger,
            narration="", remarks=remarks, lines=[(it, Decimal(str(q)), Decimal(str(r))) for it, q, r in lines],
        )
        v.narration = so.build_narration(party.name, v.lines)
        order = so.create_order(db, user, v, ctx(user))
        if action == "submit":
            so.submit(db, user, order, ctx(user))
        db.commit()
        return order

    # --- waiting for approval (same state -> CGST+SGST, other state -> IGST)
    make(sales1, 1, P[0], plant_gj, [(I[0], 120, 6200)], days_ago=0)                          # Gujarat -> Gujarat : CGST+SGST
    make(sales1, 2, P[1], plant_gj, [(I[0], 250.5, 6150), (I[2], 40, 2900)], days_ago=1)     # Gujarat -> Maharashtra : IGST, 2 lines
    make(sales2, 3, P[2], plant_tg, [(I[1], 75, 4800)], days_ago=1, trading=True)             # Telangana -> Telangana : CGST+SGST, trading
    make(sales2, 4, P[3], plant_mh, [(I[0], 60, 6400)], days_ago=2)                           # Maharashtra -> Karnataka : IGST
    make(sales1, 5, P[4], plant_gj, [(I[2], 300, 2850)], days_ago=3)                          # Gujarat -> Chhattisgarh : IGST
    # --- already decided, so the lists and the Tally queue have something in them
    ok = make(sales1, 6, P[1], plant_mh, [(I[1], 90, 4750)], days_ago=5)                      # Maharashtra -> Maharashtra : CGST+SGST
    so.approve(db, acct, ok, ctx(acct), "Verified against customer PO")
    db.commit()
    bad = make(sales2, 7, P[0], plant_gj, [(I[0], 40, 5900)], days_ago=4)
    so.reject(db, acct, bad, "Rate differs from the customer PO (6,200 per MT). Please correct and resubmit.", ctx(acct))
    db.commit()
    # --- drafts (not yet submitted)
    make(sales1, 8, P[3], plant_mh, [(I[1], 55, 4900)], days_ago=0, action="draft")
    make(sales2, 9, P[2], plant_tg, [(I[0], 30, 6300)], days_ago=0, action="draft")

    print("Sample data added: 5 customers, 3 items, 9 orders (5 waiting for approval, 1 approved, 1 rejected, 2 drafts).")
    print(f"Demo logins (password {PASSWORD}):")
    for email, role, _ in USERS:
        print(f"  {role.value:9s} {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
