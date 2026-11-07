"""Time, money and text helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
TWO = Decimal("0.01")
THREE = Decimal("0.001")


def today_ist() -> date:
    return datetime.now(IST).date()


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def financial_year(d: date) -> str:
    """Indian FY label, e.g. 2026-11-04 -> '26-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO, rounding=ROUND_HALF_UP)


def q3(value: Decimal) -> Decimal:
    return value.quantize(THREE, rounding=ROUND_HALF_UP)


def parse_decimal(raw: str | None, *, places: int) -> Decimal | None:
    """Parse user input like '1,25,000.50'. Returns None if invalid / not finite / too many places."""
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    if -d.as_tuple().exponent > places:  # type: ignore[operator]
        return None
    return d


def format_inr(value: Decimal | int | float | None, places: int = 2) -> str:
    """Indian digit grouping: 1234567.5 -> '12,34,567.50'."""
    if value is None:
        return ""
    d = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    s = f"{abs(d):.{places}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{sign}{whole}" + (f".{frac}" if places else "")


def clean_text(value: str | None, max_len: int) -> str:
    """Trim, collapse control characters (keeps newlines/tabs), cap length."""
    if not value:
        return ""
    cleaned = "".join(ch for ch in value if ch in "\n\t" or ch.isprintable())
    return cleaned.strip()[:max_len]
