"""build_llm_context — lean payload."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.agents.order_rca.llm_context import (
    build_hypothesis_seeds,
    build_llm_context,
    humanize_signal_for_narrative,
)
from app.agents.order_rca import rules, sources

CHILD = "PO13326295207344"
CLICKPOST_SFX = "PO15826548510041"
_SKU_ID_IN_SIGNAL = re.compile(r"^SKU\s+\d", re.IGNORECASE)


@pytest.mark.asyncio
async def test_llm_context_omits_order_id():
    facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
    ctx = build_llm_context(facts)
    assert "order_id" not in ctx
    assert "parent_id" not in ctx
    assert ctx["is_split_order"] is True
    assert "parent order vs child order" in ctx["order_scope_note"].lower()
    assert ctx["allocation_summary"]["badge"] in ("IDEAL", "CROSS")
    assert "perfect_order_summary" in ctx
    assert ctx["perfect_order_summary"]["overall"] in ("perfect", "imperfect")
    failed = ctx["perfect_order_summary"].get("failed_pillars") or []
    if ctx["perfect_order_summary"]["overall"] == "imperfect":
        assert failed, "imperfect orders must expose failed_pillars to the LLM"
    assert any(s["id"] == "perfect_order" for s in ctx["hypothesis_seeds"])
    assert "groot_events" in ctx["operations_summary"]
    assert len(ctx["operations_summary"]["groot_events"]) == len(facts["operations"]["groot_events"])


@pytest.mark.asyncio
async def test_llm_context_enriched_fields_for_cross():
    facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
    ctx = build_llm_context(facts)
    osum = ctx["order_summary"]
    alloc = ctx["allocation_summary"]

    assert "pincode" in osum
    assert "pincode_masked" not in osum
    assert osum.get("pincode")
    assert osum.get("placed_at") is not None
    de = osum.get("delivery_eta") or {}
    assert de.get("late_minutes_vs") == "promised_first"
    assert de.get("promised_first")
    assert "first customer" in (de.get("sla_anchor") or "").lower()
    assert "breach_kind" in de
    assert "is_eta_breached" in de
    assert "breach_minutes_display" in de
    assert "breach_minutes" not in de
    assert osum.get("promised_delivery_raw") is not None
    assert alloc.get("allocated_store_kind") in ("retail", "warehouse")
    assert alloc.get("allocated_store_kind_label") in ("Retail store", "Warehouse")
    assert ctx["nearby_context"]["allocated"].get("store_kind_label") == alloc.get(
        "allocated_store_kind_label"
    )

    skus = osum["skus"]
    assert skus
    assert "name" in skus[0]
    assert skus[0]["name"]
    assert "sku_id" not in skus[0]

    assert alloc.get("allocation_badge_reason")
    assert alloc.get("allocation_tier_label")
    assert "standard_available" in alloc or "rapid_services" in alloc

    virtual_store = ctx["nearby_context"]["nearest_rejected_stores"][0]["virtual_stores"][0]
    assert len(virtual_store.get("detail_signals") or []) <= 6
    assert "store_terminology" in ctx["nearby_context"]

    for ev in ctx["operations_summary"]["groot_events"]:
        assert "performed_by" not in ev

    assert not any(str(s).startswith("Groot: performers=") for s in ctx["signals"])

    seeds = ctx["hypothesis_seeds"]
    assert any(s["id"] == "allocation_reason" for s in seeds)
    assert any(s["id"] == "ops_Packaging" for s in seeds)
    assert any(s["id"] == "allocation" for s in seeds)


def test_synthesis_schema_caps_hypotheses_at_ten():
    from app.agents.order_rca.synthesize import HYPOTHESIS_DISPLAY_LIMIT, _SCHEMA

    assert _SCHEMA["properties"]["hypotheses"]["maxItems"] == HYPOTHESIS_DISPLAY_LIMIT


def test_build_hypothesis_seeds_no_cap_includes_all_late_ops_and_gaps():
    """Removing the old [:10] cap must not drop extra late phases or planning gaps."""
    facts = {
        "preflight": {"allocation_badge": "CROSS", "actual_vendor_code": "1MG_KOL_05"},
        "operations": {
            "status_transitions": [
                {"label": "Packaging", "sla_status": "late", "duration_min": 1288, "duration_display": "21 h 28 min", "default_sla": "20"},
                {"label": "Dispatch", "sla_status": "late", "duration_min": 300, "duration_display": "5 h", "default_sla": "100"},
                {"label": "Last mile", "sla_status": "late", "duration_min": 4000, "duration_display": "2 d 10 h 40 min", "default_sla": "3500"},
            ],
            "groot_empty": True,
        },
        "p1": {"status": "pending_api", "msn_adherence": {"status": "pending_api", "stores": []}},
        "p4": {"rows": []},
        "skus": [],
    }
    seeds = build_hypothesis_seeds(facts)
    ids = [s["id"] for s in seeds]
    assert ids.count("ops_Packaging") == 1
    assert ids.count("ops_Dispatch") == 1
    assert ids.count("ops_Last mile") == 1
    assert "msn_gap" in ids
    assert len(seeds) >= 5
    pack = next(s for s in seeds if s["id"] == "ops_Packaging")
    assert "21 h 28 min" in pack["finding_hint"]
    assert "1288" not in pack["finding_hint"]


@pytest.mark.asyncio
async def test_llm_context_detail_signals_use_product_names_not_sku_ids():
    facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
    ctx = build_llm_context(facts)
    names = {rules._s(s.get("name")) for s in facts.get("skus") or [] if isinstance(s, dict)}
    names.discard("")

    found_inventory_signal = False
    for group in ctx["nearby_context"].get("nearest_rejected_stores") or []:
        for vs in group.get("virtual_stores") or []:
            for sig in vs.get("detail_signals") or []:
                assert not _SKU_ID_IN_SIGNAL.match(str(sig)), sig
                if ": " in str(sig) and any(
                    x in str(sig).lower()
                    for x in ("unmapped", "delived", "out of stock", "partial", "stock not")
                ):
                    found_inventory_signal = True
                    prefix = str(sig).split(": ", 1)[0]
                    assert prefix in names or prefix == "Ordered product"

    assert found_inventory_signal


def test_humanize_signal_for_narrative():
    names = {"1122085": "Montair-LC Tablet"}
    assert (
        humanize_signal_for_narrative("SKU 1122085: Unmapped on vendor", names)
        == "Montair-LC Tablet: Unmapped on vendor"
    )
    assert humanize_signal_for_narrative("Address not serviceable", names) == "Address not serviceable"
    assert (
        humanize_signal_for_narrative("SKU 999: Out of stock", {})
        == "Ordered product: Out of stock"
    )


@pytest.mark.asyncio
async def test_llm_context_excludes_customer_pii():
    order = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "order_rca" / "order_child.json").read_text()
    )
    facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
    ctx = build_llm_context(facts)
    blob = json.dumps(ctx, default=str)
    assert "gaurava@gmail.com" not in blob
    assert "9818886159" not in blob
    assert "Gaurav" not in blob
    assert "Golf Course Road" not in blob


@pytest.mark.asyncio
async def test_llm_context_service_changed_includes_eta_jumps_chain():
    facts = rules.build_facts(await sources._fetch_fixtures("PO16326622291558"))
    de = (build_llm_context(facts)["order_summary"].get("delivery_eta") or {})
    jumps = de.get("eta_jumps") or []
    labels = [j.get("label") for j in jumps]
    assert de.get("promised_first") == "13 Jun, 2026 23:47 IST"
    assert labels == ["First promised", "ETA 1", "ETA 2", "Actual"]
    assert "Current" not in labels
    assert de.get("breach_kind") == "delivered_late"
    assert de.get("breach_minutes_display") == "2 d 10 h 26 min"
    assert "breach_minutes" not in de


@pytest.mark.asyncio
async def test_llm_context_clickpost_mode_excludes_groot():
    facts = rules.build_facts(await sources._fetch_fixtures(CLICKPOST_SFX))
    ctx = build_llm_context(facts)
    ops_sum = ctx["operations_summary"]
    assert ops_sum["last_mile_mode"] == "clickpost"
    assert ops_sum["fulfillment_path"] == "clickpost"
    assert len(ops_sum["clickpost_events"]) == 5
    assert ops_sum["groot_events"] == []
    assert ops_sum.get("shipping_summary", {}).get("waybill") == "SF3480902644MG"
    seed_ids = [s["id"] for s in ctx["hypothesis_seeds"]]
    assert "clickpost_timeline" in seed_ids
    assert "clickpost_span" in seed_ids
    assert "groot_timeline" not in seed_ids


@pytest.mark.asyncio
async def test_llm_context_excludes_sampling_skus():
    facts = rules.build_facts(await sources._fetch_fixtures("PO16326573780658"))
    ctx = build_llm_context(facts)
    llm_names = {rules._s(s.get("name")) for s in ctx["order_summary"]["skus"]}
    assert "Aptivate Tasty Pineapple" not in llm_names
    display_names = {rules._s(s.get("name")) for s in facts["skus"] if s.get("is_sampling")}
    assert "Aptivate Tasty Pineapple" in display_names
    order_sku_seeds = [s for s in ctx["hypothesis_seeds"] if s.get("id") == "order_skus"]
    if order_sku_seeds:
        assert "Aptivate" not in order_sku_seeds[0]["finding_hint"]
