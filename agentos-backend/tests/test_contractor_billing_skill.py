"""Deterministic HR contractor billing skill — no DB."""

from app.skills.hr.contractor_billing import (
    format_contractor_billing_detail,
    run_contractor_billing_review,
)


def test_clean_billing():
    r = run_contractor_billing_review(
        {
            "contractor_name": "Priya S",
            "hours_worked": "160",
            "hourly_rate_inr": "1000",
            "claimed_amount_inr": "160000.00",
        }
    )
    assert r["matched"] is True
    assert r["amount_matches_computed"] is True
    assert r["confidence"] >= 90


def test_amount_mismatch_flags():
    r = run_contractor_billing_review(
        {
            "hours_worked": "80",
            "hourly_rate_inr": "1000",
            "claimed_amount_inr": "90000",
        }
    )
    assert r["matched"] is False
    assert r["discrepancies"]


def test_format_detail():
    r = run_contractor_billing_review({"contractor_name": "Acme Staffing"})
    text = format_contractor_billing_detail(r)
    assert "Acme Staffing" in text
