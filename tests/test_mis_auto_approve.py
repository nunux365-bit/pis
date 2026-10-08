"""MIS auto-approve eligibility and wiring."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.exc import ProgrammingError

from app.agents.o2c_ohc.mis_summary_llm import (
    MIS_SERVICE_CHARGE_EXTERNAL_ID,
    NON_EMPLOYEE_SENTINEL,
)
from app.services.o2c import mis_auto_approve as aa
from app.services.o2c import mis_workflow as mw


def _eligible_session_factory(
    *,
    mid: str,
    prior_id: str,
    ss_id: str,
    ctv: str,
    prior_ctv: str | None = None,
    current_rows: list[dict],
    prior_rows: list[dict] | None = None,
    summary_json: dict | None = None,
    rate_rows: list[dict] | None = None,
) -> type:
    """Minimal session mock for evaluate_auto_approve_eligibility happy-path variants."""
    prior_rows = prior_rows if prior_rows is not None else current_rows
    prior_ctv = prior_ctv if prior_ctv is not None else ctv
    summary_json = summary_json if summary_json is not None else {
        "validation": {"status": "ok"},
        "summary_rows": [],
    }
    rate_rows = rate_rows if rate_rows is not None else [
        {
            "role_code": "NURSE",
            "billing_model": "rate_attendance",
            "contracted_quantity": "1",
            "rate_amount": "31000",
        }
    ]

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": summary_json,
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": prior_ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                return _Result(rows=rate_rows)
            if "o2c_mis_summary_row" in sql:
                if str(params.get("mid", "")) == prior_id:
                    return _Result(rows=prior_rows)
                return _Result(rows=current_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    return _Ctx


def test_parse_latest_auto_approve_audit() -> None:
    assert aa.parse_latest_auto_approve_audit(None) is None
    assert aa.parse_latest_auto_approve_audit("") is None
    notes = (
        "manual note\n"
        '[auto_approve] {"approved": false, "reasons": ["no_prior_approved_mis"]}\n'
        '[auto_approve] {"approved": true, "reasons": [], "pct_delta": 2.1}\n'
    )
    parsed = aa.parse_latest_auto_approve_audit(notes)
    assert parsed is not None
    assert parsed["approved"] is True
    assert parsed["pct_delta"] == 2.1


def test_prior_month_start() -> None:
    assert aa.prior_month_start(date(2026, 5, 1)) == date(2026, 4, 1)
    assert aa.prior_month_start(date(2026, 1, 15)) == date(2025, 12, 1)


def test_pct_delta_abs_symmetric() -> None:
    assert aa._pct_delta_abs(Decimal("105"), Decimal("100")) == pytest.approx(5.0)
    assert aa._pct_delta_abs(Decimal("95"), Decimal("100")) == pytest.approx(5.0)
    assert aa._pct_delta_abs(Decimal("106"), Decimal("100")) == pytest.approx(6.0)
    assert aa._pct_delta_abs(Decimal("1"), Decimal("0")) is None


def test_is_employee_row() -> None:
    assert aa._is_employee_row("E001") is True
    assert aa._is_employee_row(NON_EMPLOYEE_SENTINEL) is False
    assert aa._is_employee_row("") is False


def test_rate_amount_fingerprint_part_normalizes_equivalent_values() -> None:
    assert aa._rate_amount_fingerprint_part(None) == ""
    assert aa._rate_amount_fingerprint_part("") == ""
    assert aa._rate_amount_fingerprint_part("6000.0000") == "6000"
    assert aa._rate_amount_fingerprint_part("6000") == "6000"
    assert aa._rate_amount_fingerprint_part("31000.00") == "31000"
    assert aa._rate_amount_fingerprint_part(Decimal("1050.50")) == "1050.5"


def test_rate_amount_fingerprint_part_invalid_falls_back_without_raising() -> None:
    assert aa._rate_amount_fingerprint_part("not-a-number") == "not-a-number"
    assert aa._rate_amount_fingerprint_part(" 6000 ") == "6000"


@pytest.mark.asyncio
async def test_fingerprint_matches_when_rate_text_differs_but_amount_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6000.0000 vs 6000 must not fail contract_rate_line_fingerprint_mismatch."""
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)

    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())
    ctv = str(uuid4())
    prior_ctv = str(uuid4())

    rate_line = {
        "role_code": "NURSE",
        "billing_model": "rate_attendance",
        "contracted_quantity": "1",
    }
    snap_rows = [
        {"role_code": "NURSE", "employee_external_id": "E1", "final_amount": Decimal("6000")},
    ]

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {"validation": {"status": "ok"}, "summary_rows": []},
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": prior_ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                ctv_param = str(params.get("ctv", ""))
                if ctv_param == ctv:
                    return _Result(rows=[{**rate_line, "rate_amount": "6000.0000"}])
                return _Result(rows=[{**rate_line, "rate_amount": "6000"}])
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=snap_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert "contract_rate_line_fingerprint_mismatch" not in d.reasons


