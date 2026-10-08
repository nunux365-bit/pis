"""Unit tests for WhatsApp JIT hold agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock

import pytest

from app.agents.whatsapp_jit_hold import eligibility, handlers
from app.agents.whatsapp_jit_hold.constants import (
    ACTION_CONFIRM,
    ACTION_OPTION_A,
    ACTION_OPTION_B,
    STATE_DONE_HOLD,
    STATE_DONE_SPLIT,
    STATE_INITIAL_SENT,
    STATE_OPTION_A_PREVIEW,
    STATE_OPTION_B_PREVIEW,
)
from app.agents.whatsapp_jit_hold.fixtures import apply_fixture_overlay
from app.agents.whatsapp_jit_hold.order_client import (
    OrderNotFoundError,
    SplitOrderError,
    allocation_eta_display,
    child_order_has_allocation_eta,
    child_orders_from_search,
    default_tracking_url,
    extract_held_split_order_ids_from_response,
    format_held_orders_status_summary,
    format_tracking_links_for_orders,
    new_child_orders_after_split,
    normalize_order_created_ts,
    parse_order_search_response,
    split_jit_order,
    split_response_ok,
    tracking_url_for_order,
    wait_for_split_child_eta,
)
from app.agents.whatsapp_jit_hold.phone import session_phone
from app.agents.whatsapp_jit_hold.templates import TEMPLATE_SPECS, template_catalog


@pytest.fixture(autouse=True)
def _enable_whatsapp_jit_hold(monkeypatch):
    """Tests exercise the live send path; production default remains enabled=false."""
    from app.config.settings import settings
    from app.agents.whatsapp_jit_hold import order_client

    monkeypatch.setattr(settings, "whatsapp_jit_hold_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 0)
    monkeypatch.setattr(handlers.order_tracker, "record_triggered", AsyncMock())
    monkeypatch.setattr(handlers.order_tracker, "record_terminal", AsyncMock())


def _sample_bundle() -> dict:
    bundle = {
        "order_id": "PO13326295207344",
        "order": {
            "order_id": "PO13326295207344",
            "user": {
                "number": "9818886159",
                "properties": {"name": "Gaurav Agarwal"},
            },
            "order_lines": [
                {
                    "normalized_quantity": 9,
                    "sku": {"sku_id": 1122085, "name": "Test SKU"},
                }
            ],
        },
        "allocation": {"data": {}},
        "status": {"data": {}},
    }
    return apply_fixture_overlay(bundle)


def _overlay_session_skus() -> dict[str, list]:
    """Session held/ship fingerprints that match `_sample_bundle()` overlay."""
    return {
        "held_skus": [{"sku_id": "1122085", "name": "Test SKU", "qty_ordered": 9, "qty_stuck": 2}],
        "ship_skus": [{"sku_id": "1122085", "name": "Test SKU", "qty": 7, "jit": True}],
    }


def test_template_catalog_has_six_templates():
    catalog = template_catalog()
    assert len(catalog) == len(TEMPLATE_SPECS) == 6
    names = {t["template_name"] for t in catalog}
    assert "jit_hold_initial_v1" in names
    assert "jit_hold_option_a_done_v1" in names


def test_eligibility_passes_with_overlay():
    result = eligibility.evaluate_eligibility(_sample_bundle())
    assert result.eligible is True
    assert result.reason == "eligible"
    assert len(result.held_skus) == 1
    assert result.held_skus[0].qty_stuck == 2
    assert result.held_skus[0].qty_ordered == 9
    assert len(result.ship_skus) == 1
    assert result.ship_skus[0]["sku_id"] == "1122085"
    assert result.ship_skus[0]["qty"] == 7
    assert result.ship_skus[0]["jit"] is True
    assert result.ship_skus[0]["qty_retain"] == 7
    assert eligibility.retain_skus_for_split(result) == [{"sku_id": "1122085", "qty": 7}]


def test_eligibility_all_held_jit_when_order_totals_in_bounds():
    """Order-level gate: per-SKU ordered may be <9 when cart total ordered >= min."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 4,
            "fulfilled_quantity": 3,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT A"},
        },
        {
            "normalized_quantity": 5,
            "fulfilled_quantity": 4,
            "jit": True,
            "sku": {"sku_id": 222, "name": "JIT B"},
        },
        {
            "normalized_quantity": 2,
            "sku": {"sku_id": 333, "name": "Non-JIT C"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    held_ids = {h.sku_id for h in result.held_skus}
    assert held_ids == {"111", "222"}
    assert sum(h.qty_stuck for h in result.held_skus) == 2
    assert len(result.ship_skus) == 3


def test_eligibility_rejects_when_order_total_stuck_exceeds_max():
    """Second JIT line pushes order total stuck above max — entire order ineligible."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"].append(
        {
            "normalized_quantity": 10,
            "fulfilled_quantity": 6,
            "jit": True,
            "sku": {"sku_id": 9990001, "name": "Extra stuck JIT"},
        }
    )
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "no_eligible_jit_skus"


def test_eligibility_rejects_when_order_total_ordered_below_min():
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 4,
            "fulfilled_quantity": 3,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT A"},
        },
        {
            "normalized_quantity": 3,
            "sku": {"sku_id": 222, "name": "Non-JIT B"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "no_eligible_jit_skus"


def test_eligibility_initial_template_lists_each_held_sku_with_stuck_qty():
    """WhatsApp held_items: one line per SKU using that line's stuck qty (not cart total)."""
    from app.agents.whatsapp_jit_hold.templates import format_sku_lines

    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 4,
            "fulfilled_quantity": 3,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT A"},
        },
        {
            "normalized_quantity": 6,
            "fulfilled_quantity": 4,
            "jit": True,
            "sku": {"sku_id": 222, "name": "JIT B"},
        },
        {
            "normalized_quantity": 3,
            "sku": {"sku_id": 333, "name": "Non-JIT C"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    held_lines = [{"name": h.name, "qty": h.qty_stuck} for h in result.held_skus]
    text = format_sku_lines(held_lines, held=True)
    assert text == "🔴 JIT A × 1\n🔴 JIT B × 2"


def test_eligibility_jit_fully_fulfilled_not_in_held_skus():
    """JIT line with stuck=0 ships now only — never listed in held_skus / WhatsApp held block."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 5,
            "fulfilled_quantity": 3,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT held"},
        },
        {
            "normalized_quantity": 6,
            "fulfilled_quantity": 6,
            "jit": True,
            "sku": {"sku_id": 222, "name": "JIT fulfilled"},
        },
        {
            "normalized_quantity": 3,
            "sku": {"sku_id": 333, "name": "Non-JIT"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    assert {h.sku_id for h in result.held_skus} == {"111"}
    assert result.held_skus[0].qty_stuck == 2
    ship_jit = {s["sku_id"]: s for s in result.ship_skus if s.get("jit")}
    assert ship_jit["111"]["qty"] == 3
    assert ship_jit["222"]["qty"] == 6
    assert "222" not in {h.sku_id for h in result.held_skus}


def test_eligibility_rejects_when_two_jit_lines_two_stuck_each_order_total_four():
    """Order-level stuck max=3: 2+2 units across SKUs is ineligible (old per-SKU logic allowed this)."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 10,
            "fulfilled_quantity": 8,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT A"},
        },
        {
            "normalized_quantity": 10,
            "fulfilled_quantity": 8,
            "jit": True,
            "sku": {"sku_id": 222, "name": "JIT B"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "no_eligible_jit_skus"
    assert len(result.held_skus) == 2
    assert sum(h.qty_stuck for h in result.held_skus) == 4


def test_eligibility_jit_qty_bounds_configurable(monkeypatch):
    from app.config.settings import settings

    bundle = _sample_bundle()
    monkeypatch.setattr(settings, "whatsapp_jit_hold_jit_ordered_qty_min", 5)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_jit_stuck_qty_min", 2)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_jit_stuck_qty_max", 4)
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 3,
            "fulfilled_quantity": 1,
            "jit": True,
            "sku": {"sku_id": 111, "name": "JIT A"},
        },
        {
            "normalized_quantity": 3,
            "fulfilled_quantity": 1,
            "jit": True,
            "sku": {"sku_id": 222, "name": "JIT B"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    assert len(result.held_skus) == 2


def test_eligibility_mixed_cart_includes_non_jit_ship():
    bundle = _sample_bundle()
    bundle["order"]["order_lines"].append(
        {
            "normalized_quantity": 3,
            "sku": {"sku_id": 554433, "name": "Non-JIT SKU"},
        }
    )
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    non_jit = [s for s in result.ship_skus if not s.get("jit")]
    assert len(non_jit) == 1
    assert non_jit[0]["sku_id"] == "554433"
    assert non_jit[0]["qty"] == 3
    assert non_jit[0]["qty_retain"] == 3
    retain = {r["sku_id"]: r["qty"] for r in eligibility.retain_skus_for_split(result)}
    assert retain["1122085"] == 7
    assert retain["554433"] == 3


def test_eligibility_rejects_30_min_qc():
    bundle = _sample_bundle()
    bundle["order"]["rapid_eligibility_info"] = {"service_id": "thirty_minute_delivery"}
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "qc_rapid_30_60"


def test_eligibility_rejects_cleared_packaging_hold():
    bundle = _sample_bundle()
    bundle["order"]["sub_status"] = "processing"
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "not_packaging_on_hold"


def test_eligibility_get_order_on_hold_ignores_status_history():
    """Prod PO22826290384129: GET order is authoritative; status-history is unused."""
    bundle = _sample_bundle()
    oid = "PO22826290384129"
    bundle["order_id"] = oid
    bundle["order"]["order_id"] = oid
    bundle["order"]["sub_status"] = "On Hold"
    bundle["status"]["data"][oid] = [
        {"status": "130", "created": "2026-08-17T08:47:50.749129"},
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True


def test_eligibility_get_order_sub_status_ignores_contradictory_status_history():
    bundle = _sample_bundle()
    oid = "PO13326295207344"
    bundle["order"]["sub_status"] = "On Hold"
    bundle["status"]["data"][oid] = [
        {"status": "130", "created": "2026-08-17T08:47:50.749129", "sub_status": "processing"},
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True


def test_eligibility_order_details_requires_packaging_status_and_on_hold_sub_status():
    bundle = _sample_bundle()
    oid = "PO13326295207344"
    bundle["order"]["status_id"] = 130
    bundle["order"]["status"] = "Packaging"
    bundle["order"]["sub_status"] = "On Hold"
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True

    bundle["order"]["sub_status"] = "processing"
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "not_packaging_on_hold"

    bundle["order"]["sub_status"] = "On Hold"
    bundle["order"]["status_id"] = 40
    bundle["order"]["status"] = "Vendor Stock Allocation"
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "not_packaging_on_hold"


def test_eligibility_uses_fulfilled_quantity_from_order_line():
    bundle = _sample_bundle()
    bundle["order"]["order_lines"][0]["fulfilled_quantity"] = 7
    bundle["order"]["order_lines"][0]["jit"] = True
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    assert result.held_skus[0].qty_stuck == 2
    assert result.held_skus[0].qty_fulfillable == 7
    assert result.ship_skus[0]["qty"] == 7


def test_eligibility_chekbak_hold_is_one_pack_not_tablet_remainder():
    """quantity=10, normalized=1 is one pack — hold ×1, omit from retain so it leaves the parent."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 1,
            "fulfilled_quantity": 0,
            "quantity": 10,
            "jit": True,
            "sku": {"sku_id": 1122085, "name": "Chekbak"},
        },
        {
            "normalized_quantity": 8,
            "fulfilled_quantity": 8,
            "jit": False,
            "sku": {"sku_id": 999, "name": "Other"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    held = {h.sku_id: h for h in result.held_skus}
    assert held["1122085"].qty_stuck == 1
    ship = {s["sku_id"]: s for s in result.ship_skus}
    assert "1122085" not in ship
    assert ship["999"]["qty"] == 8
    assert ship["999"]["qty_retain"] == 8
    assert eligibility.retain_skus_for_split(result) == [{"sku_id": "999", "qty": 8}]


def test_eligibility_retain_qty_is_available_packs_times_pack_size():
    """Admenta-style: 20 tablets / 2 packs, 1 fulfilled → hold ×1, processing ×1, retain 10 on parent."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 2,
            "fulfilled_quantity": 1,
            "quantity": 20,
            "jit": True,
            "sku": {"sku_id": 17669, "name": "Admenta"},
        },
        {
            "normalized_quantity": 8,
            "fulfilled_quantity": 8,
            "jit": False,
            "sku": {"sku_id": 999, "name": "Other"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    held = {h.sku_id: h for h in result.held_skus}
    assert held["17669"].qty_stuck == 1
    ship = {s["sku_id"]: s for s in result.ship_skus}
    assert ship["17669"]["qty"] == 1
    assert ship["17669"]["qty_retain"] == 10
    assert eligibility.retain_skus_for_split(result) == [
        {"sku_id": "17669", "qty": 10},
        {"sku_id": "999", "qty": 8},
    ]


def test_eligibility_non_jit_retain_is_line_quantity_not_truncated_packs():
    """Keep the whole non-JIT line: quantity=15, not packs×int(15/8)=8."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 1,
            "fulfilled_quantity": 0,
            "quantity": 10,
            "jit": True,
            "sku": {"sku_id": 1122085, "name": "Chekbak"},
        },
        {
            "normalized_quantity": 8,
            "quantity": 15,
            "jit": False,
            "sku": {"sku_id": 999, "name": "Other"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    ship = {s["sku_id"]: s for s in result.ship_skus}
    assert ship["999"]["qty"] == 8
    assert ship["999"]["qty_retain"] == 15
    assert eligibility.retain_skus_for_split(result) == [{"sku_id": "999", "qty": 15}]


def test_eligibility_caps_fulfilled_packs_so_retain_cannot_exceed_line_quantity():
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 2,
            "fulfilled_quantity": 9,
            "quantity": 20,
            "jit": True,
            "sku": {"sku_id": 17669, "name": "Admenta"},
        },
        {
            "normalized_quantity": 1,
            "fulfilled_quantity": 0,
            "quantity": 10,
            "jit": True,
            "sku": {"sku_id": 1122085, "name": "Chekbak"},
        },
        {
            "normalized_quantity": 8,
            "fulfilled_quantity": 8,
            "jit": False,
            "sku": {"sku_id": 999, "name": "Other"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    ship = {s["sku_id"]: s for s in result.ship_skus}
    assert ship["17669"]["qty"] == 2
    assert ship["17669"]["qty_retain"] == 20


def test_eligibility_does_not_treat_tablet_quantity_as_ship_now():
    """quantity 120 / normalized 1 is 1 pack — not 119 tablets on processing now."""
    bundle = _sample_bundle()
    bundle["order"]["order_lines"] = [
        {
            "normalized_quantity": 1,
            "fulfilled_quantity": 0,
            "quantity": 120,
            "jit": True,
            "sku": {"sku_id": 649158, "name": "Eltroxin", "units_in_pack": 120},
        },
        {
            "normalized_quantity": 8,
            "fulfilled_quantity": 8,
            "jit": False,
            "sku": {"sku_id": 999, "name": "Other"},
        },
    ]
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    held = {h.sku_id: h for h in result.held_skus}
    assert held["649158"].qty_stuck == 1
    ship = {s["sku_id"]: s for s in result.ship_skus}
    assert "649158" not in ship
    assert ship["999"]["qty"] == 8
    assert eligibility.retain_skus_for_split(result) == [{"sku_id": "999", "qty": 8}]


def test_eligibility_rejects_when_fulfilled_stuck_out_of_range():
    """PO15926145629658 shape: ordered 10, fulfilled 6 → stuck 4 (max 3)."""
    oid = "PO15926145629658"
    bundle = {
        "order_id": oid,
        "order": {
            "order_id": oid,
            "sub_status": "On Hold",
            "status_id": 130,
            "status": "Packaging",
            "user": {"number": "6295346404"},
            "order_lines": [
                {
                    "normalized_quantity": 10,
                    "fulfilled_quantity": 6,
                    "jit": True,
                    "sku": {"sku_id": 5182, "name": "Dayo OD 500 Tablet PR"},
                }
            ],
        },
        "allocation": {
            "data": {
                oid: {
                    "allocated_vendor": 8212,
                    "selected_vendors": {
                        "8212": {"vendor_id": 8212, "vendor_type": "WAREHOUSE"},
                    },
                }
            }
        },
        "status": {
            "data": {
                oid: [
                    {"status": "130", "created": "2026-06-09T10:00:00+05:30", "sub_status": "on-hold"},
                ]
            }
        },
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "no_eligible_jit_skus"


def test_eligibility_rejects_when_nothing_to_ship_now(monkeypatch):
    """Defensive: never offer split/A if there is no ship-now qty (even if JIT held)."""
    bundle = _sample_bundle()
    monkeypatch.setattr(
        eligibility,
        "_jit_lines_from_order",
        lambda order: ([eligibility.JitSkuLine("1", "Held SKU", 9, 2, 0)], []),
    )
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "no_ship_now_skus"


def test_split_response_ok():
    assert split_response_ok({"is_success": True}) is True
    assert split_response_ok({"status_code": 200}) is True
    assert split_response_ok({"is_success": False, "status_code": 400}) is False
    assert split_response_ok({"is_success": False, "status_code": 200}) is False


@pytest.mark.asyncio
async def test_split_jit_order_retain_payload(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"is_success": true}'
        text = '{"is_success": true}'

        def json(self):
            return {"is_success": True}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    held_skus = [
        {"sku_id": "1122085", "qty": 2},
        {"sku_id": "554433", "qty": 1},
    ]
    await split_jit_order("PO123", held_skus)
    assert captured["json"]["skus_to_be_retained"] == [
        {"sku_id": "1122085", "quantity": 2},
        {"sku_id": "554433", "quantity": 1},
    ]
    assert captured["json"]["username"] == "wa_bot"


@pytest.mark.asyncio
async def test_split_jit_order_merges_duplicate_sku_ids(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"is_success": true}'
        text = '{"is_success": true}'

        def json(self):
            return {"is_success": True}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    await split_jit_order(
        "PO123",
        [{"sku_id": "1122085", "qty": 3}, {"sku_id": "1122085", "qty": 4}],
    )
    assert captured["json"]["skus_to_be_retained"] == [{"sku_id": "1122085", "quantity": 7}]


@pytest.mark.asyncio
async def test_split_jit_order_timeout_is_uncertain(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError
    from app.config.settings import settings

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            raise order_client.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    with pytest.raises(SplitOutcomeUncertainError):
        await split_jit_order("PO123", [{"sku_id": "1", "qty": 1}])


@pytest.mark.asyncio
async def test_split_jit_order_http_5xx_is_uncertain(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError
    from app.config.settings import settings

    class _Resp:
        status_code = 503
        text = "unavailable"
        content = b"unavailable"

        def json(self):
            return {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    with pytest.raises(SplitOutcomeUncertainError):
        await split_jit_order("PO123", [{"sku_id": "1", "qty": 1}])


@pytest.mark.asyncio
async def test_split_jit_order_http_4xx_is_failure(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    class _Resp:
        status_code = 400
        text = "bad request"
        content = b"bad request"

        def json(self):
            return {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    with pytest.raises(SplitOrderError):
        await split_jit_order("PO123", [{"sku_id": "1", "qty": 1}])


@pytest.mark.asyncio
async def test_split_jit_order_rejects_empty_retain_skus():
    with pytest.raises(SplitOrderError, match="no JIT SKUs"):
        await split_jit_order("PO123", [])


def test_session_phone_test_mode(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "919999999999")
    assert session_phone("9818886159") == "9999999999"


def test_session_phone_test_mode_refuses_missing_test_phone(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "")
    with pytest.raises(RuntimeError, match="TEST_PHONE"):
        session_phone("9818886159")


@pytest.mark.asyncio
async def test_meta_send_never_uses_customer_phone_in_test_mode(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "8076532044")
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.test"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.test"}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(
        meta_client.session_store,
        "bind_outbound_message",
        AsyncMock(),
    )

    await meta_client.send_template(
        "9818886159",
        "option_b_done",
        {"order_id": "PO123"},
        callback_data="jit_hold:PO123:done_hold",
    )
    assert captured["json"]["to"] == "918076532044"
    assert captured["json"]["type"] == "template"
    assert captured["json"]["template"]["name"] == "jit_hold_option_b_done_v1"
    assert captured["json"]["biz_opaque_callback_data"] == "jit_hold:PO123:done_hold"
    assert "1353517691170148" in captured["url"]


@pytest.mark.asyncio
async def test_meta_send_refuses_when_disabled(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_enabled", False)
    with pytest.raises(RuntimeError, match="WHATSAPP_JIT_HOLD_ENABLED"):
        await meta_client.send_template("9818886159", "option_b_done", {"order_id": "PO1"})


@pytest.mark.asyncio
async def test_meta_send_sanitizes_newlines_in_body_params(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.x"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.x"}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(meta_client.session_store, "bind_outbound_message", AsyncMock())

    await meta_client.send_template(
        "8076532044",
        "initial",
        {
            "order_id": "PO123",
            "customer_name": "Alex",
            "held_items": "🔴 Item A × 1\n🔴 Item B × 2",
        },
        callback_data="jit_hold:PO123:initial",
    )
    params = captured["json"]["template"]["components"][0]["parameters"]
    held = params[2]["text"]
    assert "\n" not in held
    assert "Item A" in held and "Item B" in held


def test_meta_parser_button_reply():
    from app.agents.whatsapp_jit_hold.meta_parser import iter_meta_inbound_events

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "918076532044"}],
                            "messages": [
                                {
                                    "from": "918076532044",
                                    "id": "wamid.inbound1",
                                    "type": "interactive",
                                    "context": {"id": "wamid.outbound1"},
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": "option_a",
                                            "title": "Option A",
                                        },
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    events = iter_meta_inbound_events(payload)
    assert len(events) == 1
    assert events[0]["action"] == "option_a"
    assert events[0]["phone"] == "8076532044"
    assert events[0]["context_id"] == "wamid.outbound1"
    assert events[0]["message_id"] == "wamid.inbound1"


def test_meta_parser_template_quick_reply_button_shape():
    """Meta docs: template QR → type=button + button.payload + context.id."""
    from app.agents.whatsapp_jit_hold.meta_parser import iter_meta_inbound_events

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1065119349375002",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550783881",
                                "phone_number_id": "1353517691170148",
                            },
                            "contacts": [{"profile": {"name": "Test"}, "wa_id": "918076532044"}],
                            "messages": [
                                {
                                    "context": {
                                        "from": "15550783881",
                                        "id": "wamid.OUTBOUND_TEMPLATE",
                                    },
                                    "from": "918076532044",
                                    "id": "wamid.INBOUND_BUTTON",
                                    "timestamp": "1750091045",
                                    "type": "button",
                                    "button": {
                                        "payload": "option_a",
                                        "text": "Option A",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    events = iter_meta_inbound_events(payload)
    assert len(events) == 1
    assert events[0]["message_type"] == "button"
    assert events[0]["action"] == "option_a"
    assert events[0]["context_id"] == "wamid.OUTBOUND_TEMPLATE"
    assert events[0]["order_id"] is None  # order comes from Redis via context.id


@pytest.mark.asyncio
async def test_meta_status_binds_opaque_callback(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_parser

    bind = AsyncMock()
    monkeypatch.setattr(meta_parser.session_store, "bind_outbound_message", bind)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.SENT1",
                                    "status": "delivered",
                                    "biz_opaque_callback_data": "jit_hold:PO999:initial",
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    n = await meta_parser.apply_meta_status_bindings(payload)
    assert n == 1
    bind.assert_awaited_once()
    assert bind.await_args.args[0] == "wamid.SENT1"
    assert bind.await_args.args[1] == "PO999"


@pytest.mark.asyncio
async def test_meta_golden_path_send_wamid_to_button_tap_order(monkeypatch):
    """
    End-to-end correlation using Meta's documented shapes:
    send response messages[0].id → Redis → button webhook context.id → order_id.
    Official template QR callback: type=button + button.payload + context.id
    (Meta Interactive Message Templates / button webhook reference).
    """
    from app.agents.whatsapp_jit_hold import meta_client, meta_parser
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "8076532044")
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "tok")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    outbound_wamid = "wamid.HBgLMTY0NjcwNDM1OTUVAgARGBJBM0Y4RUU0RUNFQkFDMjYzQUMA"
    store: dict[str, str] = {}

    async def _bind(wamid, order_id, *, callback_data=None, snapshot=None):
        store[wamid] = order_id

    async def _lookup(wamid):
        return store.get(wamid)

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"messages": [{"id": outbound_wamid}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(meta_client.session_store, "bind_outbound_message", _bind)
    monkeypatch.setattr(meta_parser.session_store, "order_id_for_outbound_message", _lookup)

    await meta_client.send_template(
        "9818886159",
        "initial",
        {"order_id": "PO13326295017145", "customer_name": "Alex", "held_items": "Item"},
        callback_data="jit_hold:PO13326295017145:initial",
    )
    assert store[outbound_wamid] == "PO13326295017145"

    # Meta Interactive Message Templates — quick reply callback shape
    webhook = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "1353517691170148"},
                            "contacts": [{"wa_id": "918076532044"}],
                            "messages": [
                                {
                                    "button": {"payload": "option_a", "text": "Option A"},
                                    "context": {
                                        "from": "15550783881",
                                        "id": outbound_wamid,
                                    },
                                    "from": "918076532044",
                                    "id": "wamid.INBOUND_TAP",
                                    "timestamp": "1591210827",
                                    "type": "button",
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    events = meta_parser.iter_meta_inbound_events(webhook)
    assert len(events) == 1
    assert events[0]["action"] == "option_a"
    assert events[0]["context_id"] == outbound_wamid
    assert events[0]["order_id"] is None  # not on message webhook

    enriched = await meta_parser.enrich_order_id(events[0])
    assert enriched["order_id"] == "PO13326295017145"
    assert enriched["phone"] == "8076532044"


def test_meta_parser_ignores_status_only():
    from app.agents.whatsapp_jit_hold.meta_parser import iter_meta_inbound_events

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.x", "status": "delivered"}]}}]}],
    }
    assert iter_meta_inbound_events(payload) == []


def test_customer_name_falls_back_to_delivery_address():
    from app.agents.whatsapp_jit_hold.order_client import customer_name

    order = {
        "user": {"properties": {"name": ""}},
        "delivery_address": {"name": "Jyotirmoy Chandra"},
    }
    assert customer_name(order) == "Jyotirmoy Chandra"


def test_vendor_type_from_order_details_not_allocation():
    bundle = _sample_bundle()
    bundle["allocation"] = {"data": {}}
    bundle["order"]["shipment_detail"] = {
        "vendor": {"tags": {"store_type": "WAREHOUSE", "fc_type": "Fulfilment Center"}}
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True

    bundle["order"]["shipment_detail"] = {"vendor": {"tags": {"store_type": "MARKETPLACE"}}}
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"

    bundle["order"]["shipment_detail"] = {"vendor": {"vendor_type": "MARKETPLACE"}}
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"


def test_vendor_rejects_vmo_tag_and_empty_defaults():
    """tags.vendor_type is VMO/Non-VMO — must not imply WH/FC. Empty must fail."""
    bundle = _sample_bundle()
    bundle["allocation"] = {"data": {}}
    bundle["order"]["shipment_detail"] = {
        "vendor": {"tags": {"vendor_type": "Non-VMO", "store_type": "", "fc_type": ""}}
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"

    bundle["order"]["shipment_detail"] = {"vendor": {"tags": {"vendor_type": "VMO"}}}
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"

    bundle["order"]["shipment_detail"] = {}
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"


def test_vendor_accepts_retail_store_type_and_fc_type_only():
    bundle = _sample_bundle()
    bundle["allocation"] = {"data": {}}
    bundle["order"]["shipment_detail"] = {"vendor": {"tags": {"store_type": "RETAIL", "vendor_type": "VMO"}}}
    assert eligibility.evaluate_eligibility(bundle).eligible is True

    bundle["order"]["shipment_detail"] = {
        "vendor": {"tags": {"fc_type": "Fulfilment Center", "vendor_type": "Non-VMO"}}
    }
    assert eligibility.evaluate_eligibility(bundle).eligible is True


def test_fc_type_rejects_marketplace_and_non_fulfilment():
    bundle = _sample_bundle()
    bundle["allocation"] = {"data": {}}
    for fc in (
        "Marketplace Fulfilment Center",
        "non-fulfilment",
        "non fulfilment",
        "unfulfilled",
    ):
        bundle["order"]["shipment_detail"] = {"vendor": {"tags": {"fc_type": fc}}}
        result = eligibility.evaluate_eligibility(bundle)
        assert result.eligible is False, fc
        assert result.reason == "vendor_not_fc_wh"


def test_marketplace_fc_type_does_not_fall_through_to_allocation():
    bundle = _sample_bundle()
    oid = bundle["order_id"]
    bundle["order"]["shipment_detail"] = {
        "vendor": {"tags": {"fc_type": "Marketplace Fulfilment Center"}}
    }
    bundle["allocation"] = {
        "data": {
            oid: {
                "allocated_vendor": 1,
                "selected_vendors": {"1": {"vendor_id": 1, "vendor_type": "WAREHOUSE"}},
            }
        }
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"


def test_marketplace_store_type_hard_denies_despite_fc_type_or_allocation():
    """Explicit MARKETPLACE must not fall through to fc_type or allocation WAREHOUSE."""
    bundle = _sample_bundle()
    oid = bundle["order_id"]
    bundle["order"]["shipment_detail"] = {
        "vendor": {
            "tags": {
                "store_type": "MARKETPLACE",
                "fc_type": "Marketplace Fulfilment Center",
            }
        }
    }
    bundle["allocation"] = {
        "data": {
            oid: {
                "allocated_vendor": 1,
                "selected_vendors": {"1": {"vendor_id": 1, "vendor_type": "WAREHOUSE"}},
            }
        }
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"


def test_nexus_merge_preserves_marketplace_store_type():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order

    envelope = {
        "order_details": {
            "vendor_details": {
                "id": 1,
                "tags": {
                    "store_type": "MARKETPLACE",
                    "fc_type": "Fulfilment Center",
                    "vendor_type": "Non-VMO",
                },
            }
        }
    }
    merged = _merge_nexus_into_order({"order_id": "PO1", "shipment_detail": {}}, envelope)
    tags = merged["shipment_detail"]["vendor"]["tags"]
    assert tags.get("store_type") == "MARKETPLACE"
    assert tags.get("store_type") != "WAREHOUSE"


def test_allocation_vendor_requires_exact_warehouse_or_retail():
    bundle = _sample_bundle()
    oid = bundle["order_id"]
    bundle["order"]["shipment_detail"] = {}
    bundle["allocation"] = {
        "data": {
            oid: {
                "allocated_vendor": 1,
                "selected_vendors": {"1": {"vendor_id": 1, "vendor_type": "MARKETPLACE"}},
            }
        }
    }
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "vendor_not_fc_wh"

    bundle["allocation"]["data"][oid]["selected_vendors"]["1"]["vendor_type"] = "WAREHOUSE"
    assert eligibility.evaluate_eligibility(bundle).eligible is True


def test_nexus_merge_does_not_promote_vmo_to_store_type():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order

    order = {"order_id": "PO1", "shipment_detail": {}}
    envelope = {
        "order_details": {
            "vendor_details": {"id": 1, "tags": {"vendor_type": "Non-VMO", "fc_type": ""}},
        }
    }
    merged = _merge_nexus_into_order(order, envelope)
    tags = merged["shipment_detail"]["vendor"]["tags"]
    assert tags.get("store_type") not in {"WAREHOUSE", "RETAIL"}
    assert tags.get("vendor_type") == "Non-VMO"

    envelope2 = {
        "order_details": {
            "vendor_details": {
                "id": 1,
                "tags": {"store_type": "WAREHOUSE", "vendor_type": "Non-VMO", "fc_type": "Fulfilment Center"},
            }
        }
    }
    merged2 = _merge_nexus_into_order({"order_id": "PO1", "shipment_detail": {}}, envelope2)
    assert merged2["shipment_detail"]["vendor"]["tags"]["store_type"] == "WAREHOUSE"


def test_nexus_merge_fills_empty_phone():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order, customer_phone

    order = {
        "order_id": "PO1",
        "user": {"number": "", "display_number": None},
        "contact_number": "",
    }
    envelope = {"order_details": {"user_details": {"contact_number": "9555560920"}}}
    merged = _merge_nexus_into_order(order, envelope)
    assert customer_phone(merged) == "9555560920"


def test_nexus_merge_does_not_invent_warehouse_from_marketplace_fc_type():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order

    envelope = {
        "order_details": {
            "vendor_details": {
                "id": 1,
                "tags": {"fc_type": "Marketplace Fulfilment Center"},
            }
        }
    }
    merged = _merge_nexus_into_order({"order_id": "PO1", "shipment_detail": {}}, envelope)
    assert merged["shipment_detail"]["vendor"]["tags"].get("store_type") != "WAREHOUSE"


@pytest.mark.asyncio
async def test_inbound_option_a_ignored_when_no_longer_eligible(monkeypatch):
    bundle = _sample_bundle()
    bundle["order"]["sub_status"] = "processing"
    session = {
        "order_id": "PO13326295207344",
        "state": "initial_sent",
        "customer_phone": "9818886159",
        "held_skus": [],
        "ship_skus": [],
    }
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "action": ACTION_OPTION_A,
            "order_id": "PO13326295207344",
            "message_id": "wamid-ineligible",
        }
    )
    assert result["status"] == "ignored"
    assert result["reason"] == "not_packaging_on_hold"
    send.assert_not_awaited()


def test_default_tracking_url_and_split_child_extraction():
    assert default_tracking_url("PO123") == "https://www.1mg.com/track/PO123"
    assert tracking_url_for_order({}, "PO123") == "https://www.1mg.com/track/PO123"
    assert tracking_url_for_order(
        {"shipment_detail": {"tracking_url": "https://track.example/PO123"}},
        "PO123",
    ) == "https://track.example/PO123"
    resp = {"data": {"child_order_id": "PO13326295207344"}}
    assert extract_held_split_order_ids_from_response(resp, "PO13326295017145") == [
        "PO13326295207344"
    ]


def test_order_search_child_discovery_from_sample_shape():
    search_resp = {
        "total_count": 2,
        "order_details": [
            {
                "order_id": "PO13326295207344",
                "parent_id": "PO13326295017145",
                "group_id": "PO13326295017145",
                "created": 1778746320,
                "shipment_detail": {"tracking_url": "https://1mg.clickpost.in/?waybill=child"},
            },
            {
                "order_id": "PO13326295017145",
                "parent_id": None,
                "group_id": "PO13326295017145",
                "created": 1778746301,
                "shipment_detail": {"tracking_url": "https://www.1mg.com/trackOrder?orderId=PO13326295017145"},
            },
        ],
    }
    orders = parse_order_search_response(search_resp)
    assert len(orders) == 2
    children = child_orders_from_search(orders, "PO13326295017145")
    assert [c["order_id"] for c in children] == ["PO13326295207344"]
    assert parse_order_search_response({"order_details": ""}) == []


@pytest.mark.asyncio
async def test_search_orders_by_parent_paginates_via_shared_helper(monkeypatch):
    from app.agents.order_rca import sources
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "https://order.example")

    calls: list[int] = []

    async def fake_search(client, base_o, root):
        assert root == "PO_PARENT"
        assert base_o == "https://order.example"
        calls.append(1)
        return [
            {"order_id": "PO_PARENT"},
            {"order_id": "PO_CHILD", "parent_id": "PO_PARENT"},
        ]

    monkeypatch.setattr(sources, "_search_family_order_rows", fake_search)
    rows = await order_client.search_orders_by_parent("PO_PARENT")
    assert calls == [1]
    assert [r["order_id"] for r in rows] == ["PO_PARENT", "PO_CHILD"]


def test_child_orders_from_search_sorts_iso_created_timestamps():
    orders = [
        {
            "order_id": "PO_CHILD_OLD",
            "parent_id": "PO_PARENT",
            "created": "2026-06-01T10:00:00+05:30",
        },
        {
            "order_id": "PO_CHILD_NEW",
            "parent_id": "PO_PARENT",
            "created": "2026-06-09T10:00:00+05:30",
        },
    ]
    children = child_orders_from_search(orders, "PO_PARENT")
    assert [c["order_id"] for c in children] == ["PO_CHILD_NEW", "PO_CHILD_OLD"]


def test_new_child_orders_after_split_excludes_pre_existing_children():
    orders = [
        {"order_id": "PO_CHILD_NEW", "parent_id": "PO_PARENT", "created": 2000},
        {"order_id": "PO_CHILD_OLD", "parent_id": "PO_PARENT", "created": 1000},
    ]
    fresh = new_child_orders_after_split(
        orders,
        "PO_PARENT",
        known_child_ids=frozenset({"PO_CHILD_OLD"}),
        split_after_ts=1900,
    )
    assert [o["order_id"] for o in fresh] == ["PO_CHILD_NEW"]


@pytest.mark.asyncio
async def test_format_tracking_links_for_multiple_new_children():
    orders = [
        {
            "order_id": "PO_CHILD_B",
            "shipment_detail": {"tracking_url": "https://track/b"},
        },
        {
            "order_id": "PO_CHILD_A",
            "shipment_detail": {"tracking_url": "https://track/a"},
        },
    ]
    text = await format_tracking_links_for_orders(orders)
    assert "PO_CHILD_B: https://track/b" in text
    assert "PO_CHILD_A: https://track/a" in text


@pytest.mark.asyncio
async def test_resolve_split_tracking_uses_search_without_extra_fetch(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    search_rows = [
        {
            "order_id": "PO13326295017145",
            "parent_id": None,
            "created": 1778746301,
            "shipment_detail": {"tracking_url": "https://track/parent"},
        },
        {
            "order_id": "PO13326295207345",
            "parent_id": "PO13326295017145",
            "created": 1778746400,
            "shipment_detail": {"tracking_url": "https://track/held-new"},
        },
        {
            "order_id": "PO13326295207344",
            "parent_id": "PO13326295017145",
            "created": 1778746320,
            "shipment_detail": {"tracking_url": "https://track/held-old"},
        },
    ]

    fetch = AsyncMock()
    monkeypatch.setattr(order_client, "fetch_order_details", fetch)
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))

    ship_url, held_url, held_orders = await order_client.resolve_split_tracking_links(
        "PO13326295017145",
        split_response={"is_success": True},
        split_after_ts=1778746390,
        known_child_ids_before=frozenset({"PO13326295207344"}),
    )
    assert ship_url == "https://track/parent"
    assert held_url == "https://track/held-new"
    assert held_orders[0]["order_id"] == "PO13326295207345"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_split_tracking_fetches_only_when_tracking_missing(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    search_rows = [
        {
            "order_id": "PO13326295017145",
            "shipment_detail": {},
        },
        {
            "order_id": "PO13326295207345",
            "parent_id": "PO13326295017145",
            "created": 1778746400,
            "shipment_detail": {},
        },
    ]

    async def fake_fetch(oid):
        if oid == "PO13326295017145":
            return {"shipment_detail": {"tracking_url": "https://fetched/parent"}}
        return {"shipment_detail": {"tracking_url": "https://fetched/child"}}

    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch)
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))

    ship_url, held_url, held_orders = await order_client.resolve_split_tracking_links(
        "PO13326295017145",
        split_after_ts=1778746390,
        known_child_ids_before=frozenset(),
    )
    assert ship_url == "https://fetched/parent"
    assert held_url == "https://fetched/child"


def test_child_order_has_allocation_eta():
    assert child_order_has_allocation_eta(None) is False
    assert child_order_has_allocation_eta({"eta": {"eta_to": 0}}) is False
    assert child_order_has_allocation_eta({"promised_eta": 1778748060}) is False
    assert child_order_has_allocation_eta({"eta": {"eta_to": 0, "to_date": "1 Jan, 1970"}}) is False
    assert child_order_has_allocation_eta({"eta": {"to_date": "13 May, 11:31 AM"}}) is False
    assert child_order_has_allocation_eta({"eta": {"eta_to": 1778748060.0}}) is True
    assert child_order_has_allocation_eta({"eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}) is True
    assert child_order_has_allocation_eta({"order_eta": "0,1778748060"}) is True
    assert allocation_eta_display({"eta": {"eta_to": 0, "to_date": "1 Jan, 1970"}}) is None
    assert allocation_eta_display({"eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}) == "13 May, 2026"


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_returns_when_child_exists(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    fetches = [
        order_client.OrderNotFoundError("PO_CHILD"),
        {"order_id": "PO_CHILD", "eta": {"eta_to": 0}},
    ]

    async def fake_fetch(oid):
        item = fetches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 10)
    monkeypatch.setattr(order_client, "_ETA_WAIT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch)
    monkeypatch.setattr(order_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        order_client,
        "extract_held_split_order_ids_from_response",
        lambda resp, parent: ["PO_CHILD"],
    )

    child = await wait_for_split_child_eta(
        "PO_PARENT",
        split_response={"data": {"child_order_id": "PO_CHILD"}},
    )
    assert child["order_id"] == "PO_CHILD"
    assert child_order_has_allocation_eta(child) is False
    assert sleeps == [0.01]


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_returns_last_child_on_timeout(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    async def fake_fetch(oid):
        return {"order_id": "PO_CHILD", "eta": {"eta_to": 0}}

    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 0)
    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch)
    monkeypatch.setattr(
        order_client,
        "extract_held_split_order_ids_from_response",
        lambda resp, parent: ["PO_CHILD"],
    )

    child = await wait_for_split_child_eta("PO_PARENT", split_response={})
    assert child["order_id"] == "PO_CHILD"
    assert child_order_has_allocation_eta(child) is False


def test_extract_child_ids_from_live_split_success_string_is_empty():
    """Live JIT split returns a success string, not a child PO id."""
    resp = {
        "data": "Operation successfully performed",
        "is_success": True,
        "status_code": 200,
    }
    assert extract_held_split_order_ids_from_response(resp, "PO13326295017145") == []


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_rediscovers_when_first_search_empty(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    discovers = [
        [],
        ["PO_CHILD"],
    ]

    async def fake_discover(*a, **k):
        return discovers.pop(0) if discovers else ["PO_CHILD"]

    async def fake_fetch(oid):
        return {"order_id": oid, "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}

    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 10)
    monkeypatch.setattr(order_client, "_ETA_WAIT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(order_client, "_discover_split_child_ids", fake_discover)
    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch)
    monkeypatch.setattr(order_client.asyncio, "sleep", fake_sleep)

    child = await wait_for_split_child_eta("PO_PARENT")
    assert child["order_id"] == "PO_CHILD"
    assert sleeps == [0.01]


@pytest.mark.asyncio
async def test_split_jit_order_accepts_qty_stuck_when_qty_missing(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"is_success": true}'

        def json(self):
            return {"is_success": True}

    class _Client:
        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    await split_jit_order("PO123", [{"sku_id": "1122085", "qty": 0, "qty_stuck": 2}])
    assert captured["json"]["skus_to_be_retained"] == [{"sku_id": "1122085", "quantity": 2}]


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_retries_after_404(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    calls = {"n": 0}

    async def fake_fetch(oid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OrderNotFoundError(oid)
        return {"order_id": oid, "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}

    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 10)
    monkeypatch.setattr(order_client, "_ETA_WAIT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch)
    monkeypatch.setattr(order_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        order_client,
        "_discover_split_child_ids",
        AsyncMock(return_value=["PO_CHILD"]),
    )

    child = await wait_for_split_child_eta("PO_PARENT")
    assert child["order_id"] == "PO_CHILD"
    assert calls["n"] == 2
    assert sleeps == [0.01]


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_stub_does_not_poll(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", True)
    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 90)
    monkeypatch.setattr(order_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        order_client,
        "_discover_split_child_ids",
        AsyncMock(return_value=["PO_CHILD"]),
    )
    monkeypatch.setattr(
        order_client,
        "fetch_order_details",
        AsyncMock(return_value={"order_id": "PO_CHILD", "eta": {"eta_to": 0}}),
    )

    child = await wait_for_split_child_eta("PO_PARENT")
    assert child["order_id"] == "PO_CHILD"
    assert sleeps == []


@pytest.mark.asyncio
async def test_wait_for_split_child_eta_returns_none_when_no_child(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 0)
    monkeypatch.setattr(
        order_client,
        "_discover_split_child_ids",
        AsyncMock(return_value=[]),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(order_client, "fetch_order_details", fetch)

    assert await wait_for_split_child_eta("PO_PARENT") is None
    fetch.assert_not_awaited()


def test_allocation_eta_display_skips_1970_and_uses_top_level_eta_to():
    assert (
        allocation_eta_display(
            {"eta": {"eta_to": 1778748060.0, "to_date": "1 Jan, 1970"}, "eta_to": "13 May, 2026"}
        )
        == "13 May, 2026"
    )
    assert allocation_eta_display({"eta": {"eta_to": "1778748060"}, "eta_to": "13 May, 2026"}) == "13 May, 2026"
    assert child_order_has_allocation_eta({"order_eta": "0,"}) is False
    assert child_order_has_allocation_eta({"order_eta": "0,0"}) is False
    assert child_order_has_allocation_eta({"eta": {"eta_to": "1778748060"}}) is True


@pytest.mark.asyncio
async def test_resolve_split_tracking_two_new_children_are_held(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    search_rows = [
        {
            "order_id": "PO_PARENT",
            "shipment_detail": {"tracking_url": "https://track/parent"},
        },
        {
            "order_id": "PO_CHILD_A",
            "parent_id": "PO_PARENT",
            "created": 1778746400,
            "shipment_detail": {"tracking_url": "https://track/a"},
        },
        {
            "order_id": "PO_CHILD_B",
            "parent_id": "PO_PARENT",
            "created": 1778746500,
            "shipment_detail": {"tracking_url": "https://track/b"},
        },
    ]
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))
    monkeypatch.setattr(order_client, "fetch_order_details", AsyncMock())

    ship_url, held_url, held_orders = await order_client.resolve_split_tracking_links(
        "PO_PARENT",
        split_after_ts=1778746390,
        known_child_ids_before=frozenset(),
    )
    assert ship_url == "https://track/parent"
    assert "https://track/a" in held_url
    assert "https://track/b" in held_url
    assert {o["order_id"] for o in held_orders} == {"PO_CHILD_A", "PO_CHILD_B"}


@pytest.mark.asyncio
async def test_resolve_split_tracking_no_child_uses_parent_as_ship_now(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    search_rows = [
        {
            "order_id": "PO_PARENT",
            "shipment_detail": {"tracking_url": "https://track/parent"},
        },
    ]
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))
    monkeypatch.setattr(order_client, "fetch_order_details", AsyncMock())

    ship_url, held_url, held_orders = await order_client.resolve_split_tracking_links(
        "PO_PARENT",
        split_after_ts=1778746390,
        known_child_ids_before=frozenset(),
    )
    assert ship_url == "https://track/parent"
    assert held_url == ""
    assert held_orders == []


@pytest.mark.asyncio
async def test_updated_eta_after_split_wait_error_and_parent_error_use_copy(monkeypatch):
    monkeypatch.setattr(handlers, "wait_for_split_child_eta", AsyncMock(side_effect=RuntimeError("search down")))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(side_effect=RuntimeError("parent down")))
    text = await handlers._updated_eta_after_split(
        "PO1",
        split_response=None,
        split_after_ts=None,
        known_child_ids=None,
    )
    assert text == "as per your order confirmation"


@pytest.mark.asyncio
async def test_trigger_and_inbound_noop_when_disabled(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_enabled", False)
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result == {"status": "skipped", "reason": "disabled"}
    inbound = await handlers.handle_inbound(
        {"phone": "9818886159", "action": ACTION_CONFIRM, "message_id": "m1"}
    )
    assert inbound["reason"] == "disabled"
    send.assert_not_awaited()


def test_mask_phone():
    from app.agents.whatsapp_jit_hold.phone import mask_phone

    assert mask_phone("9555560920") == "***0920"
    assert mask_phone("919555560920") == "***0920"


@pytest.mark.asyncio
async def test_kafka_trigger_sends_initial(monkeypatch):
    from app.config.settings import settings

    bundle = _sample_bundle()
    sent: list[str] = []
    saved: list = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {"id": "msg-1"}

    async def fake_save(phone, oid, doc):
        saved.append((phone, oid, dict(doc)))

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "release_order_send", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_session", fake_save)
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    assert sent == ["initial"]
    handlers.order_tracker.record_triggered.assert_awaited()
    assert saved
    assert saved[0][2]["customer_phone"] == "9818886159"


@pytest.mark.asyncio
async def test_kafka_trigger_stores_test_phone_in_session(monkeypatch):
    from app.config.settings import settings

    bundle = _sample_bundle()
    saved: list = []

    async def fake_save(phone, oid, doc):
        saved.append((phone, oid, dict(doc)))

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "8076532044")
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "release_order_send", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_session", fake_save)
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    assert saved[0][0] == "8076532044"
    assert saved[0][2]["customer_phone"] == "8076532044"
    assert saved[0][2]["customer_phone"] != "9818886159"


@pytest.mark.asyncio
async def test_kafka_trigger_releases_lock_if_send_fails_before_session_saved(monkeypatch):
    bundle = _sample_bundle()

    async def fail_send(*args, **kwargs):
        raise RuntimeError("meta down")

    save = AsyncMock()
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_session", save)
    monkeypatch.setattr(handlers.meta_client, "send_template", fail_send)
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)

    with pytest.raises(RuntimeError, match="meta down"):
        await handlers.handle_order_trigger("PO13326295207344")
    release.assert_awaited_once()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_kafka_trigger_does_not_send_if_pin_fails(monkeypatch):
    bundle = _sample_bundle()
    send = AsyncMock()
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handlers.session_store,
        "confirm_order_sent",
        AsyncMock(side_effect=RuntimeError("redis pin failed")),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="redis pin failed"):
        await handlers.handle_order_trigger("PO13326295207344")
    send.assert_not_awaited()
    assert release.await_count >= 1


@pytest.mark.asyncio
async def test_kafka_trigger_releases_lock_on_failure(monkeypatch):
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)

    with pytest.raises(RuntimeError):
        await handlers.handle_order_trigger("PO13326295207344")
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_kafka_trigger_keeps_lock_on_cancel_after_pin(monkeypatch):
    bundle = _sample_bundle()
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock(side_effect=asyncio.CancelledError()))
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)

    with pytest.raises(asyncio.CancelledError):
        await handlers.handle_order_trigger("PO13326295207344")
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_kafka_trigger_keeps_sent_lock_if_save_session_fails_after_send(monkeypatch):
    bundle = _sample_bundle()

    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)
    save = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(handlers.session_store, "save_session", save)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    assert save.await_count == 5
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_meta_send_timeout_treated_as_sent_keeps_lock(monkeypatch):
    """Meta timeout must not release the sent-lock (duplicate WhatsApp)."""
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    bundle = _sample_bundle()
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            raise meta_client.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    save = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "save_session", save)
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)
    # Route through real meta_client timeout handling.
    monkeypatch.setattr(handlers.meta_client, "send_template", meta_client.send_template)

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    save.assert_awaited_once()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_meta_send_network_error_on_initial_treated_as_sent(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    bundle = _sample_bundle()
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            raise meta_client.httpx.ConnectError("connection reset")

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    save = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "save_session", save)
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)
    monkeypatch.setattr(handlers.meta_client, "send_template", meta_client.send_template)

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_meta_conversation_timeout_raises(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            raise meta_client.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())

    with pytest.raises(meta_client.httpx.ReadTimeout):
        await meta_client.send_template("9818886159", "option_b_done", {"order_id": "PO123"})


@pytest.mark.asyncio
async def test_inbound_confirm_split_flow(monkeypatch):
    bundle = _sample_bundle()
    bundle["order"]["shipment_detail"] = {"tracking_url": "https://www.1mg.com/track/PO13326295207344"}
    child_bundle = {
        "order_id": "PO13326295207345",
        "order": {
            "order_id": "PO13326295207345",
            "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 11:31 AM"},
            "shipment_detail": {"tracking_url": "https://www.1mg.com/track/PO13326295207345"},
        },
    }
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_name": "Gaurav",
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    sent: list[str] = []
    sent_ctx: list[dict] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        sent_ctx.append(context)
        return {}

    async def fake_fetch_bundle(order_id):
        if order_id == "PO13326295207345":
            return child_bundle
        return bundle

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", fake_fetch_bundle)
    from app.agents.whatsapp_jit_hold import order_client

    async def fake_fetch_details(order_id):
        data = await fake_fetch_bundle(order_id)
        return data.get("order") if isinstance(data.get("order"), dict) else data

    monkeypatch.setattr(order_client, "fetch_order_details", fake_fetch_details)
    search_rows = [
        {
            "order_id": "PO13326295207344",
            "shipment_detail": {"tracking_url": "https://www.1mg.com/track/PO13326295207344"},
        },
        {
            "order_id": "PO13326295207345",
            "parent_id": "PO13326295207344",
            "created": 9_999_999_999,
            "status": "Packaging",
            "sub_status": "on-hold",
            "shipment_detail": {"tracking_url": "https://www.1mg.com/track/PO13326295207345"},
        },
    ]
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))
    split = AsyncMock(
        return_value={"is_success": True, "data": {"child_order_id": "PO13326295207345"}}
    )
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-1",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_DONE_SPLIT
    assert sent == ["option_a_done"]
    assert sent_ctx[0]["ship_now_tracking_link"] == "https://www.1mg.com/track/PO13326295207344"
    assert sent_ctx[0]["held_order_tracking_link"] == "https://www.1mg.com/track/PO13326295207345"
    assert "PO13326295207345" in sent_ctx[0]["held_orders_status"]
    assert sent_ctx[0]["updated_eta"] == "as per your order confirmation"
    split.assert_awaited_once()
    split_args = split.await_args[0]
    assert split_args[0] == "PO13326295207344"
    assert split_args[1][0]["sku_id"] == "1122085"
    assert split_args[1][0]["qty"] == 7
    handlers.order_tracker.record_terminal.assert_awaited_with("PO13326295207344", "split_done")


@pytest.mark.asyncio
async def test_inbound_confirm_waits_for_eta_before_tracking(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    order: list[str] = []

    async def fake_wait(*a, **k):
        order.append("wait")
        return {"order_id": "PO_CHILD", "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}

    async def fake_track(*a, **k):
        order.append("track")
        return ("https://ship", "https://held", [{"order_id": "PO13326295207344"}])

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "split_jit_order", AsyncMock(return_value={"is_success": True}))
    monkeypatch.setattr(handlers, "wait_for_split_child_eta", fake_wait)
    monkeypatch.setattr(handlers, "resolve_split_tracking_links", fake_track)
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-wait-then-track",
        }
    )
    assert result["status"] == "ok"
    assert order == ["wait", "track"]


@pytest.mark.asyncio
async def test_inbound_confirm_retain_payload_is_available_qty_including_non_jit(monkeypatch):
    bundle = _sample_bundle()
    bundle["order"]["order_lines"].append(
        {
            "normalized_quantity": 3,
            "sku": {"sku_id": 554433, "name": "Non-JIT SKU"},
        }
    )
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "held_skus": [
            {
                "sku_id": h.sku_id,
                "name": h.name,
                "qty_ordered": h.qty_ordered,
                "qty_stuck": h.qty_stuck,
            }
            for h in result.held_skus
        ],
        "ship_skus": result.ship_skus,
    }
    split = AsyncMock(return_value={"is_success": True})
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(
        handlers,
        "wait_for_split_child_eta",
        AsyncMock(return_value={"order_id": "PO_CHILD", "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [{"order_id": "PO13326295207344"}])),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-mixed-retain",
        }
    )
    retain = split.await_args[0][1]
    assert retain == [
        {"sku_id": "1122085", "qty": 7},
        {"sku_id": "554433", "qty": 3},
    ]


@pytest.mark.asyncio
async def test_inbound_confirm_retain_qty_is_available_packs_times_pack_size(monkeypatch):
    bundle = _sample_bundle()
    bundle["order"]["order_lines"][0]["quantity"] = 90
    result = eligibility.evaluate_eligibility(bundle)
    assert result.held_skus[0].qty_stuck == 2
    assert result.ship_skus[0]["qty_retain"] == 70
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "held_skus": [
            {
                "sku_id": h.sku_id,
                "name": h.name,
                "qty_ordered": h.qty_ordered,
                "qty_stuck": h.qty_stuck,
            }
            for h in result.held_skus
        ],
        "ship_skus": result.ship_skus,
    }
    split = AsyncMock(return_value={"is_success": True})
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(
        handlers,
        "wait_for_split_child_eta",
        AsyncMock(return_value={"order_id": "PO_CHILD", "eta": {"eta_to": 1778748060.0, "to_date": "13 May, 2026"}}),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [{"order_id": "PO13326295207344"}])),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-retain-tablets",
        }
    )
    retain = split.await_args[0][1]
    assert retain == [{"sku_id": "1122085", "qty": 70}]


@pytest.mark.asyncio
async def test_inbound_confirm_uses_pending_tracking_when_urls_empty(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    sent_ctx: list[dict] = []
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "wait_for_split_child_eta", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("", "", [])),
    )
    monkeypatch.setattr(
        handlers.meta_client,
        "send_template",
        AsyncMock(side_effect=lambda *a, **k: sent_ctx.append(k.get("context") or a[2]) or {}),
    )

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-pending-track",
        }
    )
    assert sent_ctx[0]["ship_now_tracking_link"] == "Tracking will be shared shortly"
    assert sent_ctx[0]["held_order_tracking_link"] == "Tracking will be shared shortly"
    assert sent_ctx[0]["held_orders_status"] == "Held order details will be shared shortly"


@pytest.mark.asyncio
async def test_inbound_confirm_falls_back_to_parent_eta_when_child_has_none(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    sent_ctx: list[dict] = []

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(
        handlers,
        "wait_for_split_child_eta",
        AsyncMock(return_value={"order_id": "PO_CHILD", "eta": {"eta_to": 0, "to_date": "1 Jan, 1970"}}),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [{"order_id": "PO13326295207344"}])),
    )
    monkeypatch.setattr(
        handlers.meta_client,
        "send_template",
        AsyncMock(side_effect=lambda *a, **k: sent_ctx.append(k.get("context") or a[2]) or {}),
    )
    monkeypatch.setattr(handlers, "_eta_for_template", lambda bundle: "parent-promise-eta")

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-eta-fallback",
        }
    )
    assert sent_ctx[0]["updated_eta"] == "parent-promise-eta"
    assert sent_ctx[0]["updated_eta"] != "1 Jan, 1970"


@pytest.mark.asyncio
async def test_inbound_confirm_skips_split_if_already_done(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "held_skus": [{"sku_id": "1122085", "qty_ordered": 9}],
        "ship_skus": [],
    }
    split = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handlers.session_store,
        "get_split_context",
        AsyncMock(return_value=(9_999_999_000, frozenset({"PO_CHILD_OLD"}))),
    )
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    sent_ctx: list[dict] = []
    monkeypatch.setattr(
        handlers.meta_client,
        "send_template",
        AsyncMock(side_effect=lambda *a, **k: sent_ctx.append(k.get("context") or a[2]) or {}),
    )
    from app.agents.whatsapp_jit_hold import order_client

    search_rows = [
        {
            "order_id": "PO13326295207344",
            "shipment_detail": {"tracking_url": "https://track/parent"},
        },
        {
            "order_id": "PO_CHILD_OLD",
            "parent_id": "PO13326295207344",
            "created": 1000,
            "shipment_detail": {"tracking_url": "https://track/old"},
        },
        {
            "order_id": "PO_CHILD_NEW",
            "parent_id": "PO13326295207344",
            "created": 9_999_999_999,
            "shipment_detail": {"tracking_url": "https://track/new"},
        },
    ]
    monkeypatch.setattr(order_client, "search_orders_by_parent", AsyncMock(return_value=search_rows))

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-2",
        }
    )
    split.assert_not_awaited()
    assert sent_ctx[0]["ship_now_tracking_link"] == "https://track/parent"
    assert sent_ctx[0]["held_order_tracking_link"] == "https://track/new"


@pytest.mark.asyncio
async def test_inbound_state_change_requires_order_hint(monkeypatch):
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    result = await handlers.handle_inbound(
        {"phone": "9818886159", "action": ACTION_CONFIRM, "message_id": "wamid-nocb"}
    )
    assert result["status"] == "ignored"
    assert result["reason"] == "missing_order_hint"
    split.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_no_session_does_not_dedupe(monkeypatch):
    mark = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", mark)
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(None, None)))

    result = await handlers.handle_inbound(
        {"phone": "9818886159", "action": ACTION_CONFIRM, "message_id": "wamid-3"}
    )
    assert result["status"] == "no_session"
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_busy_when_session_lock_held(monkeypatch):
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "held_skus": [],
        "ship_skus": [],
    }
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))

    class _LockBusy:
        async def __aenter__(self):
            return False

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _LockBusy())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-busy",
        }
    )
    assert result["status"] == "busy"


@pytest.mark.asyncio
async def test_inbound_split_in_progress_returns_busy(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-split-busy",
        }
    )
    assert result["status"] == "busy"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_split_redis_done_failure_does_not_release(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    release = AsyncMock()
    block = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_split", release)
    monkeypatch.setattr(handlers.session_store, "block_split_retry", block)
    monkeypatch.setattr(
        handlers.session_store,
        "confirm_split_completed",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "split_jit_order",
        AsyncMock(return_value={"is_success": True, "data": {"child_order_id": "PO9"}}),
    )
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-split-redis",
        }
    )
    assert result["status"] == "busy"
    release.assert_not_awaited()
    block.assert_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_split_timeout_without_new_child_keeps_pending(monkeypatch):
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError

    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    release = AsyncMock()
    confirm = AsyncMock()
    block = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_split", release)
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers.session_store, "block_split_retry", block)
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "split_jit_order",
        AsyncMock(side_effect=SplitOutcomeUncertainError("timeout")),
    )
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-split-uncertain",
        }
    )
    assert result["status"] == "busy"
    release.assert_not_awaited()
    confirm.assert_not_awaited()
    block.assert_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_split_timeout_marks_done_when_child_appears(monkeypatch):
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError

    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    release = AsyncMock()
    confirm = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_split", release)
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "new_child_ids_after_split",
        AsyncMock(return_value=frozenset({"PO13326295207345"})),
    )
    monkeypatch.setattr(
        handlers,
        "split_jit_order",
        AsyncMock(side_effect=SplitOutcomeUncertainError("timeout")),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-split-uncertain-ok",
        }
    )
    assert result["status"] == "ok"
    confirm.assert_awaited()
    release.assert_not_awaited()
    send.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_split_timeout_polls_until_new_child(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError

    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    monkeypatch.setattr(order_client, "_ETA_WAIT_TIMEOUT_SEC", 10)
    monkeypatch.setattr(order_client, "_ETA_WAIT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    release = AsyncMock()
    confirm = AsyncMock()
    block = AsyncMock()
    split = AsyncMock(side_effect=SplitOutcomeUncertainError("timeout"))
    monkeypatch.setattr(handlers.session_store, "release_split", release)
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers.session_store, "block_split_retry", block)
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    discover = AsyncMock(side_effect=[frozenset(), frozenset({"PO13326295207345"})])
    monkeypatch.setattr(handlers, "new_child_ids_after_split", discover)
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(
        handlers,
        "wait_for_split_child_eta",
        AsyncMock(return_value={"order_id": "PO13326295207345", "eta": {"eta_to": 1}}),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-split-uncertain-poll",
        }
    )
    assert result["status"] == "ok"
    assert split.await_count == 1
    assert discover.await_count == 2
    confirm.assert_awaited()
    block.assert_not_awaited()
    release.assert_not_awaited()
    send.assert_awaited()


@pytest.mark.asyncio
async def test_inbound_state_change_rejects_ambiguous_multi_session(monkeypatch):
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    for action in (ACTION_CONFIRM, ACTION_OPTION_A):
        result = await handlers.handle_inbound(
            {"phone": "9818886159", "action": action, "message_id": f"wamid-ambig-{action}"}
        )
        assert result["status"] == "ignored"
        assert result["reason"] == "missing_order_hint"

    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_confirm_with_order_hint_allows_multi_session(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock(return_value={"is_success": True})
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_split_context", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-hint",
        }
    )
    assert result["status"] == "ok"
    split.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_session_removes_terminal_from_phone_index(monkeypatch):
    from app.agents.whatsapp_jit_hold import session_store
    from app.agents.whatsapp_jit_hold.constants import STATE_DONE_SPLIT

    sadd = AsyncMock()
    srem = AsyncMock()
    r = AsyncMock()
    r.set = AsyncMock()
    r.sadd = sadd
    r.srem = srem
    monkeypatch.setattr(session_store, "get_redis", lambda: r)

    await session_store.save_session("9818886159", "PO123", {"state": STATE_DONE_SPLIT})
    srem.assert_awaited_once()
    sadd.assert_not_awaited()


def test_normalize_order_created_ts_seconds_ms_and_ist_iso():
    assert normalize_order_created_ts(1778746400) == 1778746400
    assert normalize_order_created_ts(1_778_746_400_000) == 1778746400
    ist_iso = "2026-06-09T18:30:00"
    parsed = normalize_order_created_ts(ist_iso)
    assert parsed is not None
    assert parsed > 1_700_000_000


def test_format_held_orders_status_summary():
    text = format_held_orders_status_summary(
        [
            {
                "order_id": "PO_CHILD",
                "status": "Packaging",
                "sub_status": "on-hold",
            }
        ]
    )
    assert text == "PO_CHILD: Packaging / on-hold"


def test_new_child_orders_after_split_accepts_millisecond_created():
    orders = [
        {"order_id": "PO_CHILD_NEW", "parent_id": "PO_PARENT", "created": 1_900_000_000_000},
    ]
    fresh = new_child_orders_after_split(
        orders,
        "PO_PARENT",
        known_child_ids=frozenset(),
        split_after_ts=1_900_000_001,
    )
    assert [o["order_id"] for o in fresh] == ["PO_CHILD_NEW"]


@pytest.mark.asyncio
async def test_inbound_session_expired_with_order_hint_is_ignored(monkeypatch):
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(None, None)))

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "action": ACTION_CONFIRM,
            "order_id": "PO13326295207344",
            "message_id": "wamid-expired",
        }
    )
    assert result == {
        "status": "ignored",
        "reason": "session_expired",
        "order_id": "PO13326295207344",
    }


SAMPLE_ORDER_STATUS_PAYLOAD = {
    "id": "1783414799",
    "order_id": "PO18726323820776",
    "event": {
        "event_type": "NON_ORDER_STATUS_UPDATE",
        "event_name": "PACKAGING",
        "sub_event_name": "ON_HOLD",
        "description": "Order On Hold at Odin",
        "triggered_at": "2026-07-07T08:59:59.039+00:00",
    },
    "order_details": {
        "basic_order_details": {
            "order_id": "PO18726323820776",
            "group_order_id": "PO18726323820776",
            "created_at": 1783414782,
            "source": "1mg",
        },
        "user_details": {
            "contact_number": "8192888124",
        },
        "vendor_details": {
            "id": 8212,
            "tags": {"store_type": "WAREHOUSE", "fc_type": "Fulfilment Center"},
        },
    },
}

SAMPLE_PACKAGING_STATUS_PAYLOAD = {
    "id": "1783414799",
    "order_id": "PO18726323820776",
    "event": {
        "event_type": "ORDER_STATUS_UPDATE",
        "event_name": "PACKAGING",
        "sub_event_name": "PACKAGING",
        "description": "Packaging your order",
        "triggered_at": "2026-07-07T08:59:42.000+00:00",
    },
    "order_details": {
        "basic_order_details": {
            "order_id": "PO18726323820776",
            "group_order_id": "PO18726323820776",
        }
    },
}


# Captured from production-nexus-pharma_group_order_updates (PLACED envelope + ON_HOLD event).
SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD = {
    "order_type": "PHARMA",
    "group_order_id": "PO22526548033785",
    "user_id": "1ce52eb1-fdf1-418a-8635-e0ccf5cf0398",
    "group_orders": [
        {
            "order_id": "PO22526548033785",
            "order_details": {
                "basic_order_details": {
                    "order_id": "PO22526548033785",
                    "group_order_id": "PO22526548033785",
                    "source": "1mg",
                    "created_at": "2026-08-14T15:13:24",
                    "platform": {"name": "WEB", "version": "0.0.1"},
                    "is_new_user_order": False,
                    "is_po_selfserve_order": False,
                    "retail_order_type": None,
                },
                "user_details": {
                    "user_id": "1ce52eb1-fdf1-418a-8635-e0ccf5cf0398",
                    "contact_number": "9414171448",
                    "email": "jainkanti48@gmail.com",
                },
                "vendor_details": None,
                "status": {"title": "Placed", "sub_title": "Placed", "id": "15"},
            },
            "triggered_at": 1786685607.536,
        }
    ],
    "event": {
        "event_type": "NON_ORDER_STATUS_UPDATE",
        "event_name": "PACKAGING",
        "sub_event_name": "ON_HOLD",
        "description": "Order On Hold at Odin",
        "triggered_at": "2026-08-14T20:43:27.536278+05:30",
        "received_at": "2026-08-14T20:43:27.836209+05:30",
    },
}

SAMPLE_PHARMA_GROUP_ON_HOLD_FANOUT_PAYLOAD = {
    **SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD,
    "group_orders": [
        SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD["group_orders"][0],
        {
            "order_id": "PO22526548033786",
            "order_details": {
                "basic_order_details": {
                    "order_id": "PO22526548033786",
                    "group_order_id": "PO22526548033785",
                }
            },
        },
    ],
}


def test_order_status_parser_prod_pharma_kafka_envelope():
    from app.agents.whatsapp_jit_hold import order_status_parser

    assert order_status_parser.is_odin_packaging_on_hold(SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD) is True
    triggers = order_status_parser.parse_order_status_triggers(SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD)
    assert len(triggers) == 1
    assert triggers[0]["order_id"] == "PO22526548033785"
    assert triggers[0]["event_id"] == "PO22526548033785:ON_HOLD:2026-08-14T20:43:27.536278+05:30"
    scoped = triggers[0]["raw_envelope"]
    assert scoped["order_id"] == "PO22526548033785"
    assert scoped["order_details"] == SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD["group_orders"][0]["order_details"]


def test_order_status_parser_ignores_group_order_id_when_group_orders_present():
    from app.agents.whatsapp_jit_hold import order_status_parser

    payload = {
        **SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD,
        "group_order_id": "PO99999999999999",
    }
    triggers = order_status_parser.parse_order_status_triggers(payload)
    assert [t["order_id"] for t in triggers] == ["PO22526548033785"]


def test_order_status_parser_skips_group_orders_without_order_id():
    from app.agents.whatsapp_jit_hold import order_status_parser

    payload = {
        **SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD,
        "group_orders": [{"order_details": {"basic_order_details": {"order_id": "PO22526548033785"}}}],
    }
    assert order_status_parser.parse_order_status_triggers(payload) == []


def test_order_status_parser_pharma_group_order_topic():
    from app.agents.whatsapp_jit_hold import order_status_parser

    triggers = order_status_parser.parse_order_status_triggers(SAMPLE_PHARMA_GROUP_ON_HOLD_FANOUT_PAYLOAD)
    assert [t["order_id"] for t in triggers] == ["PO22526548033785", "PO22526548033786"]
    assert triggers[0]["event_id"] == "PO22526548033785:ON_HOLD:2026-08-14T20:43:27.536278+05:30"


@pytest.mark.asyncio
async def test_order_status_trigger_fans_out_group_orders(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock(return_value={"status": "skipped", "reason": "x"})
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)
    monkeypatch.setattr(order_status_trigger.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(order_status_trigger.session_store, "mark_webhook_processed", AsyncMock())

    import json

    await order_status_trigger.process_order_status_raw(
        json.dumps(SAMPLE_PHARMA_GROUP_ON_HOLD_FANOUT_PAYLOAD)
    )
    assert trigger.await_count == 2
    calls = {c.args[0]: c.kwargs["nexus_event"] for c in trigger.await_args_list}
    assert set(calls) == {"PO22526548033785", "PO22526548033786"}
    assert calls["PO22526548033785"]["order_id"] == "PO22526548033785"
    assert calls["PO22526548033786"]["order_id"] == "PO22526548033786"
    assert "order_details" in calls["PO22526548033785"]
    assert "order_details" in calls["PO22526548033786"]


@pytest.mark.asyncio
async def test_order_status_trigger_prod_pharma_kafka_envelope(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock(return_value={"status": "skipped", "reason": "x"})
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)
    monkeypatch.setattr(order_status_trigger.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(order_status_trigger.session_store, "mark_webhook_processed", AsyncMock())

    import json

    await order_status_trigger.process_order_status_raw(json.dumps(SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD))
    trigger.assert_awaited_once()
    assert trigger.await_args.args[0] == "PO22526548033785"
    scoped = trigger.await_args.kwargs["nexus_event"]
    assert scoped["order_id"] == "PO22526548033785"
    assert scoped["order_details"] == SAMPLE_PHARMA_PROD_ON_HOLD_PAYLOAD["group_orders"][0]["order_details"]


def test_order_status_parser_extracts_order_id_from_nexus_payload():
    from app.agents.whatsapp_jit_hold import order_status_parser

    parsed = order_status_parser.parse_order_status_body(SAMPLE_ORDER_STATUS_PAYLOAD)
    assert parsed is not None
    assert parsed["order_id"] == "PO18726323820776"
    assert parsed["event_id"] == "PO18726323820776:ON_HOLD:2026-07-07T08:59:59.039+00:00"


def test_order_status_parser_skips_packaging_without_on_hold():
    from app.agents.whatsapp_jit_hold import order_status_parser

    assert order_status_parser.parse_order_status_body(SAMPLE_PACKAGING_STATUS_PAYLOAD) is None
    assert order_status_parser.is_odin_packaging_on_hold(SAMPLE_ORDER_STATUS_PAYLOAD) is True
    assert order_status_parser.is_odin_packaging_on_hold(SAMPLE_PACKAGING_STATUS_PAYLOAD) is False


def test_order_status_parser_flat_order_id():
    from app.agents.whatsapp_jit_hold import order_status_parser

    parsed = order_status_parser.parse_order_status_body(
        {
            "order_id": "PO11111111111",
            "event": {
                "event_type": "NON_ORDER_STATUS_UPDATE",
                "event_name": "PACKAGING",
                "sub_event_name": "ON_HOLD",
                "triggered_at": "t1",
            },
        }
    )
    assert parsed is not None
    assert parsed["order_id"] == "PO11111111111"


def test_eligibility_uses_nexus_on_hold_without_order_sub_status():
    bundle = _sample_bundle()
    bundle["order"].pop("sub_status", None)
    bundle["nexus_event"] = SAMPLE_ORDER_STATUS_PAYLOAD
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True


def test_eligibility_nexus_on_hold_rejected_if_order_hold_cleared():
    bundle = _sample_bundle()
    bundle["order"]["sub_status"] = "processing"
    bundle["nexus_event"] = SAMPLE_ORDER_STATUS_PAYLOAD
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is False
    assert result.reason == "not_packaging_on_hold"


@pytest.mark.asyncio
async def test_kafka_eligibility_bundle_fetches_order_details_only(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    order = {"order_id": "PO18726323820776", "status_id": 130, "user": {"number": "8192888124"}}
    fetch_details = AsyncMock(return_value=order)
    monkeypatch.setattr(order_client, "fetch_order_details", fetch_details)
    monkeypatch.setattr(order_client.settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(order_client.sources, "_get_json", AsyncMock(side_effect=AssertionError("status-history must not be called")))
    monkeypatch.setattr(order_client.sources, "_post_explain_allocation", AsyncMock(side_effect=AssertionError("allocation must not be called")))

    bundle = await order_client.fetch_eligibility_bundle(
        "PO18726323820776",
        nexus_event=SAMPLE_ORDER_STATUS_PAYLOAD,
    )
    fetch_details.assert_awaited_once_with("PO18726323820776")
    assert bundle["collect_mode"] == "jit_hold_kafka"
    assert bundle["nexus_event"]["event"]["sub_event_name"] == "ON_HOLD"
    assert bundle["order"]["user"]["number"] == "8192888124"
    assert bundle["order"]["shipment_detail"]["vendor"]["tags"]["store_type"] == "WAREHOUSE"
    assert bundle["allocation"] == {"data": {}}
    assert "status" not in bundle
    assert "history" not in bundle
    assert "analytics" not in bundle


def test_eligibility_kafka_uses_nexus_vendor_without_allocation():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order

    bundle = _sample_bundle()
    bundle["order"].pop("sub_status", None)
    bundle["order"].pop("shipment_detail", None)
    bundle["allocation"] = {"data": {}}
    bundle["nexus_event"] = SAMPLE_ORDER_STATUS_PAYLOAD
    bundle["order"] = _merge_nexus_into_order(bundle["order"], SAMPLE_ORDER_STATUS_PAYLOAD)
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True


@pytest.mark.asyncio
async def test_conversation_eligibility_bundle_still_fetches_side_apis(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    monkeypatch.setattr(order_client.settings, "whatsapp_jit_hold_use_fixtures", False)
    monkeypatch.setattr(order_client.settings, "order_rca_order_service_base_url", "http://order.test")
    monkeypatch.setattr(order_client.settings, "order_rca_sla_service_base_url", "http://sla.test")
    monkeypatch.setattr(order_client.settings, "order_rca_sla_auth_token", "token")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    get_json = AsyncMock(return_value={"order_id": "PO1"})
    post_alloc = AsyncMock(return_value={"data": {}})
    hist = AsyncMock(return_value={})
    analytics = AsyncMock(return_value={})
    monkeypatch.setattr(order_client.sources, "_get_json", get_json)
    monkeypatch.setattr(order_client.sources, "_post_explain_allocation", post_alloc)
    monkeypatch.setattr(order_client.sources, "_fetch_order_history", hist)
    monkeypatch.setattr(order_client.sources, "_fetch_order_analytics_safe", analytics)

    bundle = await order_client.fetch_eligibility_bundle("PO1")
    assert bundle["collect_mode"] == "jit_hold_eligibility"
    assert get_json.await_count == 1
    post_alloc.assert_awaited_once()
    hist.assert_awaited_once()
    analytics.assert_awaited_once()


def test_order_status_parser_ignores_invalid_json():
    from app.agents.whatsapp_jit_hold import order_status_parser

    assert order_status_parser.parse_order_status_body("not-json") is None


@pytest.mark.asyncio
async def test_whatsapp_fixtures_independent_of_order_rca_fixtures(monkeypatch):
    """WHATSAPP_JIT_HOLD_USE_FIXTURES alone loads JSON fixtures without ORDER_RCA_USE_FIXTURES."""
    from app.agents.whatsapp_jit_hold import order_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", True)
    monkeypatch.setattr(settings, "order_rca_use_fixtures", False)

    fetch_bundle = AsyncMock(side_effect=AssertionError("fetch_bundle must not be called"))
    monkeypatch.setattr(order_client.sources, "fetch_bundle", fetch_bundle)

    bundle = await order_client.fetch_eligibility_bundle("PO13326295017145")
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True
    assert bundle.get("collect_mode") == "full"


def test_order_status_dedupe_policy():
    assert handlers.order_status_result_should_dedupe({"status": "sent"}) is True
    assert handlers.order_status_result_should_dedupe({"status": "skipped", "reason": "not_eligible"}) is True
    assert handlers.order_status_result_should_dedupe({"status": "skipped", "reason": "already_sent"}) is False
    assert handlers.order_status_result_should_dedupe({"status": "duplicate"}) is False
    assert handlers.order_status_result_should_dedupe({"status": "busy"}) is False
    assert handlers.order_status_result_should_dedupe({"status": "skipped", "reason": "disabled"}) is False


@pytest.mark.asyncio
async def test_order_status_event_dedupe_already_sent_requires_session(monkeypatch):
    oid = "PO13326295207344"
    monkeypatch.setattr(
        handlers.session_store,
        "get_order_send_lock_value",
        AsyncMock(return_value="sent"),
    )
    monkeypatch.setattr(handlers.session_store, "has_active_session_for_order", AsyncMock(return_value=False))
    result = {"status": "skipped", "reason": "already_sent"}
    assert await handlers.order_status_event_should_dedupe(result, oid) is False

    monkeypatch.setattr(handlers.session_store, "has_active_session_for_order", AsyncMock(return_value=True))
    assert await handlers.order_status_event_should_dedupe(result, oid) is True


@pytest.mark.asyncio
async def test_inbound_option_a_saves_before_send(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_INITIAL_SENT,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    save_states: list[str] = []
    send_count = 0

    async def track_save(phone, oid, doc):
        save_states.append(doc.get("state"))

    async def fake_send(*args, **kwargs):
        nonlocal send_count
        send_count += 1
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed_retry", AsyncMock())
    monkeypatch.setattr(
        handlers.session_store,
        "resolve_session",
        AsyncMock(return_value=(dict(session), "PO13326295207344")),
    )
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", track_save)
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_OPTION_A,
            "message_id": "wamid-save-first",
        }
    )
    assert save_states == [STATE_OPTION_A_PREVIEW]
    assert send_count == 1


@pytest.mark.asyncio
async def test_inbound_option_a_send_fail_reverts_state(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_INITIAL_SENT,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    saved_states: list[str] = []

    async def track_save(phone, oid, doc):
        saved_states.append(doc.get("state"))

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed_retry", AsyncMock())
    monkeypatch.setattr(
        handlers.session_store,
        "resolve_session",
        AsyncMock(return_value=(dict(session), "PO13326295207344")),
    )
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", track_save)
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(
        handlers.meta_client,
        "send_template",
        AsyncMock(side_effect=RuntimeError("meta down")),
    )
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    with pytest.raises(RuntimeError, match="meta down"):
        await handlers.handle_inbound(
            {
                "phone": "9818886159",
                "order_id": "PO13326295207344",
                "action": ACTION_OPTION_A,
                "message_id": "wamid-send-fail",
            }
        )
    assert saved_states == [STATE_OPTION_A_PREVIEW, STATE_INITIAL_SENT]
    handlers.session_store.mark_webhook_processed_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_status_trigger_skips_duplicate_event_id(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock()
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)
    monkeypatch.setattr(order_status_trigger.session_store, "is_webhook_processed", AsyncMock(return_value=True))

    import json

    await order_status_trigger.process_order_status_raw(json.dumps(SAMPLE_ORDER_STATUS_PAYLOAD))
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_status_trigger_no_order_id_skips_trigger(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock()
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)

    import json

    await order_status_trigger.process_order_status_raw(json.dumps({"id": "1", "foo": "bar"}))
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_status_trigger_logs_packaging_on_hold_received(caplog, monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock(return_value={"status": "skipped", "reason": "not_eligible"})
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)
    monkeypatch.setattr(order_status_trigger.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(order_status_trigger.session_store, "mark_webhook_processed", AsyncMock())

    import json

    with caplog.at_level(logging.INFO):
        await order_status_trigger.process_order_status_raw(json.dumps(SAMPLE_ORDER_STATUS_PAYLOAD))

    assert any(
        "order_status packaging_on_hold received order_id=PO18726323820776" in r.message
        for r in caplog.records
    )
    assert any(
        "order_status packaging_on_hold processed order_id=PO18726323820776" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_order_status_trigger_processes_nexus_payload(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock(return_value={"status": "skipped", "reason": "not_eligible"})
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)
    monkeypatch.setattr(order_status_trigger.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(order_status_trigger.session_store, "mark_webhook_processed", AsyncMock())

    import json

    await order_status_trigger.process_order_status_raw(json.dumps(SAMPLE_ORDER_STATUS_PAYLOAD))
    trigger.assert_awaited_once_with("PO18726323820776", nexus_event=SAMPLE_ORDER_STATUS_PAYLOAD)
    order_status_trigger.session_store.mark_webhook_processed.assert_awaited_once_with(
        "order_status:PO18726323820776:ON_HOLD:2026-07-07T08:59:59.039+00:00"
    )


@pytest.mark.asyncio
async def test_order_status_trigger_skips_packaging_without_on_hold(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_status_trigger

    trigger = AsyncMock()
    monkeypatch.setattr(order_status_trigger, "handle_order_trigger", trigger)

    import json

    await order_status_trigger.process_order_status_raw(json.dumps(SAMPLE_PACKAGING_STATUS_PAYLOAD))
    trigger.assert_not_awaited()


def test_normalize_kafka_bootstrap_servers():
    from app.agents.whatsapp_jit_hold.order_status_trigger import normalize_kafka_bootstrap_servers

    assert normalize_kafka_bootstrap_servers(
        "a:9092,b:9092"
    ) == ["a:9092", "b:9092"]
    assert normalize_kafka_bootstrap_servers("") == []


def test_kafka_lookback_timestamp_ms():
    from app.agents.whatsapp_jit_hold.kafka_consumer import lookback_timestamp_ms

    now = 1_800_000_000_000
    assert lookback_timestamp_ms(hours=4, now_ms=now) == now - 4 * 3_600_000
    assert lookback_timestamp_ms(hours=0, now_ms=now) == now


@pytest.mark.asyncio
async def test_kafka_lookback_seeks_uncommitted_only():
    from typing import Any

    from app.agents.whatsapp_jit_hold.kafka_consumer import seek_uncommitted_partitions_to_lookback
    from tests.whatsapp_jit_kafka_sim import OffsetAndTimestamp, TopicPartition

    class _Fake:
        def __init__(self) -> None:
            self.sought: dict[Any, int] = {}
            self._committed: dict[Any, int | None] = {}

        async def committed(self, tp: Any) -> int | None:
            return self._committed.get(tp)

        async def offsets_for_times(self, timestamps: dict[Any, int]) -> dict[Any, OffsetAndTimestamp]:
            return {tp: OffsetAndTimestamp(12, ts) for tp, ts in timestamps.items()}

        async def end_offsets(self, tps: list[Any]) -> dict[Any, int]:
            return {tp: 99 for tp in tps}

        def seek(self, tp: Any, offset: int) -> None:
            self.sought[tp] = offset

    fake = _Fake()
    assigned = TopicPartition("t", 0)
    committed = TopicPartition("t", 1)
    fake._committed[assigned] = None
    fake._committed[committed] = 40

    sought = await seek_uncommitted_partitions_to_lookback(
        fake, [assigned, committed], lookback_hours=4, now_ms=1_800_000_000_000
    )
    assert sought == {assigned: 12}
    assert committed not in fake.sought


@pytest.mark.asyncio
async def test_kafka_lookback_negative_offset_seeks_end():
    from typing import Any

    from app.agents.whatsapp_jit_hold.kafka_consumer import seek_uncommitted_partitions_to_lookback
    from tests.whatsapp_jit_kafka_sim import OffsetAndTimestamp, TopicPartition

    class _Fake:
        def __init__(self) -> None:
            self.sought: dict[Any, int] = {}

        async def committed(self, tp: Any) -> int | None:
            return None

        async def offsets_for_times(self, timestamps: dict[Any, int]) -> dict[Any, OffsetAndTimestamp]:
            return {tp: OffsetAndTimestamp(-1, ts) for tp, ts in timestamps.items()}

        async def end_offsets(self, tps: list[Any]) -> dict[Any, int]:
            return {tp: 50 for tp in tps}

        def seek(self, tp: Any, offset: int) -> None:
            self.sought[tp] = offset

    fake = _Fake()
    tp = TopicPartition("t", 0)
    sought = await seek_uncommitted_partitions_to_lookback(
        fake, [tp], lookback_hours=4, now_ms=1_800_000_000_000
    )
    assert sought == {tp: 50}


@pytest.mark.asyncio
async def test_kafka_does_not_commit_past_failed_offset(monkeypatch):
    """A later success in the same batch must not commit past an earlier failure."""
    import asyncio
    import json

    from app.agents.whatsapp_jit_hold import kafka_consumer
    from tests.whatsapp_jit_kafka_sim import (
        FakeAIOKafkaConsumer,
        OffsetAndMetadata,
        _install_fake_aiokafka,
        build_nexus_payload,
    )
    from app.config.settings import settings

    oid = "PO13326295017145"
    payloads = [
        build_nexus_payload(event_id="fail-1", order_id=oid, triggered_at="2026-07-07T08:59:59.100+00:00"),
        build_nexus_payload(event_id="ok-2", order_id=oid, triggered_at="2026-07-07T08:59:59.200+00:00"),
    ]
    messages = [json.dumps(p) for p in payloads]

    seen_offsets: list[int] = []
    fail_once = {"done": False}

    async def process_tracking(raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        # Map event to original batch index via triggered_at suffix.
        ts = str((data.get("event") or {}).get("triggered_at") or "")
        idx = 0 if ".100+" in ts else 1
        seen_offsets.append(idx)
        if idx == 0 and not fail_once["done"]:
            fail_once["done"] = True
            raise RuntimeError("GET order failed")

    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_bootstrap_servers", "sim:9092")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_topic", "sim-topic")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_lookback_hours", 0)

    consumer = FakeAIOKafkaConsumer(topic="sim-topic", messages=messages)
    _install_fake_aiokafka(lambda **kw: consumer)

    monkeypatch.setattr(
        "app.agents.whatsapp_jit_hold.kafka_consumer.process_order_status_raw",
        process_tracking,
    )
    monkeypatch.setattr(kafka_consumer, "process_failure_backoff_sec", lambda n: 0)
    kafka_consumer.reset_stop()

    async def stop_soon():
        await asyncio.sleep(0.2)
        kafka_consumer.request_stop()

    stopper = asyncio.create_task(stop_soon())
    await kafka_consumer.run()
    await stopper

    committed = []
    for c in consumer.commits:
        for meta in c.values():
            committed.append(meta.offset if isinstance(meta, OffsetAndMetadata) else int(meta))

    # Failed offset 0 must be retried before any commit past it (commit value 2).
    assert seen_offsets[0] == 0
    assert fail_once["done"] is True
    assert seen_offsets.count(0) >= 2  # fail then retry
    if committed:
        # First commit can only be 1 (after successful retry of offset 0).
        assert committed[0] == 1
        assert 2 not in committed[:1]


def test_decode_kafka_value_utf8_and_poison():
    from app.agents.whatsapp_jit_hold.kafka_consumer import decode_kafka_value

    assert decode_kafka_value(b'{"ok":true}') == '{"ok":true}'
    assert decode_kafka_value('{"ok":true}') == '{"ok":true}'
    with pytest.raises(UnicodeDecodeError):
        decode_kafka_value(b"\xff\xfe")


@pytest.mark.asyncio
async def test_kafka_skips_utf8_poison_and_commits_past(monkeypatch):
    import asyncio
    import json

    from app.agents.whatsapp_jit_hold import kafka_consumer
    from tests.whatsapp_jit_kafka_sim import (
        FakeAIOKafkaConsumer,
        OffsetAndMetadata,
        _install_fake_aiokafka,
        build_nexus_payload,
    )
    from app.config.settings import settings

    good = json.dumps(
        build_nexus_payload(
            event_id="ok-utf8",
            order_id="PO13326295017145",
            triggered_at="2026-07-07T08:59:59.300+00:00",
        )
    )
    processed: list[str] = []

    async def capture(raw):
        processed.append(raw if isinstance(raw, str) else str(raw))

    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_bootstrap_servers", "sim:9092")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_topic", "sim-topic")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_lookback_hours", 0)

    consumer = FakeAIOKafkaConsumer(topic="sim-topic", messages=[b"\xff\xfe", good])
    _install_fake_aiokafka(lambda **kw: consumer)
    monkeypatch.setattr(
        "app.agents.whatsapp_jit_hold.kafka_consumer.process_order_status_raw",
        capture,
    )
    kafka_consumer.reset_stop()

    async def stop_soon():
        await asyncio.sleep(0.2)
        kafka_consumer.request_stop()

    stopper = asyncio.create_task(stop_soon())
    await kafka_consumer.run()
    await stopper

    committed = []
    for c in consumer.commits:
        for meta in c.values():
            committed.append(meta.offset if isinstance(meta, OffsetAndMetadata) else int(meta))
    assert 1 in committed  # skipped poison at offset 0
    assert processed == [good]


def test_session_lock_ttl_covers_confirm_path():
    from app.agents.whatsapp_jit_hold import session_store

    assert session_store._SESSION_LOCK_TTL_SEC >= 900
    assert session_store._SENT_PENDING_TTL_SEC == 900
    assert session_store._SPLIT_PENDING_TTL_SEC == 900


@pytest.mark.asyncio
async def test_try_acquire_order_send_uses_pending_ttl(monkeypatch):
    from app.agents.whatsapp_jit_hold import session_store

    captured: dict = {}

    class _Redis:
        async def set(self, key, value, ex=None, nx=None):
            captured["ex"] = ex
            captured["value"] = value
            captured["nx"] = nx
            return True

    monkeypatch.setattr(session_store, "get_redis", lambda: _Redis())
    assert await session_store.try_acquire_order_send("PO1") is True
    assert captured["value"] == "pending"
    assert captured["ex"] == 900
    assert captured["nx"] is True


@pytest.mark.asyncio
async def test_confirm_split_blocked_without_child_does_not_post(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-blocked",
        }
    )
    assert result["status"] == "busy"
    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_stale_preview_resends_option_a(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "held_skus": [{"sku_id": "1122085", "name": "Test SKU", "qty_ordered": 9, "qty_stuck": 2}],
        "ship_skus": [{"sku_id": "554433", "name": "Old preview", "qty": 3, "jit": False}],
    }
    split = AsyncMock()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    save = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "save_session", save)
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-stale",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_A_PREVIEW
    assert sent == ["option_a"]
    split.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_split_with_preexisting_child_does_not_fake_done(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handlers.session_store,
        "get_split_context",
        AsyncMock(return_value=(1_000, frozenset({"PO_OLD"}))),
    )
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-blocked-old",
        }
    )
    assert result["status"] == "busy"
    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_superseded_option_a_card_resends_preview(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "preview_fp": "live-v2",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value="old-v1"))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)

    class _Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-old-card",
            "context_id": "wamid.outbound.v1",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_A_PREVIEW
    assert sent == ["option_a"]
    split.assert_not_awaited()


@pytest.mark.asyncio
async def test_kafka_trigger_skips_fixtures_without_test_mode(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result == {"status": "skipped", "reason": "fixtures_require_test_mode"}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_not_found_is_skipped_not_raised(monkeypatch):
    from app.agents.whatsapp_jit_hold.order_client import OrderNotFoundError

    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)
    monkeypatch.setattr(
        handlers,
        "fetch_eligibility_bundle",
        AsyncMock(side_effect=OrderNotFoundError("PO404")),
    )
    result = await handlers.handle_order_trigger("PO404")
    assert result == {"status": "skipped", "reason": "order_not_found"}
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_meta_initial_retry_timeout_raises(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            raise meta_client.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())

    with pytest.raises(meta_client.httpx.ReadTimeout):
        await meta_client.send_template(
            "9818886159",
            "initial",
            {"order_id": "PO123", "customer_name": "A", "held_items": "item"},
            callback_data="jit_hold:PO123:initial_retry",
        )


@pytest.mark.asyncio
async def test_kafka_process_backoff_does_not_block_consumer_30s(monkeypatch):
    import asyncio
    import json
    import time

    from app.agents.whatsapp_jit_hold import kafka_consumer
    from tests.whatsapp_jit_kafka_sim import (
        FakeAIOKafkaConsumer,
        _install_fake_aiokafka,
        build_nexus_payload,
    )
    from app.config.settings import settings

    payload = json.dumps(
        build_nexus_payload(
            event_id="fail-backoff",
            order_id="PO13326295017145",
            triggered_at="2026-07-07T08:59:59.100+00:00",
        )
    )

    async def always_fail(_raw):
        raise RuntimeError("GET order failed")

    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_bootstrap_servers", "sim:9092")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_topic", "sim-topic")
    monkeypatch.setattr(settings, "whatsapp_jit_hold_kafka_lookback_hours", 0)
    consumer = FakeAIOKafkaConsumer(topic="sim-topic", messages=[payload])
    _install_fake_aiokafka(lambda **kw: consumer)
    monkeypatch.setattr(
        "app.agents.whatsapp_jit_hold.kafka_consumer.process_order_status_raw",
        always_fail,
    )
    monkeypatch.setattr(kafka_consumer, "process_failure_backoff_sec", lambda n: 30.0)
    kafka_consumer.reset_stop()

    async def stop_soon():
        await asyncio.sleep(0.4)
        kafka_consumer.request_stop()

    t0 = time.monotonic()
    stopper = asyncio.create_task(stop_soon())
    await kafka_consumer.run()
    await stopper
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0


def test_default_overlay_sets_shipment_vendor_tags():
    bundle = _sample_bundle()
    vendor = bundle["order"]["shipment_detail"]["vendor"]
    assert vendor["tags"]["store_type"] == "WAREHOUSE"
    assert "fulfil" in vendor["tags"]["fc_type"].lower()


def test_eligibility_kafka_slim_bundle_uses_order_vendor_tags():
    bundle = _sample_bundle()
    bundle["allocation"] = {"data": {}}
    bundle["nexus_event"] = SAMPLE_ORDER_STATUS_PAYLOAD
    bundle["order"].pop("sub_status", None)
    result = eligibility.evaluate_eligibility(bundle)
    assert result.eligible is True


@pytest.mark.asyncio
async def test_kafka_simulation_end_to_end(monkeypatch):
    from tests.whatsapp_jit_kafka_sim import run_kafka_simulation
    from app.infra import redis_client

    # Prior tests may leave a Redis client bound to a closed event loop.
    monkeypatch.setattr(redis_client, "_redis", None)
    monkeypatch.setattr(redis_client, "_pool", None)

    summary = await run_kafka_simulation(order_id="PO13326295017145")
    assert summary["commits"] == 4
    assert summary["meta_sends"] == 1
    assert summary["session_state"] == "initial_sent"
    assert summary["sent_lock"] is not None
    assert "whatsapp_jit_hold:dedupe:order_status:PO13326295017145:ON_HOLD:2026-07-07T08:59:59.001+00:00" in summary["dedupe_keys"]
    assert "whatsapp_jit_hold:dedupe:order_status:PO13326295017145:ON_HOLD:2026-07-07T08:59:59.002+00:00" in summary["dedupe_keys"]


@pytest.mark.asyncio
async def test_conversation_end_to_end_option_a_after_kafka_sim(monkeypatch):
    """Kafka sim → Redis session → Option A preview → CONFIRM split → done."""
    from tests.whatsapp_jit_kafka_sim import build_eligibility_fixture, run_kafka_simulation
    from app.config.settings import settings
    from app.infra import redis_client

    monkeypatch.setattr(redis_client, "_redis", None)
    monkeypatch.setattr(redis_client, "_pool", None)

    oid = "PO19999999999991"
    phone = (settings.whatsapp_jit_hold_test_phone or "9555560920").strip()[-10:]
    fixture = build_eligibility_fixture(oid)
    sent: list[str] = []

    async def fake_send(dest, template_key, context, **kw):
        sent.append(template_key)
        return {"messages": [{"id": f"wamid.{template_key}"}]}

    summary = await run_kafka_simulation(order_id=oid)
    assert summary["session_state"] == STATE_INITIAL_SENT
    assert summary["meta_sends"] == 1

    from app.agents.whatsapp_jit_hold import session_store as e2e_store

    r = e2e_store.get_redis()
    await r.delete(f"whatsapp_jit_hold:split:{oid}")
    await r.delete(f"whatsapp_jit_hold:split_meta:{oid}")

    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=fixture))
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "split_jit_order",
        AsyncMock(return_value={"is_success": True, "data": {"child_order_id": "POE2ECHILD"}}),
    )
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)

    mid_a = f"wamid-e2e-a-{uuid.uuid4().hex}"
    mid_ok = f"wamid-e2e-a-ok-{uuid.uuid4().hex}"
    mid_again = f"wamid-e2e-a-again-{uuid.uuid4().hex}"

    a = await handlers.handle_inbound(
        {"phone": phone, "order_id": oid, "action": ACTION_OPTION_A, "message_id": mid_a}
    )
    assert a["status"] == "ok"
    assert a["state"] == STATE_OPTION_A_PREVIEW
    assert sent == ["option_a"]

    confirm = await handlers.handle_inbound(
        {"phone": phone, "order_id": oid, "action": ACTION_CONFIRM, "message_id": mid_ok}
    )
    assert confirm["status"] == "ok"
    assert confirm["state"] == STATE_DONE_SPLIT
    assert sent == ["option_a", "option_a_done"]

    again = await handlers.handle_inbound(
        {"phone": phone, "order_id": oid, "action": ACTION_CONFIRM, "message_id": mid_again}
    )
    assert again["status"] == "terminal"
    assert again["state"] == STATE_DONE_SPLIT


@pytest.mark.asyncio
async def test_conversation_end_to_end_option_b_after_kafka_sim(monkeypatch):
    """Kafka sim → Redis session → Option B preview → CONFIRM hold → done."""
    from tests.whatsapp_jit_kafka_sim import build_eligibility_fixture, run_kafka_simulation
    from app.config.settings import settings
    from app.infra import redis_client

    monkeypatch.setattr(redis_client, "_redis", None)
    monkeypatch.setattr(redis_client, "_pool", None)

    oid = "PO19999999999992"
    phone = (settings.whatsapp_jit_hold_test_phone or "9555560920").strip()[-10:]
    fixture = build_eligibility_fixture(oid)
    sent: list[str] = []

    async def fake_send(dest, template_key, context, **kw):
        sent.append(template_key)
        return {"messages": [{"id": f"wamid.{template_key}"}]}

    summary = await run_kafka_simulation(order_id=oid)
    assert summary["session_state"] == STATE_INITIAL_SENT

    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=fixture))
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    split = AsyncMock()
    monkeypatch.setattr(handlers, "split_jit_order", split)

    mid_b = f"wamid-e2e-b-{uuid.uuid4().hex}"
    mid_hold = f"wamid-e2e-b-hold-{uuid.uuid4().hex}"
    wamid_card = f"wamid.option-b-card-{uuid.uuid4().hex}"

    b = await handlers.handle_inbound(
        {"phone": phone, "order_id": oid, "action": ACTION_OPTION_B, "message_id": mid_b}
    )
    assert b["status"] == "ok"
    assert b["state"] == STATE_OPTION_B_PREVIEW
    assert sent == ["option_b"]

    from app.agents.whatsapp_jit_hold import session_store

    await session_store.bind_outbound_message(
        wamid_card,
        oid,
        callback_data=f"jit_hold:{oid}:option_b",
    )
    hold = await handlers.handle_inbound(
        {
            "phone": phone,
            "order_id": oid,
            "action": ACTION_CONFIRM,
            "message_id": mid_hold,
            "context_id": wamid_card,
        }
    )
    assert hold["status"] == "ok"
    assert hold["state"] == STATE_DONE_HOLD
    assert sent == ["option_b", "option_b_done"]
    split.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_from_initial_sent_recovers_option_a(monkeypatch):
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_INITIAL_SENT,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split_confirm = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(
        handlers.session_store,
        "resolve_session",
        AsyncMock(return_value=(dict(session), "PO13326295207344")),
    )
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value="fp-a"))
    monkeypatch.setattr(handlers, "_confirm_split", split_confirm)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-recover-a",
            "context_id": "wamid.option-a-card",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_DONE_SPLIT
    split_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_from_initial_sent_recovers_option_b(monkeypatch):
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_INITIAL_SENT,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(
        handlers.session_store,
        "resolve_session",
        AsyncMock(return_value=(dict(session), "PO13326295207344")),
    )
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=_sample_bundle()))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handlers.session_store,
        "callback_data_for_outbound_message",
        AsyncMock(return_value="jit_hold:PO13326295207344:option_b"),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-recover-b",
            "context_id": "wamid.option-b-card",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_DONE_HOLD
    assert sent == ["option_b_done"]
    handlers.order_tracker.record_terminal.assert_awaited_with("PO13326295207344", "kept_original")


class _MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


class _Lock:
    async def __aenter__(self):
        return True

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_status_bind_does_not_wipe_preview_snapshot(monkeypatch):
    from app.agents.whatsapp_jit_hold import session_store

    redis = _MemRedis()
    monkeypatch.setattr(session_store, "get_redis", lambda: redis)
    await session_store.bind_outbound_message(
        "wamid.A",
        "PO1",
        callback_data="jit_hold:PO1:option_a",
        snapshot="fp-v2",
    )
    assert await session_store.snapshot_for_outbound_message("wamid.A") == "fp-v2"
    await session_store.bind_outbound_message(
        "wamid.A",
        "PO1",
        callback_data="jit_hold:PO1:option_a",
    )
    assert await session_store.snapshot_for_outbound_message("wamid.A") == "fp-v2"
    assert await session_store.order_id_for_outbound_message("wamid.A") == "PO1"
    assert await session_store.callback_data_for_outbound_message("wamid.A") == "jit_hold:PO1:option_a"


@pytest.mark.asyncio
async def test_confirm_missing_card_fp_resends_preview(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        "preview_fp": "live-v2",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-no-fp",
            "context_id": "wamid.outbound.wiped",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_A_PREVIEW
    assert sent == ["option_a"]
    split.assert_not_awaited()


@pytest.mark.asyncio
async def test_meta_send_redis_fail_after_200_does_not_raise(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.ok"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.ok"}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(
        meta_client.session_store,
        "bind_outbound_message",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    monkeypatch.setattr(
        meta_client.session_store,
        "confirm_order_sent",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )

    data = await meta_client.send_template(
        "9818886159",
        "initial",
        {"order_id": "PO123", "customer_name": "A", "held_items": "item"},
        callback_data="jit_hold:PO123:initial",
        treat_uncertain_as_sent=True,
    )
    assert data["messages"][0]["id"] == "wamid.ok"


@pytest.mark.asyncio
async def test_kafka_trigger_keeps_lock_if_redis_fails_after_meta_200(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    bundle = _sample_bundle()
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.ok"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.ok"}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(
        meta_client.session_store,
        "bind_outbound_message",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "try_acquire_order_send", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_order_sent", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    release = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "release_order_send", release)

    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result["status"] == "sent"
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_split_4xx_clears_meta_and_retries(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    ctx: dict = {"value": (None, None)}

    async def fake_get(_oid):
        return ctx["value"]

    async def fake_save(_oid, *, split_after_ts, known_child_ids):
        ctx["value"] = (split_after_ts, known_child_ids)

    async def fake_clear(_oid):
        ctx["value"] = (None, None)

    split = AsyncMock(side_effect=[SplitOrderError("JIT split failed"), {"is_success": True}])
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", fake_get)
    monkeypatch.setattr(handlers.session_store, "save_split_context", fake_save)
    monkeypatch.setattr(handlers.session_store, "clear_split_context", fake_clear)
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "release_split", AsyncMock())
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    with pytest.raises(SplitOrderError):
        await handlers.handle_inbound(
            {
                "phone": "9818886159",
                "order_id": "PO13326295207344",
                "action": ACTION_CONFIRM,
                "message_id": "wamid-4xx-1",
            }
        )
    assert ctx["value"] == (None, None)

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-4xx-2",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_DONE_SPLIT
    assert split.await_count == 2


@pytest.mark.asyncio
async def test_confirm_split_search_fail_does_not_post_or_save_empty_snapshot(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    save = AsyncMock()
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers.session_store, "save_split_context", save)
    monkeypatch.setattr(handlers, "snapshot_child_ids_before_split", AsyncMock(side_effect=RuntimeError("search down")))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-search-fail",
        }
    )
    assert result["status"] == "busy"
    save.assert_not_awaited()
    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_split_search_fail_does_not_fake_done(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    split = AsyncMock()
    send = AsyncMock()
    confirm = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handlers.session_store,
        "get_split_context",
        AsyncMock(return_value=(1_000, frozenset())),
    )
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(side_effect=RuntimeError("search down")))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-blocked-search-fail",
        }
    )
    assert result["status"] == "busy"
    confirm.assert_not_awaited()
    split.assert_not_awaited()
    send.assert_not_awaited()


def test_meta_free_text_yes_is_not_confirm(monkeypatch):
    from app.agents.whatsapp_jit_hold.meta_parser import iter_meta_inbound_events
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "1353517691170148"},
                            "contacts": [{"wa_id": "918076532044"}],
                            "messages": [
                                {
                                    "from": "918076532044",
                                    "id": "wamid.TEXT",
                                    "timestamp": "1591210827",
                                    "type": "text",
                                    "text": {"body": "yes"},
                                    "context": {"id": "wamid.OUTBOUND"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    assert iter_meta_inbound_events(payload) == []


def test_nexus_merge_does_not_mint_store_type_from_vendor_type():
    from app.agents.whatsapp_jit_hold.order_client import _merge_nexus_into_order

    envelope = {
        "order_details": {
            "vendor_details": {
                "id": 1,
                "vendor_type": "WAREHOUSE",
                "tags": {"vendor_type": "Non-VMO", "fc_type": ""},
            }
        }
    }
    merged = _merge_nexus_into_order({"order_id": "PO1", "shipment_detail": {}}, envelope)
    tags = merged["shipment_detail"]["vendor"]["tags"]
    assert tags.get("store_type") not in {"WAREHOUSE", "RETAIL"}
    assert tags.get("vendor_type") == "Non-VMO"


@pytest.mark.asyncio
async def test_meta_5xx_redis_confirm_fail_does_not_raise(monkeypatch):
    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Resp:
        status_code = 503
        content = b"unavailable"
        text = "unavailable"

        def raise_for_status(self):
            raise meta_client.httpx.HTTPStatusError("503", request=None, response=self)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(
        meta_client.session_store,
        "confirm_order_sent",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    data = await meta_client.send_template(
        "9818886159",
        "initial",
        {"order_id": "PO123", "customer_name": "A", "held_items": "item"},
        callback_data="jit_hold:PO123:initial",
        treat_uncertain_as_sent=True,
    )
    assert data["uncertain"] is True
    assert data["http_status"] == 503


@pytest.mark.asyncio
async def test_split_http_200_non_json_is_uncertain(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.agents.whatsapp_jit_hold.order_client import SplitOutcomeUncertainError
    from app.config.settings import settings

    class _Resp:
        status_code = 200
        content = b"not-json"
        text = "not-json"

        def json(self):
            raise ValueError("not json")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", False)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_username", "wa_bot")
    monkeypatch.setattr(settings, "order_rca_order_service_base_url", "http://orders.test")
    monkeypatch.setattr(order_client.sources, "_order_headers", lambda: {})
    monkeypatch.setattr(order_client, "get_internal_http_client", lambda: _Client())

    with pytest.raises(SplitOutcomeUncertainError):
        await split_jit_order("PO123", [{"sku_id": "1", "qty": 1}])


@pytest.mark.asyncio
async def test_kafka_trigger_skips_split_stub_without_test_mode(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    result = await handlers.handle_order_trigger("PO13326295207344")
    assert result == {"status": "skipped", "reason": "split_stub_requires_test_mode"}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_skips_fixtures_without_test_mode(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_use_fixtures", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-fix",
        }
    )
    assert result == {"status": "ignored", "reason": "fixtures_require_test_mode"}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_b_session_ignores_leftover_option_a_confirm(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_B_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value="fp-a"))
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-cross",
            "context_id": "wamid.option-a-card",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_B_PREVIEW
    assert sent == ["option_b"]


def _option_b_inbound_session() -> dict:
    return {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_B_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }


@pytest.mark.asyncio
async def test_option_b_session_ignores_option_a_confirm_without_snapshot(monkeypatch):
    bundle = _sample_bundle()
    session = _option_b_inbound_session()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handlers.session_store,
        "callback_data_for_outbound_message",
        AsyncMock(return_value="jit_hold:PO13326295207344:option_a"),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-cross-cb",
            "context_id": "wamid.option-a-card",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_B_PREVIEW
    assert sent == ["option_b"]


@pytest.mark.asyncio
async def test_option_b_session_fail_closed_when_context_untrusted(monkeypatch):
    bundle = _sample_bundle()
    session = _option_b_inbound_session()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.session_store, "callback_data_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-cross-unknown",
            "context_id": "wamid.unknown",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == STATE_OPTION_B_PREVIEW
    assert sent == ["option_b"]


@pytest.mark.asyncio
async def test_option_b_confirm_from_option_b_card_completes_hold(monkeypatch):
    bundle = _sample_bundle()
    session = _option_b_inbound_session()
    sent: list[str] = []

    async def fake_send(phone, template_key, context, **kw):
        sent.append(template_key)
        return {}

    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "snapshot_for_outbound_message", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handlers.session_store,
        "callback_data_for_outbound_message",
        AsyncMock(return_value="jit_hold:PO13326295207344:option_b"),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", fake_send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-b",
            "context_id": "wamid.option-b-card",
        }
    )
    assert result["status"] == "ok"
    assert result["state"] == "done_hold"
    assert sent == ["option_b_done"]


@pytest.mark.asyncio
async def test_meta_200_cancelled_during_bind_still_confirms(monkeypatch):
    import asyncio

    from app.agents.whatsapp_jit_hold import meta_client
    from app.config.settings import settings

    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", False)
    monkeypatch.setattr(settings, "whatsapp_meta_access_token", "test-token")
    monkeypatch.setattr(settings, "whatsapp_meta_phone_number_id", "1353517691170148")

    class _Resp:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.ok"}]}'

        def json(self):
            return {"messages": [{"id": "wamid.ok"}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    confirm = AsyncMock()
    monkeypatch.setattr(meta_client, "get_meta_http_client", lambda: _Client())
    monkeypatch.setattr(
        meta_client.session_store,
        "bind_outbound_message",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    monkeypatch.setattr(meta_client.session_store, "confirm_order_sent", confirm)

    with pytest.raises(asyncio.CancelledError):
        await meta_client.send_template(
            "9818886159",
            "initial",
            {"order_id": "PO123", "customer_name": "A", "held_items": "item"},
            callback_data="jit_hold:PO123:initial",
            treat_uncertain_as_sent=True,
        )
    confirm.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_split_known_meta_without_child_does_not_block(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    block = AsyncMock()
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(1_700_000_000, frozenset())))
    monkeypatch.setattr(handlers.session_store, "try_acquire_split", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers.session_store, "block_split_retry", block)
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-meta-busy",
        }
    )
    assert result["status"] == "busy"
    block.assert_not_awaited()
    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracking_url_404_falls_back_to_default(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client
    from app.agents.whatsapp_jit_hold.order_client import OrderNotFoundError

    monkeypatch.setattr(
        order_client,
        "fetch_order_details",
        AsyncMock(side_effect=OrderNotFoundError("PO404")),
    )
    url = await order_client.tracking_url_with_fallback(None, "PO404")
    assert url == default_tracking_url("PO404")


@pytest.mark.asyncio
async def test_new_child_ids_unscoped_without_cutoff_are_empty(monkeypatch):
    from app.agents.whatsapp_jit_hold import order_client

    monkeypatch.setattr(
        order_client,
        "search_orders_by_parent",
        AsyncMock(
            return_value=[
                {"order_id": "PO_PARENT"},
                {"order_id": "PO_CHILD", "parent_id": "PO_PARENT", "created": 1000},
            ]
        ),
    )
    ids = await order_client.new_child_ids_after_split(
        "PO_PARENT",
        known_child_ids=frozenset(),
        split_after_ts=None,
    )
    assert ids == frozenset()


@pytest.mark.asyncio
async def test_inbound_order_not_found_is_ignored(monkeypatch):
    from app.agents.whatsapp_jit_hold.order_client import OrderNotFoundError

    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(side_effect=OrderNotFoundError("PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())
    send = AsyncMock()
    monkeypatch.setattr(handlers.meta_client, "send_template", send)

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-404",
        }
    )
    assert result == {"status": "ignored", "reason": "order_not_found", "order_id": "PO13326295207344"}
    send.assert_not_awaited()
    handlers.session_store.mark_webhook_processed.assert_awaited_once_with("wamid-404")


@pytest.mark.asyncio
async def test_get_split_context_corrupt_is_not_missing(monkeypatch):
    from app.agents.whatsapp_jit_hold import session_store

    redis = _MemRedis()
    monkeypatch.setattr(session_store, "get_redis", lambda: redis)
    await redis.set("whatsapp_jit_hold:split_meta:PO1", "not-json")
    ts, known = await session_store.get_split_context("PO1")
    assert ts is None
    assert known == frozenset()
    assert known is not None


@pytest.mark.asyncio
async def test_get_split_context_bad_ts_is_unreadable(monkeypatch):
    from app.agents.whatsapp_jit_hold import session_store

    redis = _MemRedis()
    monkeypatch.setattr(session_store, "get_redis", lambda: redis)
    await redis.set(
        "whatsapp_jit_hold:split_meta:PO1",
        '{"split_after_ts": [], "known_child_ids": ["PO2"]}',
    )
    ts, known = await session_store.get_split_context("PO1")
    assert ts is None
    assert known == frozenset()
    assert known is not None


@pytest.mark.asyncio
async def test_confirm_split_empty_snapshot_without_split_ts_does_not_fake_done(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    confirm = AsyncMock()
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(None, frozenset())))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(return_value=frozenset({"PO_CHILD"})))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-empty-snap",
        }
    )
    assert result["status"] == "busy"
    confirm.assert_not_awaited()
    split.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_split_empty_snapshot_with_split_ts_completes(monkeypatch):
    bundle = _sample_bundle()
    session = {
        "order_id": "PO13326295207344",
        "state": STATE_OPTION_A_PREVIEW,
        "customer_phone": "9818886159",
        **_overlay_session_skus(),
    }
    confirm = AsyncMock()
    split = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(handlers.session_store, "is_webhook_processed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "mark_webhook_processed", AsyncMock())
    monkeypatch.setattr(handlers.session_store, "resolve_session", AsyncMock(return_value=(dict(session), "PO13326295207344")))
    monkeypatch.setattr(handlers.session_store, "get_session", AsyncMock(return_value=dict(session)))
    monkeypatch.setattr(handlers.session_store, "save_session", AsyncMock())
    monkeypatch.setattr(handlers, "fetch_eligibility_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(handlers.session_store, "is_split_completed", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "is_split_blocked", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.session_store, "get_split_context", AsyncMock(return_value=(1_700_000_000, frozenset())))
    monkeypatch.setattr(handlers.session_store, "confirm_split_completed", confirm)
    monkeypatch.setattr(handlers, "new_child_ids_after_split", AsyncMock(return_value=frozenset({"PO_CHILD"})))
    monkeypatch.setattr(handlers, "split_jit_order", split)
    monkeypatch.setattr(
        handlers,
        "resolve_split_tracking_links",
        AsyncMock(return_value=("https://ship", "https://held", [])),
    )
    monkeypatch.setattr(handlers.meta_client, "send_template", send)
    monkeypatch.setattr(handlers.session_store, "session_lock", lambda *a, **k: _Lock())

    result = await handlers.handle_inbound(
        {
            "phone": "9818886159",
            "order_id": "PO13326295207344",
            "action": ACTION_CONFIRM,
            "message_id": "wamid-empty-snap-recover",
        }
    )
    assert result["status"] == "ok"
    confirm.assert_awaited()
    split.assert_not_awaited()
    send.assert_awaited()


def test_default_overlay_does_not_overwrite_marketplace_store_type():
    from app.agents.whatsapp_jit_hold.fixtures import _default_jit_overlay

    bundle = {
        "order_id": "PO_MP",
        "order": {
            "order_id": "PO_MP",
            "order_lines": [{"normalized_quantity": 9}],
            "shipment_detail": {"vendor": {"tags": {"store_type": "MARKETPLACE"}}},
        },
        "allocation": {"data": {}},
        "status": {"data": {}},
    }
    out = _default_jit_overlay(bundle)
    assert out["order"]["shipment_detail"]["vendor"]["tags"]["store_type"] == "MARKETPLACE"


@pytest.mark.asyncio
async def test_workers_refuse_split_stub_even_with_test_mode(monkeypatch):
    from app.agents.whatsapp_jit_hold import workers
    from app.config.settings import settings

    class _R:
        async def ping(self):
            return True

    monkeypatch.setattr(settings, "whatsapp_jit_hold_split_stub", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_mode", True)
    monkeypatch.setattr(settings, "whatsapp_jit_hold_test_phone", "9999999999")
    monkeypatch.setattr(workers, "get_redis", lambda: _R())
    workers._tasks = []
    await workers.start_workers()
    assert workers._tasks == []

