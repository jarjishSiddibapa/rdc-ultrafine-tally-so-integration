"""Master data (customers, plants, items). Accounts in-charge and admins manage them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.core.constants import GST_RATES, GST_STATES, UNITS
from app.core.database import get_db
from app.core.utils import clean_text, parse_decimal
from app.deps import current_user, get_ctx, require_roles
from app.models import Item, Party, PartyAddress, Plant, Role, SalesOrder, User
from app.routers._lists import run_list
from app.services.specs import master_spec
from app.services.audit import Ctx, audit
from app.web import redirect, render

router = APIRouter(prefix="/masters", dependencies=[Depends(require_roles(Role.ADMIN, Role.ACCOUNTS))])
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


@dataclass
class Kind:
    slug: str
    model: type
    title: str
    singular: str
    fields: list[dict] = field(default_factory=list)  # name, label, type, required, max, hint
    columns: list[tuple[str, str]] = field(default_factory=list)


KINDS: dict[str, Kind] = {
    "parties": Kind("parties", Party, "Customers", "customer", [
        {"name": "name", "label": "Party A/C name", "type": "text", "required": True, "max": 200, "hint": "Exactly as it appears in Tally."},
        {"name": "gstin", "label": "GSTIN", "type": "text", "max": 15},
        {"name": "default_payment_term", "label": "Usual payment term", "type": "text", "max": 200},
        {"name": "state_code", "label": "Registered state", "type": "state", "hint": "The state of the GSTIN. Billing and shipping addresses are added separately below."},
    ], [("name", "Name"), ("gstin", "GSTIN"), ("state_code", "State")]),
    "plants": Kind("plants", Plant, "Plants", "plant", [
        {"name": "name", "label": "Plant or location name", "type": "text", "required": True, "max": 200, "hint": "For example: Nagpur plant, or just the state name."},
        {"name": "state_code", "label": "GST state of the plant", "type": "state", "required": True, "hint": "Customers in this state get CGST and SGST. All other states get IGST."},
    ], [("name", "Name"), ("state_code", "State")]),
    "items": Kind("items", Item, "Items", "item", [
        {"name": "name", "label": "Item name", "type": "text", "required": True, "max": 200, "hint": "Exactly as it appears in Tally."},
        {"name": "hsn_code", "label": "HSN code", "type": "text", "max": 8},
        {"name": "gst_rate", "label": "GST rate (%)", "type": "rate", "required": True},
        {"name": "unit", "label": "Unit of measurement", "type": "unit", "required": True, "hint": "Orders for this item use this unit for quantity and rate."},
    ], [("name", "Name"), ("hsn_code", "HSN"), ("unit", "Unit"), ("gst_rate", "GST %")]),
}


# A customer's addresses. Orders pick their billing and shipping address from these, so they are the only place an address is typed.
ADDRESS_FIELDS = [
    {"name": "label", "label": "Address name", "type": "text", "required": True, "max": 100, "hint": "How sales people recognise it, for example Head office or Pune warehouse."},
    {"name": "address_line1", "label": "Address line 1", "type": "text", "required": True, "max": 200},
    {"name": "address_line2", "label": "Address line 2", "type": "text", "max": 200},
    {"name": "city", "label": "City", "type": "text", "max": 100},
    {"name": "state_code", "label": "State", "type": "state", "required": True, "hint": "Decides whether CGST and SGST or IGST applies when this is the billing address."},
    {"name": "pincode", "label": "Pincode", "type": "text", "max": 6},
]


def _validate_address(data: FormData, prefix: str = "") -> tuple[dict, dict[str, str]]:
    values: dict = {}
    errors: dict[str, str] = {}
    for f in ADDRESS_FIELDS:
        key = prefix + f["name"]
        raw = data.get(key)
        values[f["name"]] = clean_text(raw if isinstance(raw, str) else "", f.get("max", 20))
        if f.get("required") and not values[f["name"]]:
            errors[key] = f"Enter the {f['label'].lower()}." if f["type"] != "state" else "Choose the state."
    if values["state_code"] and values["state_code"] not in GST_STATES:
        errors[prefix + "state_code"] = "Choose a valid state."
    if values["pincode"] and not re.fullmatch(r"\d{6}", values["pincode"]):
        errors[prefix + "pincode"] = "A pincode has 6 digits."
    return values, errors


def _form_ctx(k: "Kind", row, values: dict, errors: dict, db: Session | None = None) -> dict:
    ctx = {"k": k, "row": row, "values": values, "errors": errors, "rates": GST_RATES, "units": UNITS,
           "address_fields": ADDRESS_FIELDS if k.slug == "parties" else [], "addresses": []}
    if k.slug == "parties" and row is not None:
        ctx["addresses"] = [a for a in row.addresses if a.deleted_at is None]
    return ctx


def _kind(slug: str) -> Kind:
    k = KINDS.get(slug)
    if k is None:
        raise HTTPException(404, "Not found.")
    return k


def _validate(db: Session, k: Kind, data: FormData, existing) -> tuple[dict, dict[str, str]]:  # noqa: ANN001
    values: dict = {}
    errors: dict[str, str] = {}
    for f in k.fields:
        raw = clean_text(data.get(f["name"]) if isinstance(data.get(f["name"]), str) else "", f.get("max", 20))
        values[f["name"]] = raw
        if f.get("required") and not raw:
            errors[f["name"]] = f"Enter the {f['label'].lower()}."
    if k.slug == "parties":
        values["gstin"] = values["gstin"].upper()
        if values["gstin"] and not GSTIN_RE.match(values["gstin"]):
            errors["gstin"] = "This is not a valid 15 character GSTIN."
        elif values["gstin"] and values["state_code"] and values["gstin"][:2] != values["state_code"]:
            errors["state_code"] = "The state does not match the first two digits of the GSTIN."
        if values["state_code"] and values["state_code"] not in GST_STATES:
            errors["state_code"] = "Choose a valid state."
    if k.slug == "plants" and values["state_code"] not in GST_STATES:
        errors["state_code"] = "Choose the state this plant is in."
    if k.slug == "items":
        rate = parse_decimal(values["gst_rate"], places=2)
        if rate is None or rate not in {Decimal(r) for r in GST_RATES}:
            errors["gst_rate"] = "Choose one of the GST slabs."
        else:
            values["gst_rate"] = rate
        values["unit"] = values["unit"].upper()
        if values["unit"] not in UNITS:
            errors["unit"] = "Choose a unit of measurement from the list."
        if values["hsn_code"] and not re.fullmatch(r"\d{4,8}", values["hsn_code"]):
            errors["hsn_code"] = "An HSN code has 4 to 8 digits."
    name = values.get("name", "")
    if name and "name" not in errors:
        stmt = select(k.model).where(k.model.name == name, k.model.deleted_at.is_(None))
        if existing is not None:
            stmt = stmt.where(k.model.id != existing.id)
        if db.execute(stmt.limit(1)).first():
            errors["name"] = "A record with this name already exists."
    return values, errors


@router.get("/{slug}")
def list_(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    k = _kind(slug)
    spec = master_spec(slug, db)

    base = select(k.model)
    show_deleted = request.query_params.get("show_deleted") == "1" or request.query_params.get("f_status") == "removed"
    if not show_deleted:
        base = base.where(k.model.deleted_at.is_(None))
    st, page, download = run_list(request, db, user, ctx, base, spec, k.title)
    if download:
        return download
    return render(request, "masters/list.html", {"k": k, "st": st, "page": page, "show_deleted": show_deleted,
                                                 "used": dict(db.execute(select(SalesOrder.plant_id, func.count()).where(SalesOrder.deleted_at.is_(None), SalesOrder.plant_id.is_not(None)).group_by(SalesOrder.plant_id)).all()) if slug == "plants" else {}})


@router.get("/{slug}/new")
def new_page(slug: str, request: Request):
    k = _kind(slug)
    defaults = {"unit": "MT", "gst_rate": "18"} if slug == "items" else ({"addr_label": "Main address"} if slug == "parties" else {})
    return render(request, "masters/form.html", _form_ctx(k, None, defaults, {}))


async def _form(request: Request) -> FormData:
    return await request.form()


@router.post("/{slug}/new")
def create(slug: str, request: Request, data: FormData = Depends(_form), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    k = _kind(slug)
    values, errors = _validate(db, k, data, None)
    addr: dict = {}
    if k.slug == "parties":      # a customer is created together with its first address, so it can be used on an order straight away
        addr, addr_errors = _validate_address(data, "addr_")
        errors.update(addr_errors)
        values.update({"addr_" + key: v for key, v in addr.items()})
    if errors:
        return render(request, "masters/form.html", _form_ctx(k, None, values, errors), status_code=422)
    own = {key: v for key, v in values.items() if not key.startswith("addr_")}
    row = k.model(**{key: (v or None) if isinstance(v, str) and key not in ("name", "unit") else v for key, v in own.items()})
    db.add(row)
    db.flush()
    if addr:
        first = PartyAddress(party_id=row.id, is_default=True, **{key: v or None for key, v in addr.items()})
        db.add(first)
        db.flush()
        audit(db, ctx, "party_address_created", entity_type="party_address", entity_id=first.id, details={"customer": row.name, "address": first.label})
    audit(db, ctx, f"{k.slug}_created", entity_type=k.slug, entity_id=row.id, details={"name": row.name})
    db.commit()
    return redirect(f"/masters/{k.slug}", f"Added {row.name}.")


def _row(db: Session, k: Kind, row_id: int):
    row = db.get(k.model, row_id)
    if row is None:
        raise HTTPException(404, "Not found.")
    return row


@router.get("/{slug}/{row_id}")
def edit_page(slug: str, row_id: int, request: Request, db: Session = Depends(get_db)):
    k = _kind(slug)
    row = _row(db, k, row_id)
    values = {f["name"]: (str(getattr(row, f["name"])) if getattr(row, f["name"]) is not None else "") for f in k.fields}
    if slug == "items":
        values["gst_rate"] = str(row.gst_rate.normalize())
    return render(request, "masters/form.html", _form_ctx(k, row, values, {}))


@router.post("/{slug}/{row_id}")
def update(slug: str, row_id: int, request: Request, data: FormData = Depends(_form), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    k = _kind(slug)
    row = _row(db, k, row_id)
    values, errors = _validate(db, k, data, row)
    active = data.get("is_active") == "on"
    if errors:
        return render(request, "masters/form.html", _form_ctx(k, row, values, errors), status_code=422)
    for key, v in values.items():
        setattr(row, key, (v or None) if isinstance(v, str) and key not in ("name", "unit") else v)
    row.is_active = active
    audit(db, ctx, f"{k.slug}_updated", entity_type=k.slug, entity_id=row.id, details={"name": row.name, "active": active})
    db.commit()
    return redirect(f"/masters/{k.slug}", "Saved.")


@router.post("/{slug}/{row_id}/delete")
def delete(slug: str, row_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    k = _kind(slug)
    row = _row(db, k, row_id)
    row.soft_delete(ctx.user.id if ctx.user else None)
    audit(db, ctx, f"{k.slug}_deleted", entity_type=k.slug, entity_id=row.id, details={"name": row.name})
    db.commit()
    return redirect(f"/masters/{k.slug}", f"Removed {row.name}. Existing orders keep their data.")


@router.post("/{slug}/{row_id}/restore")
def restore(slug: str, row_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    k = _kind(slug)
    row = _row(db, k, row_id)
    clash = db.execute(select(k.model.id).where(k.model.name == row.name, k.model.deleted_at.is_(None), k.model.id != row.id).limit(1)).first()
    if clash:
        return redirect(f"/masters/{k.slug}?show_deleted=1", "A record with this name already exists, so this one cannot be restored.", "error")
    row.restore()
    audit(db, ctx, f"{k.slug}_restored", entity_type=k.slug, entity_id=row.id, details={"name": row.name})
    db.commit()
    return redirect(f"/masters/{k.slug}", f"Restored {row.name}.")


# --------------------------------------------------------------------------- customer addresses
def _party(db: Session, party_id: int) -> Party:
    p = db.get(Party, party_id)
    if p is None:
        raise HTTPException(404, "Not found.")
    return p


def _address(db: Session, party: Party, address_id: int) -> PartyAddress:
    a = db.get(PartyAddress, address_id)
    if a is None or a.party_id != party.id or a.deleted_at is not None:
        raise HTTPException(404, "Not found.")
    return a


def _tidy_default(party: Party) -> None:
    """Exactly one usable address is the default (the one orders pre-select); none if the customer has no usable address."""
    live = party.usable_addresses
    keep = next((a for a in live if a.is_default), live[0] if live else None)
    for a in party.addresses:
        a.is_default = a is keep


def _address_page(request: Request, party: Party, row: PartyAddress | None, values: dict, errors: dict, status: int = 200):
    return render(request, "masters/address_form.html", {"party": party, "row": row, "values": values, "errors": errors,
                                                         "address_fields": ADDRESS_FIELDS}, status_code=status)


@router.get("/parties/{party_id}/addresses/new")
def address_new(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = _party(db, party_id)
    return _address_page(request, party, None, {"state_code": party.state_code or ""}, {})


@router.post("/parties/{party_id}/addresses/new")
def address_create(party_id: int, request: Request, data: FormData = Depends(_form), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    party = _party(db, party_id)
    values, errors = _validate_address(data)
    if errors:
        return _address_page(request, party, None, values, errors, 422)
    a = PartyAddress(party_id=party.id, is_default=not party.usable_addresses, **{key: v or None for key, v in values.items()})
    db.add(a)
    db.flush()
    db.refresh(party)
    _tidy_default(party)
    audit(db, ctx, "party_address_created", entity_type="party_address", entity_id=a.id, details={"customer": party.name, "address": a.label})
    db.commit()
    return redirect(f"/masters/parties/{party.id}", f"Added the address {a.label}.")


@router.get("/parties/{party_id}/addresses/{address_id}")
def address_edit(party_id: int, address_id: int, request: Request, db: Session = Depends(get_db)):
    party = _party(db, party_id)
    a = _address(db, party, address_id)
    values = {f["name"]: getattr(a, f["name"]) or "" for f in ADDRESS_FIELDS}
    return _address_page(request, party, a, values, {})


@router.post("/parties/{party_id}/addresses/{address_id}")
def address_update(party_id: int, address_id: int, request: Request, data: FormData = Depends(_form), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    party = _party(db, party_id)
    a = _address(db, party, address_id)
    values, errors = _validate_address(data)
    active = data.get("is_active") == "on"
    if errors:
        return _address_page(request, party, a, values, errors, 422)
    for key, v in values.items():
        setattr(a, key, v or None)
    a.is_active = active
    _tidy_default(party)
    audit(db, ctx, "party_address_updated", entity_type="party_address", entity_id=a.id, details={"customer": party.name, "address": a.label, "active": active})
    db.commit()
    return redirect(f"/masters/parties/{party.id}", f"Saved the address {a.label}.")


@router.post("/parties/{party_id}/addresses/{address_id}/default")
def address_default(party_id: int, address_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    party = _party(db, party_id)
    a = _address(db, party, address_id)
    if not a.is_active:
        return redirect(f"/masters/parties/{party.id}", "An inactive address cannot be the default.", "error")
    for other in party.addresses:
        other.is_default = other is a
    audit(db, ctx, "party_address_default", entity_type="party_address", entity_id=a.id, details={"customer": party.name, "address": a.label})
    db.commit()
    return redirect(f"/masters/parties/{party.id}", f"{a.label} is now the default address.")


@router.post("/parties/{party_id}/addresses/{address_id}/delete")
def address_delete(party_id: int, address_id: int, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    party = _party(db, party_id)
    a = _address(db, party, address_id)
    a.soft_delete(ctx.user.id if ctx.user else None)
    a.is_default = False
    db.flush()
    _tidy_default(party)
    audit(db, ctx, "party_address_deleted", entity_type="party_address", entity_id=a.id, details={"customer": party.name, "address": a.label})
    db.commit()
    return redirect(f"/masters/parties/{party.id}", f"Removed the address {a.label}. Existing orders keep their copy of it.")
