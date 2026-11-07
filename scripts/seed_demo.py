"""Seed demo masters + users into the TEST database for visual checks.  Usage: python scripts/seed_demo.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "tally_so_test")

from sqlalchemy import text  # noqa: E402

from app.core.database import get_engine, session_factory  # noqa: E402
from app.models import Base, Item, Party, Plant, Role, SalesLedger, User  # noqa: E402
from app.services import auth as auth_service  # noqa: E402

assert "test" in os.environ["DB_NAME"], "demo data only goes into a test database"
with get_engine().begin() as c:
    c.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    for t in reversed(Base.metadata.sorted_tables):
        c.execute(text(f"TRUNCATE TABLE `{t.name}`"))
    c.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

db = session_factory()()
for email, role, name in [("admin@demo.test", Role.ADMIN, "Ada Admin"), ("sales@demo.test", Role.SALES, "Sam Sales"), ("accounts@demo.test", Role.ACCOUNTS, "Anil Accounts")]:
    u = User(email=email, full_name=name, role=role, password_hash="", is_active=True)
    auth_service.set_password(db, u, "Demo-Pass-2026", must_change=False)
    db.add(u)
db.add_all([
    Party(name="Alpha Traders", gstin="24ABCDE1234F1Z5", state_code="24", address_line1="1 Mill Road", city="Ahmedabad", pincode="380001", default_payment_term="30 days from invoice"),
    Party(name="Beta Industries", gstin="27ABCDE1234F1Z5", state_code="27", address_line1="9 Port Road", city="Mumbai", pincode="400001"),
    Item(name="Ultrafine", hsn_code="2523", gst_rate=18, unit="MT"),
    Item(name="Fly Ash Brick Grade", hsn_code="6901", gst_rate=5, unit="MT"),
    SalesLedger(name="Sales GST"),
    Plant(name="Gujarat plant", state_code="24"),
    Plant(name="Maharashtra plant", state_code="27"),
])
db.commit()
print("seeded: admin@demo.test / sales@demo.test / accounts@demo.test  (password Demo-Pass-2026)")
