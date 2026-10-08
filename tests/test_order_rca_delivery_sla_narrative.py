"""Delivery SLA — compute_delivery_sla canonical breach fields."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.order_rca import eta_resolution, rules, sources
from app.agents.order_rca.constants import (
    DELIVERY_BREACH_DELIVERED_LATE,
    DELIVERY_BREACH_OPEN_PAST,
    DELIVERY_BREACH_PENDING_WITHIN,
    ORDER_RCA_DISPLAY_TZ_NAME,
)

TZ = ZoneInfo(ORDER_RCA_DISPLAY_TZ_NAME)


def test_within_sla_undelivered_not_breached_even_if_api_flag():
    anchor = datetime(2026, 5, 22, 23, 59, tzinfo=TZ)
    as_of = datetime(2026, 5, 22, 22, 0, tzinfo=TZ)
    r = eta_resolution.compute_delivery_sla(
        promised_first={"instant": anchor.isoformat(), "display": "22 May, 2026 23:59 IST"},
        is_eta_breached_order_api=True,
        as_of=as_of,
    )
    assert r["breach_kind"] == DELIVERY_BREACH_PENDING_WITHIN
    assert r["is_eta_breached"] is False
    assert r["breach_minutes"] is None


def test_past_sla_undelivered_may_23_vs_may_22_promise():
    anchor = datetime(2026, 5, 22, 23, 59, tzinfo=TZ)
    as_of = datetime(2026, 5, 23, 10, 0, tzinfo=TZ)
    r = eta_resolution.compute_delivery_sla(
        promised_first={"instant": anchor.isoformat(), "display": "22 May, 2026 23:59 IST"},
        is_eta_breached_order_api=False,
        as_of=as_of,
    )
    assert r["breach_kind"] == DELIVERY_BREACH_OPEN_PAST
    assert r["is_eta_breached"] is True
    assert r["breach_minutes"] is not None
    assert r["breach_minutes"] > 600


def test_future_anchor_undelivered_api_flag_does_not_force_breach():
    future = datetime.now(TZ) + timedelta(hours=8)
    r = eta_resolution.compute_delivery_sla(
        promised_first={"instant": future.isoformat()},
        is_eta_breached_order_api=True,
        as_of=datetime.now(TZ),
    )
    assert r["breach_kind"] == DELIVERY_BREACH_PENDING_WITHIN
    assert r["is_eta_breached"] is False


def test_delivered_late():
    r = eta_resolution.compute_delivery_sla(
        promised_first={"instant": "2026-04-27T16:30:00+00:00"},
        actual_delivery="28 Apr, 2026 09:24 IST",
        late_minutes=684,
    )
    assert r["breach_kind"] == DELIVERY_BREACH_DELIVERED_LATE
    assert r["is_eta_breached"] is True
    assert r["breach_minutes"] == 684


@pytest.mark.asyncio
async def test_extract_order_delivery_includes_sla_fields():
    bundle = await sources._fetch_fixtures("PO13426186269279")
    d = rules.extract_order_delivery(bundle)
    assert d.get("breach_kind") in (
        DELIVERY_BREACH_PENDING_WITHIN,
        DELIVERY_BREACH_OPEN_PAST,
        "unknown_anchor",
    )
    assert "is_eta_breached" in d
    assert "breach_minutes" in d
    assert "is_eta_breached_order_api" in d
    assert d["is_eta_breached"] == (d["breach_kind"] in (DELIVERY_BREACH_OPEN_PAST, DELIVERY_BREACH_DELIVERED_LATE))


@pytest.mark.asyncio
async def test_preflight_passes_sla_fields_to_llm():
    from app.agents.order_rca.llm_context import build_llm_context

    facts = rules.build_facts(await sources._fetch_fixtures("PO13426186269279"))
    pf = facts["preflight"]
    de = (build_llm_context(facts).get("order_summary") or {}).get("delivery_eta") or {}
    assert de.get("breach_kind") == pf.get("breach_kind")
    assert de.get("is_eta_breached") == pf.get("is_eta_breached")
    assert de.get("breach_minutes_display") == pf.get("breach_minutes_display")
    assert "breach_minutes" not in de
    assert "may_say_late_or_breached" not in de
