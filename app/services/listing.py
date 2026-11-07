"""One engine for every list in the app: search, per-column filters, sorting, column choice,
pagination, totals and CSV / Excel downloads.

A page describes its table with a ``ListSpec`` (a list of ``Col``). The engine reads the query string
into a ``ListState`` (everything is validated, nothing the browser sends reaches SQL unchecked),
applies it to a SQLAlchemy ``select`` and returns one page of rows. The same state drives the
download, so what you see is exactly what you export.
"""
from __future__ import annotations

import csv
import enum
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.utils import IST, to_ist

PER_PAGE_CHOICES = (10, 20, 25, 50, 100, 200)
DEFAULT_PER_PAGE = 20
EXPORT_LIMIT = 50_000
MAX_TEXT = 80

NUM_KINDS = ("number", "money", "qty")
RANGE_KINDS = ("date", "datetime", "number", "money", "qty", "duration")


# --------------------------------------------------------------------------- column + spec
@dataclass(frozen=True)
class Col:
    key: str
    label: str
    expr: Any = None                       # SQL expression used to sort / filter / search / total
    kind: str = "text"                     # text | choice | bool | date | datetime | number | money | qty | duration
    choices: tuple[tuple[str, str], ...] = ()
    coerce: Callable[[str], Any] | None = None       # choice value -> what the column is compared with
    where: Callable[[str], Any] | None = None        # fully custom filter for a choice (e.g. computed status)
    search: bool = False                   # part of the free-text box
    sortable: bool = True
    filterable: bool = True
    default: bool = True                   # shown in the table unless the user changes columns
    total: bool = False                    # add up across every matching row
    value: Callable[[Any], Any] | None = None        # row -> display / export value
    hint: str = ""

    @property
    def is_num(self) -> bool:
        return self.kind in NUM_KINDS or self.kind == "duration"

    @property
    def first_dir(self) -> str:
        return "desc" if self.kind in ("date", "datetime", "number", "money", "qty", "duration") else "asc"

    @property
    def can_sort(self) -> bool:
        return self.sortable and self.expr is not None

    @property
    def can_filter(self) -> bool:
        return self.filterable and (self.expr is not None or self.where is not None)

    def raw(self, row: Any) -> Any:
        if self.value is not None:
            return self.value(row)
        if isinstance(row, Mapping):
            return row.get(self.key)
        return getattr(row, self.key, None)


@dataclass
class ListSpec:
    cols: list[Col]
    default_sort: str
    default_dir: str = "desc"
    extras: tuple[str, ...] = ()           # other query parameters a page owns and the links must keep
    tiebreak: Any = None                   # extra ORDER BY so pages never repeat or skip rows
    defaults: dict[str, str] = field(default_factory=dict)   # filter defaults, e.g. {"status": "pending_approval"}

    def col(self, key: str) -> Col | None:
        return next((c for c in self.cols if c.key == key), None)


# --------------------------------------------------------------------------- query string -> state
def _clean(v: str | None, limit: int = MAX_TEXT) -> str:
    return "".join(ch for ch in (v or "") if ch.isprintable()).strip()[:limit]


def parse_date(v: str | None) -> date | None:
    try:
        return date.fromisoformat((v or "").strip()) if v else None
    except ValueError:
        return None


def _parse_num(v: str | None) -> Decimal | None:
    s = (v or "").strip().replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() and abs(d) < Decimal("1e15") else None


