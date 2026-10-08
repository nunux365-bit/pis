from datetime import date

from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records


def test_build_detailed_json_includes_employee_external_id():
    det = build_detailed_json_from_records(
        "SITE-1",
        [
            {
                "employee_name": "Alice",
                "employee_external_id": "NA-158",
                "role_code": "DOCTOR",
                "roll_type": "off_roll",
                "ohc_attendance_daywise": {"2025-01-01": "Present"},
            },
            {
                "employee_name": "Bob",
                "role_code": "NURSE",
                "roll_type": "on_roll",
                "ohc_attendance_daywise": {},
            },
        ],
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 1),
    )
    emps = det["employees"]
    assert emps[0]["employee_external_id"] == "NA-158"
    assert emps[1].get("employee_external_id") is None
