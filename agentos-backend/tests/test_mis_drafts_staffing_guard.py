"""MIS draft helpers: non-employee final_amount backfill (no attendance_required branch in mis_drafts).

``attendance_required`` is normalized at PDF ingest; MIS draft code does not re-derive it.
``_apply_non_employee_fixed_final_defaults`` only skips ``as_per_actuals`` and requires positive
``rate_amount`` × ``contracted_quantity``.
"""

from __future__ import annotations

from decimal import Decimal

from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_PCT_BILLING_RULE_KEY,
    OHC_INVOICE_ADMIN_ROLE_CODE,
    SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE,
)
from app.agents.o2c_ohc.mis_drafts import (
    MIS_STAFFING_CONTRACT_GAP_FMO_LINE_MISSING,
    _apply_fmo_mo_attendance_hints,
    _apply_non_employee_fixed_final_defaults,
    _cap_employee_summary_rows_by_contracted_quantity,
    _flag_fmo_contract_line_missing_vs_attendance,
    _has_server_tac_o_invoice_admin_line,
    _reconcile_invoice_admin_pct_rows_from_staffing,
)
from app.agents.o2c_ohc.mis_summary_llm import MIS_SERVICE_CHARGE_EXTERNAL_ID


def test_non_employee_fixed_final_fills_even_when_attendance_required_true() -> None:
    """Staffing lines may still get rate×qty if LLM left a non-employee row with null final_amount."""
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": "doc",
                "employee_external_id": None,
                "final_amount": None,
            }
        ]
    }
    rate_lines = [
        {
            "id": "doc",
            "billing_model": "rate_attendance",
            "attendance_required": True,
            "rate_amount": 50000,
            "contracted_quantity": 1,
        }
    ]
    _apply_non_employee_fixed_final_defaults(summary_json, rate_lines)
    assert summary_json["summary_rows"][0].get("final_amount") == 50000.0


def test_non_employee_fixed_final_skips_as_per_actuals() -> None:
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": "bmw",
                "employee_external_id": None,
                "final_amount": None,
            }
        ]
    }
    rate_lines = [
        {
            "id": "bmw",
            "billing_model": "as_per_actuals",
            "attendance_required": False,
            "rate_amount": 10000,
            "contracted_quantity": 1,
        }
    ]
    _apply_non_employee_fixed_final_defaults(summary_json, rate_lines)
    assert summary_json["summary_rows"][0].get("final_amount") is None


def test_fmo_mo_hints_mark_highest_paid_days_physicians() -> None:
    att = [
        {
            "employee_external_id": "A",
            "role_code": "MO_MBBS",
            "present_days": Decimal(10),
            "leave_days": Decimal(0),
        },
        {
            "employee_external_id": "B",
            "role_code": "MO_MBBS",
            "present_days": Decimal(20),
            "leave_days": Decimal(0),
        },
    ]
    lines = [
        {
            "id": "f1",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "contracted_quantity": 1,
        },
        {
            "id": "m1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "contracted_quantity": 1,
        },
    ]
    _apply_fmo_mo_attendance_hints(att, lines)
    assert att[0].get("mis_staffing_slot_hint") is None
    assert att[1].get("mis_staffing_slot_hint") == "fmo_preferred"
    assert att[1].get("mis_staffing_fmo_role_code") == "FMO_MBBS_AFIH"


def test_fmo_mo_hints_prioritize_roster_fmo_employee_over_paid_days() -> None:
    """On-roll FMO (FMO_* on attendance) fills FMO slot before a higher-paid MO-labeled row."""
    att = [
        {
            "employee_external_id": "FMO_EMP",
            "role_code": "FMO_MBBS_AFIH",
            "present_days": Decimal(5),
            "leave_days": Decimal(0),
        },
        {
            "employee_external_id": "MO_EMP",
            "role_code": "MO_MBBS",
            "present_days": Decimal(25),
            "leave_days": Decimal(0),
        },
    ]
    lines = [
        {
            "id": "f1",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "contracted_quantity": 1,
        },
        {
            "id": "m1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "contracted_quantity": 1,
        },
    ]
    _apply_fmo_mo_attendance_hints(att, lines)
    assert att[0].get("mis_staffing_slot_hint") == "fmo_preferred"
    assert att[0].get("mis_staffing_fmo_role_code") == "FMO_MBBS_AFIH"
    assert att[1].get("mis_staffing_slot_hint") is None