@dataclass
class ListState:
    spec: ListSpec
    q: str = ""
    sort: str = ""
    dir: str = "desc"
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    raw: dict[str, str] = field(default_factory=dict)       # validated filter inputs, keyed by parameter name
    filters: dict[str, dict[str, Any]] = field(default_factory=dict)
    cols: list[str] = field(default_factory=list)           # explicitly chosen columns (empty = defaults)
    extras: dict[str, str] = field(default_factory=dict)
    export: str = ""

    # ---- which columns are on screen
    @property
    def default_keys(self) -> list[str]:
        return [c.key for c in self.spec.cols if c.default]

    @property
    def visible(self) -> list[Col]:
        chosen = set(self.cols) if self.cols else set(self.default_keys)
        return [c for c in self.spec.cols if c.key in chosen]

    @property
    def visible_keys(self) -> set[str]:
        return {c.key for c in self.visible}

    # ---- url building (any change except page resets to page 1)
    def _params(self) -> dict[str, Any]:
        p: dict[str, Any] = {}
        if self.q:
            p["q"] = self.q
        if (self.sort, self.dir) != (self.spec.default_sort, self.spec.default_dir):
            p["sort"], p["dir"] = self.sort, self.dir
        if self.per_page != DEFAULT_PER_PAGE:
            p["per_page"] = self.per_page
        if self.cols and set(self.cols) != set(self.default_keys):
            p["cols"] = list(self.cols)
        p.update(self.raw)
        p.update(self.extras)
        return p

    def url(self, **kw: Any) -> str:
        p = self._params()
        for k, v in kw.items():
            if v is None or v == "":
                p.pop(k, None)
            else:
                p[k] = v
        if "page" not in kw and self.page > 1:
            p.pop("page", None)
        if "page" in kw and kw["page"] in (1, "1"):
            p.pop("page", None)
        return "?" + urlencode(p, doseq=True)

    def clear_url(self) -> str:
        keep: dict[str, str] = dict(self.extras)
        keep.update({f"f_{k}": "all" for k in self.spec.defaults})   # "clear" really means everything, not the page's default view
        return "?" + urlencode(keep)

    def pairs(self, exclude: tuple[str, ...] = ()) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for k, v in self._params().items():
            if k in exclude:
                continue
            if isinstance(v, list):
                out.extend((k, str(x)) for x in v)
            else:
                out.append((k, str(v)))
        return out

    # ---- what is filtered right now
    def chips(self) -> list[tuple[str, str]]:
        """(description, url that removes it) for every active search / filter."""
        out: list[tuple[str, str]] = []
        if self.q:
            out.append((f"Search: {self.q}", self.url(q=None)))
        for c in self.spec.cols:
            if not c.can_filter:
                continue
            name = f"f_{c.key}"
            if c.kind in RANGE_KINDS:
                lo, hi = self.raw.get(f"{name}_from"), self.raw.get(f"{name}_to")
                if lo or hi:
                    txt = f"{c.label}: {lo or 'any'} to {hi or 'any'}" if lo and hi else (f"{c.label}: from {lo}" if lo else f"{c.label}: up to {hi}")
                    out.append((txt, self.url(**{f"{name}_from": None, f"{name}_to": None})))
            elif name in self.raw and self.raw[name] != "all":
                v = self.raw[name]
                shown = dict(c.choices).get(v, v) if c.kind in ("choice", "bool") else v
                out.append((f"{c.label}: {shown}", self.url(**{name: ("all" if c.key in self.spec.defaults else None)})))
        return out

    @property
    def filter_count(self) -> int:
        return len([1 for t, _ in self.chips() if not t.startswith("Search:")])

    def describe(self) -> list[tuple[str, str]]:
        rows = [("Search", self.q)] if self.q else []
        rows += [tuple(t.split(": ", 1)) for t, _ in self.chips() if not t.startswith("Search:")]  # type: ignore[misc]
        rows.append(("Sorted by", f"{(self.spec.col(self.sort).label if self.spec.col(self.sort) else self.sort)} ({'descending' if self.dir == 'desc' else 'ascending'})"))
        return rows