@pytest.mark.asyncio
async def test_evaluate_blocks_when_handover_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "FROM o2c_mis_run" in sql and "WHERE id = CAST" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": str(uuid4()),
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {
                            "validation": {"status": "ok"},
                            "handover": {"applied": True, "pairs": []},
                        },
                    }
                )
            if "status = 'approved'" in sql and "billing_period_start" in sql:
                return _Result(first_row=None)
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql:
                return _Result(rows=[])
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=[])
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert "lwd_doj_handover_applied" in d.reasons


@pytest.mark.asyncio
async def test_recon_check_rolls_back_on_undefined_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undefined-table on recon must not poison the session for later eligibility SQL."""
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    mid = str(uuid4())
    ss_id = str(uuid4())
    rollback_calls: list[bool] = []

    class _Result:
        def __init__(self, first_row: dict | None = None, rows: list[dict] | None = None):
            self._first = first_row
            self._rows = rows or []

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "o2c_attendance_site_recon" in sql:
                raise ProgrammingError(
                    "stmt",
                    {},
                    asyncpg.exceptions.UndefinedTableError('relation "o2c_attendance_site_recon" does not exist'),
                )
            if "FROM o2c_mis_run" in sql and "WHERE id = CAST" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": str(uuid4()),
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {"validation": {"status": "ok"}},
                    }
                )
            if "status = 'approved'" in sql and "billing_period_start" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql:
                return _Result(rows=[])
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=[])
            return _Result()

        async def rollback(self):
            rollback_calls.append(True)

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert rollback_calls == [True]
    assert d.eligible is False
    assert "no_prior_approved_mis" in d.reasons


@pytest.mark.asyncio
async def test_evaluate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", False)
    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert "auto_approve_disabled" in d.reasons


@pytest.mark.asyncio
async def test_try_auto_approve_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", False)
    out = await aa.try_auto_approve_mis_run(uuid4())
    assert out["attempted"] is False


@pytest.mark.asyncio
async def test_evaluate_no_prior_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "FROM o2c_mis_run" in sql and "WHERE id = CAST" in sql:
                return _Result(
                    first_row={
                        "id": str(params["mid"]),
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": str(uuid4()),
                        "contract_terms_version_id": str(uuid4()),
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {"validation": {"status": "ok"}, "summary_rows": []},
                    }
                )
            if "status = 'approved'" in sql and "billing_period_start" in sql:
                return _Result(first_row=None)
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql:
                return _Result(rows=[])
            if "o2c_mis_summary_row" in sql and "NOT is_omitted" in sql:
                return _Result(rows=[])
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(session, tv, ss):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert "no_prior_approved_mis" in d.reasons


@pytest.mark.asyncio
async def test_evaluate_eligible_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)

    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())
    ctv = str(uuid4())

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    snap_rows = [
        {
            "role_code": "NURSE",
            "employee_external_id": "E1",
            "final_amount": Decimal("1000"),
        },
        {
            "role_code": "MO",
            "employee_external_id": NON_EMPLOYEE_SENTINEL,
            "final_amount": Decimal("500"),
        },
    ]

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {
                            "validation": {"status": "ok"},
                            "summary_rows": [
                                {
                                    "contract_rate_line_id": str(uuid4()),
                                    "employee_external_id": "E1",
                                    "is_omitted": False,
                                }
                            ],
                        },
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                return _Result(
                    rows=[
                        {
                            "role_code": "NURSE",
                            "billing_model": "rate_attendance",
                            "contracted_quantity": "1",
                            "rate_amount": "31000",
                        }
                    ]
                )
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=snap_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(session, tv, ss):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is True
    assert d.reasons == []
    assert d.pct_delta == pytest.approx(0.0)
    assert d.line_item_pct_delta == pytest.approx(0.0)
    assert d.current_line_item_total == Decimal("500")


@pytest.mark.asyncio
async def test_evaluate_ignores_validation_needs_human_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """D (validation status / tier) removed — needs_human_review alone must not block."""
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)

    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())
    ctv = str(uuid4())

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    snap_rows = [
        {
            "role_code": "NURSE",
            "employee_external_id": "E1",
            "final_amount": Decimal("1000"),
        },
    ]

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {
                            "validation": {"status": "needs_human_review"},
                            "summary_rows": [],
                        },
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                return _Result(
                    rows=[
                        {
                            "role_code": "NURSE",
                            "billing_model": "rate_attendance",
                            "contracted_quantity": "1",
                            "rate_amount": "31000",
                        }
                    ]
                )
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=snap_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is True
    assert "validation_needs_human_review" not in d.reasons


@pytest.mark.asyncio
async def test_evaluate_blocks_line_item_total_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)

    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())
    ctv = str(uuid4())

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    current_rows = [
        {"role_code": "MO", "employee_external_id": "E1", "final_amount": Decimal("1000")},
        {
            "role_code": "FEE",
            "employee_external_id": NON_EMPLOYEE_SENTINEL,
            "final_amount": Decimal("600"),
        },
    ]
    prior_rows = [
        {"role_code": "MO", "employee_external_id": "E1", "final_amount": Decimal("1000")},
        {
            "role_code": "FEE",
            "employee_external_id": NON_EMPLOYEE_SENTINEL,
            "final_amount": Decimal("500"),
        },
    ]

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {"validation": {"status": "ok"}, "summary_rows": []},
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                return _Result(
                    rows=[
                        {
                            "role_code": "MO",
                            "billing_model": "rate_attendance",
                            "contracted_quantity": "1",
                            "rate_amount": "1000",
                        }
                    ]
                )
            if "o2c_mis_summary_row" in sql:
                target = str(params.get("mid", ""))
                if target == prior_id:
                    return _Result(rows=prior_rows)
                return _Result(rows=current_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert any(r.startswith("line_item_total_delta_") for r in d.reasons)


@pytest.mark.asyncio
async def test_evaluate_blocks_contract_rate_amount_fingerprint_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)

    mid = str(uuid4())
    prior_id = str(uuid4())
    ss_id = str(uuid4())
    ctv = str(uuid4())
    prior_ctv = str(uuid4())

    class _Result:
        def __init__(self, rows: list[dict] | None = None, first_row: dict | None = None):
            self._rows = rows or []
            self._first = first_row

        def mappings(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._rows

    snap_rows = [
        {"role_code": "NURSE", "employee_external_id": "E1", "final_amount": Decimal("1000")},
    ]
    rate_line = {
        "role_code": "NURSE",
        "billing_model": "rate_attendance",
        "contracted_quantity": "1",
    }

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "WHERE id = CAST" in sql and "o2c_mis_run" in sql:
                return _Result(
                    first_row={
                        "id": mid,
                        "status": "pending_human",
                        "client_site_key": "Site-A",
                        "service_site_id": ss_id,
                        "contract_terms_version_id": ctv,
                        "billing_period_start": date(2026, 5, 1),
                        "billing_period_end": date(2026, 5, 31),
                        "summary_json": {"validation": {"status": "ok"}, "summary_rows": []},
                    }
                )
            if "status = 'approved'" in sql:
                return _Result(
                    first_row={"id": prior_id, "contract_terms_version_id": prior_ctv}
                )
            if "o2c_attendance_site_recon" in sql:
                return _Result(first_row=None)
            if "human_correction" in sql:
                return _Result(first_row=None)
            if "contract_rate_line" in sql and "is_active" in sql:
                ctv_param = str(params.get("ctv", ""))
                if ctv_param == ctv:
                    return _Result(rows=[{**rate_line, "rate_amount": "32000"}])
                return _Result(rows=[{**rate_line, "rate_amount": "31000"}])
            if "o2c_mis_summary_row" in sql:
                return _Result(rows=snap_rows)
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert "contract_rate_line_fingerprint_mismatch" in d.reasons


@pytest.mark.asyncio
async def test_build_bill_snapshot_line_item_total() -> None:
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "role_code": "NURSE",
                    "employee_external_id": "E1",
                    "final_amount": Decimal("1000"),
                },
                {
                    "role_code": "FEE",
                    "employee_external_id": NON_EMPLOYEE_SENTINEL,
                    "final_amount": Decimal("250.50"),
                },
            ]

    class _Session:
        async def execute(self, query, params=None):
            return _Result()

    snap = await aa._build_bill_snapshot(_Session(), str(uuid4()))
    assert snap.total_amount == Decimal("1250.50")
    assert snap.line_item_count == 1
    assert snap.line_item_total == Decimal("250.50")
    assert snap.employee_count_by_role == {"NURSE": 1}


@pytest.mark.asyncio
async def test_build_bill_snapshot_service_charge_counts_as_line_item() -> None:
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "role_code": "SC",
                    "employee_external_id": MIS_SERVICE_CHARGE_EXTERNAL_ID,
                    "final_amount": Decimal("99"),
                },
            ]

    class _Session:
        async def execute(self, query, params=None):
            return _Result()

    snap = await aa._build_bill_snapshot(_Session(), str(uuid4()))
    assert snap.line_item_count == 1
    assert snap.line_item_total == Decimal("99")
    assert snap.employee_count_by_role == {}


def test_evaluate_eligibility_source_has_no_quality_gate() -> None:
    import inspect

    src = inspect.getsource(aa.evaluate_auto_approve_eligibility)
    assert "get_mis_run" not in src
    assert "_quality_reasons" not in src


@pytest.mark.asyncio
async def test_evaluate_blocks_line_item_total_prior_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    mid, prior_id, ss_id, ctv = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    row = {"role_code": "MO", "employee_external_id": "E1", "final_amount": Decimal("1000")}
    fee = {
        "role_code": "FEE",
        "employee_external_id": NON_EMPLOYEE_SENTINEL,
        "final_amount": Decimal("200"),
    }
    monkeypatch.setattr(
        aa,
        "AgenosAsyncSessionLocal",
        lambda: _eligible_session_factory(
            mid=mid,
            prior_id=prior_id,
            ss_id=ss_id,
            ctv=ctv,
            current_rows=[row, fee],
            prior_rows=[row],
        )(),
    )

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert "line_item_total_prior_zero" in d.reasons


@pytest.mark.asyncio
async def test_evaluate_line_item_delta_at_tolerance_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)
    mid, prior_id, ss_id, ctv = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    emp = {"role_code": "MO", "employee_external_id": "E1", "final_amount": Decimal("10000")}
    prior_fee = {
        "role_code": "FEE",
        "employee_external_id": NON_EMPLOYEE_SENTINEL,
        "final_amount": Decimal("500"),
    }
    current_fee = {**prior_fee, "final_amount": Decimal("525")}
    monkeypatch.setattr(
        aa,
        "AgenosAsyncSessionLocal",
        lambda: _eligible_session_factory(
            mid=mid,
            prior_id=prior_id,
            ss_id=ss_id,
            ctv=ctv,
            current_rows=[emp, current_fee],
            prior_rows=[emp, prior_fee],
        )(),
    )

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is True
    assert d.line_item_pct_delta == pytest.approx(5.0)
    assert not any(r.startswith("line_item_total_delta_") for r in d.reasons)


@pytest.mark.asyncio
async def test_evaluate_grand_total_ok_line_item_over_tolerance_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Employee total unchanged; fee row +25% — grand total may stay within 5% but line-item check fails."""
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_enabled", True)
    monkeypatch.setattr(aa.settings, "o2c_mis_auto_approve_tolerance_pct", 5.0)
    mid, prior_id, ss_id, ctv = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    emp = {"role_code": "MO", "employee_external_id": "E1", "final_amount": Decimal("10000")}
    prior_fee = {
        "role_code": "FEE",
        "employee_external_id": NON_EMPLOYEE_SENTINEL,
        "final_amount": Decimal("400"),
    }
    current_fee = {**prior_fee, "final_amount": Decimal("500")}
    monkeypatch.setattr(
        aa,
        "AgenosAsyncSessionLocal",
        lambda: _eligible_session_factory(
            mid=mid,
            prior_id=prior_id,
            ss_id=ss_id,
            ctv=ctv,
            current_rows=[emp, current_fee],
            prior_rows=[emp, prior_fee],
        )(),
    )

    async def _empty_rate_lines(*_a, **_k):
        return []

    monkeypatch.setattr(aa, "_rate_lines_for_site_async", _empty_rate_lines)

    d = await aa.evaluate_auto_approve_eligibility(uuid4())
    assert d.eligible is False
    assert any(r.startswith("line_item_total_delta_") for r in d.reasons)
    assert d.pct_delta == pytest.approx(100 / 10400 * 100, rel=1e-3)