def test_cap_sets_clear_omit_reason_and_prepends_server_calc_notes() -> None:
    """contracted_quantity cap: higher paid_days row kept; omitted row gets server calc_notes."""
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": "doc",
                "employee_external_id": "E_LOW",
                "final_amount": 83000.0,
                "is_omitted": False,
                "calc_notes": "MONTHLY_RETAINER_INGEST draft math",
            },
            {
                "contract_rate_line_id": "doc",
                "employee_external_id": "E_HIGH",
                "final_amount": 50000.0,
                "is_omitted": False,
                "calc_notes": "other",
            },
        ]
    }
    att = [
        {
            "employee_external_id": "E_LOW",
            "present_days": Decimal(5),
            "leave_days": Decimal(0),
        },
        {
            "employee_external_id": "E_HIGH",
            "present_days": Decimal(22),
            "leave_days": Decimal(0),
        },
    ]
    rate_lines = [
        {
            "id": "doc",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "contracted_quantity": 1,
        },
    ]
    _cap_employee_summary_rows_by_contracted_quantity(summary_json, att, rate_lines)
    rows = summary_json["summary_rows"]
    assert rows[0].get("is_omitted") is True
    assert rows[0].get("final_amount") == 0.0
    assert "contracted_quantity" in (rows[0].get("omit_reason") or "").lower()
    cn0 = str(rows[0].get("calc_notes") or "")
    assert cn0.startswith("[server]")
    assert "contracted_quantity=1" in cn0
    assert "MONTHLY_RETAINER_INGEST" in cn0
    assert rows[1].get("is_omitted") is False


def test_cap_headcount_prefers_attendance_paid_days_for_ranking() -> None:
    """Server cap must match MIS prompt: rank by explicit paid_days when present, else present+leave."""
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": "doc",
                "employee_external_id": "E_WIN",
                "final_amount": 100.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": "doc",
                "employee_external_id": "E_LOSE",
                "final_amount": 200.0,
                "is_omitted": False,
            },
        ]
    }
    att = [
        {
            "employee_external_id": "E_WIN",
            "present_days": 5,
            "leave_days": 0,
            "paid_days": 25,
        },
        {
            "employee_external_id": "E_LOSE",
            "present_days": 22,
            "leave_days": 0,
            "paid_days": 22,
        },
    ]
    rate_lines = [
        {
            "id": "doc",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "contracted_quantity": 1,
        },
    ]
    _cap_employee_summary_rows_by_contracted_quantity(summary_json, att, rate_lines)
    by_emp = {r["employee_external_id"]: r for r in summary_json["summary_rows"]}
    assert by_emp["E_WIN"]["is_omitted"] is False
    assert by_emp["E_LOSE"]["is_omitted"] is True
    assert by_emp["E_LOSE"]["final_amount"] == 0.0


def test_headcount_kept_zero_anomaly_message_detects_inconsistency() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_headcount_kept_zero_anomaly_message

    crl = "nurse-line-uuid"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E_A",
                "final_amount": 25500.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E_B",
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {"id": crl, "billing_model": "rate_attendance", "role_code": "MO_STAFF", "rate_amount": 25500.0},
    ]
    msg = _mis_headcount_kept_zero_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert "E_B" in msg
    assert "contract_rate_line_id=" in msg


def test_headcount_kept_zero_anomaly_includes_per_visit_peers() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_headcount_kept_zero_anomaly_message

    crl = "phys-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "D1",
                "final_amount": 83000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "D2",
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "per_visit", "role_code": "PHYSICIAN", "rate_amount": 83000}]
    msg = _mis_headcount_kept_zero_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert "D2" in msg


def test_headcount_kept_zero_sole_per_visit_nonzero_rate() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_headcount_kept_zero_anomaly_message

    crl = "phys-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "D1",
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "per_visit", "role_code": "PHYSICIAN", "rate_amount": 83000}]
    msg = _mis_headcount_kept_zero_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert "sole non-omitted" in msg