def parse(qp: Mapping[str, str] | Any, spec: ListSpec) -> ListState:
    """Read and validate the query string. Unknown or malformed values are ignored, never trusted."""
    getlist = qp.getlist if hasattr(qp, "getlist") else (lambda k: [qp[k]] if k in qp else [])
    st = ListState(spec=spec)
    st.q = _clean(qp.get("q"))
    st.sort = qp.get("sort") if (spec.col(qp.get("sort") or "") and spec.col(qp.get("sort") or "").can_sort) else spec.default_sort  # type: ignore[union-attr]
    st.dir = qp.get("dir") if qp.get("dir") in ("asc", "desc") else spec.default_dir
    try:
        st.page = max(1, min(int(qp.get("page", 1)), 100_000))
    except (TypeError, ValueError):
        st.page = 1
    try:
        pp = int(qp.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        pp = DEFAULT_PER_PAGE
    st.per_page = pp if pp in PER_PAGE_CHOICES else DEFAULT_PER_PAGE
    st.cols = [k for k in getlist("cols") if spec.col(k)]
    st.export = qp.get("export") if qp.get("export") in ("csv", "xlsx") else ""
    for k in spec.extras:
        v = _clean(qp.get(k), 40)
        if v:
            st.extras[k] = v

    for c in spec.cols:
        if not c.can_filter:
            continue
        name = f"f_{c.key}"
        if c.kind in RANGE_KINDS:
            conv = parse_date if c.kind in ("date", "datetime") else _parse_num
            f: dict[str, Any] = {}
            for side in ("from", "to"):
                raw = _clean(qp.get(f"{name}_{side}"), 24)
                val = conv(raw)
                if val is not None:
                    f[side] = val
                    st.raw[f"{name}_{side}"] = val.isoformat() if isinstance(val, date) else format(val.normalize(), "f")
            if f:
                st.filters[c.key] = f
        else:
            v = qp.get(name)
            if v is None and c.key in spec.defaults:
                v = spec.defaults[c.key]
            v = _clean(v) if v is not None else ""
            if not v:
                continue
            if v == "all" and c.key in spec.defaults:
                st.raw[name] = "all"
            elif c.kind == "choice":
                if v in {x for x, _ in c.choices}:
                    st.filters[c.key] = {"eq": v}
                    st.raw[name] = v
            elif c.kind == "bool":
                if v in ("1", "0"):
                    st.filters[c.key] = {"eq": v == "1"}
                    st.raw[name] = v
            else:
                st.filters[c.key] = {"like": v}
                st.raw[name] = v
    return st


# --------------------------------------------------------------------------- state -> SQL
def ist_day_start_utc(d: date) -> datetime:
    """Midnight IST on ``d`` as a naive UTC datetime (how the database stores times)."""
    return datetime.combine(d, time.min, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def range_conditions(expr: Any, kind: str, lo: Any = None, hi: Any = None) -> list[Any]:
    out = []
    if kind == "datetime":
        if lo is not None:
            out.append(expr >= ist_day_start_utc(lo))
        if hi is not None:
            out.append(expr < ist_day_start_utc(hi + timedelta(days=1)))
    else:
        if lo is not None:
            out.append(expr >= lo)
        if hi is not None:
            out.append(expr <= hi)
    return out


def apply_filters(stmt: Any, st: ListState) -> Any:
    spec = st.spec
    # free text: every word must appear in at least one searchable column
    cols = [c for c in spec.cols if c.search and c.expr is not None]
    if st.q and cols:
        for term in st.q.split()[:6]:
            stmt = stmt.where(or_(*[c.expr.contains(term, autoescape=True) for c in cols]))
    for key, f in st.filters.items():
        c = spec.col(key)
        if c is None:
            continue
        if c.kind == "choice":
            if c.where is not None:
                stmt = stmt.where(c.where(f["eq"]))
            else:
                stmt = stmt.where(c.expr == (c.coerce(f["eq"]) if c.coerce else f["eq"]))
        elif c.kind == "bool":
            stmt = stmt.where(c.where(f["eq"]) if c.where is not None else c.expr == f["eq"])
        elif c.kind in RANGE_KINDS:
            for cond in range_conditions(c.expr, c.kind, f.get("from"), f.get("to")):
                stmt = stmt.where(cond)
        else:
            stmt = stmt.where(c.expr.contains(f["like"], autoescape=True))
    return stmt


def apply_sort(stmt: Any, st: ListState) -> Any:
    c = st.spec.col(st.sort)
    if c is None or c.expr is None:
        return stmt
    order = c.expr.desc() if st.dir == "desc" else c.expr.asc()
    tie = st.spec.tiebreak
    return stmt.order_by(order, tie) if tie is not None else stmt.order_by(order)


# --------------------------------------------------------------------------- paging
@dataclass
class Page:
    rows: list[Any]
    total: int
    page: int
    pages: int
    per_page: int
    totals: dict[str, Any] = field(default_factory=dict)

    @property
    def start(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def end(self) -> int:
        return min(self.total, self.page * self.per_page)

    def window(self, span: int = 2) -> list[int | None]:
        """Page numbers to show, with None where numbers are skipped: 1 … 4 5 [6] 7 8 … 20."""
        nums = sorted({1, self.pages, *range(max(1, self.page - span), min(self.pages, self.page + span) + 1)})
        out: list[int | None] = []
        for n in nums:
            if out and out[-1] is not None and n - out[-1] > 1:  # type: ignore[operator]
                out.append(None)
            out.append(n)
        return out


def _count(db: Session, stmt: Any) -> int:
    return db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()


def column_totals(db: Session, stmt: Any, st: ListState) -> dict[str, Any]:
    cols = [c for c in st.visible if c.total and c.expr is not None]
    if not cols:
        return {}
    agg = stmt.order_by(None).with_only_columns(*[func.coalesce(func.sum(c.expr), 0) for c in cols], maintain_column_froms=True)
    row = db.execute(agg).one()
    return {c.key: v for c, v in zip(cols, row)}


def paginate(db: Session, base: Any, st: ListState, *, entities: bool = True) -> Page:
    """Filter, sort and cut one page. ``entities`` = the select returns ORM objects (not plain rows)."""
    filtered = apply_filters(base, st)
    total = _count(db, filtered)
    pages = max(1, math.ceil(total / st.per_page))
    st.page = min(st.page, pages)
    stmt = apply_sort(filtered, st).limit(st.per_page).offset((st.page - 1) * st.per_page)
    res = db.execute(stmt)
    rows = list(res.unique().scalars().all()) if entities else list(res.all())
    return Page(rows, total, st.page, pages, st.per_page, column_totals(db, filtered, st))


def fetch_all(db: Session, base: Any, st: ListState, *, entities: bool = True) -> list[Any]:
    stmt = apply_sort(apply_filters(base, st), st).limit(EXPORT_LIMIT)
    res = db.execute(stmt)
    return list(res.unique().scalars().all()) if entities else list(res.all())


# --------------------------------------------------------------------------- downloads
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _plain(c: Col, row: Any) -> Any:
    v = c.raw(row)
    if isinstance(v, enum.Enum):
        v = getattr(v, "label", v.value)
    if isinstance(v, datetime):
        local = to_ist(v)
        return local.replace(tzinfo=None) if local else None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if c.kind == "duration" and isinstance(v, (int, float, Decimal)):
        return fmt_duration(v)
    return v


def fmt_duration(seconds: Any) -> str:
    if seconds is None or seconds == "":
        return ""
    s = int(seconds)
    if s < 0:
        s = 0
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    parts = [f"{d}d"] if d else []
    if h or d:
        parts.append(f"{h}h")
    if m or h or d:
        parts.append(f"{m}m")
    if not d:
        parts.append(f"{sec}s")
    return " ".join(parts)


def _csv_cell(c: Col, v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, str) and v.startswith(_FORMULA):
        return "'" + v          # stops Excel treating text as a formula
    return v


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def export_response(fmt: str, title: str, st: ListState, rows: list[Any], *, who: str, info: list[tuple[str, str]] | None = None) -> Response:
    """CSV (opens in Excel, UTF-8) or a formatted .xlsx with a second sheet describing the report."""
    cols = [c for c in st.spec.cols]
    now = datetime.now(IST)
    stamp = now.strftime("%Y%m%d-%H%M")
    fname = f"ultrafine-{_safe_name(title)}-{stamp}.{fmt}"
    meta = [("Report", title), ("Generated on", now.strftime("%d %b %Y, %I:%M:%S %p IST")), ("Generated by", who),
            ("Rows", str(len(rows)))] + (info or []) + st.describe()
    headers = {"Content-Disposition": f'attachment; filename="{fname}"', "X-Content-Type-Options": "nosniff"}
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c.label for c in cols])
        for r in rows:
            w.writerow([_csv_cell(c, _plain(c, r)) for c in cols])
        w.writerow([])
        w.writerow(["Report", title])
        w.writerow(["Generated on", now.strftime("%d-%m-%Y %H:%M:%S IST")])
        w.writerow(["Generated by", who])
        for k, v in (info or []) + st.describe():
            w.writerow([k, v])
        return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8", headers=headers)

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = (title[:28] or "Report")
    head_fill = PatternFill("solid", fgColor="089604")
    ws.append([c.label for c in cols])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    widths = [len(c.label) for c in cols]
    for r in rows:
        line = []
        for i, c in enumerate(cols):
            v = _plain(c, r)
            if isinstance(v, Decimal):
                v = float(v)
            line.append(v)
            widths[i] = max(widths[i], min(len(str(v)) if v is not None else 0, 48))
        ws.append(line)
    fmts = {"money": "#,##0.00", "qty": "#,##0.###", "date": "dd-mmm-yyyy", "datetime": "dd-mmm-yyyy hh:mm:ss", "number": "#,##0.##"}
    for i, c in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(10, min(widths[i - 1] + 3, 52))
        nf = fmts.get(c.kind)
        if nf:
            for cell in ws.iter_cols(min_col=i, max_col=i, min_row=2, max_row=ws.max_row):
                for x in cell:
                    x.number_format = nf
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    sheet = wb.create_sheet("Report info")
    for k, v in meta:
        sheet.append([k, v])
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 70
    for cell in sheet["A"]:
        cell.font = Font(bold=True)
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)
