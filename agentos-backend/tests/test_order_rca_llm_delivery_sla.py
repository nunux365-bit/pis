"""LLM delivery_eta uses canonical breach_kind / is_eta_breached."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.order_rca.llm_context import _delivery_eta_for_llm, build_llm_context
from app.agents.order_rca import rules, sources
from app.agents.order_rca.constants import ORDER_RCA_DISPLAY_TZ_NAME

CHILD = "PO13326295207344"
RETURN_REFUND = "PO11526254696421"


def test_delivery_eta_within_sla_ignores_api_flag():
    tz = ZoneInfo(ORDER_RCA_DISPLAY_TZ_NAME)
    future = (datetime.now(tz) + timedelta(hours=6)).isoformat()
    pf = {
        "promised_first": {"display": "22 May, 2026 23:59 IST", "instant": future},
        "actual_delivery": None,
        "is_eta_breached": False,
        "is_eta_breached_order_api": True,
        "breach_kind": "pending_within_sla",
        "breach_minutes": None,
    }
    de = _delivery_eta_for_llm(pf)
    assert de["breach_kind"] == "pending_within_sla"
    assert de["is_eta_breached"] is False
    assert de["is_eta_breached_order_api"] is True
    assert "breach_minutes" not in de


def test_delivery_eta_delivered_late():
    pf = {
        "promised_first": {"display": "27 Apr, 2026 22:00 IST", "instant": "2026-04-27T16:30:00+00:00"},
        "actual_delivery": "28 Apr, 2026 09:24 IST",
        "late_minutes": 684,
        "breach_kind": "delivered_late",
        "is_eta_breached": True,
        "breach_minutes": 684,
        "breach_minutes_display": "11 h 24 min",
    }
    de = _delivery_eta_for_llm(pf)
    assert de["breach_kind"] == "delivered_late"
    assert de["is_eta_breached"] is True
    assert de["breach_minutes_display"] == "11 h 24 min"
    assert "breach_minutes" not in de


@pytest.mark.asyncio
async def test_llm_context_allocated_store_kind_matches_vendor_type():
    facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
    ctx = build_llm_context(facts)
    assert ctx["allocation_summary"]["allocated_store_kind"] == "warehouse"
    assert ctx["allocation_summary"]["allocated_store_kind_label"] == "Warehouse"


@pytest.mark.asyncio
async def test_llm_context_return_refund_delivery_late():
    facts = rules.build_facts(await sources._fetch_fixtures(RETURN_REFUND))
    de = (build_llm_context(facts).get("order_summary") or {}).get("delivery_eta") or {}
    assert de.get("is_eta_breached") is True
    assert de.get("breach_kind") == "delivered_late"
    assert de.get("breach_minutes_display")
    assert "breach_minutes" not in de
