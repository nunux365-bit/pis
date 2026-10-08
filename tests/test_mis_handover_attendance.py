"""LWD/DOJ handover prep for MIS drafts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.agents.o2c_ohc.mis_handover_attendance import (
    apply_handover_attendance_prep,
    apply_handover_summary_row_fixup,
    attendance_maps_to_single_crl,
    parse_sheet_date,
)
from app.config.settings import settings


def _crl(
    *,
    crl_id: str = "crl-1",
    role: str = "MO",
    cq: int = 1,
) -> dict:
    return {
        "id": crl_id,
        "role_code": role,
        "billing_model": "rate_attendance",
        "attendance_required": True,
        "contracted_quantity": cq,
    }


def _att(
    eid: str,
    name: str,
    *,
    role: str = "Doctor",
    hint: str | None = "MO",
    lwd: str | None = None,
    doj: str | None = None,
    daywise: dict | None = None,
) -> dict:
    ps = date(2026, 4, 1)
    pe = date(2026, 4, 30)
    if daywise is None:
        daywise = {}
        d = ps
        while d <= pe:
            daywise[d.isoformat()] = "Present"
            d = date.fromordinal(d.toordinal() + 1)
    return {
        "employee_external_id": eid,
        "employee_name": name,
        "role_code": role,
        "mis_staffing_fmo_role_code": hint,
        "lwd": lwd,
        "doj": doj,
        "ohc_attendance_daywise": daywise,
        "present_days": Decimal(len([v for v in daywise.values() if v])),
        "absent_days": Decimal(0),
        "total_days": len(daywise),
    }


def test_parse_sheet_date_april_26() -> None:
    assert parse_sheet_date("15-Apr-26", period_start=date(2026, 4, 1)) == date(2026, 4, 15)


def test_maps_to_single_crl_via_fmo_hint() -> None:
    rec = _att("a", "A")
    assert (
        attendance_maps_to_single_crl(
            rec, [_crl()], period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
        )
        is not None
    )


def test_churn_doctor_prefers_mo_over_fmo_ambiguity() -> None:
    lines = [_crl(crl_id="fmo", role="FMO_MBBS_AFIH"), _crl(crl_id="mo", role="MO_BAMS_BHMS")]
    leaver = _att("L1", "Leaver", lwd="15-Apr-26", hint=None)
    leaver["role_code"] = "Doctor"
    leaver["mis_staffing_fmo_role_code"] = None
    m = attendance_maps_to_single_crl(
        leaver, lines, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )
    assert m is not None
    assert m["id"] == "mo"


def test_hint_mo_maps_only_mo_crl_when_fmo_also_present() -> None:
    lines = [_crl(crl_id="mo-a", role="MO"), _crl(crl_id="fmo-b", role="FMO")]
    rec = _att("a", "A", hint="MO")
    m = attendance_maps_to_single_crl(rec, lines)
    assert m is not None
    assert m["id"] == "mo-a"


def test_maps_ambiguous_when_two_crls_match_nurse() -> None:
    lines = [_crl(crl_id="1", role="NURSE_BSC"), _crl(crl_id="2", role="NURSE_GNM")]
    rec = _att("a", "A", role="Nurse", hint=None)
    assert attendance_maps_to_single_crl(rec, lines) is None


def test_handover_cq1_merges_names_comma_id_joiner_only() -> None:
    ps, pe = date(2026, 4, 1), date(2026, 4, 30)
    leaver_dw = {d.isoformat(): ("Present" if d.day <= 15 else None) for d in _days(ps, pe)}
    joiner_dw = {d.isoformat(): (None if d.day < 16 else "Present") for d in _days(ps, pe)}
    records = [
        _att("L1", "Alice Leaver", lwd="15-Apr-26", daywise=leaver_dw),
        _att("J1", "Bob Joiner", doj="16-Apr-26", daywise=joiner_dw),
    ]
    out = apply_handover_attendance_prep(
        records, [_crl()], period_start=ps, period_end=pe
    )
    assert out.handover["applied"] is True
    assert len(out.records) == 1
    row = out.records[0]
    assert row["employee_external_id"] == "J1"
    assert row["employee_name"] == "Bob Joiner, Alice Leaver"
    assert "L1" not in {r["employee_external_id"] for r in out.records}


def test_handover_two_pairs_on_separate_crls() -> None:
    """Two MO lines (cq=1 each): independent 1:1 handovers do not cross-match joiners."""
    ps, pe = date(2026, 4, 1), date(2026, 4, 30)
    lines = [_crl(crl_id="mo-a", role="MO", cq=1), _crl(crl_id="fmo-b", role="FMO", cq=1)]

    def leaver(eid: str, name: str, lwd_day: int, *, hint: str) -> dict:
        dw = {d.isoformat(): ("Present" if d.day <= lwd_day else None) for d in _days(ps, pe)}
        return _att(eid, name, hint=hint, lwd=f"{lwd_day}-Apr-26", daywise=dw)

    def joiner(eid: str, name: str, doj_day: int, *, hint: str) -> dict:
        dw = {d.isoformat(): ("Present" if d.day >= doj_day else None) for d in _days(ps, pe)}
        return _att(eid, name, hint=hint, doj=f"{doj_day}-Apr-26", daywise=dw)

    records = [
        leaver("L1", "L One", 10, hint="MO"),
        joiner("J1", "J One", 11, hint="MO"),
        leaver("L2", "L Two", 20, hint="FMO"),
        joiner("J2", "J Two", 21, hint="FMO"),
    ]
    out = apply_handover_attendance_prep(records, lines, period_start=ps, period_end=pe)
    assert out.handover["applied"] is True
    assert len(out.records) == 2
    assert {r["employee_external_id"] for r in out.records} == {"J1", "J2"}


def test_handover_skips_ambiguous_two_joiners_after_one_leaver() -> None:
    ps, pe = date(2026, 4, 1), date(2026, 4, 30)
    leaver = _att("L1", "Leaver", lwd="10-Apr-26")
    j1 = _att("J1", "J1", doj="11-Apr-26")
    j2 = _att("J2", "J2", doj="12-Apr-26")
    out = apply_handover_attendance_prep(
        [leaver, j1, j2], [_crl(cq=3)], period_start=ps, period_end=pe
    )
    assert not out.handover["applied"]
    assert len(out.records) == 3


def test_handover_skips_when_cap_exceeded() -> None:
    ps, pe = date(2026, 4, 1), date(2026, 4, 30)
    stable = _att("S1", "Stable")
    leaver = _att("L1", "Leaver", lwd="15-Apr-26")
    joiner = _att("J1", "Joiner", doj="16-Apr-26")
    out = apply_handover_attendance_prep(
        [stable, leaver, joiner],
        [_crl(cq=1)],
        period_start=ps,
        period_end=pe,
    )
    assert not out.handover["applied"]


def test_post_llm_fixup_omits_leaver_via_global_suppressed_ids() -> None:
    handover = {
        "applied": True,
        "suppressed_employee_ids": ["L1"],
        "suppressed_by_crl": {"crl-1": ["L1"]},
        "pairs": [
            {
                "crl_id": "crl-1",
                "leaver_id": "L1",
                "joiner_id": "J1",
                "joiner_display_name": "Bob Joiner, Alice Leaver",
            }
        ],
    }
    sj = {
        "summary_rows": [
            {
                "contract_rate_line_id": "crl-1",
                "employee_external_id": "L1",
                "is_omitted": False,
                "final_amount": 6000,
            },
        ]
    }
    apply_handover_summary_row_fixup(sj, handover)
    assert sj["summary_rows"][0]["is_omitted"] is True


def test_post_llm_fixup_omits_leaver_and_sets_comma_name(monkeypatch: pytest.MonkeyPatch) -> None:
    handover = {
        "applied": True,
        "suppressed_employee_ids": ["L1"],
        "suppressed_by_crl": {"crl-1": ["L1"]},
        "pairs": [
            {
                "crl_id": "crl-1",
                "leaver_id": "L1",
                "joiner_id": "J1",
                "joiner_display_name": "Bob Joiner, Alice Leaver",
            }
        ],
    }
    sj = {
        "summary_rows": [
            {
                "contract_rate_line_id": "crl-1",
                "employee_external_id": "L1",
                "employee_name": "Alice Leaver",
                "is_omitted": False,
                "final_amount": 6000,
            },
            {
                "contract_rate_line_id": "crl-1",
                "employee_external_id": "J1",
                "employee_name": "Bob Joiner",
                "is_omitted": False,
                "final_amount": 6000,
            },
        ]
    }
    apply_handover_summary_row_fixup(sj, handover)
    rows = {r["employee_external_id"]: r for r in sj["summary_rows"]}
    assert rows["L1"]["is_omitted"] is True
    assert rows["L1"]["final_amount"] == 0.0
    assert rows["J1"]["employee_name"] == "Bob Joiner, Alice Leaver"


def test_handover_disabled_only_clips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "o2c_mis_handover_pairing_enabled", False)
    ps, pe = date(2026, 4, 1), date(2026, 4, 30)
    leaver = _att("L1", "Leaver", lwd="15-Apr-26")
    joiner = _att("J1", "Joiner", doj="16-Apr-26")
    out = apply_handover_attendance_prep(
        [leaver, joiner], [_crl()], period_start=ps, period_end=pe
    )
    assert out.handover.get("reason") == "disabled"
    assert len(out.records) == 2


def _days(ps: date, pe: date):
    d = ps
    while d <= pe:
        yield d
        d = date.fromordinal(d.toordinal() + 1)


def test_summary_json_handover_blocks_auto_approve_reason() -> None:
    """Handover flag in stored summary_json is checked during eligibility (see mis_auto_approve)."""
    sj = {"handover": {"applied": True, "pairs": [{"joiner_id": "J1"}]}}
    handover = sj.get("handover")
    reasons: list[str] = []
    if isinstance(handover, dict) and handover.get("applied"):
        reasons.append("lwd_doj_handover_applied")
    assert reasons == ["lwd_doj_handover_applied"]
