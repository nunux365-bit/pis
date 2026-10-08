"""Human final_amount overrides preserved across MIS save+rerun."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.agents.o2c_ohc.mis_db import (
    AbsentDaysLock,
    FinalAmountLock,
    _absent_days_lock_from_summary_row,
    _merge_human_correction_json,
    _merge_preserved_human_correction_json,
    apply_absent_days_locks_to_attendance_records,
    build_final_amount_locks_from_db,
    build_final_amount_locks_from_row_edits,
    reapply_absent_days_locks_session,
    reapply_final_amount_locks,
)
from app.agents.o2c_ohc.mis_summary_llm import NON_EMPLOYEE_SENTINEL
from app.services.o2c import mis_workflow as mw


def _mock_session_factory(session_cls: type):
    class _Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Wrapped(session_cls):
        def begin(self):
            return _Begin()

    class _Ctx:
        async def __aenter__(self):
            return _Wrapped()

        async def __aexit__(self, *args):
            return None

    return lambda: _Ctx()


def test_employee_external_id_lock_key() -> None:
    from app.agents.o2c_ohc.mis_db import _employee_external_id_lock_key

    assert _employee_external_id_lock_key("E1") == "E1"
    assert _employee_external_id_lock_key("") is None
    assert _employee_external_id_lock_key(None) is None
    assert _employee_external_id_lock_key(NON_EMPLOYEE_SENTINEL) == NON_EMPLOYEE_SENTINEL


@pytest.mark.asyncio
async def test_build_final_amount_locks_from_row_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    mid = uuid4()
    row_id = str(uuid4())
    crl_id = str(uuid4())

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "id": row_id,
                    "contract_rate_line_id": crl_id,
                    "employee_external_id": "E99",
                }
            ]

    class _Session:
        async def execute(self, query, params=None):
            return _Result()

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    locks = await build_final_amount_locks_from_row_edits(
        mid,
        [
            {"id": row_id, "contractual_rate": 100},
            {"id": row_id, "final_amount": 42000.5},
        ],
    )
    assert len(locks) == 1
    assert locks[0].contract_rate_line_id == crl_id
    assert locks[0].employee_external_id == "E99"
    assert locks[0].final_amount == Decimal("42000.5")


@pytest.mark.asyncio
async def test_build_final_amount_locks_from_db_only_final_amount_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    crl_id = str(uuid4())

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "contract_rate_line_id": crl_id,
                    "employee_external_id": "E1",
                    "final_amount": Decimal("5000"),
                    "human_correction": {
                        "changes": {
                            "final_amount": {"old": 4000.0, "new": 5000.0},
                            "absent_days": {"old": 0, "new": 1},
                        }
                    },
                },
                {
                    "contract_rate_line_id": crl_id,
                    "employee_external_id": "E2",
                    "final_amount": Decimal("3000"),
                    "human_correction": {
                        "changes": {"absent_days": {"old": 0, "new": 2}},
                    },
                },
            ]

    class _Session:
        async def execute(self, query, params=None):
            return _Result()

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    locks = await build_final_amount_locks_from_db(mid)
    assert len(locks) == 1
    assert locks[0].contract_rate_line_id == crl_id
    assert locks[0].employee_external_id == "E1"
    assert locks[0].final_amount == Decimal("5000")


@pytest.mark.asyncio
async def test_build_final_amount_locks_empty_when_no_amount_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Ctx:
        async def __aenter__(self):
            raise AssertionError("should not open session")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        lambda: _Ctx(),
    )
    locks = await build_final_amount_locks_from_row_edits(
        uuid4(),
        [{"id": str(uuid4()), "absent_days": 2}],
    )
    assert locks == []


@pytest.mark.asyncio
async def test_reapply_final_amount_locks_updates_matching_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    crl = str(uuid4())
    updates: list[dict] = []

    class _SelResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class _UpResult:
        def __init__(self, n: int):
            self.rowcount = n

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "SELECT id::text" in sql:
                return _SelResult([{"id": str(uuid4()), "final_amount": Decimal("1000")}])
            if "UPDATE o2c_mis_summary_row" in sql:
                updates.append(dict(params or {}))
                return _UpResult(1)
            return _UpResult(0)

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    out = await reapply_final_amount_locks(
        mid,
        [
            FinalAmountLock(
                contract_rate_line_id=crl,
                employee_external_id="E1",
                final_amount=Decimal("5555"),
            )
        ],
        saved_by="tester",
    )
    assert out["lock_count"] == 1
    assert out["rows_updated"] == 1
    assert out["locks_unmatched"] == 0
    assert len(updates) == 1
    assert updates[0]["amt"] == Decimal("5555")
    assert "preserved_after_rerun" in updates[0]["hcorr"]


@pytest.mark.asyncio
async def test_reapply_skips_unmatched_lock_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SelResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Session:
        async def execute(self, query, params=None):
            return _SelResult()

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    out = await reapply_final_amount_locks(
        uuid4(),
        [
            FinalAmountLock(
                contract_rate_line_id=str(uuid4()),
                employee_external_id="MISSING",
                final_amount=Decimal("1"),
            )
        ],
        saved_by="tester",
    )
    assert out["rows_updated"] == 0
    assert out["locks_unmatched"] == 1


@pytest.mark.asyncio
async def test_save_mis_with_rerun_reapplies_amount_locks_after_ok_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    calls: list[str] = []

    async def _persist(**_kwargs):
        calls.append("persist")
        return {"ok": True, "correction_count": 1}

    async def _build_locks(_mid, row_edits):
        calls.append("build")
        assert row_edits[0]["final_amount"] == 999
        return [
            FinalAmountLock(
                contract_rate_line_id=str(uuid4()),
                employee_external_id="E1",
                final_amount=Decimal("999"),
            )
        ]

    rerun_kwargs: dict = {}

    async def _rerun(**kwargs):
        calls.append("rerun")
        rerun_kwargs.update(kwargs)
        return {
            "status": "ok",
            "rerun": {"status": "ok", "mis_run_id": str(mid)},
            "final_amount_locks": {
                "lock_count": 1,
                "rows_updated": 1,
                "locks_unmatched": 0,
            },
        }

    monkeypatch.setattr(mw, "persist_mis_row_edits", _persist)
    monkeypatch.setattr(mw, "build_final_amount_locks_from_row_edits", _build_locks)
    monkeypatch.setattr(mw, "rerun_mis_for_existing_run", _rerun)

    result = await mw.save_mis_with_rerun(
        mis_run_id=mid,
        row_edits=[{"id": str(uuid4()), "final_amount": 999}],
        saved_by="human@test",
    )
    assert calls == ["persist", "build", "rerun"]
    assert rerun_kwargs.get("attempt_auto_approve") is False
    assert rerun_kwargs.get("preserved_by") == "human@test"
    assert len(rerun_kwargs.get("amount_locks") or []) == 1
    assert result["final_amount_locks"]["rows_updated"] == 1


@pytest.mark.asyncio
async def test_save_mis_with_rerun_skips_reapply_when_no_amount_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    reapply_called = False

    async def _persist(**_kwargs):
        return {"ok": True, "correction_count": 0}

    async def _build_locks(_mid, _row_edits):
        return []

    async def _rerun(**_kwargs):
        return {"status": "ok", "rerun": {"status": "ok"}}

    monkeypatch.setattr(mw, "persist_mis_row_edits", _persist)
    monkeypatch.setattr(mw, "build_final_amount_locks_from_row_edits", _build_locks)
    monkeypatch.setattr(mw, "rerun_mis_for_existing_run", _rerun)

    result = await mw.save_mis_with_rerun(
        mis_run_id=mid,
        row_edits=[{"id": str(uuid4()), "absent_days": 1}],
        saved_by="human@test",
    )
    assert reapply_called is False
    assert "final_amount_locks" not in result


@pytest.mark.asyncio
async def test_save_mis_with_rerun_does_not_reapply_when_rerun_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    reapply_called = False

    async def _persist(**_kwargs):
        return {"ok": True, "correction_count": 1}

    async def _build_locks(_mid, _row_edits):
        return [
            FinalAmountLock(
                contract_rate_line_id=str(uuid4()),
                employee_external_id="E1",
                final_amount=Decimal("1"),
            )
        ]

    async def _rerun(**_kwargs):
        return {"status": "ok", "rerun": {"status": "failed", "reason": "llm_error"}}

    monkeypatch.setattr(mw, "persist_mis_row_edits", _persist)
    monkeypatch.setattr(mw, "build_final_amount_locks_from_row_edits", _build_locks)
    monkeypatch.setattr(mw, "rerun_mis_for_existing_run", _rerun)

    with pytest.raises(HTTPException):
        await mw.save_mis_with_rerun(
            mis_run_id=mid,
            row_edits=[{"id": str(uuid4()), "final_amount": 1}],
            saved_by="human@test",
        )
    assert reapply_called is False


@pytest.mark.asyncio
async def test_rerun_mis_builds_db_locks_and_reapplies_after_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    calls: list[str] = []
    lock = FinalAmountLock(
        contract_rate_line_id=str(uuid4()),
        employee_external_id="E1",
        final_amount=Decimal("12345"),
    )

    async def _context(_mid):
        return (
            {"service_site_id": str(uuid4())},
            "ALIAS1",
            date(2026, 5, 1),
            date(2026, 5, 31),
        )

    async def _build_db(_mid):
        calls.append("build_db")
        return [lock]

    async def _llm_rerun(**_kwargs):
        calls.append("llm_rerun")
        return {"status": "ok", "mis_run_id": str(mid)}

    async def _reapply(_mid, locks, *, saved_by):
        calls.append("reapply")
        assert locks == [lock]
        assert saved_by == "reviewer@test"
        return {"lock_count": 1, "rows_updated": 1, "locks_unmatched": 0}

    monkeypatch.setattr(mw, "_mis_run_rerun_context", _context)
    monkeypatch.setattr(mw, "build_final_amount_locks_from_db", _build_db)
    monkeypatch.setattr(mw, "run_single_site_mis_rerun_async", _llm_rerun)
    monkeypatch.setattr(mw, "reapply_final_amount_locks", _reapply)

    out = await mw.rerun_mis_for_existing_run(
        mis_run_id=mid,
        preserved_by="reviewer@test",
    )
    assert calls == ["build_db", "llm_rerun", "reapply"]
    assert out["final_amount_locks"]["rows_updated"] == 1


@pytest.mark.asyncio
async def test_rerun_mis_skips_reapply_when_rerun_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid = uuid4()
    reapply_called = False

    async def _context(_mid):
        return ({}, "ALIAS1", date(2026, 5, 1), date(2026, 5, 31))

    async def _build_db(_mid):
        return [
            FinalAmountLock(
                contract_rate_line_id=str(uuid4()),
                employee_external_id="E1",
                final_amount=Decimal("1"),
            )
        ]

    async def _llm_rerun(**_kwargs):
        return {"status": "failed", "reason": "llm_error"}

    async def _reapply(*_a, **_k):
        nonlocal reapply_called
        reapply_called = True
        return {}

    monkeypatch.setattr(mw, "_mis_run_rerun_context", _context)
    monkeypatch.setattr(mw, "build_final_amount_locks_from_db", _build_db)
    monkeypatch.setattr(mw, "run_single_site_mis_rerun_async", _llm_rerun)
    monkeypatch.setattr(mw, "reapply_final_amount_locks", _reapply)

    out = await mw.rerun_mis_for_existing_run(mis_run_id=mid)
    assert reapply_called is False
    assert "final_amount_locks" not in out


def test_absent_days_lock_from_summary_row() -> None:
    lock = _absent_days_lock_from_summary_row(
        {
            "employee_external_id": "E42",
            "absent_days": Decimal("2"),
            "human_correction": {
                "corrected_by": "reviewer@test",
                "changes": {"absent_days": {"old": 0, "new": 2}},
            },
        }
    )
    assert lock is not None
    assert lock.employee_external_id == "E42"
    assert lock.absent_days == Decimal("2")
    assert lock.corrected_by == "reviewer@test"


def test_absent_days_lock_ignores_non_employee_rows() -> None:
    assert _absent_days_lock_from_summary_row(
        {
            "employee_external_id": NON_EMPLOYEE_SENTINEL,
            "absent_days": 1,
            "human_correction": {"changes": {"absent_days": {"old": 0, "new": 1}}},
        }
    ) is None


def test_merge_preserved_human_correction_keeps_prior_fields() -> None:
    merged = json.loads(
        _merge_preserved_human_correction_json(
            {
                "corrected_by": "human",
                "preserved_after_rerun": True,
                "changes": {"absent_days": {"old": 0, "new": 2}},
            },
            corrected_by="rerun",
            field_changes={"final_amount": {"old": 1000.0, "new": 900.0}},
        )
    )
    assert merged["changes"]["absent_days"] == {"old": 0, "new": 2}
    assert merged["changes"]["final_amount"] == {"old": 1000.0, "new": 900.0}
    assert merged["preserved_after_rerun"] is True


def test_merge_human_correction_keeps_prior_absent_on_later_amount_edit() -> None:
    merged = json.loads(
        _merge_human_correction_json(
            {
                "corrected_by": "human",
                "changes": {"absent_days": {"old": 0, "new": 2}},
            },
            corrected_by="human",
            field_changes={"final_amount": {"old": 1000.0, "new": 900.0}},
        )
    )
    assert merged["changes"]["absent_days"] == {"old": 0, "new": 2}
    assert merged["changes"]["final_amount"] == {"old": 1000.0, "new": 900.0}
    assert "preserved_after_rerun" not in merged


def test_absent_days_lock_from_merged_human_correction() -> None:
    lock = _absent_days_lock_from_summary_row(
        {
            "employee_external_id": "E42",
            "absent_days": Decimal("2"),
            "human_correction": {
                "corrected_by": "human",
                "changes": {
                    "absent_days": {"old": 0, "new": 2},
                    "final_amount": {"old": 1000.0, "new": 900.0},
                },
            },
        }
    )
    assert lock is not None
    assert lock.absent_days == Decimal("2")


def test_apply_absent_days_locks_to_attendance_records() -> None:
    records = [
        {"employee_external_id": "E1", "absent_days": Decimal("0")},
        {"employee_external_id": "E2", "absent_days": Decimal("1")},
    ]
    n = apply_absent_days_locks_to_attendance_records(
        records,
        [AbsentDaysLock(employee_external_id="E1", absent_days=Decimal("3"))],
    )
    assert n == 1
    assert records[0]["absent_days"] == Decimal("3")
    assert records[1]["absent_days"] == Decimal("1")


@pytest.mark.asyncio
async def test_reapply_absent_days_locks_session_updates_rows() -> None:
    mid = str(uuid4())
    updates: list[dict] = []

    class _SelResult:
        def mappings(self):
            return self

        def all(self):
            return [{"id": str(uuid4()), "absent_days": Decimal("0")}]

    class _UpResult:
        rowcount = 1

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "SELECT id::text" in sql:
                return _SelResult()
            if "UPDATE o2c_mis_summary_row" in sql:
                updates.append(dict(params or {}))
                return _UpResult()
            return _UpResult()

    out = await reapply_absent_days_locks_session(
        _Session(),
        mid,
        [AbsentDaysLock(employee_external_id="E9", absent_days=Decimal("2"), corrected_by="human")],
        saved_by="human",
    )
    assert out["rows_updated"] == 1
    assert updates[0]["abd"] == Decimal("2")
