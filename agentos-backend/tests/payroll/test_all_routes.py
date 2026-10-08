"""
test_all_routes.py – Unit tests for payroll route-resolution pure functions.

Tests the logic that maps (earning_head, employee_home, initiator) → (hrbp, hod)
without touching the database or HTTP layer.
"""
import pytest
from fastapi import HTTPException

from app.api.routes.payroll import (
    resolve_route,
    get_user_allowed_routes,
    get_user_allowed_modules,
    enforce_sheet_authorization,
    format_month_key,
    serialize_workflow_item,
)
from tests.payroll.conftest import SAMPLE_CONFIG, SAMPLE_MATRIX, _FakeUser


# ─── format_month_key ─────────────────────────────────────────────────────────

def test_format_month_key_valid_datetime():
    month_key, label = format_month_key("2025-06-15 10:30:00")
    assert month_key == "2025-06"
    assert label == "June 2025"


def test_format_month_key_date_only():
    month_key, label = format_month_key("2024-03-01")
    assert month_key == "2024-03"
    assert label == "March 2024"


def test_format_month_key_none_falls_back_to_today():
    from datetime import datetime
    month_key, label = format_month_key(None)
    now = datetime.now()
    assert month_key == now.strftime("%Y-%m")
    assert label == now.strftime("%B %Y")


def test_format_month_key_garbage_falls_back_to_today():
    from datetime import datetime
    month_key, label = format_month_key("not-a-date")
    now = datetime.now()
    assert month_key == now.strftime("%Y-%m")


# ─── serialize_workflow_item ──────────────────────────────────────────────────

def test_serialize_workflow_item_maps_all_fields():
    from unittest.mock import MagicMock
    item = MagicMock()
    item.id = 42
    item.empCode = "E001"
    item.empName = "Alice"
    item.grade = "L2"
    item.designation = "Analyst"
    item.employeeHome = "Dataverse"
    item.type = None
    item.module = "OVERTIME HOURS"
    item.amount = "5000"
    item.overtimeHours = "10"
    item.holidayDate = None
    item.remarks = "test"
    item.status = "MAKER"
    item.hrbpComments = None
    item.hodComments = None
    item.flaggedColumns = []
    item.history = []
    item.initiatorEmail = "alice@1mg.com"
    item.initiatorEmpCode = None
    item.effectiveFrom = None
    item.effectiveTo = None
    item.paymentMonth = None
    item.closedAt = None
    item.closedByEmail = None

    result = serialize_workflow_item(item)

    assert result["id"] == 42
    assert result["empCode"] == "E001"
    assert result["module"] == "OVERTIME HOURS"
    assert result["status"] == "MAKER"
    assert result["initiatorEmail"] == "alice@1mg.com"


# ─── resolve_route ────────────────────────────────────────────────────────────

def test_resolve_route_found_in_matrix():
    hrbp, approver = resolve_route(
        "OVERTIME HOURS", "tanya.agrawal@1mg.com", "Dataverse",
        SAMPLE_CONFIG, SAMPLE_MATRIX,
    )
    assert hrbp == "charvi.sarin@1mg.com"
    assert approver == "nikhil.doegar@1mg.com"


def test_resolve_route_case_insensitive_email():
    hrbp, approver = resolve_route(
        "OVERTIME HOURS", "TANYA.AGRAWAL@1MG.COM", "Dataverse",
        SAMPLE_CONFIG, SAMPLE_MATRIX,
    )
    assert hrbp == "charvi.sarin@1mg.com"


def test_resolve_route_not_in_matrix_falls_back_to_config():
    """When the matrix has no matching rule, falls back to earning_heads config."""
    hrbp, approver = resolve_route(
        "OVERTIME HOURS", "unknown@1mg.com", "Dataverse",
        SAMPLE_CONFIG, [],           # empty matrix → config fallback
    )
    assert hrbp == "charvi.sarin@1mg.com"
    assert approver == "nikhil.doegar@1mg.com"


def test_resolve_route_unknown_module_returns_na():
    hrbp, approver = resolve_route(
        "NONEXISTENT MODULE", "tanya.agrawal@1mg.com", "Dataverse",
        SAMPLE_CONFIG, SAMPLE_MATRIX,
    )
    assert hrbp == "NA"
    assert approver is None


def test_resolve_route_na_hrbp_in_config():
    config = {
        "earning_heads": [
            {
                "name": "DIRECT MODULE",
                "employee_home": "ALL",
                "initiators": ["maker@1mg.com"],
                "hrbp": "NA",
                "approver": "hod@1mg.com",
                "routing_rules": [],
            }
        ],
        "users": [],
    }
    hrbp, approver = resolve_route("DIRECT MODULE", "maker@1mg.com", "Anywhere", config, [])
    assert hrbp.upper() == "NA"
    assert approver == "hod@1mg.com"


