"""Tests for o2c_attendance_site_recon persistence (async agenos)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.agents.o2c_ohc.attendance_site_recon import (
    clear_open_o2c_attendance_site_recon_for_period_key,
    list_open_o2c_attendance_site_recon,
    persist_o2c_attendance_site_recon,
    resolve_o2c_attendance_site_recon,
)
from app.agents.o2c_ohc.o2c_utils import O2cAttendanceSiteSkip


class _FakeBeg:
    def __init__(self, session: "_FakeSession") -> None:
        self._s = session

    async def __aenter__(self) -> "_FakeSession":
        return self._s

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeSessCtx:
    def __init__(self, maker: "_FakeMaker") -> None:
        self._m = maker

    async def __aenter__(self) -> "_FakeSession":
        self._m._session = _FakeSession(self._m)
        return self._m._session

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, maker: "_FakeMaker") -> None:
        self._m = maker

    def begin(self) -> _FakeBeg:
        return _FakeBeg(self)

    async def execute(self, stmt: object, params: object | None = None) -> object:
        self._m.executes.append((str(stmt), params))
        return self._m.next_result()


class _FakeMaker:
    def __init__(self, next_result_factory: object) -> None:
        self.executes: list[tuple[str, object | None]] = []
        self._next_result_factory = next_result_factory
        self._session: _FakeSession | None = None

    def __call__(self) -> _FakeSessCtx:
        return _FakeSessCtx(self)

    def next_result(self) -> object:
        return self._next_result_factory(self.executes)


class _RowcountResult:
    def __init__(self, n: int = 1) -> None:
        self.rowcount = n


class _ListResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_ListResult":
        return self

    def all(self) -> list[object]:
        return [dict(r) for r in self._rows]


class _FirstResult:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    def mappings(self) -> "_FirstResult":
        return self

    def first(self) -> object | None:
        if not self._row:
            return None
        return dict(self._row)


def test_persist_o2c_attendance_site_recon_runs_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(_calls: list) -> _RowcountResult:
        return _RowcountResult(1)

    fm = _FakeMaker(factory)
    monkeypatch.setattr(
        "app.agents.o2c_ohc.attendance_site_recon.AgenosAsyncSessionLocal",
        fm,
    )

    n = persist_o2c_attendance_site_recon(
        [
            O2cAttendanceSiteSkip(
                client_site_key="Site-A",
                reason_code="unresolved_site",
                detail="x",
                attendance_row_count=2,
                llm_match_attempted=True,
            )
        ],
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
    )
    assert n == 1
    assert len(fm.executes) == 1
    sql, params = fm.executes[0]
    assert "INSERT INTO o2c_attendance_site_recon" in sql
    assert params == {
        "csk": "Site-A",
        "d0": date(2026, 2, 1),
        "d1": date(2026, 2, 28),
        "rc": "unresolved_site",
        "det": "x",
        "arc": 2,
        "llm": True,
    }


@pytest.mark.asyncio
async def test_list_open_o2c_attendance_site_recon(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "1", "client_site_key": "Site-A", "status": "open"}]

    def factory(_calls: list) -> _ListResult:
        return _ListResult(rows)

    fm = _FakeMaker(factory)
    monkeypatch.setattr(
        "app.agents.o2c_ohc.attendance_site_recon.AgenosAsyncSessionLocal",
        fm,
    )
    out = await list_open_o2c_attendance_site_recon(limit=10)
    assert out == rows


@pytest.mark.asyncio
async def test_resolve_o2c_attendance_site_recon_writes_alias_and_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recon_id = uuid4()
    site_id = uuid4()
    step = {"n": 0}

    def factory(calls: list) -> object:
        step["n"] += 1
        if step["n"] == 1:
            return _FirstResult({"ok": 1})
        if step["n"] == 2:
            return _RowcountResult(1)
        return _RowcountResult(1)

    fm = _FakeMaker(factory)
    monkeypatch.setattr(
        "app.agents.o2c_ohc.attendance_site_recon.AgenosAsyncSessionLocal",
        fm,
    )
    ok = await resolve_o2c_attendance_site_recon(
        recon_id=recon_id,
        service_site_id=site_id,
        alias_code="Unknown Site A",
        resolved_by="ops@acme.com",
        resolution_notes="mapped from dashboard",
    )
    assert ok is True
    assert any("INSERT INTO site_alias" in q for q, _ in fm.executes)
    assert any("DELETE FROM o2c_attendance_site_recon" in q for q, _ in fm.executes)


def test_clear_open_o2c_attendance_site_recon_for_period_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(_calls: list) -> _RowcountResult:
        return _RowcountResult(3)

    fm = _FakeMaker(factory)
    monkeypatch.setattr(
        "app.agents.o2c_ohc.attendance_site_recon.AgenosAsyncSessionLocal",
        fm,
    )
    n = clear_open_o2c_attendance_site_recon_for_period_key(
        client_site_key="Site-A",
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
    )
    assert n == 3
    assert len(fm.executes) == 1
    sql, params = fm.executes[0]
    assert "DELETE FROM o2c_attendance_site_recon" in sql
    assert params == {
        "csk": "Site-A",
        "d0": date(2026, 2, 1),
        "d1": date(2026, 2, 28),
    }


def test_clear_open_o2c_attendance_site_recon_empty_key() -> None:
    assert clear_open_o2c_attendance_site_recon_for_period_key(
        client_site_key="   ",
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
    ) == 0
