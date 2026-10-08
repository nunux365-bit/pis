"""Order RCA HTTP retry helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.agents.order_rca import sources
from app.config.settings import settings


@pytest.mark.asyncio
async def test_get_json_retries_transient_error(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_http_retries", 2)
    client = MagicMock()
    req = httpx.Request("GET", "http://example.test/x")
    ok = httpx.Response(200, json={"ok": True}, request=req)
    client.get = AsyncMock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.ConnectError("down"),
            ok,
        ]
    )
    out = await sources._get_json(client, "http://example.test/x")
    assert out == {"ok": True}
    assert client.get.await_count == 3


@pytest.mark.asyncio
async def test_get_json_retries_503(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_http_retries", 1)
    client = MagicMock()
    req = httpx.Request("GET", "http://example.test/x")
    bad = httpx.Response(503, request=req)
    ok = httpx.Response(200, json={"data": 1}, request=req)
    client.get = AsyncMock(side_effect=[bad, ok])
    out = await sources._get_json(client, "http://example.test/x")
    assert out == {"data": 1}
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_post_explain_allocation_404_returns_empty_data():
    client = MagicMock()
    req = httpx.Request("POST", "http://sla.test/v1/analytics/PO1/explain_allocation")
    not_found = httpx.Response(404, request=req)
    client.post = AsyncMock(return_value=not_found)
    out = await sources._post_explain_allocation(client, "http://sla.test/v1/analytics/PO1/explain_allocation")
    assert out == {"data": {}}
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_post_explain_allocation_retries_503(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_http_retries", 1)
    client = MagicMock()
    req = httpx.Request("POST", "http://sla.test/v1/analytics/PO1/explain_allocation")
    bad = httpx.Response(503, request=req)
    ok = httpx.Response(200, json={"data": {"PO1": {}}}, request=req)
    client.post = AsyncMock(side_effect=[bad, ok])
    out = await sources._post_explain_allocation(
        client, "http://sla.test/v1/analytics/PO1/explain_allocation"
    )
    assert out == {"data": {"PO1": {}}}
    assert client.post.await_count == 2
