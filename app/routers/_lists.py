"""Glue between a request and the list engine: parse, page or download, and audit every download."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.models import User
from app.services.audit import Ctx, audit
from app.services.listing import ListSpec, ListState, Page, export_response, fetch_all, paginate, parse


def run_list(request: Request, db: Session, user: User, ctx: Ctx, base: Any, spec: ListSpec, title: str, *,
             entities: bool = True, info: list[tuple[str, str]] | None = None,
             prepare: Any = None) -> tuple[ListState, Page | None, Response | None]:
    """Returns (state, page, None) for a screen, or (state, None, download) when ?export=csv|xlsx is present."""
    st = parse(request.query_params, spec)
    if prepare:
        prepare(st)
    if st.export:
        rows = fetch_all(db, base, st, entities=entities)
        audit(db, ctx, "report_downloaded", details={
            "report": title, "format": st.export, "rows": len(rows),
            "filters": "; ".join(f"{k}: {v}" for k, v in st.describe()),
        })
        db.commit()
        return st, None, export_response(st.export, title, st, rows, who=f"{user.full_name} ({user.email})", info=info)
    return st, paginate(db, base, st, entities=entities), None
