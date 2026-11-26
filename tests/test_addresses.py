"""Customer addresses: an order picks its billing and shipping address from the customer's saved addresses and cannot type one."""
from sqlalchemy import select

from app.models import Party, PartyAddress, SalesOrder
from tests.conftest import extract_id, login, order_form, post


def _second_client(client, email):
    other = client.__class__(client.app, base_url="http://testserver")
    login(other, email)
    return other


def test_order_form_offers_the_customers_addresses_and_no_address_boxes(client, data):
    login(client, "sales@example.com")
    html = client.get("/orders/new").text
    assert 'name="billing_address_id"' in html and 'name="shipping_address_id"' in html
    assert 'name="billing_address"' not in html and 'name="shipping_address"' not in html     # nothing to type
    assert "Head office" in html and "Mumbai depot" in html                                  # both of Alpha's addresses are offered


def test_order_stores_the_chosen_addresses_and_ignores_typed_text(client, data, db):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(
        data, billing_address_id=str(data["addr_gj"].id), shipping_address_id=str(data["addr_gj_mh"].id),
        billing_address="Typed by a sneaky user", shipping_address="Also typed", billing_state_code="99"))
    assert r.status_code == 303
    o = db.get(SalesOrder, extract_id(r.headers["location"]))
    assert o.billing_address == "1 Mill Road, Ahmedabad, 380001" and o.billing_state_code == "24"
    assert o.shipping_address == "22 Dock Lane, Mumbai, 400010" and o.shipping_state_code == "27"
    assert o.billing_address_id == data["addr_gj"].id and o.shipping_address_id == data["addr_gj_mh"].id


def test_an_address_of_another_customer_is_rejected(client, data):
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, billing_address_id=str(data["addr_mh"].id)))     # Beta's address on Alpha's order
    assert r.status_code == 422 and "Choose the billing address" in r.text
    r = post(client, "/orders/new", order_form(data, shipping_address_id="999999"))
    assert r.status_code == 422 and "Choose the shipping address" in r.text


def test_removed_or_inactive_addresses_cannot_be_used(client, data, db):
    acc = _second_client(client, "accounts@example.com")
    post(acc, f"/masters/parties/{data['party_gj'].id}/addresses/{data['addr_gj_mh'].id}/delete")
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, shipping_address_id=str(data["addr_gj_mh"].id)))
    assert r.status_code == 422 and "Choose the shipping address" in r.text
    assert "Mumbai depot" not in client.get("/orders/new").text


def test_customer_without_an_address_cannot_be_ordered_for(client, data, db):
    db.add(Party(name="No Address Ltd", state_code="27"))
    db.commit()
    pid = db.execute(select(Party.id).where(Party.name == "No Address Ltd")).scalar_one()
    login(client, "sales@example.com")
    r = post(client, "/orders/new", order_form(data, party_id=str(pid), billing_address_id="", shipping_address_id=""))
    assert r.status_code == 422 and "no address on file" in r.text


def test_old_orders_keep_their_copy_when_an_address_changes(client, data, db):
    login(client, "sales@example.com")
    oid = extract_id(post(client, "/orders/new", order_form(data)).headers["location"])
    acc = _second_client(client, "accounts@example.com")
    r = post(acc, f"/masters/parties/{data['party_gj'].id}/addresses/{data['addr_gj'].id}",
             {"label": "Head office", "address_line1": "77 New Street", "city": "Surat", "state_code": "24", "pincode": "395001", "is_active": "on"})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(PartyAddress, data["addr_gj"].id).address_line1 == "77 New Street"
    assert db.get(SalesOrder, oid).billing_address == "1 Mill Road, Ahmedabad, 380001"


def test_address_management_defaults_and_validation(client, data, db):
    acc = _second_client(client, "accounts@example.com")
    pid = data["party_gj"].id
    page = acc.get(f"/masters/parties/{pid}").text
    assert "Head office" in page and "Mumbai depot" in page and "Default" in page

    assert post(acc, f"/masters/parties/{pid}/addresses/new", {"label": "", "address_line1": "", "state_code": ""}).status_code == 422
    assert post(acc, f"/masters/parties/{pid}/addresses/new", {"label": "Plant gate", "address_line1": "1 Gate", "state_code": "24", "pincode": "12"}).status_code == 422
    assert post(acc, f"/masters/parties/{pid}/addresses/new", {"label": "Plant gate", "address_line1": "1 Gate Road", "state_code": "24"}).status_code == 303

    # making another address the default moves the flag; there is only ever one
    post(acc, f"/masters/parties/{pid}/addresses/{data['addr_gj_mh'].id}/default")
    db.expire_all()
    flags = {a.label: a.is_default for a in db.get(Party, pid).addresses}
    assert flags == {"Head office": False, "Mumbai depot": True, "Plant gate": False}

    # removing the default hands the flag to another usable address
    post(acc, f"/masters/parties/{pid}/addresses/{data['addr_gj_mh'].id}/delete")
    db.expire_all()
    live = db.get(Party, pid).usable_addresses
    assert len(live) == 2 and sum(a.is_default for a in live) == 1

    # an address of one customer cannot be touched through another customer's URL
    assert acc.get(f"/masters/parties/{data['party_mh'].id}/addresses/{data['addr_gj'].id}").status_code == 404


def test_new_customer_needs_a_first_address(client, data, db):
    acc = _second_client(client, "accounts@example.com")
    assert post(acc, "/masters/parties/new", {"name": "Delta Ltd", "state_code": "27"}).status_code == 422
    assert post(acc, "/masters/parties/new", {"name": "Delta Ltd", "state_code": "27", "addr_label": "Head office",
                                              "addr_address_line1": "3 Hill Road", "addr_state_code": "27"}).status_code == 303
    p = db.query(Party).filter_by(name="Delta Ltd").one()
    assert [a.label for a in p.usable_addresses] == ["Head office"] and p.usable_addresses[0].is_default