def test_headcount_kept_zero_skips_per_visit_when_attendance_not_required() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_headcount_kept_zero_anomaly_message

    crl = "amb-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "V1",
                "final_amount": 1000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "V2",
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "per_visit",
            "attendance_required": False,
            "role_code": "AMBULANCE",
            "rate_amount": 1000.0,
        },
    ]
    assert _mis_headcount_kept_zero_anomaly_message(summary_json, rate_lines) is None


def test_duplicate_employee_staffing_anomaly_message_two_lines() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_duplicate_employee_staffing_anomaly_message

    fmo = "fmo-line"
    mo = "mo-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": fmo,
                "employee_external_id": "E_DUP",
                "final_amount": 50000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": mo,
                "employee_external_id": "E_DUP",
                "final_amount": 45000.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {"id": fmo, "billing_model": "rate_attendance", "role_code": "FMO_MBBS_AFIH", "rate_amount": 50000},
        {"id": mo, "billing_model": "rate_attendance", "role_code": "MO_MBBS", "rate_amount": 45000},
    ]
    msg = _mis_duplicate_employee_staffing_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert "E_DUP" in msg
    assert "billed twice" in msg.lower() or "Same employee" in msg


def test_duplicate_employee_staffing_anomaly_message_none_when_single_row() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_duplicate_employee_staffing_anomaly_message

    crl = "line-1"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E_SOLO",
                "final_amount": 100.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 100}]
    assert _mis_duplicate_employee_staffing_anomaly_message(summary_json, rate_lines) is None


def test_duplicate_employee_staffing_anomaly_message_none_when_peer_omitted() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_duplicate_employee_staffing_anomaly_message

    a, b = "line-a", "line-b"
    summary_json = {
        "summary_rows": [
            {"contract_rate_line_id": a, "employee_external_id": "E1", "final_amount": 1.0, "is_omitted": False},
            {"contract_rate_line_id": b, "employee_external_id": "E1", "final_amount": 0.0, "is_omitted": True},
        ]
    }
    rate_lines = [
        {"id": a, "billing_model": "rate_attendance", "rate_amount": 1},
        {"id": b, "billing_model": "rate_attendance", "rate_amount": 2},
    ]
    assert _mis_duplicate_employee_staffing_anomaly_message(summary_json, rate_lines) is None


def test_cq_underbill_anomaly_when_two_rows_but_only_one_billed() -> None:
    """Detect nurse-style bug: contracted_quantity=2, two employee rows, one wrongly omitted."""
    from app.agents.o2c_ohc.mis_drafts import _mis_contracted_quantity_underbill_anomaly_message

    crl = "nurse-line-uuid"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E1",
                "final_amount": 31000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E2",
                "final_amount": 0.0,
                "is_omitted": True,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "rate_attendance",
            "role_code": "NURSE_GNM",
            "rate_amount": 31000.0,
            "contracted_quantity": 2,
        },
    ]
    msg = _mis_contracted_quantity_underbill_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert crl in msg
    assert "contracted_quantity=2" in msg
    assert "only 1 non-omitted" in msg


def test_cq_underbill_anomaly_none_when_cap_filled() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_contracted_quantity_underbill_anomaly_message

    crl = "nurse-line-uuid"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E1",
                "final_amount": 31000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E2",
                "final_amount": 31000.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "rate_attendance",
            "role_code": "NURSE_GNM",
            "rate_amount": 31000.0,
            "contracted_quantity": 2,
        },
    ]
    assert _mis_contracted_quantity_underbill_anomaly_message(summary_json, rate_lines) is None


def test_cq_underbill_anomaly_none_when_not_enough_summary_rows() -> None:
    """Single row cannot satisfy cap=2 — do not flag (missing rows is a different failure)."""
    from app.agents.o2c_ohc.mis_drafts import _mis_contracted_quantity_underbill_anomaly_message

    crl = "nurse-line-uuid"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E1",
                "final_amount": 31000.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "rate_attendance",
            "role_code": "NURSE_GNM",
            "rate_amount": 31000.0,
            "contracted_quantity": 2,
        },
    ]
    assert _mis_contracted_quantity_underbill_anomaly_message(summary_json, rate_lines) is None


