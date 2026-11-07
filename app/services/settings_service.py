"""Runtime (admin-editable) settings stored in the DB."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppSetting

DEFAULTS: dict[str, str] = {
    "company_name": "",
    "tax_ledger_cgst": "Output CGST",   # may contain {rate}, e.g. "Output CGST {rate}%"
    "tax_ledger_sgst": "Output SGST",
    "tax_ledger_igst": "Output IGST",
}


def get_all(db: Session) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.query(AppSetting).all():
        if row.key in DEFAULTS:
            values[row.key] = row.value
    return values


def set_value(db: Session, key: str, value: str, user_id: int | None) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by_id=user_id))
    else:
        row.value = value
        row.updated_by_id = user_id
