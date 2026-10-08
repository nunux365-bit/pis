"""ingest: coercion of attendance_required for staffing rate_lines + TACO invoice-level admin."""

from __future__ import annotations

from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_PCT_BILLING_RULE_KEY,
    OHC_INVOICE_ADMIN_ROLE_CODE,
)
from app.agents.o2c_ohc.ingest_helpers import _normalize_rate_lines_attendance_required
from app.agents.o2c_ohc.ingest_strategies import (
    apply_profile_rate_line_normalization,
    normalize_taco_invoice_level_admin_lines,
)


def test_mo_mbbs_per_visit_forces_attendance_required() -> None:
    rates = [
        {
            "role_code": "MO_MBBS",
            "billing_model": "per_visit",
            "attendance_required": False,
        }
    ]
    assert _normalize_rate_lines_attendance_required(rates) is True
    assert rates[0]["attendance_required"] is True


def test_fixed_monthly_ambulance_unchanged() -> None:
    rates = [
        {
            "role_code": "ACLS_AMBULANCE",
            "billing_model": "fixed_monthly",
            "attendance_required": False,
        }
    ]
    assert _normalize_rate_lines_attendance_required(rates) is False
    assert rates[0]["attendance_required"] is False


def test_nurse_rate_attendance_forced_true_even_if_false() -> None:
    rates = [
        {
            "role_code": "NURSE_GNM_SHIFT",
            "billing_model": "rate_attendance",
            "attendance_required": False,
        }
    ]
    assert _normalize_rate_lines_attendance_required(rates) is True
    assert rates[0]["attendance_required"] is True


def test_already_true_returns_false() -> None:
    rates = [{"role_code": "MO_MBBS", "billing_model": "per_visit", "attendance_required": True}]
    assert _normalize_rate_lines_attendance_required(rates) is False


def test_sr_mo_prefix_forced() -> None:
    rates = [{"role_code": "SR_MO_MBBS", "billing_model": "per_visit", "attendance_required": False}]
    assert _normalize_rate_lines_attendance_required(rates) is True
    assert rates[0]["attendance_required"] is True


def test_ambulance_never_forced_even_if_billing_model_wrong() -> None:
    rates = [
        {
            "role_code": "ACLS_AMBULANCE",
            "billing_model": "rate_attendance",
            "attendance_required": False,
        }
    ]
    assert _normalize_rate_lines_attendance_required(rates) is False
    assert rates[0]["attendance_required"] is False


def test_bmw_disposal_never_forced() -> None:
    rates = [{"role_code": "BMW_DISPOSAL", "billing_model": "rate_attendance", "attendance_required": False}]
    assert _normalize_rate_lines_attendance_required(rates) is False


def test_medicines_never_forced() -> None:
    rates = [{"role_code": "MEDICINES", "billing_model": "rate_attendance", "attendance_required": False}]
    assert _normalize_rate_lines_attendance_required(rates) is False


def test_taco_strips_per_line_sc_and_adds_invoice_admin_line() -> None:
    rates = [
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "service_charge_type": "percentage",
            "service_charge_value": 10,
        },
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "NURSE_GNM_SHIFT",
            "service_charge_type": "none",
            "service_charge_value": None,
        },
    ]
    em = {"contract_prompt_profile": "taco"}
    assert normalize_taco_invoice_level_admin_lines(rates, extraction_metadata=em) is True
    assert rates[0]["service_charge_type"] == "none"
    assert rates[0]["service_charge_value"] is None
    assert rates[1]["service_charge_type"] == "none"
    admin = [r for r in rates if r.get("role_code") == OHC_INVOICE_ADMIN_ROLE_CODE]
    assert len(admin) == 1
    br = admin[0]["billing_rules"]
    assert br[INVOICE_ADMIN_PCT_BILLING_RULE_KEY] == 10.0
    assert admin[0]["billing_model"] == "fixed_monthly"


def test_taco_defaults_10_invoice_admin_when_no_per_line_sc() -> None:
    rates = [
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "service_charge_type": "none",
            "service_charge_value": None,
        },
    ]
    em = {"contract_prompt_profile": "taco"}
    assert normalize_taco_invoice_level_admin_lines(rates, extraction_metadata=em) is True
    admin = [r for r in rates if r.get("role_code") == OHC_INVOICE_ADMIN_ROLE_CODE]
    assert len(admin) == 1
    assert admin[0]["billing_rules"][INVOICE_ADMIN_PCT_BILLING_RULE_KEY] == 10.0


def test_taco_mixed_per_line_pct_strips_but_no_synthetic_admin_line() -> None:
    rates = [
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "service_charge_type": "percentage",
            "service_charge_value": 10,
        },
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "NURSE_GNM_SHIFT",
            "service_charge_type": "percentage",
            "service_charge_value": 12,
        },
    ]
    em = {"contract_prompt_profile": "taco"}
    assert normalize_taco_invoice_level_admin_lines(rates, extraction_metadata=em) is True
    assert not any(r.get("role_code") == OHC_INVOICE_ADMIN_ROLE_CODE for r in rates)


def test_taco_normalization_via_billing_profile_without_prompt_profile() -> None:
    rates = [
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "service_charge_type": "none",
            "service_charge_value": None,
        },
    ]
    em: dict = {}
    assert (
        apply_profile_rate_line_normalization(
            rates, billing_profile="taco", extraction_metadata=em
        )
        is True
    )
    assert any(r.get("role_code") == OHC_INVOICE_ADMIN_ROLE_CODE for r in rates)


def test_non_taco_profile_no_invoice_admin_normalization() -> None:
    rates = [
        {
            "site_key": "s1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "service_charge_type": "percentage",
            "service_charge_value": 10,
        },
    ]
    em = {"contract_prompt_profile": "generic"}
    assert normalize_taco_invoice_level_admin_lines(rates, extraction_metadata=em) is False
    assert rates[0].get("service_charge_type") == "percentage"