def test_resolve_route_matrix_fallback_when_initiator_unlisted():
    """When an initiator is not explicitly listed in matrix.initiators, resolve_route still uses the matrix rule for that module."""
    matrix = [
        {
            "earning_head": "RETENTION BONUS",
            "employee_home": "ALL",
            "initiators": ["kanika.katyal@1mg.com"],
            "hrbps": ["NA"],
            "approvers": ["neeraj.singh@1mg.com"],
        }
    ]
    config = {
        "earning_heads": [
            {
                "name": "RETENTION BONUS",
                "employee_home": "ALL",
                "initiators": ["kanika.katyal@1mg.com"],
                "hrbp": "NA",
                "approver": "rashi.agarwal@1mg.com",
            }
        ]
    }
    # nandini.aggarwal@1mg.com is not in initiators, but should resolve to neeraj.singh@1mg.com from matrix, NOT rashi.agarwal@1mg.com from config
    hrbp, approver = resolve_route("RETENTION BONUS", "nandini.aggarwal@1mg.com", "ALL", config, matrix)
    assert hrbp.upper() == "NA"
    assert approver == "neeraj.singh@1mg.com"



# ─── get_user_allowed_routes ──────────────────────────────────────────────────

def test_allowed_routes_maker_returns_own_modules():
    routes = get_user_allowed_routes(
        "tanya.agrawal@1mg.com", "maker", SAMPLE_CONFIG, SAMPLE_MATRIX
    )
    assert any(r["earning_head"] == "OVERTIME HOURS" for r in routes)


def test_allowed_routes_hrbp_returns_assigned_modules():
    routes = get_user_allowed_routes(
        "charvi.sarin@1mg.com", "hrbp", SAMPLE_CONFIG, SAMPLE_MATRIX
    )
    assert any(r["earning_head"] == "OVERTIME HOURS" for r in routes)


def test_allowed_routes_hod_returns_assigned_modules():
    routes = get_user_allowed_routes(
        "nikhil.doegar@1mg.com", "hod", SAMPLE_CONFIG, SAMPLE_MATRIX
    )
    assert any(r["earning_head"] == "OVERTIME HOURS" for r in routes)


def test_allowed_routes_payroll_admin_returns_none():
    """payroll_admin gets None meaning unrestricted (all routes)."""
    routes = get_user_allowed_routes("payroll@1mg.com", "payroll_admin", SAMPLE_CONFIG, SAMPLE_MATRIX)
    assert routes is None


def test_allowed_routes_unknown_maker_gets_empty():
    routes = get_user_allowed_routes(
        "nobody@1mg.com", "maker", SAMPLE_CONFIG, SAMPLE_MATRIX
    )
    assert routes == []


# ─── get_user_allowed_modules ─────────────────────────────────────────────────

def test_allowed_modules_maker():
    modules = get_user_allowed_modules(
        "tanya.agrawal@1mg.com", "maker", SAMPLE_CONFIG, SAMPLE_MATRIX
    )
    assert "OVERTIME HOURS" in modules


def test_allowed_modules_payroll_admin_returns_wildcard():
    modules = get_user_allowed_modules("payroll@1mg.com", "payroll_admin", SAMPLE_CONFIG, SAMPLE_MATRIX)
    assert modules == ["*"]


# ─── enforce_sheet_authorization ─────────────────────────────────────────────

def test_enforce_auth_passes_for_authorized_maker():
    # Should not raise
    enforce_sheet_authorization(
        _FakeUser("tanya.agrawal@1mg.com", "maker"), "maker",
        "OVERTIME HOURS", "Dataverse",
        SAMPLE_CONFIG, SAMPLE_MATRIX,
    )


def test_enforce_auth_passes_for_payroll_admin():
    enforce_sheet_authorization(
        _FakeUser("payroll@1mg.com", "payroll_admin"), "payroll_admin",
        "OVERTIME HOURS", "Dataverse",
        SAMPLE_CONFIG, SAMPLE_MATRIX,
    )


def test_enforce_auth_raises_403_for_unauthorized_maker():
    with pytest.raises(HTTPException) as exc_info:
        enforce_sheet_authorization(
            _FakeUser("nobody@1mg.com", "maker"), "maker",
            "OVERTIME HOURS", "Dataverse",
            SAMPLE_CONFIG, SAMPLE_MATRIX,
        )
    assert exc_info.value.status_code == 403


def test_enforce_auth_raises_400_for_missing_module():
    with pytest.raises(HTTPException) as exc_info:
        enforce_sheet_authorization(
            _FakeUser("tanya.agrawal@1mg.com", "maker"), "maker",
            "", "Dataverse",
            SAMPLE_CONFIG, SAMPLE_MATRIX,
        )
    assert exc_info.value.status_code == 400


def test_enforce_auth_raises_403_for_wrong_home():
    with pytest.raises(HTTPException) as exc_info:
        enforce_sheet_authorization(
            _FakeUser("tanya.agrawal@1mg.com", "maker"), "maker",
            "OVERTIME HOURS", "WRONG HOME",
            SAMPLE_CONFIG, SAMPLE_MATRIX,
        )
    assert exc_info.value.status_code == 403
