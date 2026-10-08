"""MIS display labels: contract-first base + optional (specialization) suffix."""

from __future__ import annotations

from app.agents.o2c_ohc.billing_constants import OHC_INVOICE_ADMIN_ROLE_CODE
from app.agents.o2c_ohc.mis_display_roles import (
    apply_display_role_labels,
    label_from_attendance_role_code,
    label_from_contract_role_code,
)


def test_label_from_contract_exact_and_prefix() -> None:
    assert label_from_contract_role_code("MO_MBBS") == "Doctor"
    assert label_from_contract_role_code("NURSE_GNM_SHIFT") == "Nurse"
    assert label_from_contract_role_code("ACLS_AMBULANCE") == "Ambulance"
    assert label_from_contract_role_code("BLS_FOO") == "Ambulance"
    assert label_from_contract_role_code("FMO_MBBS_AFIH") == "FMO (AFIH)"
    assert label_from_contract_role_code("FMO_MBBS") == "FMO"
    assert label_from_contract_role_code("COMPANY_MO_MBBS") == "FO"
    assert label_from_contract_role_code("BMW_DISPOSAL") == "BMW"
    assert label_from_contract_role_code("BMW_EXTRA") == "BMW"
    assert "invoice administration" in label_from_contract_role_code(OHC_INVOICE_ADMIN_ROLE_CODE).lower()
    assert label_from_contract_role_code("LAB_MANAGER") == "Lab Manager"
    assert label_from_contract_role_code("LAB_TECHNICIAN") == "Lab Technician"
    assert label_from_contract_role_code("RADIOLOGY_TECHNICIAN") == "Radiology Technician"
    assert label_from_contract_role_code("REPORTS_SOFTWARE") == "Report cost / software cost"


def test_label_april_2026_style_roles_and_fallback_humanize() -> None:
    """Catalog + prefixes + snake_case humanizer for production role_code drift."""
    assert label_from_contract_role_code("DRIVER_AMBULANCE") == "Driver"
    assert label_from_contract_role_code("AMBULANCE_DRIVERS") == "Driver"
    assert label_from_contract_role_code("MEDICAL_EQUIPMENT") == "Equipment"
    assert label_from_contract_role_code("PHARMACIST") == "Pharmacist"
    assert label_from_contract_role_code("CSR_HEALTH_OUTREACH_COORDINATOR") == (
        "CSR / Health Outreach Coordinator"
    )
    assert label_from_contract_role_code("EQUIPMENT_COST_MPL_+_ANANDAM") == (
        "Equipment Cost Mpl + Anandam"
    )
    assert label_from_contract_role_code("UNKNOWN_X_ROLE") == "Unknown X Role"


def test_apply_driver_ambulance_label_with_contract_suffix() -> None:
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "DRIVER_AMBULANCE",
            "description": "Ambulance Drivers",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Driver (Ambulance Drivers)"


def test_lab_radiology_reports_roles_no_redundant_suffix_when_description_matches_base() -> None:
    summary = {
        "summary_rows": [
            {"contract_rate_line_id": "rl1", "service": "x"},
            {"contract_rate_line_id": "rl2", "service": "x"},
            {"contract_rate_line_id": "rl3", "service": "x"},
            {"contract_rate_line_id": "rl4", "service": "x"},
        ]
    }
    lines = [
        {
            "id": "rl1",
            "role_code": "LAB_TECHNICIAN",
            "description": "Lab Technician - Main OHC",
        },
        {
            "id": "rl2",
            "role_code": "RADIOLOGY_TECHNICIAN",
            "description": "Radiology Technician - Imaging",
        },
        {
            "id": "rl3",
            "role_code": "REPORTS_SOFTWARE",
            "description": "Report cost / software cost",
        },
        {
            "id": "rl4",
            "role_code": "LAB_MANAGER",
            "description": "Lab Manager - Main OHC",
        },
    ]
    apply_display_role_labels(summary, lines)
    rows = summary["summary_rows"]
    assert rows[0]["service"] == "Lab Technician"
    assert rows[1]["service"] == "Radiology Technician"
    assert rows[2]["service"] == "Report cost / software cost"
    assert rows[3]["service"] == "Lab Manager"