def test_cq_underbill_anomaly_none_for_non_attendance_line() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_contracted_quantity_underbill_anomaly_message

    crl = "fixed-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E1",
                "final_amount": 100.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E2",
                "final_amount": 0.0,
                "is_omitted": True,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "fixed_monthly",
            "role_code": "FOO",
            "rate_amount": 100.0,
            "contracted_quantity": 2,
        },
    ]
    assert _mis_contracted_quantity_underbill_anomaly_message(summary_json, rate_lines) is None


def test_attendance_roll_id_anomaly_omitted_line_item_ok() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_attendance_staffing_roll_id_anomaly_message
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "homeo-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 0.0,
                "is_omitted": True,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 1000}]
    att = [{"employee_external_id": "E_REAL"}]
    assert _mis_attendance_staffing_roll_id_anomaly_message(summary_json, rate_lines, att) is None


def test_attendance_roll_id_anomaly_line_item_on_non_omitted_rate_attendance() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_attendance_staffing_roll_id_anomaly_message
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "homeo-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 1000.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 1000}]
    att = [{"employee_external_id": "E_REAL"}]
    msg = _mis_attendance_staffing_roll_id_anomaly_message(summary_json, rate_lines, att)
    assert msg is not None
    assert "Attendance-based" in msg or "roll" in msg.lower()


def test_attendance_roll_id_anomaly_unknown_employee_non_omitted() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_attendance_staffing_roll_id_anomaly_message

    crl = "mo-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "NOT_ON_ROLL",
                "final_amount": 100.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 100}]
    att = [{"employee_external_id": "E_ON_ROLL"}]
    msg = _mis_attendance_staffing_roll_id_anomaly_message(summary_json, rate_lines, att)
    assert msg is not None
    assert "NOT_ON_ROLL" in msg


def test_attendance_roll_id_anomaly_none_when_valid_id() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_attendance_staffing_roll_id_anomaly_message

    crl = "line-1"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": "E1",
                "final_amount": 100.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 100}]
    att = [{"employee_external_id": "E1"}]
    assert _mis_attendance_staffing_roll_id_anomaly_message(summary_json, rate_lines, att) is None


def test_attendance_roll_id_anomaly_skips_per_visit_when_attendance_not_required() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_attendance_staffing_roll_id_anomaly_message
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "bundle-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {"id": crl, "billing_model": "per_visit", "attendance_required": False, "rate_amount": 1},
    ]
    att: list[dict] = []
    assert _mis_attendance_staffing_roll_id_anomaly_message(summary_json, rate_lines, att) is None


def test_coerce_attendance_staffing_without_roll_id_marks_omitted() -> None:
    from app.agents.o2c_ohc.mis_drafts import _coerce_attendance_staffing_rows_without_roll_id_inplace
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "ra-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 5000.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 5000}]
    att = [{"employee_external_id": "E1"}]
    _coerce_attendance_staffing_rows_without_roll_id_inplace(summary_json, rate_lines, att)
    row = summary_json["summary_rows"][0]
    assert row["is_omitted"] is True
    assert row["final_amount"] == 0.0
    assert row["employee_external_id"] == NON_EMPLOYEE_SENTINEL
    assert "omit_reason" in row and row["omit_reason"]


def test_coerce_attendance_staffing_idempotent_on_second_call() -> None:
    from app.agents.o2c_ohc.mis_drafts import _coerce_attendance_staffing_rows_without_roll_id_inplace
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "ra-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 0.0,
                "is_omitted": True,
                "omit_reason": "x",
                "calc_notes": "y",
            },
        ]
    }
    rate_lines = [{"id": crl, "billing_model": "rate_attendance", "rate_amount": 100}]
    att = [{"employee_external_id": "E1"}]
    _coerce_attendance_staffing_rows_without_roll_id_inplace(summary_json, rate_lines, att)
    row = summary_json["summary_rows"][0]
    snap_notes = row["calc_notes"]
    snap_omit = row["omit_reason"]
    _coerce_attendance_staffing_rows_without_roll_id_inplace(summary_json, rate_lines, att)
    assert row["calc_notes"] == snap_notes
    assert row["omit_reason"] == snap_omit


