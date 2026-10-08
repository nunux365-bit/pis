"""Deterministic invoice skill — no DB."""

from app.skills.finance.invoice_match import (
    format_match_for_approval_detail,
    run_invoice_3way_match,
)


def test_clean_match():
    r = run_invoice_3way_match(
        {
            "vendor": "Acme",
            "invoice_total": "100000.00",
            "po_total": "100000.00",
            "grn_received_value": "100000.00",
            "taxable_value": "84745.76",
            "gst_rate_pct": "18",
        }
    )
    assert r["matched"] is True
    assert r["gst_computation_ok"] is True
    assert r["confidence"] >= 90


def test_variance_flags():
    r = run_invoice_3way_match(
        {"invoice_total": "100500", "po_total": "100000", "grn_received_value": "100000"}
    )
    assert r["matched"] is False
    assert r["discrepancies"]
    assert "invoice_vs_po" in r["discrepancies"][0]["field"]


def test_format_detail_contains_vendor():
    r = run_invoice_3way_match({"vendor": "X"})
    text = format_match_for_approval_detail(r)
    assert "X" in text
    assert "₹" in text or "PO" in text
