"""MSN adherence status and table builder."""

from __future__ import annotations

import pytest

from app.agents.order_rca import msn_adherence, rules, sources

CHILD = "PO13326295207344"


class TestClassifyMsnAdherenceStatus:
    def test_no_msn_can_fulfil(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=None, on_shelf_qty=10, asked_qty=2
        )
        assert r["status_label"] == "No MSN. Can fulfil order"
        assert r["status_tone"] == "ok"

    def test_no_msn_partial(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=0, on_shelf_qty=1, asked_qty=2
        )
        assert r["status_label"] == "No MSN. Partial vs order"

    def test_no_msn_oos(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=None, on_shelf_qty=0, asked_qty=2
        )
        assert r["status_label"] == "No MSN. Out of stock vs order"

    def test_can_fulfil_msn_met(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=12, on_shelf_qty=20, asked_qty=2
        )
        assert r["status_label"] == "Can fulfil · MSN met"

    def test_can_fulfil_below_msn(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=12, on_shelf_qty=8, asked_qty=2
        )
        assert r["status_label"] == "Can fulfil · below MSN"
        assert r["status_tone"] == "warn"

    def test_partial_below_msn(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=12, on_shelf_qty=1, asked_qty=2
        )
        assert r["status_label"] == "Partial vs order · below MSN"

    def test_oos_below_msn(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=8, on_shelf_qty=0, asked_qty=2
        )
        assert r["status_label"] == "Out of stock vs order · below MSN"

    def test_unavailable_without_on_shelf(self):
        r = msn_adherence.classify_msn_adherence_status(
            effective_msn=12, on_shelf_qty=None, asked_qty=2
        )
        assert r["status_label"] == "—"


@pytest.mark.asyncio
async def test_build_facts_includes_msn_adherence_stores():
    b = await sources._fetch_fixtures(CHILD)
    facts = rules.build_facts(b)
    msn = facts["p1"]["msn_adherence"]
    assert msn["status"] in ("pending_api", "pending_data")
    assert len(msn["stores"]) >= 8
    store = msn["stores"][0]
    assert store["physical_store"]
    assert store["rollup_label"]
    assert len(store["skus"]) >= 1
    line = store["skus"][0]
    assert line["asked_qty"] is not None
    assert line["status_label"] == "—"
    assert line["on_shelf_display"] is None


def test_build_msn_adherence_with_p1_data():
    groups = [
        {
            "physical_store": "1MG_ROF",
            "distance_km": 1.85,
            "store_kind": "retail",
            "vendor_type": "RETAIL",
        }
    ]
    order_skus = [{"sku_id": "133187", "name": "Montair-LC", "quantity": 2}]
    p1 = {
        "stores": {
            "1MG_ROF": {
                "skus": {
                    "133187": {
                        "effective_msn": 12,
                        "on_shelf_qty": 8,
                        "sku_sub_grade": "B",
                    }
                }
            }
        }
    }
    block = msn_adherence.build_msn_adherence_block(
        matrix_store_groups=groups,
        order_skus=order_skus,
        p1_msn_raw=p1,
        p1_api_configured=True,
    )
    assert block["status"] == "connected"
    sku = block["stores"][0]["skus"][0]
    assert sku["sku_sub_grade"] == "B"
    assert sku["status_label"] == "Can fulfil · below MSN"
    assert block["stores"][0]["rollup_tone"] == "warn"


def test_hypothesis_seeds_include_can_fulfil_below_msn():
    from app.agents.order_rca.llm_context import build_hypothesis_seeds

    facts = {
        "preflight": {"allocation_badge": "CROSS", "actual_vendor_code": "1MG_KOL_05"},
        "p1": {
            "msn_adherence": {
                "status": "connected",
                "stores": [
                    {
                        "physical_store": "1MG_ROF",
                        "skus": [
                            {
                                "sku_id": "133187",
                                "name": "Montair-LC",
                                "status_code": "can_fulfil_below_msn",
                                "status_label": "Can fulfil · below MSN",
                                "effective_msn": 12,
                                "on_shelf_qty": 8,
                                "asked_qty": 2,
                            }
                        ],
                    }
                ],
            }
        },
        "p4": {"rows": []},
        "operations": {"status_transitions": [], "groot_empty": True},
        "skus": [],
    }
    ids = [s["id"] for s in build_hypothesis_seeds(facts)]
    assert any("msn_1MG_ROF" in i for i in ids)


def test_msn_llm_summary_when_p1_fields_present():
    block = msn_adherence.build_msn_adherence_block(
        matrix_store_groups=[
            {
                "physical_store": "1MG_ROF",
                "distance_km": 1.85,
                "store_kind": "retail",
                "vendor_type": "RETAIL",
            }
        ],
        order_skus=[{"sku_id": "133187", "name": "Montair-LC", "quantity": 2}],
        p1_msn_raw={
            "stores": {
                "1MG_ROF": {
                    "skus": {"133187": {"effective_msn": 12, "on_shelf_qty": 8, "sku_sub_grade": "B"}}
                }
            }
        },
        p1_api_configured=True,
    )
    summary = msn_adherence.build_msn_adherence_llm_summary(block)
    assert summary is not None
    assert summary["below_msn_count"] >= 1
    assert summary["notable_lines"][0]["status"] == "Can fulfil · below MSN"


def test_msn_llm_summary_none_without_p1_fields():
    block = msn_adherence.build_msn_adherence_block(
        matrix_store_groups=[{"physical_store": "1MG_ROF", "distance_km": 1.0, "store_kind": "retail"}],
        order_skus=[{"sku_id": "1", "name": "A", "quantity": 1}],
        p1_msn_raw={},
        p1_api_configured=False,
    )
    assert msn_adherence.build_msn_adherence_llm_summary(block) is None


@pytest.mark.asyncio
async def test_llm_context_includes_msn_summary_when_connected():
    from app.agents.order_rca.llm_context import build_llm_context

    b = await sources._fetch_fixtures(CHILD)
    facts = rules.build_facts(b)
    facts["p1"]["msn_adherence"] = msn_adherence.build_msn_adherence_block(
        matrix_store_groups=msn_adherence.collect_matrix_physical_stores(facts["p3"]),
        order_skus=facts["skus"],
        p1_msn_raw={
            "stores": {
                facts["p3"]["nearby_stores"][0]["physical_store"]: {
                    "skus": {
                        str(facts["skus"][0]["sku_id"]): {
                            "effective_msn": 10,
                            "on_shelf_qty": 5,
                            "sku_sub_grade": "A",
                        }
                    }
                }
            }
        },
        p1_api_configured=True,
    )
    ctx = build_llm_context(facts)
    assert "msn_adherence_summary" in ctx
    assert ctx["msn_adherence_summary"]["notable_lines"]
