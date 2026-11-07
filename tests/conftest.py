"""Test fixtures. Tests run against the dedicated TEST database, never the real one."""
from __future__ import annotations

import os
import re

os.environ["APP_ENV"] = "test"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_engine, init_engine, session_factory
from app.core.security import csrf_token_for, login_limiter
from app.models import Base, Item, Party, Plant, Role, SalesLedger, User
from app.services import auth as auth_service

PASSWORD = "Str0ngPassw0rd!"


@pytest.fixture(scope="session", autouse=True)
def _engine():
    s = get_settings()
    assert s.test_db_name != s.db_name, "refusing to run tests against the real database"
    init_engine(s.test_db_name)
    # Rebuild the schema every run so it can never drift from the models (this is the TEST database only).
    with get_engine().begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        Base.metadata.drop_all(conn)
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    Base.metadata.create_all(get_engine())
    yield


@pytest.fixture(autouse=True)
def clean_db(_engine):
    login_limiter.reset()
    with get_engine().begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"TRUNCATE TABLE `{t.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    yield


@pytest.fixture
def db():
    s = session_factory()()
    yield s
    s.close()


def _mk_user(db, email, role, name):
    u = User(email=email, full_name=name, role=role, password_hash="", is_active=True)
    auth_service.set_password(db, u, PASSWORD, must_change=False)
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def data(db):
    """Users, masters and company settings used by most tests."""
    d = {
        "sales": _mk_user(db, "sales@example.com", Role.SALES, "Sam Sales"),
        "sales2": _mk_user(db, "sales2@example.com", Role.SALES, "Sia Sales"),
        "accounts": _mk_user(db, "accounts@example.com", Role.ACCOUNTS, "Anil Accounts"),
        "admin": _mk_user(db, "admin@example.com", Role.ADMIN, "Ada Admin"),
    }
    d["party_gj"] = Party(name="Alpha Traders", gstin="24ABCDE1234F1Z5", state_code="24", address_line1="1 Mill Road", city="Ahmedabad", pincode="380001", default_payment_term="30 days")
    d["party_mh"] = Party(name="Beta Industries", gstin="27ABCDE1234F1Z5", state_code="27", address_line1="9 Port Road", city="Mumbai", pincode="400001")
    d["item"] = Item(name="Ultrafine", hsn_code="2523", gst_rate=18, unit="MT")
    d["ledger"] = SalesLedger(name="Sales GST")
    d["plant_gj"] = Plant(name="Gujarat plant", state_code="24")
    d["plant_mh"] = Plant(name="Maharashtra plant", state_code="27")
    db.add_all([d["party_gj"], d["party_mh"], d["item"], d["ledger"], d["plant_gj"], d["plant_mh"]])
    d["sales"].plants = [d["plant_gj"], d["plant_mh"]]    # sales people only see the plants assigned to them
    d["sales2"].plants = [d["plant_gj"], d["plant_mh"]]
    db.commit()
    return d


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app(run_background=False), base_url="http://testserver") as c:
        yield c


def csrf(client: TestClient) -> str:
    seed = client.cookies.get("csrf_seed")
    if not seed:
        client.get("/login")
        seed = client.cookies.get("csrf_seed")
    return csrf_token_for(seed)


def login(client: TestClient, email: str, password: str = PASSWORD):
    client.get("/login")
    return client.post("/login", data={"email": email, "password": password, "csrf_token": csrf(client)}, follow_redirects=False)


def post(client: TestClient, url: str, data: dict | None = None, **kw):
    payload = dict(data or {})
    payload["csrf_token"] = csrf(client)
    return client.post(url, data=payload, follow_redirects=False, **kw)


def order_form(d: dict, **over) -> dict:
    base = {
        "so_type": "sales_order", "order_date": __import__("app.core.utils", fromlist=["today_ist"]).today_ist().isoformat(),
        "party_id": str(d["party_gj"].id), "plant_id": str(d["plant_gj"].id), "payment_term": "30 days from invoice", "customer_po_number": "PO-1001",
        "billing_address": "1 Mill Road, Ahmedabad", "billing_state_code": "24",
        "shipping_address": "1 Mill Road, Ahmedabad", "shipping_state_code": "24",
        "remarks": "OK",
        "item_id": [str(d["item"].id)], "quantity": ["100"], "rate": ["12500"], "action": "submit",
    }
    base.update(over)
    return base


def extract_id(location: str) -> int:
    m = re.search(r"/orders/(\d+)", location)
    assert m, location
    return int(m.group(1))