@pytest.mark.asyncio
async def test_persist_audit_includes_line_item_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Session:
        def begin(self):
            return _Begin()

        async def execute(self, query, params=None):
            captured["note"] = params["note"]

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aa, "AgenosAsyncSessionLocal", lambda: _Ctx())

    decision = aa.AutoApproveDecision(
        eligible=False,
        reasons=["test"],
        current_line_item_total=Decimal("525"),
        prior_line_item_total=Decimal("500"),
        line_item_pct_delta=5.0,
    )
    run_id = uuid4()
    await aa._persist_auto_approve_audit(run_id, decision=decision, approved=False)

    assert "[auto_approve]" in captured["note"]
    payload = captured["note"].replace("[auto_approve] ", "", 1)
    import json

    parsed = json.loads(payload)
    assert parsed["current_line_item_total"] == "525"
    assert parsed["prior_line_item_total"] == "500"
    assert parsed["line_item_pct_delta"] == 5.0


@pytest.mark.asyncio
async def test_attach_auto_approve_skips_non_ok_draft() -> None:
    resp = await aa.attach_auto_approve_after_draft(
        {"status": "failed"}, mis_run_id=None, draft_status="failed"
    )
    assert resp["auto_approve"]["attempted"] is False


@pytest.mark.asyncio
async def test_graph_mis_node_calls_auto_approve_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.o2c_ohc import graph as g

    called: list[str] = []

    async def _fake_draft(**_kwargs):
        from app.agents.o2c_ohc.mis_drafts import MisDraftResult

        return MisDraftResult(
            status="ok",
            mis_run_id="run-123",
            client_site_key="Site-A",
            xlsx_path="/tmp/x.xlsx",
        )

    async def _fake_attach(resp, *, mis_run_id, draft_status):
        called.append(str(mis_run_id))
        resp["auto_approve"] = {"attempted": True, "approved": False, "reasons": ["test"]}
        return resp

    async def _fake_finalize(**_kwargs):
        return "/tmp/x.xlsx", None

    async def _fake_existing(**_kwargs):
        return {}

    class _Parsed:
        amap = {"Site-A": [{"employee_external_id": "E1"}]}
        resolved_path = None

    monkeypatch.setattr(g, "create_or_refresh_mis_draft_for_site_async", _fake_draft)
    monkeypatch.setattr(g, "attach_auto_approve_after_draft", _fake_attach)
    monkeypatch.setattr(g, "finalize_mis_xlsx_to_gdrive_after_draft_async", _fake_finalize)
    monkeypatch.setattr(g, "client_site_keys_with_existing_mis_summary", _fake_existing)
    monkeypatch.setattr(
        g,
        "load_parsed_ohc_attendance_workbook_cached",
        lambda **_: _Parsed(),
    )
    monkeypatch.setattr(g, "build_mis_drive_svc", lambda: object())
    monkeypatch.setattr(
        g.settings,
        "o2c_invoice_out_dir",
        "/tmp",
    )
    monkeypatch.setattr(
        g.settings,
        "o2c_invoice_mis_template_path",
        "/tmp/tpl.xlsx",
    )

    def _noop_unlink(_paths):
        return None

    monkeypatch.setattr(g, "unlink_o2c_temp_paths", _noop_unlink)

    async def _run_blocking(fn):
        return fn()

    monkeypatch.setattr(g, "run_blocking", _run_blocking)

    out = await g._node_mis(
        {
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "attendance_xlsx": "/tmp/att.xlsx",
        }
    )
    assert called == ["run-123"]
    assert out["mis_results"][0]["auto_approve"]["attempted"] is True


