"""Order RCA graph — integration with fixtures + mock LLM."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.order_rca import graph, run_store
from app.agents.order_rca.constants import ORDER_RCA_INTERNAL_USER_ID
from app.config.settings import settings

CHILD = "PO13326295207344"


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def get(self, key: str) -> str | None:
        return self._data.get(key)


@pytest.mark.asyncio
async def test_graph_build_facts_uses_run_blocking(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
    monkeypatch.setattr(settings, "order_rca_mock_llm", True)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)

    blocking_calls: list[object] = []

    async def _capture_blocking(factory):
        blocking_calls.append(factory)
        return factory()

    with patch("app.agents.order_rca.graph.run_blocking", side_effect=_capture_blocking):
        doc = await run_store.create_run(CHILD, user_id="test-user")
        await graph.run_graph(doc["run_id"], CHILD)

    assert blocking_calls, "expected build_facts to run via run_blocking"
    assert blocking_calls[0]().__getitem__("order_id") == CHILD


@pytest.mark.asyncio
async def test_graph_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
    monkeypatch.setattr(settings, "order_rca_mock_llm", True)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)

    doc = await run_store.create_run(CHILD, user_id="test-user")
    rid = doc["run_id"]
    await graph.run_graph(rid, CHILD)
    raw = await fake.get(f"order_rca:run:{rid}")
    assert raw is not None
    out = json.loads(raw)
    assert out["status"] == "completed"
    assert out["report"]["synthesis"]["verdict"]
    assert out["report"]["facts"]["order_id"] == CHILD
    assert "order_details" not in out["report"]


@pytest.mark.asyncio
async def test_graph_internal_skips_synthesis_and_exports_order_details(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
    monkeypatch.setattr(settings, "order_rca_run_ttl_sec", 300)
    fake = _FakeRedis()
    monkeypatch.setattr("app.agents.order_rca.run_store.get_redis", lambda: fake)

    with patch("app.agents.order_rca.graph.synthesize.synthesize_rca", new_callable=AsyncMock) as synth:
        doc = await run_store.create_run(CHILD, user_id=ORDER_RCA_INTERNAL_USER_ID)
        rid = doc["run_id"]
        await graph.run_graph(rid, CHILD)

    synth.assert_not_awaited()
    raw = await fake.get(f"order_rca:run:{rid}")
    out = json.loads(raw)
    report = out["report"]
    assert out["status"] == "completed"
    assert report["synthesis_source"] == "skipped"
    assert report["synthesis"]["verdict"] == ""
    assert report["synthesis"]["hypotheses"] == []
    assert "order_details" in report
    assert report["order_details"]["email"]
    assert report["order_details"]["delivery_address"]
    assert report["order_details"]["order_id"] == CHILD
    assert "parent_order_details" in report
    assert report["payment_details"]["is_success"] is True
    assert report["payment_details"]["data"][0]["order_id"] == CHILD
