"""Order RCA rules — fixture-backed unit tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents.order_rca import rules, sources
from app.agents.order_rca.constants import STATUS_LABELS
from app.config.settings import settings

FIX = Path(__file__).resolve().parent / "fixtures" / "order_rca"
CHILD = "PO13326295207344"
PARENT = "PO13326295017145"
RETURN_REFUND = "PO11526254696421"
SPLIT_MOUNJARO = "PO13426186269279"
SPLIT_MOUNJARO_PARENT = "PO13026639774401"
SERVICE_CHANGED = "PO16326622291558"
RAPID_DELIVERED_LATE = "PO16326573780658"
CLICKPOST_SFX = "PO15826548510041"
ONE_HOUR_GROOT = "PO16126626766912"
INVENTORY_MIXED = "PO17026573780658"


@pytest.mark.asyncio
async def test_fixture_rapid_delivered_late_loads():
  b = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  assert b["order_id"] == RAPID_DELIVERED_LATE
  assert b["collect_mode"] == "full"
  assert b["history"]["history"]


@pytest.mark.asyncio
async def test_rapid_delivered_late_promised_first_from_order_sla():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  delivery = rules.extract_order_delivery(bundle)
  assert delivery["promised_delivery"] == "14 Jun, 2026 22:00 IST"
  assert delivery["promised_delivery_source"] == "analytics.order_sla"
  assert delivery["breach_kind"] == "delivered_late"
  assert delivery["breach_minutes"] == 2307
  assert delivery["breach_minutes_display"] == "1 d 14 h 27 min"
  assert delivery["late_minutes_vs"] == "promised_first"


@pytest.mark.asyncio
async def test_rapid_delivered_late_eta_jumps():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  delivery = rules.extract_order_delivery(bundle)
  jumps = delivery.get("eta_jumps") or []
  labels = [j.get("label") for j in jumps]
  assert labels == ["First promised", "ETA 1", "Actual"]
  assert jumps[0]["display"] == "14 Jun, 2026 22:00 IST"
  assert jumps[1]["display"] == "18 Jun, 2026 22:00 IST"
  assert jumps[2]["display"] == "16 Jun, 2026 12:27 IST"
  assert "Current" not in labels


@pytest.mark.asyncio
async def test_rapid_delivered_late_preflight_includes_eta_jumps():
  facts = rules.build_facts(await sources._fetch_fixtures(RAPID_DELIVERED_LATE))
  pf = facts["preflight"]
  assert pf["promised_delivery"] == "14 Jun, 2026 22:00 IST"
  assert isinstance(pf.get("eta_jumps"), list)
  assert len(pf["eta_jumps"]) == 3


@pytest.mark.asyncio
async def test_rapid_delivered_late_pack_normalized_qty():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  qty_map = rules.order_qty_by_sku(bundle["order"])
  assert qty_map["17669"] == 2.0  # Admenta: 20 tablets / 10 per pack
  assert qty_map["40780"] == 2.0  # Ecosprin 150: 28 / 14
  skus = rules._order_skus(bundle["order"])
  by_id = {s["sku_id"]: s["quantity"] for s in skus}
  assert by_id["17669"] == 2.0


@pytest.mark.asyncio
async def test_rapid_delivered_late_not_split_order():
  from app.agents.order_rca.llm_context import build_llm_context

  facts = rules.build_facts(await sources._fetch_fixtures(RAPID_DELIVERED_LATE))
  assert facts.get("parent_id") is None
  ctx = build_llm_context(facts)
  assert ctx["is_split_order"] is False
  assert "never say parent order" in ctx["order_scope_note"].lower()
  failed = ctx["perfect_order_summary"]["failed_pillars"]
  assert len(failed) == 1
  assert failed[0]["id"] == "delivery"
  pack = next(
    t for t in facts["operations"]["status_transitions"] if t.get("label") == "Packaging"
  )
  assert pack["duration_display"] == "2 d 29 min"
  seed = next(s for s in ctx["hypothesis_seeds"] if s["id"] == "ops_Packaging")
  assert "2 d 29 min" in seed["finding_hint"]
  assert "2909" not in seed["finding_hint"]


def test_sanitize_strips_parent_order_on_non_split():
  from app.agents.order_rca.synthesize import _sanitize_synthesis_narrative

  ctx = {
    "is_split_order": False,
    "perfect_order_summary": {
      "failed_pillars": [{"id": "delivery", "detail": "1 d 14 h 27 min late vs first promise"}],
    },
    "order_summary": {
      "delivery_eta": {
        "breach_minutes_display": "1 d 14 h 27 min",
        "breach_kind": "delivered_late",
      }
    },
  }
  syn = {
    "verdict": "Preferred warehouse allocation succeeded, but packaging delay caused a late delivery breach.",
    "verdict_subline": "The parent order was allocated to preferred warehouse 1MG_NRL_01.",
    "primary_cause": "Packaging took 2909 minutes against a 20 minute SLA.",
    "contributing_factors": [],
    "recommended_action": "Review parent order ops.",
    "hypotheses": [{"finding": "Parent order late", "hypothesis": "x", "alignment": "supported"}],
  }
  out = _sanitize_synthesis_narrative(ctx, syn)
  assert "parent order" not in out["verdict"].lower()
  assert "allocation succeeded" not in out["verdict"].lower()
  assert "2909" not in out["primary_cause"]
  assert "2 d 29 min" in out["primary_cause"]
  assert "Delivered 1 d 14 h 27 min late" in out["verdict"]


def test_rank_failed_pillars_delivery_before_allocation():
  pillars = [
    {"id": "allocation", "pass": False, "status": "fail"},
    {"id": "delivery", "pass": False, "status": "fail"},
  ]
  ranked = rules.rank_failed_pillars(pillars)
  assert [p["id"] for p in ranked] == ["delivery", "allocation"]


def test_failed_pillars_includes_pushback_unknown():
  pillars = [
    {"id": "allocation", "pass": True, "status": "pass"},
    {"id": "pushback", "pass": None, "status": "unknown", "label": "Unknown", "detail": "Status history unavailable"},
  ]
  failed = rules.rank_failed_pillars(pillars)
  assert len(failed) == 1
  assert failed[0]["id"] == "pushback"


def test_imperfect_verdict_open_past_promise_not_delivered_late():
  from app.agents.order_rca.synthesize import _imperfect_verdict

  failed = [{"id": "delivery", "detail": "27 d past first promise; not delivered"}]
  eta = {"breach_kind": "open_past_promise", "breach_minutes_display": "27 d 13 h 35 min"}
  verdict = _imperfect_verdict(failed, eta)
  assert verdict == "27 d 13 h 35 min past first promise; not delivered"
  assert "Delivered" not in verdict


@pytest.mark.asyncio
async def test_mock_cross_child_headlines_delivery_not_allocation(monkeypatch):
  from app.agents.order_rca import synthesize

  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  assert facts["preflight"]["allocation_badge"] == "CROSS"
  monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", True)
  syn, source = await synthesize.synthesize_rca(facts)
  assert source == "mock"
  verdict = syn["verdict"].lower()
  assert "non-ideal" not in verdict
  assert "allocated" not in verdict or "late" in verdict or "breach" in verdict or "delivered" in verdict


@pytest.mark.asyncio
async def test_llm_failed_pillars_includes_pushback_unknown():
  from app.agents.order_rca.llm_context import build_llm_context

  facts = {
    "preflight": {"allocation_badge": "IDEAL"},
    "perfect_order": {
      "overall": "imperfect",
      "overall_pass": False,
      "pillars": [
        {"id": "allocation", "pass": True, "status": "pass", "label": "Ideal", "detail": "OK"},
        {"id": "pushback", "pass": None, "status": "unknown", "label": "Unknown", "detail": "No history"},
        {"id": "delivery", "pass": True, "status": "pass", "label": "On time", "detail": "OK"},
        {"id": "customer_contact", "pass": None, "status": "unknown", "counts_as_perfect": True},
        {"id": "price_integrity", "pass": True, "status": "pass", "label": "OK", "detail": "OK"},
      ],
    },
    "operations": {"status_transitions": [], "groot_empty": True},
    "p1": {"status": "not_configured"},
    "p4": {"rows": []},
    "skus": [],
  }
  ctx = build_llm_context(facts)
  failed = ctx["perfect_order_summary"]["failed_pillars"]
  assert len(failed) == 1
  assert failed[0]["id"] == "pushback"


@pytest.mark.asyncio
async def test_rapid_delivered_late_mrp_increase_under_limit():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  inc, source, _ = rules.compute_order_mrp_increase(bundle["order"])
  assert source == "order_lines"
  assert round(inc, 2) == 10.48
  po = rules.build_facts(bundle)["perfect_order"]
  assert po["total_mrp_increase"] == 10.48
  price = next(p for p in po["pillars"] if p["id"] == "price_integrity")
  assert price["status"] == "pass"


@pytest.mark.asyncio
async def test_build_facts_includes_sku_price_increases():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  facts = rules.build_facts(bundle)
  block = facts["sku_price_increases"]
  assert block["source"] == "order_lines"
  assert block["total_increase"] == 10.48
  assert block["count"] >= 1
  assert all(row.get("sku_id") for row in block["lines"])
  assert abs(sum(r["line_increase"] for r in block["lines"]) - 10.48) < 0.02
  dec = facts["sku_price_decreases"]
  assert "total_decrease" in dec
  assert isinstance(dec["lines"], list)
  # Fixture has lines that decreased MRP as well.
  assert dec["count"] >= 1
  assert all("line_decrease" in r for r in dec["lines"])


def test_build_sku_price_increases_fail_soft_on_bad_input():
  out = rules.build_sku_price_increases(None)  # type: ignore[arg-type]
  assert out["count"] == 0
  assert out["lines"] == []
  assert out["source"] == "none"


def test_build_sku_price_decreases_from_lines():
  order = {
    "order_lines": [
      {
        "quantity": 2,
        "unit_cart_item_mrp": 100.0,
        "unit_current_mrp": 80.0,
        "sku": {"sku_id": "s1", "name": "Down"},
      },
      {
        "quantity": 1,
        "unit_cart_item_mrp": 10.0,
        "unit_current_mrp": 12.0,
        "sku": {"sku_id": "s2", "name": "Up"},
      },
    ]
  }
  dec = rules.build_sku_price_decreases(order)
  assert dec["count"] == 1
  assert dec["total_decrease"] == 40.0
  assert dec["lines"][0]["sku_id"] == "s1"
  assert dec["lines"][0]["line_decrease"] == 40.0
  inc = rules.build_sku_price_increases(order)
  assert inc["count"] == 1
  assert inc["lines"][0]["sku_id"] == "s2"


def test_sku_price_decrease_skips_payment_fallback_when_line_fields_present():
  """Line MRP present and all up → do not invent payment_summary decrease."""
  order = {
    "order_lines": [
      {
        "quantity": 1,
        "unit_cart_item_mrp": 100.0,
        "unit_current_mrp": 120.0,
        "sku": {"sku_id": "1", "name": "A"},
      },
    ],
    "payment_summary": {"cart_mrp": 200.0, "current_mrp": 150.0},
  }
  dec = rules.build_sku_price_decreases(order)
  assert dec["count"] == 0
  assert dec["source"] == "none"
  assert dec["total_decrease"] == 0.0
  inc = rules.build_sku_price_increases(order)
  assert inc["source"] == "order_lines"
  assert inc["total_increase"] == 20.0


@pytest.mark.asyncio
async def test_fixture_service_changed_rapid_loads():
  b = await sources._fetch_fixtures(SERVICE_CHANGED)
  assert b["order_id"] == SERVICE_CHANGED
  assert b["collect_mode"] == "full"
  assert b["history"]["history"]


@pytest.mark.asyncio
async def test_service_changed_promised_first_is_rapid_comms_not_standard():
  bundle = await sources._fetch_fixtures(SERVICE_CHANGED)
  delivery = rules.extract_order_delivery(bundle)
  assert delivery["promised_delivery"] == "13 Jun, 2026 23:47 IST"
  assert "history.eta_communicated" in (delivery["promised_delivery_source"] or "")
  assert delivery["breach_kind"] == "delivered_late"
  assert delivery["breach_minutes"] == 3506
  assert delivery["breach_minutes_display"] == "2 d 10 h 26 min"
  assert delivery["late_minutes_vs"] == "promised_first"


@pytest.mark.asyncio
async def test_service_changed_eta_jumps_use_numbered_labels_only():
  bundle = await sources._fetch_fixtures(SERVICE_CHANGED)
  delivery = rules.extract_order_delivery(bundle)
  jumps = delivery.get("eta_jumps") or []
  labels = [j.get("label") for j in jumps]
  assert labels == ["First promised", "ETA 1", "ETA 2", "Actual"]
  assert jumps[0]["display"] == "13 Jun, 2026 23:47 IST"
  assert jumps[1]["display"] == "15 Jun, 2026 23:59 IST"
  assert jumps[2]["display"] == "16 Jun, 2026 23:59 IST"
  assert jumps[3]["display"] == "16 Jun, 2026 10:13 IST"
  assert "Current" not in labels


@pytest.mark.asyncio
async def test_service_changed_preflight_includes_eta_jumps():
  facts = rules.build_facts(await sources._fetch_fixtures(SERVICE_CHANGED))
  pf = facts["preflight"]
  assert pf["promised_delivery"] == "13 Jun, 2026 23:47 IST"
  assert isinstance(pf.get("eta_jumps"), list)
  assert len(pf["eta_jumps"]) == 4


@pytest.mark.asyncio
async def test_fixture_bundle_loads():
  b = await sources._fetch_fixtures(CHILD)
  assert b["order_id"] == CHILD
  assert b["parent_id"] == PARENT
  assert "data" in b["allocation"]


@pytest.mark.asyncio
async def test_fixture_bundle_loads_parent_po():
  b = await sources._fetch_fixtures(PARENT)
  assert b["order_id"] == PARENT
  assert b["parent_id"] is None
  assert b["collect_mode"] == "full"


@pytest.mark.asyncio
async def test_synthesize_perfect_skips_openai(monkeypatch):
  from app.agents.order_rca import synthesize

  facts = {
    "perfect_order": {
      "overall": "perfect",
      "overall_pass": True,
      "pillars": [
        {"id": "allocation", "pass": True, "status": "pass", "label": "Ideal", "detail": "Preferred store"},
        {"id": "delivery", "pass": True, "status": "pass", "label": "On time", "detail": "On time vs first promise"},
      ],
    },
    "preflight": {"actual_vendor": "1MG_ROF_02 · Gurgaon, Haryana"},
    "signals": ["Allocation: IDEAL"],
    "p1": {"status": "pending_data"},
    "p2": {"status": "not_configured"},
  }
  monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", False)
  monkeypatch.setattr(synthesize.settings, "openai_api_key", "sk-test-should-not-call")
  syn, source = await synthesize.synthesize_rca(facts)
  assert source == "perfect_template"
  assert "Perfect order" in syn["verdict"]
  assert syn["hypotheses"] == []


@pytest.mark.asyncio
async def test_ideal_imperfect_uses_llm_path_not_perfect_template(monkeypatch):
  from app.agents.order_rca import synthesize

  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  facts = rules.build_facts(bundle)
  assert facts["perfect_order"]["overall"] == "imperfect"
  monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", True)
  syn, source = await synthesize.synthesize_rca(facts)
  assert source == "mock"
  assert "Delivered 1 d 14 h 27 min late" in syn["verdict"]
  assert "parent order" not in syn["verdict"].lower()


@pytest.mark.asyncio
async def test_ideal_imperfect_has_full_ops_and_store_matrix():
  bundle = await sources._fetch_fixtures(RAPID_DELIVERED_LATE)
  facts = rules.build_facts(bundle)
  assert facts["analysis_skipped"] is False
  assert facts["preflight"]["allocation_badge"] == "IDEAL"
  assert facts["perfect_order"]["overall"] == "imperfect"
  assert len(facts["operations"]["status_transitions"]) >= 1
  assert not facts["p3"].get("skipped")
  assert facts["p3"]["rows"]


@pytest.mark.asyncio
async def test_build_facts_child_order():
  b = await sources._fetch_fixtures(CHILD)
  facts = rules.build_facts(b)
  assert facts["order_id"] == CHILD
  assert facts["parent_id"] == PARENT
  msn = facts["p1"]["msn_adherence"]
  assert len(msn["stores"]) >= 8
  assert msn["stores"][0]["skus"]
  assert len(facts["operations"]["status_transitions"]) >= 3
  assert facts["operations"]["split_child"] is True
  assert facts["preflight"]["allocation_badge"] == "CROSS"
  assert facts["split_insights"] is not None
  assert len(facts["p3"]["nearby_stores"]) == 5
  assert facts["vendor_types_in_allocation"] == ["Retail", "Warehouse"]
  assert facts["operations"]["segments"]


def test_allocation_badge_ideal_retail():
  v_retail = {"vendor_code": "1MG_X_02", "vendor_type": "RETAIL", "distance": 1}
  assert rules.compute_allocation_badge(v_retail, [v_retail], [])[0] == "IDEAL"


def test_allocation_badge_ideal_warehouse_in_preferred_top3():
  retail = {"vendor_code": "1MG_A_02", "vendor_type": "RETAIL", "distance": 50}
  wh_near = {"vendor_code": "1MG_B_01", "vendor_type": "WAREHOUSE", "distance": 5}
  wh_far = {"vendor_code": "1MG_C_01", "vendor_type": "WAREHOUSE", "distance": 100}
  preferred = [wh_near, wh_far]
  assert rules.compute_allocation_badge(wh_near, [retail, wh_near, wh_far], preferred)[0] == "IDEAL"


def test_allocation_badge_cross_when_nearest_retail():
  retail = {"vendor_code": "1MG_A_02", "vendor_type": "RETAIL", "distance": 1}
  wh = {"vendor_code": "1MG_B_01", "vendor_type": "WAREHOUSE", "distance": 100}
  preferred = [wh]
  assert rules.compute_allocation_badge(wh, [retail, wh], preferred)[0] == "CROSS"


def test_preferred_vendors_grouped_by_physical_store():
  wh_a = {"vendor_code": "1MG_BRM_01", "vendor_type": "WAREHOUSE", "distance": 8}
  wh_b = {"vendor_code": "1MG_BRM_02", "vendor_type": "WAREHOUSE", "distance": 9}
  wh_c = {"vendor_code": "1MG_NOI_01", "vendor_type": "WAREHOUSE", "distance": 32}
  out = rules.build_preferred_physical_stores([wh_b, wh_a, wh_c])
  assert len(out) == 2
  assert out[0]["physical_store"] == "1MG_BRM"
  assert out[0]["virtual_store_count"] == 2
  assert out[0]["rank"] == 1
  assert out[1]["physical_store"] == "1MG_NOI"


def test_fulfilment_segment_labels_not_pick_pack():
  bundle = {
    "order_id": "PO_TEST",
    "order": {"order_id": "PO_TEST", "delivery_address": {}, "eta": {}, "order_lines": [], "shipment_detail": {}},
    "parent_id": None,
    "allocation": {
      "data": {
        "PO_TEST": {
          "allocated_vendor": "2",
          "selected_vendors": {"2": {"vendor_id": 2, "vendor_code": "1MG_FAR_01", "vendor_type": "WAREHOUSE", "distance": 500}},
          "rejected_vendors": {"1": {"vendor_id": 1, "vendor_code": "1MG_NEAR_02", "vendor_type": "RETAIL", "distance": 1}},
        }
      }
    },
    "status": {
      "data": {
        "PO_TEST": [
          {"status": "40", "created": "2026-05-17T09:24:16"},
          {"status": "30", "created": "2026-05-15T08:12:22"},
          {"status": "25", "created": "2026-05-15T05:40:45"},
          {"status": "130", "created": "2026-05-14T08:12:00"},
        ]
      }
    },
    "history": {},
    "groot": {"data": {}},
  }
  facts = rules.build_facts(bundle)
  labels = [s["label"] for s in facts["operations"]["segments"]]
  assert "Pick + pack" not in labels
  assert "Packaging → To be delivered" in labels


def test_physical_store_key():
  assert rules.physical_store_key("1MG_ROF_02") == "1MG_ROF"
  assert rules.physical_store_key("1MG_QTB_219") == "1MG_QTB"
  assert rules.physical_store_key("1MG_CHN_CHR_05") == "1MG_CHN_CHR"


@pytest.mark.asyncio
async def test_promised_delivery_from_order_api():
  bundle = await sources._fetch_fixtures(CHILD)
  delivery = rules.extract_order_delivery(bundle)
  src = delivery["promised_delivery_source"] or ""
  assert "history.eta_communicated" in src or "analytics.eta" in src
  assert delivery["promised_delivery"] == "17 May, 2026 23:00 IST"
  assert delivery["promised_first"]["display"] == "17 May, 2026 23:00 IST"
  assert delivery["actual_delivery"] is not None
  facts = rules.build_facts(bundle)
  assert facts["preflight"]["promised_delivery"] == delivery["promised_delivery"]


@pytest.mark.asyncio
async def test_delivery_timestamps_prod_json_utc_api_ist_display():
  """Prod child JSON: delivery_date aligns with status-40 created (UTC); comms ETA is IST."""
  bundle = await sources._fetch_fixtures(CHILD)
  delivery = rules.extract_order_delivery(bundle)
  assert delivery["promised_delivery"] == "17 May, 2026 23:00 IST"
  assert delivery["actual_delivery"] == "17 May, 2026 14:54 IST"
  assert delivery["late_minutes"] == 0
  raw = delivery["promised_delivery_raw"]
  assert raw["history_eta_communicated"] == "17 May, 2026 23:00 IST"
  assert raw["history_eta_communicated_raw"] == "17 May, 2026 23:00"
  assert raw["promised_delivery_sla_end"] == "17 May, 2026 23:00 IST"


def test_format_ts_display_date_only():
  assert rules._format_ts_display("17 May, 2026") == "17 May, 2026 IST"
  assert "14:54" in rules._format_ts_display("2026-05-17 09:24:15")


@pytest.mark.asyncio
async def test_split_child_does_not_merge_parent_groot():
  bundle = await sources._fetch_fixtures(CHILD)
  assert "parent_groot" not in bundle
  facts = rules.build_facts(bundle)
  assert not any(e.get("source_po") == "parent" for e in facts["operations"]["groot_events"])


@pytest.mark.asyncio
async def test_ideal_parent_has_full_facts_path():
  facts = rules.build_facts(await sources._fetch_fixtures(PARENT))
  assert facts["analysis_skipped"] is False
  assert facts["preflight"]["allocation_badge"] == "IDEAL"
  assert "operations" in facts
  assert facts["p3"]["rows"]


@pytest.mark.asyncio
async def test_parent_fixture_order_does_not_copy_child_shipment():
  """Parent RCA must not attribute child delivery_date to the parent PO."""
  bundle = await sources._fetch_fixtures(PARENT)
  order = bundle.get("order") or {}
  assert order.get("shipment_detail") in ({}, None) or not (order.get("shipment_detail") or {}).get(
    "delivery_date"
  )
  delivery = rules.extract_order_delivery(bundle)
  assert not delivery.get("actual_delivery")
  assert delivery.get("late_minutes") is None
  facts = rules.build_facts(bundle)
  pf = facts["preflight"]
  assert pf.get("breach_kind") != "delivered_late"


def test_groot_prod_timestamps_are_ist_wall_clock_not_utc():
  """Prod Groot uses DD-MM-YYYY; aligns with order status 08:54 UTC = 14:24 IST."""
  assert rules._format_groot_ts_display("14-05-2026 14:24:16") == "14 May, 2026 14:24 IST"
  assert rules._format_groot_ts_display("14-05-2026 14:24:16") != "14 May, 2026 19:54 IST"


def test_fixture_groot_parent_payload_loads():
  payload = sources._fixture_groot_payload("groot_parent.json")
  assert payload.get("data")
  events = rules.parse_groot_events(payload)
  assert len(events) >= 1
  completed = next(e for e in events if e.get("status") == "completed")
  assert completed["at"] == "14 May, 2026 14:24 IST"


@pytest.mark.asyncio
async def test_ops_transitions_at_display_ist():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  transitions = facts["operations"]["status_transitions"]
  assert transitions
  delivered = next(t for t in transitions if t.get("to_status_id") == "40")
  assert delivered["at"] == "17 May, 2026 14:54 IST"
  assert "IST" in str(delivered.get("hover", ""))
  chron = facts["operations"]["status_chronology"]
  assert all("at_display" in row and "IST" in row["at_display"] for row in chron)


@pytest.mark.asyncio
async def test_p4_store_groups_aggregate_virtual_stores():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  p4 = facts["p4"]
  assert len(p4["nearby_stores"]) == 5
  assert len(p4["nearby_warehouses"]) <= 3
  assert p4["allocated_store"]["physical_store"] == "1MG_KOL"
  assert not any(g["physical_store"] == "1MG_KOL" for g in p4["nearby_stores"])
  p3 = facts["p3"]
  assert len(p3["nearby_stores"]) == 5
  assert p3["allocated_store"]["physical_store"] == "1MG_KOL"
  cnp = next(g for g in p4["nearby_stores"] if g["physical_store"] == "1MG_CNP")
  assert cnp["distance_km"] == 0.87
  assert cnp["vendor_type"] == "Retail"
  qtb = next(g for g in p4["nearby_stores"] if g["physical_store"] == "1MG_QTB")
  assert len(qtb["virtual_stores"]) >= 1


def test_location_serviceable_rules():
  assert rules.location_serviceable({"pincode_mapped": False, "active_services_with_capacity": {}}, selected=False) is False
  assert rules.location_serviceable({"pincode_mapped": True}, selected=False) is True
  assert rules.location_serviceable({}, selected=True) is True
  assert rules.standard_available({}) is True
  assert rules.standard_available({"1_hour": "1 h"}) is False


def test_extract_service_windows_and_location_fields():
  vendor = {
    "pincode_mapped": False,
    "active_services_with_capacity": {"1_hour": "07:00 AM-10:50 PM"},
    "inactive_and_unavailable_capacity": {"30_minute": "08:00 AM-11:00 PM"},
    "eta": {"1_hour": "1 hours"},
  }
  windows = rules.extract_service_windows(vendor)
  assert windows["active_services"][0]["service_label"] == "1 hour"
  assert windows["active_services"][0]["window"] == "07:00 AM-10:50 PM"
  assert windows["inactive_services"][0]["service_label"] == "30 minute"
  assert windows["order_sla_keys"] == ["1 hour"]
  loc = rules.location_serviceable_fields(vendor, selected=False)
  assert loc["location_serviceable"] is True
  assert "Active delivery windows" in loc["location_reason"]


def test_instant_in_service_window():
  from zoneinfo import ZoneInfo

  ist = ZoneInfo("Asia/Kolkata")
  afternoon = datetime(2026, 6, 16, 14, 0, tzinfo=ist)
  morning = datetime(2026, 6, 16, 10, 0, tzinfo=ist)
  assert rules.instant_in_service_window(afternoon, "01:00 PM-11:59 PM") is True
  assert rules.instant_in_service_window(morning, "01:00 PM-11:59 PM") is False
  assert rules.instant_in_service_window(morning, "12:00 AM-01:00 PM") is True
  assert rules.instant_in_service_window(afternoon, "12:00 AM-01:00 PM") is False
  assert rules.instant_in_service_window(morning, "07:00-11:00") is True


def test_classify_inactive_service_timing():
  from zoneinfo import ZoneInfo

  ist = ZoneInfo("Asia/Kolkata")
  placed = datetime(2026, 6, 16, 14, 0, tzinfo=ist)
  rows = rules.classify_inactive_service_timing(
    [{"service_key": "1_day", "service_label": "1 day", "window": "01:00 PM-11:59 PM", "status": "inactive"}],
    placed,
  )
  assert rows[0]["inactivity_kind"] == rules.INACTIVITY_DISABLED

  morning = datetime(2026, 6, 16, 10, 0, tzinfo=ist)
  rows2 = rules.classify_inactive_service_timing(rows, morning)
  assert rows2[0]["inactivity_kind"] == rules.INACTIVITY_BEYOND_TIME


@pytest.mark.asyncio
async def test_inactive_timing_on_split_mounjaro_rejected_vendor():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  rejected = [r for r in facts["p3"]["rows"] if r.get("outcome") == "rejected" and r.get("inactive_services")]
  assert rejected, "expected rejected vendors with inactive services"
  for row in rejected:
    kinds = {s.get("inactivity_kind") for s in row.get("inactive_services") or []}
    assert kinds <= {rules.INACTIVITY_BEYOND_TIME, rules.INACTIVITY_DISABLED, rules.INACTIVITY_UNKNOWN}
    assert row.get("inactive_beyond_time") is not None
    assert row.get("inactive_disabled") is not None


def test_location_not_ok_label():
  assert (
    rules.location_not_ok_label(pincode_mapped=False, has_active_services=False)
    == "Address not serviceable — no pincode and no active delivery windows"
  )
  assert (
    rules.location_not_ok_label(pincode_mapped=False, has_active_services=True)
    == "Address not serviceable — pincode not mapped"
  )
  assert (
    rules.location_not_ok_label(pincode_mapped=True, has_active_services=False)
    == "Address not serviceable — no active delivery windows"
  )
  fields = rules.location_serviceable_fields(
    {"pincode_mapped": False, "active_services_with_capacity": {}},
    selected=False,
  )
  assert fields["location_serviceable"] is False
  assert fields["location_reason"] == "Address not serviceable — no pincode and no active delivery windows"


def test_service_type_label_from_key():
  assert rules.service_type_label("1_hour") == "1 hour"
  assert rules.service_type_label("30_minute") == "30 minute"
  assert rules.service_type_label("three_day_delivery") == "three day delivery"
  assert rules.service_type_label("standard") == "standard"


def test_order_service_type_label_mapping():
  assert rules.order_service_type_label({}) is None
  assert rules.order_service_type_label({"rapid_eligibility_info": {}}) is None
  assert (
    rules.order_service_type_label(
      {"rapid_eligibility_info": {"service_id": "one_day_delivery"}}
    )
    == "Zero day"
  )
  assert (
    rules.order_service_type_label(
      {"rapid_eligibility_info": {"service_id": "one_hour_delivery"}}
    )
    == "1 hour"
  )
  assert (
    rules.order_service_type_label({"service_details": {"service_type": "standard"}})
    == "Standard"
  )
  assert (
    rules.order_service_type_label(
      {"rapid_eligibility_info": {"service_id": "three_day_delivery"}}
    )
    == "three day delivery"
  )


@pytest.mark.asyncio
async def test_preflight_service_type_from_fixtures():
  rapid = rules.build_facts(await sources._fetch_fixtures(RAPID_DELIVERED_LATE))
  assert rapid["preflight"]["service_type"] == "Zero day"

  one_hr = rules.build_facts(await sources._fetch_fixtures(ONE_HOUR_GROOT))
  assert one_hr["preflight"]["service_type"] == "1 hour"

  std = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  assert std["preflight"]["service_type"] == "Standard"

  missing = rules.build_facts(await sources._fetch_fixtures(RETURN_REFUND))
  assert missing["preflight"].get("service_type") is None


@pytest.mark.asyncio
async def test_ops_transitions_child_po_sla_minutes():
  """PO13326295207344 — minute cutoffs vs fixture status history."""
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  by_label = {t["label"]: t for t in facts["operations"]["status_transitions"]}

  assert by_label["Payment completed"]["default_sla"] == "5"
  assert by_label["Payment completed"]["sla_status"] == "on_time"

  pack = by_label["Packaging"]
  assert pack["default_sla"] == "20"
  assert pack["duration_min"] == 1288
  assert pack["sla_status"] == "late"

  dispatch = by_label["Dispatch"]
  assert dispatch["default_sla"] == "229"
  assert dispatch["duration_min"] == 151
  assert dispatch["sla_status"] == "on_time"

  last_mile = by_label["Last mile"]
  # eta_to unix is IST wall clock (23:00 May 17), not UTC-shifted SLA end
  assert last_mile["default_sla"] == "3437"
  assert last_mile["sla_status"] == "on_time"


def test_is_active_service_order_standard_path():
  order = {"rapid_eligibility_info": {}}
  allocated = {"pincode_mapped": True, "eta": {}}
  assert rules.is_active_service_order(order, allocated) is False
  assert rules.build_ops_sla_context(order, is_active=False)["packaging_cutoff_min"] is None


def test_is_active_service_order_three_day():
  order = {
    "rapid_eligibility_info": {
      "service_id": "three_day_delivery",
      "rapid_opted": True,
      "delivery_cutoff": 1778837400,
    },
    "eta": {"eta_to": 1779058800.0},
  }
  assert rules.is_active_service_order(order, {"eta": {"3_day": "3 days"}}) is True


def test_extract_service_windows_standard_flags():
  mapped = rules.extract_service_windows({"pincode_mapped": True, "eta": {"0_day": "today"}})
  assert mapped["standard_at_store"] is True
  assert mapped["standard_unavailable"] is False
  unmapped = rules.extract_service_windows({"pincode_mapped": False, "eta": {}})
  assert unmapped["standard_at_store"] is False
  assert unmapped["standard_unavailable"] is True
  unknown = rules.extract_service_windows({"pincode_mapped": None, "eta": {}})
  assert unknown["standard_unavailable"] is True


def test_advertised_rapid_sla_when_active_empty_on_rapid_order():
  order = {
    "rapid_eligibility_info": {
      "service_id": "three_day_delivery",
      "rapid_opted": True,
    },
  }
  vendor = {"eta": {"3_day": "3 days"}, "active_services_with_capacity": {}}
  rows = rules.advertised_rapid_sla_rows(
    vendor,
    order=order,
    active_services=[],
  )
  assert len(rows) == 1
  assert rows[0]["status"] == "advertised"
  assert rows[0]["service_key"] == "3_day"


def test_advertised_rapid_sla_omits_keys_already_inactive():
  order = {"rapid_eligibility_info": {"service_id": "three_day_delivery", "rapid_opted": True}}
  vendor = {"eta": {"3_day": "3 days", "0_day": "today"}, "active_services_with_capacity": {}}
  inactive = [{"service_key": "3_day", "service_label": "3 day", "status": "inactive"}]
  rows = rules.advertised_rapid_sla_rows(
    vendor,
    order=order,
    active_services=[],
    inactive_services=inactive,
  )
  assert [r["service_key"] for r in rows] == ["0_day"]


def test_is_rapid_order():
  assert rules.is_rapid_order({"rapid_eligibility_info": {}}) is False
  assert rules.is_rapid_order(
    {"rapid_eligibility_info": {"service_id": "three_day_delivery", "rapid_opted": True}}
  ) is True


def test_advertised_rapid_sla_skipped_when_active_or_standard_order():
  order = {"rapid_eligibility_info": {}}
  vendor = {"eta": {"3_day": "3 days"}, "active_services_with_capacity": {}}
  assert rules.advertised_rapid_sla_rows(vendor, order=order, active_services=[]) == []
  rapid_order = {"rapid_eligibility_info": {"service_id": "three_day_delivery", "rapid_opted": True}}
  active = [{"service_key": "0_day", "service_label": "0 day", "status": "active"}]
  assert (
    rules.advertised_rapid_sla_rows(vendor, order=rapid_order, active_services=active) == []
  )


def test_p3_row_allocation_matrix_fields():
  """Synthetic vendors cover Standard+rapid, advertised SLA, and standard unavailable."""
  po = "PO_MATRIX_TEST"
  order = {
    "order_id": po,
    "created": "2026-05-14T13:41:00+05:30",
    "rapid_eligibility_info": {"service_id": "three_day_delivery", "rapid_opted": True},
    "delivery_address": {},
    "eta": {},
    "order_lines": [],
  }
  allocated = {
    "vendor_id": "kol05",
    "vendor_code": "1MG_KOL_05",
    "distance": 5.0,
    "pincode_mapped": None,
    "eta": {"3_day": "3 days"},
    "active_services_with_capacity": {},
    "inactive_and_unavailable_capacity": {},
    "city_and_state": {"city": "Kolkata", "state": "WB"},
  }
  pin_rapid = {
    "vendor_id": "brm01",
    "vendor_code": "BRM_01",
    "distance": 8.0,
    "pincode_mapped": True,
    "eta": {"0_day": "today", "1_day": "tomorrow"},
    "active_services_with_capacity": {"0_day": "07:00-22:00", "1_day": "07:00-22:00"},
    "inactive_and_unavailable_capacity": {},
    "city_and_state": {"city": "B", "state": "WB"},
  }
  retail_unmapped = {
    "vendor_id": "ret01",
    "vendor_code": "RET_01",
    "vendor_type": "retail",
    "distance": 12.0,
    "pincode_mapped": False,
    "eta": {},
    "active_services_with_capacity": {},
    "inactive_and_unavailable_capacity": {},
    "city_and_state": {"city": "K", "state": "WB"},
  }
  bundle = {
    "order_id": po,
    "order": order,
    "allocation": {
      "data": {
        po: {
          "allocated_vendor": "kol05",
          "selected_vendors": {"kol05": allocated, "brm01": pin_rapid},
          "rejected_vendors": {"ret01": retail_unmapped},
        }
      }
    },
    "status": {"data": {}},
    "history": {"history": []},
    "groot": {"data": {}},
    "analytics": {"data": []},
  }
  facts = rules.build_facts(bundle)
  by_code = {r["vendor_code"]: r for r in facts["p3"]["rows"]}

  kol = by_code["1MG_KOL_05"]
  assert kol["outcome"] == "allocated"
  assert kol["standard_at_store"] is False
  assert kol["standard_unavailable"] is True
  assert kol["advertised_rapid_sla"] == [
    {"service_key": "3_day", "service_label": "3 day", "status": "advertised"}
  ]

  brm = by_code["BRM_01"]
  assert brm["standard_at_store"] is True
  assert brm["standard_unavailable"] is False
  assert len(brm["active_services"]) == 2
  assert brm.get("advertised_rapid_sla") == []

  ret = by_code["RET_01"]
  assert ret["standard_unavailable"] is True
  assert ret.get("advertised_rapid_sla") == []


@pytest.mark.asyncio
async def test_fixture_po13326295207344_allocated_matrix():
  """Golden PO: allocated KOL_05 shows advertised 3_day; pin-mapped WH shows Standard+active."""
  facts = rules.build_facts(await sources._fetch_fixtures("PO13326295207344"))
  by_code = {r["vendor_code"]: r for r in facts["p3"]["rows"]}
  kol = by_code["1MG_KOL_05"]
  assert kol["outcome"] == "allocated"
  assert kol["pincode_mapped"] is None
  assert kol["standard_unavailable"] is True
  assert kol["advertised_rapid_sla"] == [
    {"service_key": "3_day", "service_label": "3 day", "status": "advertised"}
  ]
  assert kol["active_services"] == []
  brm = by_code.get("1MG_BRM_01") or by_code.get("BRM_01")
  assert brm is not None
  assert brm["standard_at_store"] is True
  assert {s["service_key"] for s in brm["active_services"]} >= {"0_day", "1_day"}
  assert brm.get("advertised_rapid_sla") == []
  # No advertised key may also appear in inactive (dedup contract)
  for r in facts["p3"]["rows"]:
    adv = {x["service_key"] for x in (r.get("advertised_rapid_sla") or [])}
    inact = {x["service_key"] for x in (r.get("inactive_services") or [])}
    assert not (adv & inact), r.get("vendor_code")


def test_pincode_string_normalized_on_p3_row():
  po = "PO_PIN_STR"
  vendor = {
    "vendor_id": "v1",
    "vendor_code": "WH_01",
    "distance": 1.0,
    "pincode_mapped": "true",
    "eta": {"0_day": "today"},
    "active_services_with_capacity": {"0_day": "07:00-22:00"},
    "city_and_state": {"city": "X", "state": "Y"},
  }
  bundle = {
    "order_id": po,
    "order": {"order_id": po, "order_lines": [], "delivery_address": {}, "eta": {}},
    "allocation": {
      "data": {
        po: {
          "allocated_vendor": "v1",
          "selected_vendors": {"v1": vendor},
          "rejected_vendors": {},
        }
      }
    },
    "status": {"data": {}},
    "history": {"history": []},
    "groot": {"data": {}},
    "analytics": {"data": []},
  }
  row = rules.build_facts(bundle)["p3"]["rows"][0]
  assert row["pincode_mapped"] is True
  assert row["standard_at_store"] is True
  assert row["standard_unavailable"] is False


def test_format_duration_display():
  assert rules.format_duration_display(0) == "< 1 min"
  assert rules.format_duration_display(None) == "—"
  assert rules.format_duration_display(1288) == "21 h 28 min"
  assert rules.format_duration_display(2909) == "2 d 29 min"


@pytest.mark.asyncio
async def test_split_child_ops_timeline_borrows_parent_through_allocation():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  ops = facts["operations"]
  assert ops.get("split_child") is True
  assert "120" in ops.get("borrowed_status_ids", [])
  chron = ops["status_chronology"]
  ids = [r["status_id"] for r in chron]
  assert "15" in ids and "120" in ids and "130" in ids
  assert chron[0]["source_po"] == "parent"
  assert chron[-1]["source_po"] == "this"
  transitions = ops["status_transitions"]
  assert any(t["label"] == "Payment completed" for t in transitions)
  assert any(t["label"] == "Packaging" for t in transitions)
  assert not any(t.get("from_status_id") == "120" and t.get("to_status_id") == "130" for t in transitions)
  pay = next(t for t in transitions if t["from_status_id"] == "15" and t["to_status_id"] == "16")
  assert pay["duration_min"] == 0
  assert pay["duration_display"] == "< 1 min"
  assert pay["default_sla"] == "5"
  assert pay["sla_status"] == "on_time"
  assert "hover" in pay
  assert ops["ops_sla"]["is_active_service"] is True


def test_statuses_borrowed_from_parent_dynamic():
  parent = rules._status_rows(
    {"data": {"P": [{"status": "15", "created": "a"}, {"status": "120", "created": "b"}]}},
    "P",
  )
  borrow = rules.statuses_borrowed_from_parent(parent)
  assert borrow == {"15", "120"}


@pytest.mark.asyncio
async def test_child_nearby_retail_rejection_categories():
  """Child PO: ROF/CNP have stock=0; services active — inventory only, not service."""
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  rof = p4["1MG_ROF_02"]
  assert rof["location_not_serviceable"] is False
  assert rof["inventory_not_available"] is True
  assert rof["service_not_available"] is False
  assert "Capacity" not in " ".join(rof["signals"])
  assert any("unmapped on vendor" in s.lower() for s in rof["signals"])

  cnp = p4["1MG_CNP_02"]
  assert cnp["inventory_not_available"] is True
  assert cnp["service_not_available"] is False
  assert any("unmapped on vendor" in s.lower() for s in cnp["signals"])

  glr = p4["1MG_GLR_02"]
  assert glr["location_not_serviceable"] is True
  assert glr["inventory_not_available"] is True
  assert glr["service_not_available"] is True


@pytest.mark.asyncio
async def test_brm_01_delive_state_not_available_and_delived():
  """BRM_01: delive + fulfillable 0 → not available and Delived (not partial)."""
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  brm01 = p4["1MG_BRM_01"]
  assert brm01["inventory_not_available"] is True
  assert any("Unavailable and Delived" in s and "SKU" in s for s in brm01["signals"])
  assert not any("Partial qty available but Delived" in s for s in brm01["signals"])


def test_format_inventory_rejection_line_pattern():
  line = rules.format_inventory_rejection_line("SKU 1122085: not mapped on vendor (ordered 2)")
  assert line == "SKU 1122085: Unmapped on vendor"
  line2 = rules.format_inventory_rejection_line("SKU 99: SKU not available and Delived")
  assert line2 == "SKU 99: Unavailable and Delived"


def test_analyze_vendor_rejection_omits_generic_service_window_reason():
  """Service issues surface in Active/Inactive columns, not as a generic Reason chip."""
  vendor = {
    "pincode_mapped": False,
    "active_services_with_capacity": {},
    "inactive_and_unavailable_capacity": {"1_hour": "07:00-11:00"},
    "eta": {"1_hour": "1 hours"},
    "skus": {},
  }
  analyzed = rules.analyze_vendor_rejection(vendor)
  assert analyzed["service_not_available"] is True
  assert not any("No active delivery service windows" in s for s in analyzed["signals"])


def test_inventory_rejection_lists_all_blocking_skus():
  vendor = {
    "pincode_mapped": True,
    "active_services_with_capacity": {"1_hour": "07:00-11:00"},
    "skus": {
      "s1": {"state": "live", "availability": "no", "normalized_quantity": 0},
      "s2": {"state": "delive", "availability": "yes", "normalized_quantity": 0},
      "s3": {"state": "live", "availability": "yes", "normalized_quantity": 1},
      "s4": {"state": "delive", "availability": "yes", "normalized_quantity": 5},
      "s5": {"state": "live", "availability": "yes", "normalized_quantity": 10},
      "s6": {"state": "live", "availability": "yes", "normalized_quantity": 10},
    },
  }
  qty = {"s1": 2, "s2": 2, "s3": 2, "s4": 2, "s5": 2, "s6": 2}
  analyzed = rules.analyze_vendor_rejection(vendor, order_qty_by_sku=qty)
  inv_signals = [s for s in analyzed["signals"] if s.upper().startswith("SKU ")]
  assert len(inv_signals) == 4
  assert any("s1:" in s and "Out of stock" in s for s in inv_signals)
  assert any("s2:" in s and "Unavailable and Delived" in s for s in inv_signals)
  assert any("s3:" in s and "Partial stock" in s for s in inv_signals)
  assert not any("s5:" in s for s in inv_signals)
  assert not any("s6:" in s for s in inv_signals)


@pytest.mark.asyncio
async def test_fixture_inventory_mixed_skus_loads():
  b = await sources._fetch_fixtures(INVENTORY_MIXED)
  assert b["order_id"] == INVENTORY_MIXED
  assert b["collect_mode"] == "full"
  assert len(b["order"]["order_lines"]) == 4


@pytest.mark.asyncio
async def test_inventory_mixed_fixture_vendor_stock_labels():
  """Fixture PO17026573780658 — one rejected row with unmapped + OOS + partial."""
  facts = rules.build_facts(await sources._fetch_fixtures(INVENTORY_MIXED))
  row = next(
    r for r in facts["p3"]["rows"]
    if r.get("vendor_code") == "1MG_INV_MIX_01" and r.get("outcome") == "rejected"
  )
  assert row["inventory_labels"] == ["Unmapped", "Out of stock", "Partial stock"]
  assert row["inventory_classes"] == ["unmapped", "oos", "partial"]
  inv_signals = [s for s in row["signals"] if s.upper().startswith("SKU ")]
  assert len(inv_signals) == 4
  assert any("900101" in s and "Unmapped" in s for s in inv_signals)
  assert any("900102" in s and "Unmapped" in s for s in inv_signals)
  assert any("900103" in s and "Out of stock" in s for s in inv_signals)
  assert any("900104" in s and "Partial stock" in s for s in inv_signals)


def test_sampling_sku_priority_detection():
  assert rules.is_sampling_order_line({"sku": {"sku_id": "s1", "priority": 30}})
  assert not rules.is_sampling_order_line({"sku": {"sku_id": "s2", "priority": 44}})
  assert not rules.is_sampling_order_line({"sku": {"sku_id": "s3"}})


def test_fulfillment_order_excludes_sampling_lines():
  order = {
    "order_lines": [
      {"sku": {"sku_id": "keep", "priority": 44}, "quantity": 1},
      {"sku": {"sku_id": "sample", "priority": 30}, "quantity": 1},
    ]
  }
  assert len(rules.fulfillment_order_lines(order)) == 1
  assert rules.order_qty_by_sku(rules.fulfillment_order(order)) == {"keep": 1.0}
  skus = rules._order_skus(order)
  assert len(skus) == 2
  assert rules.fulfillment_skus(skus) == [skus[0]]


def test_inventory_empty_fulfillment_qty_map_does_not_scan_vendor_catalog():
  vendor = {
    "pincode_mapped": True,
    "skus": {
      "catalog_a": {"state": "live", "availability": "no", "normalized_quantity": 0},
      "catalog_b": {"state": "live", "availability": "yes", "normalized_quantity": 5},
    },
  }
  na, details = rules.inventory_not_available(vendor, order_qty_by_sku={})
  assert na is False
  assert details == []
  summary = rules.vendor_inventory_summary(vendor, order_qty_by_sku={})
  assert summary["inventory_not_available"] is False
  assert summary["inventory_label"] == "OK"


@pytest.mark.asyncio
async def test_sampling_sku_displayed_but_excluded_from_msn():
  facts = rules.build_facts(await sources._fetch_fixtures(RAPID_DELIVERED_LATE))
  assert len(facts["skus"]) == 29
  sampling = [s for s in facts["skus"] if s.get("is_sampling")]
  assert len(sampling) == 1
  assert sampling[0]["sku_id"] == "1135694"
  msn_ids: set[str] = set()
  for store in (facts["p1"]["msn_adherence"].get("stores") or []):
    for s in store.get("skus") or []:
      msn_ids.add(str(s.get("sku_id")))
  assert "1135694" not in msn_ids
  assert len(msn_ids) == 28


def test_inventory_mixed_unmapped_oos_and_partial():
  """All blocking SKUs appear in Reason; Stock shows each unique issue type."""
  vendor = {
    "pincode_mapped": True,
    "skus": {
      "mapped_oos": {"state": "live", "availability": "no", "normalized_quantity": 0},
      "mapped_partial": {"state": "live", "availability": "yes", "normalized_quantity": 1},
    },
  }
  qty = {"unmapped_a": 1, "unmapped_b": 2, "mapped_oos": 2, "mapped_partial": 3}
  na, details = rules.inventory_not_available(vendor, order_qty_by_sku=qty)
  assert na is True
  assert len([d for d in details if "not mapped" in d]) == 2
  assert any("mapped_oos" in d and "out of stock" in d for d in details)
  assert any("mapped_partial" in d and "partial" in d for d in details)

  summary = rules.vendor_inventory_summary(vendor, order_qty_by_sku=qty)
  assert summary["inventory_classes"] == ["unmapped", "oos", "partial"]
  assert summary["inventory_labels"] == ["Unmapped", "Out of stock", "Partial stock"]
  assert summary["inventory_partial"] is True

  analyzed = rules.analyze_vendor_rejection(vendor, order_qty_by_sku=qty)
  inv_signals = [s for s in analyzed["signals"] if s.upper().startswith("SKU ")]
  assert len(inv_signals) == 4
  assert analyzed["inventory_labels"] == ["Unmapped", "Out of stock", "Partial stock"]


def test_classify_sku_inventory_delive_uses_qty():
  c0 = rules.classify_sku_inventory({"state": "delive", "availability": "yes", "normalized_quantity": 0}, 2)
  assert c0["class"] == "delived_oos"
  assert c0["label"] == "Unavailable and Delived"
  assert c0["detail"] == "not available and Delived"

  c1 = rules.classify_sku_inventory({"state": "delive", "availability": "yes", "normalized_quantity": 1}, 2)
  assert c1["class"] == "delived_partial"
  assert c1["label"] == "Partial and Delived"

  c5 = rules.classify_sku_inventory({"state": "delive", "availability": "yes", "normalized_quantity": 5}, 2)
  assert c5["class"] == "delived_avail"
  assert c5["label"] == "Delived pipeline"
  assert c5["blocks_fulfillment"] is True

  live = rules.classify_sku_inventory({"state": "live", "availability": "yes", "normalized_quantity": 3}, 2)
  assert live["class"] == "ok"
  assert live["blocks_fulfillment"] is False


@pytest.mark.asyncio
async def test_brm_01_inv_column_delived_not_oos():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  assert p4["1MG_BRM_01"]["inventory_class"] == "delived_oos"
  assert p4["1MG_BRM_01"]["inventory_label"] == "Unavailable and Delived"


@pytest.mark.asyncio
async def test_empty_skus_is_not_mapped_not_generic_oos():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  rof = p4["1MG_ROF_02"]
  assert any("Unmapped on vendor" in s for s in rof["signals"])


@pytest.mark.asyncio
async def test_preferred_vendors_three_distinct_physical_warehouses():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  pref = facts["preflight"]["preferred_vendors"]
  assert len(pref) == 3
  stores = {p["physical_store"] for p in pref}
  assert len(stores) == 3
  assert "1MG_BRM" in stores
  assert "1MG_MNJ_BRM" in stores


@pytest.mark.asyncio
async def test_p3_rejected_virtual_stores_include_signals():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  rejected = [
    v
    for g in facts["p3"]["nearby_stores"] + facts["p3"]["nearby_warehouses"]
    for v in g.get("virtual_stores") or []
    if v.get("outcome") == "rejected"
  ]
  assert rejected
  assert all(v.get("signals") for v in rejected)
  cnp = next(v for v in rejected if v["vendor_code"] == "1MG_CNP_02")
  assert any(
    "unmapped on vendor" in s.lower() or "out of stock" in s.lower() or "depleted" in s.lower()
    for s in cnp["signals"]
  )


def test_parent_rejected_retail_can_have_stock():
  """Parent PO: CNP rejected but had yes/maybe SKUs — not an inventory rejection."""
  data = json.loads((FIX / "allocation.json").read_text())["data"]
  rej = data[PARENT]["rejected_vendors"]
  cnp = next(v for v in rej.values() if v.get("vendor_code") == "1MG_CNP_02")
  analyzed = rules.analyze_vendor_rejection(cnp)
  assert analyzed["inventory_not_available"] is False
  assert analyzed["service_not_available"] is False


def test_allocation_fixture_has_both_pos():
  data = json.loads((FIX / "allocation.json").read_text())["data"]
  assert PARENT in data and CHILD in data


@pytest.mark.asyncio
async def test_build_facts_parent_retail_is_ideal_full_path():
  """Parent PO in allocation fixture is retail ROF — IDEAL badge, full facts."""
  alloc = json.loads((FIX / "allocation.json").read_text())
  bundle = {
    "order_id": PARENT,
    "order": {
      "order_id": PARENT,
      "delivery_address": {"city": "Gurgaon", "state": "Haryana", "pincode": "122009"},
      "eta": {"eta_to": 1779058800.0},
      "order_lines": [],
      "shipment_detail": {"delivery_date": "17 May, 2026 18:00"},
    },
    "parent_id": None,
    "allocation": alloc,
    "status": {
      "data": {
        PARENT: [
          {"status": "40", "created": "2026-05-17T18:00:00"},
          {"status": "25", "created": "2026-05-17T12:00:00"},
          {"status": "130", "created": "2026-05-17T08:00:00"},
        ]
      }
    },
    "history": {},
    "groot": {"data": {}},
    "analytics": {"data": []},
  }
  facts = rules.build_facts(bundle)
  assert facts["analysis_skipped"] is False
  assert facts["preflight"]["allocation_badge"] == "IDEAL"
  assert not facts["p3"].get("skipped")
  assert facts["operations"]["status_transitions"]


def test_eta_to_unix_decodes_as_ist_wall_clock_not_utc_shift():
  """Prod eta_to unix 1779058800 → 17 May, 2026 23:00 IST (not 18 May 04:30)."""
  dt = rules._parse_eta_to_unix(1779058800.0)
  assert dt is not None
  assert rules._format_ist_wall(dt) == "17 May, 2026 23:00 IST"


def test_late_minutes_only_vs_eta_to():
  bundle = {
    "order": {
      "eta": {"eta_to": 1779058800.0, "to_date": "17 May, 2026"},
      "shipment_detail": {"delivery_date": "2026-05-18T00:00:00+00:00"},
    },
    "history": {},
  }
  d = rules.extract_order_delivery(bundle)
  assert d["promised_delivery"] == "17 May, 2026 23:00 IST"
  assert d["promised_delivery_source"] == "order.eta.eta_to"
  assert d["late_minutes_vs"] == "promised_first"
  # Delivered 05:30 IST May 18 vs promised 23:00 IST May 17
  assert d["late_minutes"] == 390


def test_promised_eta_fallback_when_eta_to_missing():
  from datetime import UTC, datetime

  pe = int(datetime(2026, 5, 17, 17, 30, tzinfo=UTC).timestamp())
  bundle = {
    "order": {
      "eta": {},
      "promised_eta": pe,
      "shipment_detail": {},
    },
    "history": {},
  }
  d = rules.extract_order_delivery(bundle)
  assert d["promised_delivery"] == "17 May, 2026 23:00 IST"
  assert d["promised_delivery_source"] == "order.promised_eta"


@pytest.mark.asyncio
async def test_is_ideal_bundle_parent_allocation():
  alloc = json.loads((FIX / "allocation.json").read_text())
  bundle = {
    "order_id": PARENT,
    "order": {"order_id": PARENT},
    "allocation": alloc,
  }
  assert rules.is_ideal_bundle(bundle) is True


@pytest.mark.asyncio
async def test_fetch_fixtures_ideal_full_collect(monkeypatch):
  from app.config.settings import settings

  monkeypatch.setattr(settings, "order_rca_use_fixtures", True)
  monkeypatch.setattr(rules, "is_ideal_bundle", lambda _b: True)
  b = await sources._fetch_fixtures(CHILD)
  assert b.get("collect_mode") == "full"
  assert b["history"]


@pytest.mark.asyncio
async def test_fixture_return_refund_case_loads():
  b = await sources._fetch_fixtures(RETURN_REFUND)
  assert b["order_id"] == RETURN_REFUND
  assert b["collect_mode"] == "full"
  assert b["order"]["status"] == "Returned and Refunded"


@pytest.mark.asyncio
async def test_return_refund_promised_first_uses_analytics_not_march():
  bundle = await sources._fetch_fixtures(RETURN_REFUND)
  delivery = rules.extract_order_delivery(bundle)
  assert delivery["promised_delivery"] == "27 Apr, 2026 22:00 IST"
  src = delivery["promised_delivery_source"] or ""
  assert "history.eta_communicated" in src or "analytics.eta" in src
  assert delivery["promised_current"]["display"] == "29 Apr, 2026 00:00 IST"
  assert "Mar" not in (delivery["promised_delivery"] or "")
  assert "30 Mar" not in (delivery["promised_delivery"] or "")


@pytest.mark.asyncio
async def test_return_refund_preflight_delivery_and_status():
  facts = rules.build_facts(await sources._fetch_fixtures(RETURN_REFUND))
  pf = facts["preflight"]
  assert pf["order_status"] == "Returned and Refunded"
  assert pf["order_status_id"] == "142"
  assert pf["promised_delivery"] == "27 Apr, 2026 22:00 IST"
  assert pf["actual_delivery"] is not None
  assert "Apr" in pf["actual_delivery"]
  assert pf["city"] == "Bangalore"
  assert pf["zone"] == "ZONE_B"
  assert pf.get("return_reason")
  assert not any("truncated" in w.lower() or "incomplete" in w.lower() for w in facts.get("warnings") or [])


@pytest.mark.asyncio
async def test_return_refund_ops_return_phases_no_forward_sla():
  facts = rules.build_facts(await sources._fetch_fixtures(RETURN_REFUND))
  ops = facts["operations"]
  assert ops.get("return_followed") is True
  assert ops.get("return_note")
  transitions = ops["status_transitions"]
  ret = next(t for t in transitions if t.get("to_status_id") == "140")
  assert ret["label"] == "Return requested"
  assert ret.get("sla_excluded") is True
  assert ret["sla_status"] == "—"
  last_mile = next(t for t in transitions if t.get("label") == "Last mile")
  assert last_mile.get("sla_excluded") is not True


def test_status_labels_include_return_and_cancel():
  assert STATUS_LABELS["140"] == "Request for Return and Refund"
  assert STATUS_LABELS["99"] == "Cancelled"
  assert STATUS_LABELS["100"] == "Waiting For Rx"


def test_extract_history_return_reason():
  payload = {
    "history": [
      {
        "comment": "PO1|request_for_return_and_refund|return_reason|quality / duplicate product",
        "created": 1,
      }
    ]
  }
  assert rules.extract_history_insights(payload)["return_reason"] == "quality / duplicate product"


@pytest.mark.asyncio
async def test_fixture_split_mounjaro_case_loads():
  b = await sources._fetch_fixtures(SPLIT_MOUNJARO)
  assert b["order_id"] == SPLIT_MOUNJARO
  assert b["parent_id"] == SPLIT_MOUNJARO_PARENT
  assert b["collect_mode"] == "full"
  assert len(b["history"].get("history") or []) == 20


@pytest.mark.asyncio
async def test_split_mounjaro_cross_far_warehouse_and_ofd():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  pf = facts["preflight"]
  assert pf["allocation_badge"] == "CROSS"
  assert pf["order_status"] == "Out for delivery"
  assert pf["order_status_id"] == "30"
  assert pf["city"] == "Dehradun"
  assert pf["zone"] == "ZONE_B"
  assert pf["pincode"] == "248001"
  assert pf["actual_vendor_code"] == "1MG_NOI_501"
  assert not pf.get("actual_delivery")
  assert pf["promised_first"]["display"] == "20 May, 2026 23:59 IST"
  assert pf["promised_current"]["display"] == "27 May, 2026 23:59 IST"
  assert pf["promised_delivery"] == "20 May, 2026 23:59 IST"
  assert facts["operations"]["split_child"] is True
  assert facts["operations"]["groot_empty"] is True
  assert facts["operations"]["history_count"] == 20
  assert not any("truncated" in w.lower() or "incomplete" in w.lower() for w in facts.get("warnings") or [])


@pytest.mark.asyncio
async def test_split_mounjaro_borrows_parent_pre_packaging():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  chron = facts["operations"]["status_chronology"]
  ids = [r["status_id"] for r in chron]
  assert "15" in ids and "120" in ids and "130" in ids and "30" in ids
  assert chron[0]["source_po"] == "parent"
  assert chron[-1]["source_label"] in ("this", None) or chron[-1].get("source_po") == "this"


def test_implicit_rejection_split_enabled_false_appends_hint():
  vendor = {
    "pincode_mapped": True,
    "active_services_with_capacity": {"0_day": "12:00 AM-01:00 PM"},
    "inactive_and_unavailable_capacity": {},
    "eta": {"0_day": "11 hours"},
    "split_enabled": False,
    "skus": {
      "1122603": {
        "availability": "yes",
        "jit": False,
        "partial_quantity": False,
        "normalized_quantity": 12,
        "state": "live",
      }
    },
  }
  rej = rules.analyze_vendor_rejection(vendor, order_qty_by_sku={"1122603": 1})
  assert rej["implicit_rejection"] is True
  assert rej["split_enabled"] is False
  assert rej["signals"][0] == (
    "Not chosen by allocation engine (Split fulfilment disabled, Correlate further)"
  )


def test_implicit_rejection_split_enabled_true_keeps_default_tail():
  vendor = {
    "pincode_mapped": True,
    "active_services_with_capacity": {"1_hour": "07:00-11:00"},
    "split_enabled": True,
    "skus": {"s1": {"availability": "yes", "normalized_quantity": 5, "state": "live"}},
  }
  rej = rules.analyze_vendor_rejection(vendor, order_qty_by_sku={"s1": 1})
  assert rej["implicit_rejection"] is True
  assert rej["signals"][0] == "Not chosen by allocation engine (Correlate further)"


def test_implicit_rejection_missing_split_enabled_keeps_default_tail():
  vendor = {
    "pincode_mapped": True,
    "active_services_with_capacity": {"1_hour": "07:00-11:00"},
    "skus": {"s1": {"availability": "yes", "normalized_quantity": 5, "state": "live"}},
  }
  rej = rules.analyze_vendor_rejection(vendor, order_qty_by_sku={"s1": 1})
  assert rej["implicit_rejection"] is True
  assert rej["signals"][0] == "Not chosen by allocation engine (Correlate further)"


@pytest.mark.asyncio
async def test_split_mounjaro_vsv_319_implicit_rejection_shows_split_flag():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  v319 = p4["1MG_VSV_319"]
  assert v319.get("implicit_rejection") is True
  assert any("Split fulfilment disabled" in s for s in v319.get("signals") or [])


@pytest.mark.asyncio
async def test_split_mounjaro_nearest_dehradun_retail_rejected():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  p4 = {r["vendor_code"]: r for r in facts["p4"]["rows"]}
  vsv = p4.get("1MG_VSV_319") or p4.get("1MG_VSV_02")
  assert vsv is not None
  assert vsv["distance_km"] < 10


@pytest.mark.asyncio
async def test_fetch_order_history_paginates(monkeypatch):
  from app.agents.order_rca import sources as src

  calls: list[int] = []

  async def fake_get(client, url, **kw):
    page = kw.get("params", {}).get("page_number", 1)
    calls.append(page)
    if page == 1:
      return {"history": [{"comment": "a", "created": 1}], "total_pages": 2, "total_count": 2}
    return {"history": [{"comment": "b", "created": 2}], "total_pages": 2, "total_count": 2}

  monkeypatch.setattr(src, "_get_json", fake_get)
  import httpx

  async with httpx.AsyncClient() as client:
    out = await src._fetch_order_history(client, "http://order.test", "PO_TEST")
  assert calls == [1, 2]
  assert len(out["history"]) == 2
  assert out["total_count"] == 2


@pytest.mark.asyncio
async def test_perfect_order_synthesis_has_no_hypotheses():
  from app.agents.order_rca import synthesize

  facts = {
    "perfect_order": {
      "overall": "perfect",
      "overall_pass": True,
      "pillars": [{"id": "allocation", "pass": True, "status": "pass", "label": "Ideal", "detail": "OK"}],
    },
    "preflight": {},
    "p1": {"status": "pending_data"},
    "p2": {"status": "not_configured"},
  }
  syn, source = await synthesize.synthesize_rca(facts)
  assert source == "perfect_template"
  assert syn["hypotheses"] == []


def _minimal_full_bundle(*, created: int, order_id: str = "PO_OLD") -> dict:
  return {
    "order_id": order_id,
    "order": {
      "order_id": order_id,
      "created": created,
      "delivery_address": {},
      "eta": {},
      "order_lines": [],
    },
    "allocation": {"data": {}},
    "status": {"data": {}},
    "history": {"history": []},
    "groot": {"data": {}},
    "p1_msn": {},
  }


def test_build_allocation_unavailable_message_with_days():
  old = int((datetime.now(UTC) - timedelta(days=45)).timestamp())
  out = rules.build_allocation_unavailable(
    _minimal_full_bundle(created=old)["order"], {}
  )
  assert out is not None
  assert out["reason"] == "unavailable"
  assert out["days_placed"] == 45
  assert out["message"] == (
    "Allocation Data not available for this Order, Order Placed 45 days ago."
  )


def test_build_allocation_unavailable_message_without_created():
  out = rules.build_allocation_unavailable(
    {"order_lines": []}, {}
  )
  assert out is not None
  assert out["message"] == "Allocation Data not available for this Order."
  assert out["days_placed"] is None


@pytest.mark.asyncio
async def test_build_facts_missing_allocation_sets_unavailable():
  old = int((datetime.now(UTC) - timedelta(days=12)).timestamp())
  facts = rules.build_facts(_minimal_full_bundle(created=old))
  unavail = facts.get("allocation_unavailable")
  assert unavail is not None
  assert unavail["reason"] == "unavailable"
  assert facts["analysis_mode"] == "allocation_unavailable"
  assert facts["preflight"]["allocation_badge"] == "N/A"
  assert facts["p3"]["panel_note"] == unavail["message"]
  assert unavail["message"] in (facts.get("warnings") or [])
  assert not any("No allocation block for this order id" in w for w in facts.get("warnings") or [])


@pytest.mark.asyncio
async def test_synthesize_allocation_unavailable_skips_openai(monkeypatch):
  from app.agents.order_rca import synthesize

  old = int((datetime.now(UTC) - timedelta(days=81)).timestamp())
  facts = rules.build_facts(_minimal_full_bundle(created=old))
  assert facts.get("allocation_unavailable") is not None
  monkeypatch.setattr(synthesize.settings, "order_rca_mock_llm", False)
  monkeypatch.setattr(synthesize.settings, "openai_api_key", "sk-test-should-not-call")
  syn, source = await synthesize.synthesize_rca(facts)
  assert source == "allocation_unavailable_template"
  msg = facts["allocation_unavailable"]["message"]
  assert syn["verdict"] == msg
  assert syn["hypotheses"] == []
  assert syn["data_gaps"] == []


def test_compute_order_mrp_increase_sums_lines_increases_only():
  order = {
    "order_lines": [
      {"unit_cart_item_mrp": 135.0, "unit_current_mrp": 140.0, "quantity": 2},
      {"unit_cart_item_mrp": 110.0, "unit_current_mrp": 100.0, "quantity": 1},
    ]
  }
  total, source, used = rules.compute_order_mrp_increase(order)
  assert total == 10.0
  assert source == "order_lines"
  assert used is True


def test_compute_order_mrp_increase_payment_summary_fallback():
  order = {
    "order_lines": [{"quantity": 1, "sku": {"sku_id": "1"}}],
    "payment_summary": {"cart_mrp": 245.0, "current_mrp": 400.0},
  }
  total, source, used = rules.compute_order_mrp_increase(order)
  assert total == 155.0
  assert source == "payment_summary"
  assert used is False


def test_detect_allocation_pushback_after_packaging():
  rows = [
    {"status": "130", "created": "2026-05-15T05:10:26"},
    {"status": "25", "created": "2026-05-15T19:39:40"},
    {"status": "120", "created": "2026-05-16T10:00:00"},
  ]
  assert rules.detect_allocation_pushback(rows) is False


def test_detect_allocation_pushback_true_when_20_and_120_both_present():
  rows = [
    {"status": "130", "created": "2026-05-15T05:10:26"},
    {"status": "20", "created": "2026-05-16T09:00:00"},
    {"status": "130", "created": "2026-05-16T10:00:00"},
    {"status": "120", "created": "2026-05-16T11:00:00"},
  ]
  assert rules.detect_allocation_pushback(rows) is True


def test_detect_allocation_pushback_true_when_only_20_repeats():
  rows = [
    {"status": "130", "created": "2026-05-15T05:10:26"},
    {"status": "20", "created": "2026-05-16T09:00:00"},
    {"status": "130", "created": "2026-05-16T10:00:00"},
    {"status": "20", "created": "2026-05-16T11:00:00"},
  ]
  assert rules.detect_allocation_pushback(rows) is True


def test_detect_allocation_pushback_true_when_only_120_repeats():
  rows = [
    {"status": "130", "created": "2026-05-15T05:10:26"},
    {"status": "120", "created": "2026-05-16T09:00:00"},
    {"status": "130", "created": "2026-05-16T10:00:00"},
    {"status": "120", "created": "2026-05-16T11:00:00"},
  ]
  assert rules.detect_allocation_pushback(rows) is True


def test_detect_allocation_pushback_none_when_monotonic():
  rows = [
    {"status": "130", "created": "2026-05-15T05:10:26"},
    {"status": "25", "created": "2026-05-15T19:39:40"},
    {"status": "30", "created": "2026-05-22T06:14:56"},
  ]
  assert rules.detect_allocation_pushback(rows) is False


def test_build_perfect_order_price_fail_over_limit():
  order = {
    "order_lines": [
      {"unit_cart_item_mrp": 100.0, "unit_current_mrp": 200.0, "quantity": 2},
    ],
    "is_eta_breached": False,
  }
  card = rules.build_perfect_order_scorecard(
    order,
    allocation_badge="IDEAL",
    allocation_unavailable=False,
    child_status_rows=[{"status": "130", "created": "1"}, {"status": "40", "created": "2"}],
    delivery={"is_eta_breached": False, "actual_delivery": "1 Jan, 2026", "late_minutes": 0},
  )
  price = next(p for p in card["pillars"] if p["id"] == "price_integrity")
  assert price["pass"] is False
  assert card["overall_pass"] is False
  assert card["total_mrp_increase"] == 200.0


@pytest.mark.asyncio
async def test_service_changed_perfect_order_delivery_human_duration():
  facts = rules.build_facts(await sources._fetch_fixtures(SERVICE_CHANGED))
  po = facts["perfect_order"]
  delivery_pillar = next(p for p in po["pillars"] if p["id"] == "delivery")
  assert delivery_pillar["pass"] is False
  assert "2 d 10 h 26 min" in (delivery_pillar.get("detail") or "")


@pytest.mark.asyncio
async def test_split_mounjaro_perfect_order_imperfect():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  po = facts["perfect_order"]
  assert po["overall"] == "imperfect"
  assert po["overall_pass"] is False
  by_id = {p["id"]: p for p in po["pillars"]}
  assert by_id["allocation"]["pass"] is False
  assert by_id["delivery"]["pass"] is False
  assert by_id["customer_contact"]["status"] == "unknown"
  assert by_id["price_integrity"]["pass"] is True
  assert po["total_mrp_increase"] == 0.0


@pytest.mark.asyncio
async def test_build_facts_child_includes_perfect_order():
  facts = rules.build_facts(await sources._fetch_fixtures(CHILD))
  assert "perfect_order" in facts
  assert facts["perfect_order"]["overall"] in ("perfect", "imperfect")


def test_ordered_quantity_from_line_prefers_normalized_quantity():
  line = {
    "quantity": 50,
    "normalized_quantity": 5,
    "sku": {"sku_id": 966163, "units_in_pack": 10},
  }
  assert rules.ordered_quantity_from_line(line) == 5.0


def test_ordered_quantity_from_line_single_pack_tablet_sku():
  """quantity equals pack size when one bottle/strip ordered — must not show pack size."""
  line = {
    "quantity": 120,
    "normalized_quantity": 1,
    "sku": {"sku_id": 649158, "units_in_pack": 120},
  }
  assert rules.ordered_quantity_from_line(line) == 1.0


def test_ordered_quantity_from_line_derives_packs_without_normalized():
  line = {"quantity": 100, "sku": {"sku_id": 5182, "units_in_pack": 10}}
  assert rules.ordered_quantity_from_line(line) == 10.0


def test_ordered_quantity_from_line_keeps_pack_count_when_below_pack_size():
  line = {"quantity": 1, "sku": {"sku_id": 1, "units_in_pack": 10}}
  assert rules.ordered_quantity_from_line(line) == 1.0


def test_ordered_quantity_from_line_sold_by_pack_no_conversion():
  line = {"quantity": 2, "normalized_quantity": 2, "sku": {"sku_id": 1122085, "units_in_pack": 2}}
  assert rules.ordered_quantity_from_line(line) == 2.0


def test_order_qty_by_sku_po16226588679400_lines():
  order = {
    "order_lines": [
      {
        "quantity": 50,
        "normalized_quantity": 5,
        "sku": {"sku_id": 966163, "name": "Bisojoy 2.5mg Tablet", "units_in_pack": 10},
      },
      {
        "quantity": 120,
        "normalized_quantity": 1,
        "sku": {"sku_id": 649158, "name": "Eltroxin 75mcg Tablet", "units_in_pack": 120},
      },
      {
        "quantity": 100,
        "normalized_quantity": 1,
        "sku": {"sku_id": 134965, "name": "Himalaya Liv. 52 Tablet", "units_in_pack": 100},
      },
      {
        "quantity": 6,
        "normalized_quantity": 1,
        "sku": {"sku_id": 1140363, "name": "Miduty Liver Detox Capsule", "units_in_pack": 6},
      },
    ]
  }
  assert rules.order_qty_by_sku(order) == {
    "966163": 5.0,
    "649158": 1.0,
    "134965": 1.0,
    "1140363": 1.0,
  }
  skus = rules._order_skus(order)
  assert [s["quantity"] for s in skus] == [5.0, 1.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_build_facts_skus_and_msn_use_pack_normalized_qty():
  from app.agents.order_rca import msn_adherence

  bundle = {
    "order_id": "PO16226588679400",
    "parent_id": None,
    "order": {
      "order_id": "PO16226588679400",
      "created": 1781281267,
      "delivery_address": {"city": "Lucknow", "pincode": "226010"},
      "eta": {"zone": {"name": "ZONE_A"}},
      "order_lines": [
        {
          "quantity": 120,
          "normalized_quantity": 1,
          "sku": {"sku_id": 649158, "name": "Eltroxin 75mcg Tablet", "units_in_pack": 120},
        },
      ],
    },
    "allocation": {
      "data": {
        "PO16226588679400": {
          "allocated_vendor": 8212,
          "selected_vendors": {},
          "rejected_vendors": {
            "1": {
              "vendor_code": "1MG_TNR_01",
              "vendor_type": "WAREHOUSE",
              "distance": 3.0,
              "skus": {
                "649158": {
                  "availability": "yes",
                  "normalized_quantity": 5,
                  "state": "live",
                }
              },
            }
          },
        }
      }
    },
    "status": {"data": {"PO16226588679400": [{"status": "40", "created": "2026-06-13"}]}},
    "history": {"history": []},
    "groot": {"data": {}},
    "p1_msn": {},
  }
  facts = rules.build_facts(bundle)
  assert facts["skus"][0]["quantity"] == 1.0
  msn_line = facts["p1"]["msn_adherence"]["stores"][0]["skus"][0]
  assert msn_line["asked_qty"] == 1.0
  assert msn_line["asked_display"] == "1"

  from app.agents.order_rca.llm_context import build_llm_context

  ctx = build_llm_context(facts)
  assert ctx["order_summary"]["skus"][0]["quantity"] == 1.0


def test_parse_clickpost_events_success():
  payload = {
      "data": [
          {"clickpost_status_bucket_description": "Delivered", "location": "City", "timestamp": "2026-06-15T16:37:53"},
          {"clickpost_status_bucket_description": "Shipped", "location": "DC", "timestamp": "2026-06-09T21:02:38"},
      ],
      "is_success": True,
  }
  events = rules.parse_clickpost_events(payload)
  assert len(events) == 2
  assert events[0]["status"] == "Delivered"
  assert events[0]["at"] != "—"


def test_parse_clickpost_events_400_empty():
  payload = {"is_success": False, "status_code": 400, "data": []}
  assert rules.parse_clickpost_events(payload) == []


def test_resolve_last_mile_mode_prefers_groot():
  groot = [{"status": "completed"}]
  clickpost = [{"status": "Delivered"}]
  assert rules.resolve_last_mile_mode(groot, clickpost) == "groot"
  assert rules.resolve_last_mile_mode([], clickpost) == "clickpost"
  assert rules.resolve_last_mile_mode([], []) == "none"


@pytest.mark.asyncio
async def test_rapid_delivered_late_uses_groot_when_present():
  facts = rules.build_facts(await sources._fetch_fixtures(RAPID_DELIVERED_LATE))
  ops = facts["operations"]
  assert ops["last_mile_mode"] == "groot"
  assert not ops["groot_empty"]
  assert ops["clickpost_empty"] is True
  assert len(ops["groot_events"]) >= 10


@pytest.mark.asyncio
async def test_clickpost_sfx_when_groot_empty():
  facts = rules.build_facts(await sources._fetch_fixtures(CLICKPOST_SFX))
  ops = facts["operations"]
  assert ops["last_mile_mode"] == "clickpost"
  assert ops["groot_empty"] is True
  assert len(ops["clickpost_events"]) == 5
  ship = ops["shipping_summary"]
  assert ship["delivery_partners_code"] == "SFXEcomCP"
  assert ship["waybill"] == "SF3480902644MG"
  signals = " ".join(facts.get("signals") or [])
  assert "ClickPost" in signals
  assert "span" in signals.lower()


@pytest.mark.asyncio
async def test_one_hour_groot_skips_clickpost_events_in_facts():
  facts = rules.build_facts(await sources._fetch_fixtures(ONE_HOUR_GROOT))
  ops = facts["operations"]
  assert ops["last_mile_mode"] == "groot"
  assert ops["clickpost_empty"] is True
  assert len(ops["clickpost_events"]) == 0


@pytest.mark.asyncio
async def test_split_mounjaro_clickpost_400_graceful():
  facts = rules.build_facts(await sources._fetch_fixtures(SPLIT_MOUNJARO))
  ops = facts["operations"]
  assert ops["groot_empty"] is True
  assert ops["last_mile_mode"] == "none"
  assert ops["clickpost_empty"] is True
  assert any("ClickPost tracking unavailable" in w for w in facts.get("warnings") or [])
