"""Unit tests for shared internal + Meta httpx clients."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.infra import httpx_clients as hc


@pytest.fixture(autouse=True)
async def _reset_clients():
    await hc.close_shared_http_clients()
    yield
    await hc.close_shared_http_clients()


@pytest.mark.asyncio
async def test_internal_client_is_singleton(monkeypatch):
    monkeypatch.setattr(hc.settings, "order_rca_http_timeout_sec", 12)
    a = hc.get_internal_http_client()
    b = hc.get_internal_http_client()
    assert a is b
    assert not a.is_closed


@pytest.mark.asyncio
async def test_meta_client_is_separate_from_internal():
    internal = hc.get_internal_http_client()
    meta = hc.get_meta_http_client()
    assert internal is not meta


@pytest.mark.asyncio
async def test_close_is_idempotent_and_recreates():
    first = hc.get_internal_http_client()
    meta = hc.get_meta_http_client()
    await hc.close_shared_http_clients()
    await hc.close_shared_http_clients()
    assert first.is_closed
    assert meta.is_closed
    assert hc._internal_client is None
    assert hc._meta_client is None
    second = hc.get_internal_http_client()
    assert second is not first
    assert not second.is_closed


@pytest.mark.asyncio
async def test_internal_client_has_no_default_auth_headers():
    client = hc.get_internal_http_client()
    # httpx stores defaults on headers; Authorization must not be baked in.
    assert "Authorization" not in client.headers
    assert "authorization" not in {k.lower() for k in client.headers.keys()}


@pytest.mark.asyncio
async def test_concurrent_gets_share_one_instance():
    clients = await asyncio.gather(
        *[asyncio.to_thread(hc.get_internal_http_client) for _ in range(40)]
    )
    assert all(c is clients[0] for c in clients)


@pytest.mark.asyncio
async def test_recreate_after_external_close():
    client = hc.get_internal_http_client()
    await client.aclose()
    again = hc.get_internal_http_client()
    assert again is not client
    assert not again.is_closed


@pytest.mark.asyncio
async def test_fetch_bundle_uses_shared_client(monkeypatch):
    from app.agents.order_rca import sources

    seen: list[httpx.AsyncClient] = []

    async def fake_get_json(client, url, **kw):
        seen.append(client)
        if "orders/" in url and "status-history" not in url:
            return {"order_id": "PO1", "parent_id": ""}
        return {"data": {}}

    async def fake_post_alloc(client, url, **kw):
        seen.append(client)
        return {"data": {}}

    async def empty_client_call(client, *a, **k):
        seen.append(client)
        return {}

    async def empty_groot(client, *a, **k):
        seen.append(client)
        return {"data": {}, "is_success": True, "status_code": 200}

    async def empty_soft(client, *a, **k):
        seen.append(client)
        return {}

    async def empty_pay(client, *a, **k):
        seen.append(client)
        return {}

    async def empty_family(client, *a, **k):
        seen.append(client)
        return []

    async def attach(client, out, **kw):
        seen.append(client)
        return out

    monkeypatch.setattr(sources.settings, "order_rca_use_fixtures", False)
    monkeypatch.setattr(sources.settings, "order_rca_order_service_base_url", "http://o.test")
    monkeypatch.setattr(sources.settings, "order_rca_sla_service_base_url", "http://s.test")
    monkeypatch.setattr(sources.settings, "order_rca_groot_base_url", "")
    monkeypatch.setattr(sources.settings, "order_rca_post_order_base_url", "")
    monkeypatch.setattr(sources.settings, "order_rca_admin_service_base_url", "")
    monkeypatch.setattr(sources.settings, "order_rca_sla_auth_token", "t")
    monkeypatch.setattr(sources, "_order_headers", lambda: {})
    monkeypatch.setattr(sources, "_get_json", fake_get_json)
    monkeypatch.setattr(sources, "_post_explain_allocation", fake_post_alloc)
    monkeypatch.setattr(sources, "_fetch_p1_msn_with_client", empty_client_call)
    monkeypatch.setattr(sources, "_fetch_order_history", empty_client_call)
    monkeypatch.setattr(sources, "_fetch_order_analytics_safe", empty_client_call)
    monkeypatch.setattr(sources, "_fetch_soft_allocation_for_order", empty_soft)
    monkeypatch.setattr(sources, "_fetch_payment_details_safe", empty_pay)
    monkeypatch.setattr(sources, "_resolve_family_orders", empty_family)
    monkeypatch.setattr(sources, "_attach_live_clickpost", attach)

    shared = hc.get_internal_http_client()
    out = await sources.fetch_bundle("PO1")
    assert out["order_id"] == "PO1"
    assert seen
    assert all(c is shared for c in seen)


@pytest.mark.asyncio
async def test_split_uses_shared_client(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    class _Resp:
        status_code = 200
        content = b'{"is_success": true}'

        def json(self):
            return {"is_success": True}

    class _Client:
        async def post(self, url, headers=None, json=None):
            return _Resp()

    fake = _Client()
    monkeypatch.setattr(order_client.settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(order_client.settings, "whatsapp_jit_hold_split_username", "bot")
    monkeypatch.setattr(order_client.settings, "order_rca_order_service_base_url", "http://o.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: fake)

    out = await order_client.split_jit_order(
        "PO1", [{"sku_id": "1", "qty": 1, "jit": False}]
    )
    assert out["is_success"] is True


@pytest.mark.asyncio
async def test_meta_send_uses_meta_pool_not_internal(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.x"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.x"}]}

        def raise_for_status(self):
            return None

    class _Meta:
        async def post(self, url, headers=None, json=None):
            assert "Authorization" in (headers or {})
            return _Resp()

    meta = _Meta()
    monkeypatch.setattr(meta_client.settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(meta_client.settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(meta_client.settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(meta_client.settings, "whatsapp_meta_access_token", "tok")
    monkeypatch.setattr(meta_client.settings, "whatsapp_meta_phone_number_id", "123")
    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: meta)
    monkeypatch.setattr(meta_client.session_store, "bind_outbound_message", __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock())
    monkeypatch.setattr(meta_client, "_confirm_sent_best_effort", __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock())

    # Ensure internal getter is not required / not confused
    called = {"internal": False}

    def boom():
        called["internal"] = True
        raise AssertionError("internal client must not be used for Meta")

    monkeypatch.setattr("app.infra.httpx_clients.get_internal_http_client", boom)

    out = await meta_client.send_template(
        "9818886159",
        "option_b_done",
        {"order_id": "PO1"},
        treat_uncertain_as_sent=False,
    )
    assert out["messages"][0]["id"] == "wamid.x"
    assert called["internal"] is False