def test_duplicate_staffing_anomaly_none_for_per_visit_attendance_not_required() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_duplicate_employee_staffing_anomaly_message

    a, b = "pv-a", "pv-b"
    summary_json = {
        "summary_rows": [
            {"contract_rate_line_id": a, "employee_external_id": "E1", "final_amount": 1.0, "is_omitted": False},
            {"contract_rate_line_id": b, "employee_external_id": "E1", "final_amount": 2.0, "is_omitted": False},
        ]
    }
    rate_lines = [
        {"id": a, "billing_model": "per_visit", "attendance_required": False, "rate_amount": 1},
        {"id": b, "billing_model": "per_visit", "attendance_required": False, "rate_amount": 2},
    ]
    assert _mis_duplicate_employee_staffing_anomaly_message(summary_json, rate_lines) is None


def test_as_per_kept_zero_positive_rate_anomaly_message() -> None:
    from app.agents.o2c_ohc.mis_drafts import _mis_as_per_kept_zero_positive_rate_anomaly_message
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "bmw-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "as_per_actuals",
            "rate_amount": 9500.0,
            "billing_rule_text": (
                "Bio-medical waste disposal to be billed on actuals or approximate monthly cost cap, "
                "whichever is lower, with approximate monthly cost Rs 9,500 as per Annexure B.4."
            ),
        }
    ]
    msg = _mis_as_per_kept_zero_positive_rate_anomaly_message(summary_json, rate_lines)
    assert msg is not None
    assert "lower of" in msg.lower()
    assert "bmw-line" in msg


def test_as_per_kept_zero_positive_rate_does_not_fire_for_equipment_style_line() -> None:
    """Equipment as_per_actuals can legitimately stay 0 until invoices; do not force self-correction."""
    from app.agents.o2c_ohc.mis_drafts import _mis_as_per_kept_zero_positive_rate_anomaly_message
    from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL

    crl = "equip-line"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": crl,
                "employee_external_id": NON_EMPLOYEE_SENTINEL,
                "final_amount": 0.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines = [
        {
            "id": crl,
            "billing_model": "as_per_actuals",
            "rate_amount": 472.0,
            "billing_rule_text": (
                "Medical equipment cost including calibration to be billed on actuals. Annexure B states "
                "equipment cost shall be based on pre-defined cost mutually discussed over email."
            ),
        }
    ]
    assert _mis_as_per_kept_zero_positive_rate_anomaly_message(summary_json, rate_lines) is None


def test_cap_skips_service_charge_row_on_same_contract_rate_line() -> None:
    """Synthetic admin row must not participate in contracted_quantity headcount cap."""
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": "same",
                "employee_external_id": "E1",
                "final_amount": 100.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": "same",
                "employee_external_id": MIS_SERVICE_CHARGE_EXTERNAL_ID,
                "final_amount": 50.0,
                "is_omitted": False,
            },
        ]
    }
    att = [{"employee_external_id": "E1", "present_days": 1, "leave_days": 0}]
    rate_lines = [
        {
            "id": "same",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "contracted_quantity": 1,
        }
    ]
    _cap_employee_summary_rows_by_contracted_quantity(summary_json, att, rate_lines)
    assert summary_json["summary_rows"][0].get("is_omitted") is False
    assert summary_json["summary_rows"][1].get("is_omitted") is False


def test_has_server_tac_o_invoice_admin_line() -> None:
    assert _has_server_tac_o_invoice_admin_line([]) is False
    assert (
        _has_server_tac_o_invoice_admin_line(
            [
                {
                    "role_code": OHC_INVOICE_ADMIN_ROLE_CODE,
                    "billing_rules": {INVOICE_ADMIN_PCT_BILLING_RULE_KEY: 10},
                }
            ]
        )
        is False
    )
    assert (
        _has_server_tac_o_invoice_admin_line(
            [
                {
                    "role_code": OHC_INVOICE_ADMIN_ROLE_CODE,
                    "source_ref": {"note": SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE},
                    "billing_rules": {INVOICE_ADMIN_PCT_BILLING_RULE_KEY: 10},
                }
            ]
        )
        is True
    )


