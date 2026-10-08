"""Normalizer — type coercion for Indian-grouped numbers and mixed date formats."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.email_automation.engine.normalizer import normalize_value


def test_decimal_indian_grouping():
    assert normalize_value("12,34,567.89", "decimal") == Decimal("1234567.89")
    assert normalize_value("1,234", "decimal") == Decimal("1234")
    assert normalize_value("(1,234.50)", "decimal") == Decimal("-1234.50")
    assert normalize_value("\u20b9 10,000", "decimal") == Decimal("10000")
    assert normalize_value("INR 5,000", "decimal") == Decimal("5000")


def test_decimal_blanks_and_junk_become_none():
    assert normalize_value("", "decimal") is None
    assert normalize_value(None, "decimal") is None
    assert normalize_value("-", "decimal") is None
    assert normalize_value("n/a", "decimal") is None


def test_decimal_passes_through_real_numbers():
    assert normalize_value(1234.56, "decimal") == Decimal("1234.56")
    assert normalize_value(Decimal("1"), "decimal") == Decimal("1")


def test_int_coerce():
    assert normalize_value("12,345", "int") == 12345
    assert normalize_value("", "int") is None


def test_date_multiple_formats():
    assert normalize_value("2026-03-31", "date") == dt.date(2026, 3, 31)
    assert normalize_value("31-03-2026", "date") == dt.date(2026, 3, 31)
    assert normalize_value("31/03/2026", "date") == dt.date(2026, 3, 31)
    assert normalize_value("31-Mar-2026", "date") == dt.date(2026, 3, 31)
    d = dt.datetime(2026, 3, 31, 10, 0)
    assert normalize_value(d, "date") == dt.date(2026, 3, 31)
    assert normalize_value("definitely not a date", "date") is None


def test_bool():
    assert normalize_value("Y", "bool") is True
    assert normalize_value("no", "bool") is False
    assert normalize_value("maybe", "bool") is None


def test_str_strips_nbsp():
    assert normalize_value("  a\u00a0b  ", "str") == "a b"
    assert normalize_value("", "str") is None


def test_raw_passthrough():
    assert normalize_value("x", "raw") == "x"
    assert normalize_value(7, "raw") == 7