def test_apply_display_role_labels_contract_only_no_suffix() -> None:
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "rl1",
                "employee_external_id": "E1",
                "service": "MO_MBBS",
            },
            {
                "contract_rate_line_id": "rl2",
                "employee_external_id": None,
                "service": "NURSE_GNM",
            },
        ]
    }
    lines = [
        {"id": "rl1", "role_code": "MO_MBBS"},
        {"id": "rl2", "role_code": "NURSE_GNM"},
    ]
    apply_display_role_labels(summary, lines)
    rows = summary["summary_rows"]
    assert rows[0]["service"] == "Doctor"
    assert rows[1]["service"] == "Nurse"


def test_suffix_from_contract_description_before_dash() -> None:
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "rl1",
                "employee_external_id": "E1",
                "service": "x",
            },
        ]
    }
    lines = [
        {
            "id": "rl1",
            "role_code": "DOCTOR",
            "description": "Gynecologist - Carnac Medical Centre",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Doctor (Gynecologist)"


def test_suffix_from_attendance_when_doctor_line_no_description() -> None:
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "rl1",
                "employee_external_id": "E1",
                "employee_name": "Dr. Neena Patwardhan",
                "service": "Doctor",
            },
            {
                "contract_rate_line_id": "rl2",
                "employee_external_id": None,
                "employee_name": "",
                "service": "Nurse",
            },
        ]
    }
    lines = [
        {"id": "rl1", "role_code": "DOCTOR", "description": ""},
        {"id": "rl2", "role_code": "NURSE_GNM"},
    ]
    attendance = [
        {
            "employee_external_id": "E1",
            "employee_name": "Dr. Neena Patwardhan",
            "role_code": "Gynaecologist",
        }
    ]
    apply_display_role_labels(summary, lines, attendance)
    rows = summary["summary_rows"]
    assert rows[0]["service"] == "Doctor (Gynaecologist)"
    assert rows[1]["service"] == "Nurse"


def test_long_contract_description_without_dash_no_suffix() -> None:
    """Prose lines without ``Role - Site`` must not become parenthetical novels."""
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "FMO_MBBS_AFIH",
            "description": "Factory Medical Officer for Chakan-Ranjangaon pooled band",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "FMO (AFIH)"


def test_short_contract_description_without_dash_still_suffix() -> None:
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {"id": "rl1", "role_code": "DOCTOR", "description": "General Physician"},
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Doctor (General Physician)"


def test_ambulance_no_redundant_acls_blurbs_suffix() -> None:
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "ACLS_AMBULANCE",
            "description": "ACLS Ambulance - Carnac Medical Centre",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Ambulance"


def test_paramedic_no_paramedic_nurse_slash_suffix() -> None:
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "PARAMEDIC_GNM",
            "description": "Paramedic/Nurse - OHC",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Paramedic"


def test_paramedic_no_nurse_suffix_from_contract_description() -> None:
    summary = {
        "summary_rows": [
            {
                "contract_rate_line_id": "rl1",
                "employee_external_id": "E1",
                "service": "x",
            },
        ]
    }
    lines = [
        {
            "id": "rl1",
            "role_code": "PARAMEDIC_GNM",
            "description": "Nurse - OHC",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Paramedic"


def test_label_from_attendance_role_code_normalizes_typos() -> None:
    assert label_from_attendance_role_code("Radiaology Tech") == "Radiology Technician"
    assert label_from_attendance_role_code("  General   Physician  ") == "General Physician"
    assert label_from_attendance_role_code("doctor") == ""


def test_tac_site_band_nurse_description_no_redundant_brackets() -> None:
    """Annexure ``Role – Site – cluster`` must not become ``Nurse (full site string)``."""
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "NURSE_GNM",
            "description": "Nurses (GNM) – ASAL – Chakan-Ranjangaon",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Nurse"


def test_nurse_gnm_explicit_qualifier_in_description_still_no_bracket() -> None:
    """``GNM`` is already implied by ``NURSE_GNM`` — do not render ``Nurse (GNM)``."""
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "NURSE_GNM",
            "description": "Nurses (GNM) – Ward A",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Nurse"


def test_nurse_nonencoded_specialization_keeps_brackets() -> None:
    summary = {"summary_rows": [{"contract_rate_line_id": "rl1", "service": "x"}]}
    lines = [
        {
            "id": "rl1",
            "role_code": "NURSE_GNM",
            "description": "ICU Nurse – Main Hospital",
        },
    ]
    apply_display_role_labels(summary, lines)
    assert summary["summary_rows"][0]["service"] == "Nurse (ICU)"
