"""GST computation. Pure functions, no I/O, easy to test.

Rule (from the SOP): billing address in the *same state* as the supplying plant -> Output CGST + Output SGST
(half the rate each); billing address *outside* that state -> Output IGST at the full rate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.utils import q2
from app.models.enums import TaxMode


@dataclass(slots=True)
class TaxLine:
    ledger_key: str   # "cgst" | "sgst" | "igst"
    rate: Decimal     # rate of this component (e.g. 9 for 18% GST split into CGST+SGST)
    base: Decimal
    amount: Decimal


@dataclass(slots=True)
class Totals:
    mode: TaxMode
    subtotal: Decimal
    cgst: Decimal = Decimal("0.00")
    sgst: Decimal = Decimal("0.00")
    igst: Decimal = Decimal("0.00")
    lines: list[TaxLine] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.cgst + self.sgst + self.igst


def tax_mode_for(plant_state: str, billing_state: str) -> TaxMode:
    return TaxMode.CGST_SGST if plant_state == billing_state else TaxMode.IGST


def compute_totals(items: list[tuple[Decimal, Decimal]], mode: TaxMode) -> Totals:
    """`items` is a list of (taxable_amount, gst_rate_percent). Tax is computed per rate group."""
    by_rate: dict[Decimal, Decimal] = {}
    for amount, rate in items:
        by_rate[rate] = by_rate.get(rate, Decimal("0")) + amount

    t = Totals(mode=mode, subtotal=q2(sum(by_rate.values(), Decimal("0"))))
    for rate in sorted(by_rate):
        base = q2(by_rate[rate])
        if rate == 0:
            continue
        if mode is TaxMode.CGST_SGST:
            half = rate / 2
            comp = q2(base * half / 100)
            t.cgst += comp
            t.sgst += comp
            t.lines.append(TaxLine("cgst", half, base, comp))
            t.lines.append(TaxLine("sgst", half, base, comp))
        else:
            comp = q2(base * rate / 100)
            t.igst += comp
            t.lines.append(TaxLine("igst", rate, base, comp))
    t.cgst, t.sgst, t.igst = q2(t.cgst), q2(t.sgst), q2(t.igst)
    return t


def ledger_name(template: str, rate: Decimal) -> str:
    """'Output CGST {rate}%' + 9 -> 'Output CGST 9%'. Templates without {rate} are returned as-is."""
    if "{rate}" not in template:
        return template
    r = rate.normalize()
    text = format(r, "f")
    return template.replace("{rate}", text)
