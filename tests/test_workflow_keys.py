"""Canonical workflow key normalization."""

from app.services.workflow_runner import (
    CANONICAL_HR_CONTRACTOR_BILLING_KEY,
    CANONICAL_INVOICE_WORKFLOW_KEY,
    canonical_workflow_key,
    workflow_eligible_for_post_hitl_erp,
)


def test_invoice_aliases_normalize():
    for k in (
        "invoice_3way_match",
        "INVOICE_3WAY_MATCH",
        "demo_variance_check",
        "finance.invoice_3way",
    ):
        assert canonical_workflow_key(k) == CANONICAL_INVOICE_WORKFLOW_KEY


def test_other_keys_pass_through_lowercased():
    assert canonical_workflow_key("Custom.Flow") == "custom.flow"


def test_empty_becomes_unknown():
    assert canonical_workflow_key("") == "unknown"
    assert canonical_workflow_key("   ") == "unknown"


def test_hr_contractor_aliases():
    for k in ("hr.contractor_billing", "skill.hr.contractor_billing", "DEMO_CONTRACTOR_BILLING"):
        assert canonical_workflow_key(k) == CANONICAL_HR_CONTRACTOR_BILLING_KEY


def test_post_hitl_erp_eligibility():
    assert workflow_eligible_for_post_hitl_erp("invoice_3way_match") is True
    assert workflow_eligible_for_post_hitl_erp("skill.hr.contractor_billing") is True
    assert workflow_eligible_for_post_hitl_erp("pipeline.rate_times_hours") is False
