"""Order RCA run store — idempotency and TTL."""

from __future__ import annotations

import json

import pytest

from app.agents.order_rca import run_store
from app.config.settings import settings


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._ex: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        if ex is not None:
            self._ex[key] = ex

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._ex.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    return fake


@pytest.mark.asyncio
async def test_idempotency_reuses_queued_run(fake_redis):
    doc = await run_store.create_run("PO1", user_id="u1")
    again = await run_store.find_reusable_run("u1", "po1")
    assert again is not None
    assert again["run_id"] == doc["run_id"]
    assert fake_redis._ex[f"order_rca:idem:u1:PO1"] == 300
    assert fake_redis._ex[f"order_rca:run:{doc['run_id']}"] == 300


@pytest.mark.asyncio
async def test_idempotency_does_not_reuse_failed_run(fake_redis):
    doc = await run_store.create_run("PO2", user_id="u1")
    await run_store.patch_run(doc["run_id"], status="failed", error={"code": "x", "message": "y"})
    assert await run_store.find_reusable_run("u1", "PO2") is None


@pytest.mark.asyncio
async def test_idempotency_reuses_completed_run(fake_redis):
    doc = await run_store.create_run("PO3", user_id="u1")
    await run_store.patch_run(
        doc["run_id"],
        status="completed",
        report={"order_id": "PO3", "schema_version": 1},
    )
    again = await run_store.find_reusable_run("u1", "PO3")
    assert again is not None
    assert again["status"] == "completed"


@pytest.mark.asyncio
async def test_stale_run_clears_idempotency(fake_redis, monkeypatch):
    from datetime import UTC, datetime, timedelta

    doc = await run_store.create_run("PO4", user_id="u1")
    stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    raw = await fake_redis.get(f"order_rca:run:{doc['run_id']}")
    stored = json.loads(raw)
    stored["updated_at"] = stale_ts
    stored["status"] = "running"
    await fake_redis.set(f"order_rca:run:{doc['run_id']}", json.dumps(stored), ex=300)

    out = await run_store.get_run(doc["run_id"])
    assert out["status"] == "failed"
    assert await run_store.find_reusable_run("u1", "PO4") is None
