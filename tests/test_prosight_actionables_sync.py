"""Unit tests for the Prosight actionables sync (mocked DB session).

Regression cover for the empty-fetch wipe: an unconditional
`UPDATE ... SET is_current=False` used to run *before* the insert guard, so a
zero-row Databricks response demoted every actionable and re-inserted nothing —
every BU then read "not_processed" until the next good sync.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy.sql.dml import Insert, Update

from app.agents.optimus.prosight.databricks_sync import (
    actionables_window_start,
    fetch_actionables_from_databricks,
    sync_prosight_from_databricks,
)
from app.agents.optimus.prosight.service import (
    _to_float,
    _to_int,
    sync_actionables,
)
from app.config.settings import settings


def _session() -> AsyncMock:
    return AsyncMock()



def _patch_actionables_claim(monkeypatch, granted: bool = True) -> None:
    """Stand in for the skip-locked actionables claim so this file stays DB-free."""

    @asynccontextmanager
    async def _claim(_name=None):
        yield object() if granted else None

    monkeypatch.setattr(
        "app.agents.optimus.prosight.databricks_sync.claimed_session", _claim
    )


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "as_of_date": "2026-08-15",
        "bu": "pharma",
        "segment": "metro",
        "lens": "orders",
        "rank": 1,
        "action": "Push L2 coverage in Delhi.",
        "action_id": "a-1",
    }
    row.update(overrides)
    return row


def _statements(session: AsyncMock) -> list[Any]:
    return [call.args[0] for call in session.execute.await_args_list]


async def test_empty_rows_leave_the_live_set_untouched() -> None:
    session = _session()

    assert await sync_actionables([], session=session) == 0

    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_rows_that_all_get_skipped_leave_the_live_set_untouched() -> None:
    """A payload of unusable rows is the same hazard as an empty one."""
    session = _session()

    rows = [_row(action="   "), _row(action="", action_id="a-2")]
    assert await sync_actionables(rows, session=session) == 0

    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_demote_runs_after_the_insert_and_is_scoped_to_stale_rows() -> None:
    session = _session()

    assert await sync_actionables([_row()], session=session) == 1

    stmts = _statements(session)
    assert len(stmts) == 2
    assert isinstance(stmts[0], Insert)
    assert isinstance(stmts[1], Update)
    # The demote must never be a blanket UPDATE again — it is keyed to rows this
    # run did not stamp with the current synced_at.
    assert stmts[1].whereclause is not None
    session.commit.assert_not_awaited()


async def test_upsert_is_chunked_to_stay_under_the_bind_parameter_cap() -> None:
    """One statement for every row would blow asyncpg's 32767-parameter Bind limit."""
    session = _session()

    rows = [_row(action_id=f"a-{i}", action=f"Action {i}") for i in range(1100)]
    assert await sync_actionables(rows, session=session) == 1100

    stmts = _statements(session)
    inserts = [s for s in stmts if isinstance(s, Insert)]
    assert len(inserts) > 1
    for stmt in inserts:
        # 32 bound columns a row — keep each statement well under the int16 cap.
        assert len(stmt.compile().params) < 32767


async def test_demote_is_scoped_to_the_fetched_window() -> None:
    """Days older than the window were never fetched, so they must not be demoted."""
    session = _session()

    await sync_actionables([_row()], session=session, window_start=date(2026, 7, 17))

    demote = _statements(session)[-1]
    assert isinstance(demote, Update)
    assert "as_of_date" in str(demote.whereclause)


async def test_actionables_query_is_date_bounded_and_limited(monkeypatch) -> None:
    """An unbounded scan outgrows the query timeout as the source table accumulates days."""
    module = "app.agents.optimus.prosight.databricks_sync"
    captured: dict[str, str] = {}

    async def _capture(query: str) -> dict:
        captured["query"] = query
        return {"manifest": {"schema": {"columns": []}}, "result": {"data_array": []}}

    monkeypatch.setattr(f"{module}._execute_databricks_query", _capture)

    await fetch_actionables_from_databricks()

    query = captured["query"]
    window_start = actionables_window_start()
    assert f"WHERE as_of_date >= DATE '{window_start.isoformat()}'" in query
    assert "LIMIT" in query
    # The QUALIFY de-dups within a partition; it is not a date bound.
    assert query.index("WHERE") < query.index("QUALIFY")


async def test_window_start_spans_the_configured_lookback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "prosight_actionables_lookback_days", 30)

    start = actionables_window_start(today=date(2026, 8, 16))

    # Inclusive of both ends: 30 days of data, ending today.
    assert start == date(2026, 7, 18)


async def test_sync_reports_an_error_when_databricks_returns_no_actionables(
    monkeypatch,
) -> None:
    """Zero rows is a successful query returning nothing — it must not read as a clean sync."""
    module = "app.agents.optimus.prosight.databricks_sync"
    _patch_actionables_claim(monkeypatch)
    monkeypatch.setattr(
        f"{module}.fetch_prosight_data_from_databricks",
        AsyncMock(return_value={"snapshot_date": "2026-08-15", "data": {"summary": {}}}),
    )
    monkeypatch.setattr(f"{module}.upsert_snapshot", AsyncMock(return_value="snap-1"))
    monkeypatch.setattr(
        f"{module}.fetch_actionables_from_databricks", AsyncMock(return_value=[])
    )
    synced = AsyncMock(return_value=0)
    monkeypatch.setattr(f"{module}.sync_actionables", synced)

    result = await sync_prosight_from_databricks()

    assert result["status"] == "success"  # the dashboard half still succeeded
    assert "actionables_error" in result
    assert "actionables_synced" not in result
    synced.assert_not_awaited()


# ── Numeric coercion ─────────────────────────────────────────────────────────
# `_normalize_row` promises "a malformed cell becomes None rather than failing
# the batch". NaN/inf used to break that promise in the worst possible way:
# `int(nan)` raises, and nothing in sync_actionables catches it, so one bad cell
# from Databricks aborted every row in the run.


def test_non_finite_numbers_coerce_to_none_instead_of_raising() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "NaN", "-Infinity"):
        assert _to_float(bad) is None, bad
        assert _to_int(bad) is None, bad  # int(nan) raised ValueError before


def test_finite_coercion_is_unchanged() -> None:
    """The NaN guard must not disturb ordinary values — 0 especially, which is falsy."""
    assert _to_float("3.5") == 3.5
    assert _to_float(0) == 0.0
    assert _to_int("3") == 3
    assert _to_int(3.7) == 3  # truncates, as before
    assert _to_int(0) == 0
    assert _to_float(None) is None
    assert _to_float("abc") is None


async def test_a_nan_rank_blanks_one_field_rather_than_killing_the_batch() -> None:
    """The whole point: a single malformed cell must not cost the other rows."""
    session = _session()

    rows = [
        _row(action_id="a-1", rank=float("nan")),
        _row(action_id="a-2", rank=2),
    ]
    assert await sync_actionables(rows, session=session) == 2

    insert = _statements(session)[0]
    assert isinstance(insert, Insert)
    ranks = sorted(
        (p for k, p in insert.compile().params.items() if k.startswith("rank")),
        key=lambda v: (v is not None, v),
    )
    assert ranks == [None, 2]
    session.commit.assert_not_awaited()
