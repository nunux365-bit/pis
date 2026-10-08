"""Actionables write is skip-locked; dashboard upsert is unique last-write-wins.

Three entrypoints reach ``sync_prosight_from_databricks`` — ``/sync/trigger``,
``/sync/now`` and the daily job. Overlapping demotes are serialized by a
``resource_claims`` row (``prosight:actionables``), not an advisory lock.
Databricks fetches stay unlocked.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.agents.optimus.prosight.databricks_sync import sync_prosight_from_databricks

MODULE = "app.agents.optimus.prosight.databricks_sync"


def _patch_actionables_claim(monkeypatch, granted: bool) -> None:
    @asynccontextmanager
    async def _claim(_name):
        yield object() if granted else None

    monkeypatch.setattr(f"{MODULE}.claimed_session", _claim)


async def test_skips_actionables_write_when_claim_held(monkeypatch):
    _patch_actionables_claim(monkeypatch, granted=False)
    fetch = AsyncMock(
        return_value={"snapshot_date": "2026-08-15", "data": {"summary": {}}}
    )
    monkeypatch.setattr(f"{MODULE}.fetch_prosight_data_from_databricks", fetch)
    monkeypatch.setattr(f"{MODULE}.upsert_snapshot", AsyncMock(return_value="snap-1"))
    monkeypatch.setattr(
        f"{MODULE}.fetch_actionables_from_databricks",
        AsyncMock(return_value=[{"action_id": "a-1", "action": "do it", "bu": "x"}]),
    )
    synced = AsyncMock()
    monkeypatch.setattr(f"{MODULE}.sync_actionables", synced)

    result = await sync_prosight_from_databricks()

    assert result["status"] == "success"
    assert "in progress" in result["actionables_error"]
    fetch.assert_awaited_once()
    synced.assert_not_awaited()


async def test_writes_actionables_when_claim_is_free(monkeypatch):
    _patch_actionables_claim(monkeypatch, granted=True)
    monkeypatch.setattr(
        f"{MODULE}.fetch_prosight_data_from_databricks",
        AsyncMock(return_value={"snapshot_date": "2026-08-15", "data": {"summary": {}}}),
    )
    monkeypatch.setattr(f"{MODULE}.upsert_snapshot", AsyncMock(return_value="snap-1"))
    monkeypatch.setattr(
        f"{MODULE}.fetch_actionables_from_databricks",
        AsyncMock(return_value=[{"action_id": "a-1", "action": "do it", "bu": "x"}]),
    )
    synced = AsyncMock(return_value=1)
    monkeypatch.setattr(f"{MODULE}.sync_actionables", synced)

    result = await sync_prosight_from_databricks()

    assert result["status"] == "success"
    assert result["actionables_synced"] == 1
    synced.assert_awaited_once()


async def test_dashboard_error_does_not_need_the_claim(monkeypatch):
    claimed = AsyncMock()
    monkeypatch.setattr(f"{MODULE}.claimed_session", claimed)
    monkeypatch.setattr(
        f"{MODULE}.fetch_prosight_data_from_databricks",
        AsyncMock(side_effect=RuntimeError("databricks exploded")),
    )

    result = await sync_prosight_from_databricks()

    assert result["status"] == "error"
    claimed.assert_not_called()


@pytest.fixture
async def requires_db():
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal, engine

    await engine.dispose()
    session = AsyncSessionLocal()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database available: {type(exc).__name__}: {exc}")
    finally:
        await session.close()


async def test_second_claim_is_refused_while_the_first_holds(requires_db):
    from sqlalchemy.exc import ProgrammingError

    from app.infra.row_claim import claimed_session

    try:
        async with claimed_session("test:prosight-mutex") as first:
            assert first is not None
            async with claimed_session("test:prosight-mutex") as second:
                assert second is None
    except ProgrammingError as exc:
        pytest.skip(f"resource_claims not migrated: {exc}")
