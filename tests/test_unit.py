from datetime import date
from decimal import Decimal

from app.core.security import csrf_valid, hash_password, new_csrf_seed, csrf_token_for, password_problems, verify_password
from app.core.utils import financial_year, format_inr, parse_decimal
from app.models.enums import TaxMode
from app.services.tax import compute_totals, ledger_name, tax_mode_for
from app.web import safe_next


def test_same_state_splits_gst_into_cgst_and_sgst():
    t = compute_totals([(Decimal("1250000.00"), Decimal("18"))], tax_mode_for("24", "24"))
    assert t.mode is TaxMode.CGST_SGST
    assert (t.cgst, t.sgst, t.igst) == (Decimal("112500.00"), Decimal("112500.00"), Decimal("0.00"))
    assert t.total == Decimal("1475000.00")


def test_other_state_uses_igst_at_full_rate():
    t = compute_totals([(Decimal("1250000.00"), Decimal("18"))], tax_mode_for("24", "27"))
    assert t.mode is TaxMode.IGST
    assert (t.cgst, t.sgst, t.igst) == (Decimal("0.00"), Decimal("0.00"), Decimal("225000.00"))


def test_mixed_rates_are_taxed_per_rate_group():
    t = compute_totals([(Decimal("1000.00"), Decimal("5")), (Decimal("1000.00"), Decimal("18"))], TaxMode.IGST)
    assert t.igst == Decimal("230.00")
    assert {(l.rate, l.amount) for l in t.lines} == {(Decimal("5"), Decimal("50.00")), (Decimal("18"), Decimal("180.00"))}


def test_zero_rate_has_no_tax_lines():
    t = compute_totals([(Decimal("500.00"), Decimal("0"))], TaxMode.CGST_SGST)
    assert t.lines == [] and t.total == Decimal("500.00")


def test_rounding_is_half_up_to_paise():
    t = compute_totals([(Decimal("0.05"), Decimal("18"))], TaxMode.IGST)
    assert t.igst == Decimal("0.01")  # 0.009 -> 0.01


def test_ledger_name_template():
    assert ledger_name("Output CGST {rate}%", Decimal("9.00")) == "Output CGST 9%"
    assert ledger_name("Output CGST {rate}%", Decimal("2.5")) == "Output CGST 2.5%"
    assert ledger_name("Output IGST", Decimal("18")) == "Output IGST"


def test_indian_number_format():
    assert format_inr(Decimal("1234567.5")) == "12,34,567.50"
    assert format_inr(Decimal("999")) == "999.00"
    assert format_inr(Decimal("100000")) == "1,00,000.00"
    assert format_inr(Decimal("-1500.5")) == "-1,500.50"


def test_decimal_parsing_rejects_junk():
    assert parse_decimal("1,25,000.50", places=2) == Decimal("125000.50")
    for bad in ("", "abc", "NaN", "Infinity", "1.234", "1e5x"):
        assert parse_decimal(bad, places=2) is None
    assert parse_decimal("100.123", places=3) == Decimal("100.123")


def test_financial_year_rolls_over_in_april():
    assert financial_year(date(2026, 3, 31)) == "25-26"
    assert financial_year(date(2026, 4, 1)) == "26-27"


def test_password_hashing_and_policy():
    h = hash_password("Str0ngPassw0rd!")
    assert h.startswith("$argon2id$") and verify_password("Str0ngPassw0rd!", h)
    assert not verify_password("wrong", h) and not verify_password("x", None)
    assert password_problems("short1A")
    assert password_problems("alllowercase123")
    assert not password_problems("Reasonable-Pass-42", "bob@example.com")
    assert password_problems("bobsmith-Pass-42", "bobsmith@example.com")


def test_csrf_token_binding():
    seed = new_csrf_seed()
    assert csrf_valid(seed, csrf_token_for(seed))
    assert not csrf_valid(seed, csrf_token_for(new_csrf_seed()))
    assert not csrf_valid(None, "x") and not csrf_valid(seed, None)


def test_open_redirect_is_blocked():
    assert safe_next("/orders?x=1") == "/orders?x=1"
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)", "/a\r\nSet-Cookie: x=1", None):
        assert safe_next(bad) == "/"
