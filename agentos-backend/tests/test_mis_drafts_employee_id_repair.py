"""Repair LLM putting employee_name into employee_external_id (persist would skip row)."""

from __future__ import annotations

from app.agents.o2c_ohc.mis_drafts import _repair_summary_row_employee_external_ids
from app.agents.o2c_ohc.mis_summary_llm import MIS_SERVICE_CHARGE_EXTERNAL_ID


def test_repair_maps_name_in_external_id_to_unique_attendance_id() -> None:
    att = [
        {
            "employee_external_id": "CON1MGHC0122",
            "employee_name": "Chetan Shankar Wanjari",
            "present_days": 20,
        }
    ]
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "7d788f46-0480-453c-9514-1764cf158868",
                "employee_external_id": "Chetan Shankar Wanjari",
                "employee_name": "Chetan Shankar Wanjari",
                "final_amount": 83000.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    row = summary["summary_rows"][0]
    assert row["employee_external_id"] == "CON1MGHC0122"


def test_no_repair_when_id_already_valid() -> None:
    att = [{"employee_external_id": "HLD-D-56", "employee_name": "Aditya Naik"}]
    summary = {
        "summary_rows": [
            {
                "employee_external_id": "HLD-D-56",
                "employee_name": "Aditya Naik",
                "final_amount": 1.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == "HLD-D-56"


def test_no_repair_when_name_ambiguous() -> None:
    att = [
        {"employee_external_id": "A1", "employee_name": "John Smith"},
        {"employee_external_id": "A2", "employee_name": "John Smith"},
    ]
    summary = {
        "summary_rows": [
            {
                "employee_external_id": "John Smith",
                "employee_name": "John Smith",
                "final_amount": 1.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == "John Smith"


def test_repair_fills_empty_external_id_when_name_unique() -> None:
    att = [{"employee_external_id": "NA-158", "employee_name": "Sumaiya Yousuf", "present_days": 10}]
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "crl-1",
                "employee_external_id": "",
                "employee_name": "Sumaiya Yousuf",
                "final_amount": 100.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == "NA-158"


def test_repair_maps_na_158_to_numeric_sheet_id() -> None:
    """LLM ``NA-158`` vs sheet ``data_only`` numeric id ``158`` (name match can also fix; this row has no name)."""
    att = [{"employee_external_id": "158", "employee_name": "Sumaiya Yousuf", "present_days": 10}]
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "crl-1",
                "employee_external_id": "NA-158",
                "employee_name": "",
                "final_amount": 100.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == "158"


def test_repair_does_not_map_na_158_when_two_numeric_ids_align() -> None:
    att = [
        {"employee_external_id": "158", "employee_name": "A", "present_days": 1},
        {"employee_external_id": "NA-158", "employee_name": "B", "present_days": 1},
    ]
    summary = {
        "summary_rows": [
            {
                "employee_external_id": "NA-158",
                "employee_name": "Someone",
                "final_amount": 1.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == "NA-158"


def test_repair_does_not_touch_service_charge_sentinel_row() -> None:
    att = [{"employee_external_id": "E1", "employee_name": "Doc One"}]
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "crl-fmo",
                "employee_external_id": MIS_SERVICE_CHARGE_EXTERNAL_ID,
                "employee_name": None,
                "final_amount": 500.0,
            }
        ]
    }
    _repair_summary_row_employee_external_ids(summary, att)
    assert summary["summary_rows"][0]["employee_external_id"] == MIS_SERVICE_CHARGE_EXTERNAL_ID
