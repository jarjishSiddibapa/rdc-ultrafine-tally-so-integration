"""Reports: a catalogue of ready-made reports, each with period, search, filters, sorting and downloads."""
from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.utils import today_ist
from app.deps import current_user, get_ctx
from app.models import Role, User
from app.routers._lists import run_list
from app.services import reports as rep
from app.services.audit import Ctx
from app.services.listing import parse_date
from app.web import render

router = APIRouter(prefix="/reports")

GROUPS = ["Orders", "Tally", "Summaries", "Administration"]


@router.get("")
def index(request: Request, user: User = Depends(current_user)):
    mine = [r for r in rep.REPORTS if user.role in r.roles]
    groups = [(g, [r for r in mine if r.group == g]) for g in GROUPS]
    exports = []
    if user.role in (Role.ACCOUNTS, Role.ADMIN):
        exports += [("Customers", "/masters/parties", "Every customer with GSTIN, state and timestamps."),
                    ("Items", "/masters/items", "Items with HSN, unit and GST rate."),
                    ("Plants", "/masters/plants", "Plants, their state and how many orders each supplied.")]
    if user.role is Role.ADMIN:
        exports += [("Users", "/admin/users", "Users, roles, assigned plants, last sign-in."),
                    ("Audit log", "/admin/audit", "Every recorded action with time, user and IP address."),
                    ("Sign-in history", "/admin/audit?f_action=login", "Successful sign-ins."),
                    ("Failed sign-ins", "/admin/audit?f_action=login_failed", "Failed sign-in attempts."),
                    ("Tally queue", "/admin/tally", "Every approved order queued for Tally.")]
    return render(request, "reports/index.html", {"groups": [g for g in groups if g[1]], "exports": exports})


@router.get("/{slug}")
def view(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user), ctx: Ctx = Depends(get_ctx)):
    report = rep.BY_SLUG.get(slug)
    if report is None or user.role not in report.roles:
        raise HTTPException(404, "Report not found.")
    lo, hi = parse_date(request.query_params.get("date_from")), parse_date(request.query_params.get("date_to"))
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    built = report.build(db, user, lo, hi)
    spec = replace(built.spec, extras=("date_from", "date_to"))

    def prepare(st) -> None:
        st.extras = {k: v.isoformat() for k, v in (("date_from", lo), ("date_to", hi)) if v}

    info = [("Period applies to", report.period_on), ("From", lo.strftime("%d %b %Y") if lo else "start"), ("To", hi.strftime("%d %b %Y") if hi else "today")]
    st, page, download = run_list(request, db, user, ctx, built.stmt, spec, report.title, entities=built.entities, info=info, prepare=prepare)
    if download:
        return download
    return render(request, "reports/view.html", {
        "report": report, "st": st, "page": page, "lo": lo, "hi": hi,
        "presets": [(label, a, b) for label, a, b in rep.presets(today_ist())],
    })
