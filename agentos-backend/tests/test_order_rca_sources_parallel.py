"""Parallel collect in fetch_bundle."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.order_rca import sources
from app.config.settings import settings


@pytest.mark.asyncio
async def test_fetch_bundle_runs_independent_fetches_in_parallel(monkeypatch):
    monkeypatch.setattr(settings, "order_rca_use_fixtures", False)
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://order.test")
    monkeypatch.setattr(settings, "order_rca_sla_service_base_url", "http://sla.test")
    monkeypatch.setattr(settings, "order_rca_groot_base_url", "http://groot.test")
    monkeypatch.setattr(settings, "order_rca_post_order_base_url", "")
    monkeypatch.setattr(settings, "order_rca_admin_service_base_url", "http://admin.test")
    monkeypatch.setattr(settings, "order_rca_p1_msn_base_url", "http://p1.test")
    monkeypatch.setattr(settings, "order_rca_sla_auth_token", "token")
    monkeypatch.setattr(settings, "order_rca_http_timeout_sec", 5)

    order = {
        "order_id": "PO1",
        "parent_id": None,
        "created": 1778746301,
        "user": {"number": "9999999999"},
        "shipment_detail": {},
    }
    delay = 0.08

    async def slow_get_json(_client, url, **kw):
        await asyncio.sleep(delay)
        if url.endswith("/orders/PO1"):
            return order
        if url.endswith("/status-history"):
            return {"data": {"PO1": []}}
        if "order_analytics" in url:
            return {"data": {}}
        if "msn-adherence" in url:
            return {"stores": []}
        if "history" in url:
            return {"history": [], "total_pages": 1}
        if "payment_transactions" in url:
            return {"data": [], "is_success": True, "status_code": 200, "meta": {"count": 0}}
        raise AssertionError(f"unexpected GET {url}")

    async def slow_post_explain_allocation(_client, url, **kw):
        await asyncio.sleep(delay)
        return {"data": {"PO1": {}}}

    async def slow_groot(_client, _base, _oid):
        await asyncio.sleep(delay)
        return {"data": {}, "is_success": True, "status_code": 200}

    async def slow_soft_alloc(_client, _base, _order):
        await asyncio.sleep(delay)
        return {"data": {"response": []}}

    async def fast_post_json(_client, url, **kw):
        # Family search runs after gather; must not hit the network (retries blow timing).
        if "/search" in url:
            return {"order_details": [{"order_id": "PO1", "order_lines": []}]}
        raise AssertionError(f"unexpected POST {url}")

    with patch.object(sources, "_get_json", side_effect=slow_get_json):
        with patch.object(sources, "_post_json", side_effect=fast_post_json):
            with patch.object(sources, "_post_explain_allocation", side_effect=slow_post_explain_allocation):
                with patch.object(sources, "_fetch_groot_timeline", side_effect=slow_groot):
                    with patch.object(sources, "_fetch_soft_allocation_for_order", side_effect=slow_soft_alloc):
                        started = time.monotonic()
                        bundle = await sources.fetch_bundle("PO1")
                        elapsed = time.monotonic() - started

    assert bundle["order_id"] == "PO1"
    assert bundle["allocation"]["data"]["PO1"] == {}
    assert bundle["payment_details"]["is_success"] is True
    assert isinstance(bundle.get("family_orders"), list)
    assert any(o.get("order_id") == "PO1" for o in bundle["family_orders"])
    # Sequential would be ~7 * delay; parallel should be closer to 2 * delay (order + gather).
    assert elapsed < delay * 5


def test_payment_entity_id_prefers_parent_when_split():
    assert sources._payment_entity_id("PO_CHILD", "PO_PARENT") == "PO_PARENT"
    assert sources._payment_entity_id("po_child", " po_parent ") == "PO_PARENT"


def test_payment_entity_id_uses_order_when_unsplit_or_self_parent():
    assert sources._payment_entity_id("PO1", None) == "PO1"
    assert sources._payment_entity_id("PO1", "") == "PO1"
    assert sources._payment_entity_id("PO1", "PO1") == "PO1"


def _live_bundle_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "order_rca_use_fixtures", False)
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://order.test")
    monkeypatch.setattr(settings, "order_rca_sla_service_base_url", "http://sla.test")
    monkeypatch.setattr(settings, "order_rca_groot_base_url", "")
    monkeypatch.setattr(settings, "order_rca_post_order_base_url", "")
    monkeypatch.setattr(settings, "order_rca_admin_service_base_url", "http://admin.test")
    monkeypatch.setattr(settings, "order_rca_p1_msn_base_url", "")
    monkeypatch.setattr(settings, "order_rca_sla_auth_token", "token")
    monkeypatch.setattr(settings, "order_rca_http_timeout_sec", 5)


async def _empty_payload(*_a, **_k):
    return {}


async def _empty_list(*_a, **_k):
    return []


async def _passthrough_attach(_client, out, **_kw):
    return out


@pytest.mark.asyncio
async def test_fetch_bundle_payment_details_uses_parent_id(monkeypatch):
    _live_bundle_settings(monkeypatch)
    pay_ids: list[str] = []

    async def fake_get_json(_client, url, **kw):
        if url.endswith("/orders/PO_CHILD"):
            return {"order_id": "PO_CHILD", "parent_id": "PO_PARENT"}
        if url.endswith("/orders/PO_PARENT"):
            return {"order_id": "PO_PARENT", "parent_id": None}
        if "status-history" in url:
            return {"data": {}}
        raise AssertionError(f"unexpected GET {url}")

    async def capture_pay(_client, _base, order_id):
        pay_ids.append(order_id)
        return {"is_success": True, "data": []}

    with patch.object(sources, "_get_json", side_effect=fake_get_json):
        with patch.object(sources, "_post_explain_allocation", side_effect=_empty_payload):
            with patch.object(sources, "_fetch_p1_msn_with_client", side_effect=_empty_payload):
                with patch.object(sources, "_fetch_order_history", side_effect=_empty_payload):
                    with patch.object(sources, "_fetch_order_analytics_safe", side_effect=_empty_payload):
                        with patch.object(sources, "_fetch_soft_allocation_for_order", side_effect=_empty_payload):
                            with patch.object(sources, "_fetch_payment_details_safe", side_effect=capture_pay):
                                with patch.object(sources, "_resolve_family_orders", side_effect=_empty_list):
                                    with patch.object(sources, "_attach_live_clickpost", side_effect=_passthrough_attach):
                                        bundle = await sources.fetch_bundle("PO_CHILD")

    assert pay_ids == ["PO_PARENT"]
    assert bundle["parent_id"] == "PO_PARENT"
    assert bundle["payment_details"]["is_success"] is True


@pytest.mark.asyncio
async def test_fetch_bundle_payment_details_uses_order_id_when_unsplit(monkeypatch):
    _live_bundle_settings(monkeypatch)
    pay_ids: list[str] = []

    async def fake_get_json(_client, url, **kw):
        if url.endswith("/orders/PO1"):
            return {"order_id": "PO1", "parent_id": None}
        if "status-history" in url:
            return {"data": {}}
        raise AssertionError(f"unexpected GET {url}")

    async def capture_pay(_client, _base, order_id):
        pay_ids.append(order_id)
        return {"is_success": True, "data": []}

    with patch.object(sources, "_get_json", side_effect=fake_get_json):
        with patch.object(sources, "_post_explain_allocation", side_effect=_empty_payload):
            with patch.object(sources, "_fetch_p1_msn_with_client", side_effect=_empty_payload):
                with patch.object(sources, "_fetch_order_history", side_effect=_empty_payload):
                    with patch.object(sources, "_fetch_order_analytics_safe", side_effect=_empty_payload):
                        with patch.object(sources, "_fetch_soft_allocation_for_order", side_effect=_empty_payload):
                            with patch.object(sources, "_fetch_payment_details_safe", side_effect=capture_pay):
                                with patch.object(sources, "_resolve_family_orders", side_effect=_empty_list):
                                    with patch.object(sources, "_attach_live_clickpost", side_effect=_passthrough_attach):
                                        await sources.fetch_bundle("PO1")

    assert pay_ids == ["PO1"]