def test_reconcile_invoice_admin_only_with_tac_o_server_marker() -> None:
    staff_id, admin_id = "st", "adm"
    summary_json = {
        "summary_rows": [
            {
                "contract_rate_line_id": staff_id,
                "employee_external_id": "E1",
                "final_amount": 1000.0,
                "is_omitted": False,
            },
            {
                "contract_rate_line_id": admin_id,
                "employee_external_id": None,
                "final_amount": 999.0,
                "is_omitted": False,
            },
        ]
    }
    rate_lines_tac_o = [
        {
            "id": staff_id,
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "service_charge_type": "none",
        },
        {
            "id": admin_id,
            "billing_model": "fixed_monthly",
            "role_code": OHC_INVOICE_ADMIN_ROLE_CODE,
            "source_ref": {"note": SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE},
            "billing_rules": {INVOICE_ADMIN_PCT_BILLING_RULE_KEY: 10},
        },
    ]
    _reconcile_invoice_admin_pct_rows_from_staffing(summary_json, rate_lines_tac_o)
    assert summary_json["summary_rows"][1]["final_amount"] == 100.0

    summary_json["summary_rows"][1]["final_amount"] = 999.0
    rate_lines_no_marker = [
        rate_lines_tac_o[0],
        {
            **rate_lines_tac_o[1],
            "source_ref": {"note": "manual"},
        },
    ]
    _reconcile_invoice_admin_pct_rows_from_staffing(summary_json, rate_lines_no_marker)
    assert summary_json["summary_rows"][1]["final_amount"] == 999.0


def test_flag_when_attendance_fmo_but_contract_only_mo_lines() -> None:
    """ASAL-style: roll shows FMO+MO; extract wrongly has two MO_* contract lines — no silent MO mapping."""
    att = [
        {
            "employee_external_id": "F1",
            "role_code": "FMO_MBBS_AFIH",
            "present_days": Decimal(20),
            "leave_days": Decimal(0),
        },
        {
            "employee_external_id": "M1",
            "role_code": "MO_MBBS",
            "present_days": Decimal(20),
            "leave_days": Decimal(0),
        },
    ]
    lines = [
        {"id": "m1", "billing_model": "rate_attendance", "role_code": "MO_MBBS", "contracted_quantity": 1},
        {"id": "m2", "billing_model": "rate_attendance", "role_code": "MO_BAMS_BHMS", "contracted_quantity": 1},
    ]
    _flag_fmo_contract_line_missing_vs_attendance(att, lines)
    assert att[0].get("mis_staffing_contract_gap") == MIS_STAFFING_CONTRACT_GAP_FMO_LINE_MISSING
    assert att[1].get("mis_staffing_contract_gap") is None


def test_no_fmo_gap_flag_when_contract_has_fmo_line() -> None:
    att = [
        {"employee_external_id": "F1", "role_code": "FMO_MBBS_AFIH", "present_days": Decimal(1), "leave_days": Decimal(0)},
    ]
    lines = [
        {"id": "f1", "billing_model": "rate_attendance", "role_code": "FMO_MBBS_AFIH", "contracted_quantity": 1},
        {"id": "m1", "billing_model": "rate_attendance", "role_code": "MO_MBBS", "contracted_quantity": 1},
    ]
    _flag_fmo_contract_line_missing_vs_attendance(att, lines)
    assert att[0].get("mis_staffing_contract_gap") is None


def test_fmo_mo_hints_skipped_when_roster_only_fmo_labeled() -> None:
    """No MO-side roster rows → no FMO/MO disambiguation hints (TCS-safe)."""
    att = [
        {
            "employee_external_id": "A",
            "role_code": "FMO_MBBS_AFIH",
            "present_days": Decimal(20),
            "leave_days": Decimal(0),
        },
        {
            "employee_external_id": "B",
            "role_code": "FMO_MBBS_AFIH",
            "present_days": Decimal(18),
            "leave_days": Decimal(0),
        },
    ]
    lines = [
        {
            "id": "f1",
            "billing_model": "rate_attendance",
            "role_code": "FMO_MBBS_AFIH",
            "contracted_quantity": 1,
        },
        {
            "id": "m1",
            "billing_model": "rate_attendance",
            "role_code": "MO_MBBS",
            "contracted_quantity": 1,
        },
    ]
    _apply_fmo_mo_attendance_hints(att, lines)
    assert att[0].get("mis_staffing_slot_hint") is None
    assert att[1].get("mis_staffing_slot_hint") is None