@pytest.mark.asyncio
async def test_graph_skipped_existing_pending_human_auto_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.o2c_ohc import graph as g

    aa_called: list[str] = []

    async def _fake_try(mid):
        aa_called.append(str(mid))
        return {"attempted": True, "approved": True, "reasons": []}

    existing_id = "a1b2c3d4-e5f6-4789-a012-3456789abcde"

    async def _fake_existing(**_kwargs):
        return {"Site-A": {"mis_run_id": existing_id, "status": "pending_human"}}

    class _Parsed:
        amap = {"Site-A": []}
        resolved_path = None

    monkeypatch.setattr(g, "try_auto_approve_mis_run", _fake_try)
    monkeypatch.setattr(g, "client_site_keys_with_existing_mis_summary", _fake_existing)
    monkeypatch.setattr(
        g,
        "load_parsed_ohc_attendance_workbook_cached",
        lambda **_: _Parsed(),
    )
    monkeypatch.setattr(g, "build_mis_drive_svc", lambda: object())
    monkeypatch.setattr(g.settings, "o2c_invoice_out_dir", "/tmp")
    monkeypatch.setattr(g.settings, "o2c_invoice_mis_template_path", "/tmp/tpl.xlsx")
    monkeypatch.setattr(g, "unlink_o2c_temp_paths", lambda _paths: None)

    async def _run_blocking(fn):
        return fn()

    monkeypatch.setattr(g, "run_blocking", _run_blocking)

    out = await g._node_mis(
        {
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "attendance_xlsx": "/tmp/att.xlsx",
        }
    )
    assert aa_called == [existing_id]
    assert out["mis_results"][0]["reason"] == "mis_summary_exists"
    assert out["mis_results"][0]["auto_approve"]["approved"] is True


def test_rerun_attempt_auto_approve_defaults_false() -> None:
    import inspect

    sig = inspect.signature(mw.rerun_mis_for_existing_run)
    assert sig.parameters["attempt_auto_approve"].default is False


@pytest.mark.asyncio
async def test_save_rerun_disables_auto_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"flag": None}

    async def _fake_rerun(**kwargs):
        called["flag"] = kwargs.get("attempt_auto_approve")
        return {"status": "ok", "rerun": {"status": "ok"}}

    async def _fake_persist(**_kwargs):
        return {"ok": True, "correction_count": 0, "mis_status": "pending_human"}

    monkeypatch.setattr(mw, "rerun_mis_for_existing_run", _fake_rerun)
    monkeypatch.setattr(mw, "persist_mis_row_edits", _fake_persist)

    await mw.save_mis_with_rerun(
        mis_run_id=uuid4(),
        row_edits=[],
        saved_by="tester",
    )
    assert called["flag"] is False
