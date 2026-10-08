"""Tests for pre-order soft allocation cart journey (#19)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agents.order_rca import rules, soft_allocation, sources

IST = ZoneInfo("Asia/Kolkata")
CHILD = "PO13326295207344"


def _snap(
    *,
    cart_id: str,
    ts: str,
    skus: dict[str, int],
    single: bool = False,
    multi: bool = False,
) -> dict:
    shipments: dict = {}
    if single:
        shipments["single"] = [
            [
                {
                    "title": "By Today, 11 PM",
                    "eta_to": "14 May 2026 23:00:00 IST",
                    "sku_ids": list(skus.keys()),
                    "service_name": "zero_day_delivery",
                },
                {
                    "title": "By Tomorrow, 3 PM",
                    "eta_to": "15 May 2026 15:00:00 IST",
                    "sku_ids": list(skus.keys()),
                },
            ]
        ]
    if multi:
        keys = list(skus.keys())
        g1 = keys[:2] if len(keys) >= 2 else keys
        g2 = keys[2:] if len(keys) > 2 else []
        shipments["multi"] = []
        if g1:
            shipments["multi"].append(
                [
                    {
                        "title": "By Today, 07 PM",
                        "eta_to": "14 May 2026 19:00:00 IST",
                        "sku_ids": g1,
                    }
                ]
            )
        if g2:
            shipments["multi"].append(
                [
                    {
                        "title": "Saturday, 17 May",
                        "eta_to": "17 May 2026 22:00:00 IST",
                        "sku_ids": g2,
                    }
                ]
            )
    return {
        "cart_id": cart_id,
        "cart_timestamp": ts,
        "skus_info": {k: {"quantity": v, "name": k} for k, v in skus.items()},
        "shipments": shipments,
    }


@pytest.mark.parametrize(
    "raw,expected_hour",
    [
        ("14 May 2026 13:41:41 IST", 13),
        (1778746301, 13),
    ],
)
def test_parse_cart_timestamp_ist_and_unix(raw, expected_hour):
    dt = soft_allocation.parse_cart_timestamp(raw)
    assert dt is not None
    local = dt.astimezone(IST)
    assert local.hour == expected_hour


def test_parse_cart_timestamp_null():
    assert soft_allocation.parse_cart_timestamp(None) is None
    assert soft_allocation.parse_cart_timestamp("") is None


def test_pick_cart_id_sku_overlap_not_latest_wrong_cart():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    order_skus = {"1122085": 2.0}
    snaps = [
        _snap(cart_id="383687611", ts="14 May 2026 13:40:00 IST", skus={"1122085": 2}, multi=True),
        _snap(cart_id="388645139", ts="14 May 2026 13:40:30 IST", skus={"964144": 2}, single=True),
        _snap(cart_id="388645139", ts="15 May 2026 10:00:00 IST", skus={"964144": 2}, single=True),
    ]
    assert soft_allocation.pick_cart_id(snaps, placed_at=placed, order_skus=order_skus) == "383687611"


def test_pick_cart_id_ignores_missing_cart_id():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    order_skus = {"1122085": 2.0}
    snap = _snap(cart_id="383687611", ts="14 May 2026 13:40:00 IST", skus={"1122085": 2}, multi=True)
    snap["cart_id"] = None
    assert soft_allocation.pick_cart_id([snap], placed_at=placed, order_skus=order_skus) is None


def test_time_gate_excludes_after_place():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    order_skus = {"1122085": 2.0}
    snaps = [
        _snap(cart_id="388645139", ts="17 June 2026 18:45:57 IST", skus={"964144": 2}, single=True),
    ]
    assert soft_allocation.pick_cart_id(snaps, placed_at=placed, order_skus=order_skus) is None


def test_journey_dedupes_identical_steps():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    snaps = [
        _snap(cart_id="1", ts="14 May 2026 11:00:00 IST", skus={"1122085": 1}, multi=True),
        _snap(cart_id="1", ts="14 May 2026 11:30:00 IST", skus={"1122085": 1}, multi=True),
        _snap(
            cart_id="1",
            ts="14 May 2026 13:40:00 IST",
            skus={"1122085": 2},
            multi=True,
        ),
    ]
    steps = soft_allocation.build_journey_steps(snaps, cart_id="1", placed_at=placed, sku_names={"1122085": "Farmina"})
    assert len(steps) == 2
    assert steps[1].get("sku_delta")


def test_layout_flip_single_multi_detected():
    placed = datetime(2026, 6, 17, 18, 50, 0, tzinfo=IST)
    snaps = [
        _snap(cart_id="383687611", ts="17 June 2026 18:45:23 IST", skus={"1132600": 1, "964144": 1}, multi=True),
        _snap(cart_id="383687611", ts="17 June 2026 18:45:27 IST", skus={"964144": 1}, single=True),
    ]
    steps = soft_allocation.build_journey_steps(
        snaps, cart_id="383687611", placed_at=placed, sku_names={"964144": "Apoquel"}
    )
    assert any("Split shipment" in (s.get("shipment_delta") or "") for s in steps)


@pytest.mark.asyncio
async def test_build_facts_includes_cart_journey_fixture_child():
    bundle = await sources._fetch_fixtures(CHILD)
    facts = rules.build_facts(bundle)
    journey = facts.get("cart_allocation_journey") or {}
    assert journey.get("available") is True
    assert journey.get("empty") is False
    assert journey.get("cart_id") == "383687611"
    assert len(journey.get("steps") or []) >= 1
    assert journey.get("headline")
    assert len(journey.get("insights") or []) >= 1
    assert journey.get("cart_vs_order") is not None


@pytest.mark.asyncio
async def test_build_facts_empty_without_soft_alloc_payload():
    bundle = await sources._fetch_fixtures(CHILD)
    bundle["soft_allocation"] = {}
    facts = rules.build_facts(bundle)
    journey = facts.get("cart_allocation_journey") or {}
    assert journey.get("empty") is True


def test_build_cart_allocation_journey_no_place_time():
    out = soft_allocation.build_cart_allocation_journey({}, {"data": {"response": []}})
    assert out.get("empty_reason") == "no_place_time"


def test_journey_steps_include_all_options():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    snaps = [
        _snap(cart_id="1", ts="14 May 2026 13:40:00 IST", skus={"1122085": 2}, single=True),
    ]
    steps = soft_allocation.build_journey_steps(snaps, cart_id="1", placed_at=placed, sku_names={"1122085": "Farmina"})
    assert len(steps) == 1
    groups = steps[0].get("shipments") or []
    assert groups
    opts = groups[0].get("options") or []
    assert len(opts) == 2
    assert opts[0].get("is_fastest") is True


def test_narrative_headline_and_insights():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    snaps = [
        _snap(cart_id="1", ts="14 May 2026 11:00:00 IST", skus={"1122085": 1}, multi=True),
        _snap(cart_id="1", ts="14 May 2026 13:40:00 IST", skus={"1122085": 2}, single=True),
    ]
    journey = soft_allocation.build_cart_allocation_journey(
        {
            "created": int(placed.timestamp()),
            "order_lines": [{"sku": {"sku_id": "1122085", "name": "Farmina"}, "quantity": 2}],
        },
        {"data": {"response": snaps}},
    )
    assert journey.get("cart_id") == "1"
    assert journey.get("headline")
    assert isinstance(journey.get("insights"), list)
    assert journey.get("cart_vs_order") is not None


def test_options_delta_eta_title_change_as_changed():
    prev = [
        {
            "title": "By Today, 07 PM",
            "eta_to": "14 May 2026 19:00:00 IST",
            "sku_ids": ["1122085", "964144"],
            "service_name": "zero_day_delivery",
        },
        {
            "title": "Between 17 - 19 June",
            "eta_to": "19 May 2026 23:59:00 IST",
            "sku_ids": ["1122085", "964144"],
            "service_name": "standard",
        },
    ]
    cur = [
        {
            "title": "By Today, 11 PM",
            "eta_to": "14 May 2026 23:00:00 IST",
            "sku_ids": ["1122085", "964144"],
            "service_name": "zero_day_delivery",
        },
        {
            "title": "Between 18 - 19 June",
            "eta_to": "19 May 2026 23:59:00 IST",
            "sku_ids": ["1122085", "964144"],
            "service_name": "standard",
        },
    ]
    view = soft_allocation._group_view(cur, 0, names={}, prev_group=prev)
    delta = view.get("options_delta") or {}
    assert not delta.get("added")
    assert not delta.get("removed")
    assert len(delta.get("changed") or []) == 2
    assert any("07 PM → By Today, 11 PM" in c for c in delta["changed"])


def test_options_delta_same_title_eta_only():
    prev = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 19:00:00 IST", "sku_ids": ["1"]},
        {"title": "By Tomorrow, 3 PM", "eta_to": "15 May 2026 15:00:00 IST", "sku_ids": ["1"]},
    ]
    cur = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 23:00:00 IST", "sku_ids": ["1"]},
        {"title": "By Tomorrow, 3 PM", "eta_to": "15 May 2026 15:00:00 IST", "sku_ids": ["1"]},
    ]
    view = soft_allocation._group_view(cur, 0, names={}, prev_group=prev)
    delta = view.get("options_delta") or {}
    assert not delta.get("added")
    assert not delta.get("removed")
    assert delta.get("changed") or delta.get("eta_changed")


@pytest.mark.asyncio
async def test_fixture_eta_step_uses_changed_not_add_remove():
    bundle = await sources._fetch_fixtures(CHILD)
    facts = rules.build_facts(bundle)
    steps = (facts.get("cart_allocation_journey") or {}).get("steps") or []
    assert len(steps) >= 5
    eta_step = steps[-1]
    assert eta_step.get("step_kind") in ("fastest_eta", "shipment_options", "options")
    g1 = (eta_step.get("shipments") or [])[0]
    delta = g1.get("options_delta") or {}
    assert not delta.get("added")
    assert not delta.get("removed")
    assert len(delta.get("changed") or []) >= 1


@pytest.mark.asyncio
async def test_enriched_fixture_shows_full_journey_story():
    bundle = await sources._fetch_fixtures(CHILD)
    journey = rules.build_facts(bundle).get("cart_allocation_journey") or {}
    labels = [s.get("step_label") for s in journey.get("steps") or []]
    assert "Cart snapshot" in labels
    assert "SKU edit" in labels
    assert "Layout change" in labels
    assert journey.get("cart_vs_order", {}).get("match") is True


def test_sku_delta_parts_added_removed_qty():
    names = {"964144": "Apoquel", "1122085": "Farmina", "982058": "Gardasil"}
    label, items = soft_allocation._sku_delta_parts(
        {"1122085": 1.0, "982058": 1.0},
        {"1122085": 2.0, "964144": 1.0},
        names,
    )
    assert "Added Apoquel" in (label or "")
    assert "Removed Gardasil" in (label or "")
    assert "Farmina qty 1→2" in (label or "")
    kinds = {i["kind"] for i in items}
    assert kinds == {"added", "removed", "qty"}


def test_option_count_reduced_shows_removed():
    prev = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 23:00:00 IST", "sku_ids": ["1"], "service_name": "fast"},
        {"title": "By Tomorrow, 3 PM", "eta_to": "15 May 2026 15:00:00 IST", "sku_ids": ["1"], "service_name": "std"},
        {"title": "Saturday", "eta_to": "17 May 2026 22:00:00 IST", "sku_ids": ["1"], "service_name": "slow"},
    ]
    cur = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 23:00:00 IST", "sku_ids": ["1"], "service_name": "fast"},
        {"title": "By Tomorrow, 3 PM", "eta_to": "15 May 2026 15:00:00 IST", "sku_ids": ["1"], "service_name": "std"},
    ]
    view = soft_allocation._group_view(cur, 0, names={}, prev_group=prev)
    delta = view.get("options_delta") or {}
    assert delta.get("removed") == ["Saturday"]
    assert not delta.get("added")


def test_option_count_increased_shows_new():
    prev = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 23:00:00 IST", "sku_ids": ["1"], "service_name": "fast"},
    ]
    cur = [
        {"title": "By Today, 11 PM", "eta_to": "14 May 2026 23:00:00 IST", "sku_ids": ["1"], "service_name": "fast"},
        {"title": "By Tomorrow, 3 PM", "eta_to": "15 May 2026 15:00:00 IST", "sku_ids": ["1"], "service_name": "std"},
    ]
    view = soft_allocation._group_view(cur, 0, names={}, prev_group=prev)
    delta = view.get("options_delta") or {}
    assert delta.get("added") == ["By Tomorrow, 3 PM"]
    assert not delta.get("removed")


def test_qty_change_step_has_summary_and_changes():
    placed = datetime(2026, 5, 14, 13, 41, 41, tzinfo=IST)
    snaps = [
        _snap(cart_id="1", ts="14 May 2026 11:00:00 IST", skus={"1122085": 1}, single=True),
        _snap(cart_id="1", ts="14 May 2026 13:40:00 IST", skus={"1122085": 2}, single=True),
    ]
    steps = soft_allocation.build_journey_steps(
        snaps, cart_id="1", placed_at=placed, sku_names={"1122085": "Farmina"}
    )
    assert len(steps) == 2
    edit = steps[1]
    assert edit.get("step_label") == "SKU edit"
    assert edit.get("sku_summary")
    assert edit.get("sku_changes")
    assert any(c.get("kind") == "qty" for c in edit["sku_changes"])


def test_removed_shipment_group_surfaces():
    placed = datetime(2026, 6, 17, 18, 50, 0, tzinfo=IST)
    snaps = [
        _snap(
            cart_id="1",
            ts="17 June 2026 18:45:23 IST",
            skus={"1132600": 1, "964144": 1, "982058": 1},
            multi=True,
        ),
        _snap(cart_id="1", ts="17 June 2026 18:45:27 IST", skus={"964144": 1}, single=True),
    ]
    steps = soft_allocation.build_journey_steps(
        snaps,
        cart_id="1",
        placed_at=placed,
        sku_names={"964144": "Apoquel", "1132600": "At home", "982058": "Gardasil"},
    )
    layout_step = steps[-1]
    groups = layout_step.get("shipments") or []
    assert any(g.get("group_status") == "removed" for g in groups)
    removed = next(g for g in groups if g.get("group_status") == "removed")
    assert removed.get("options_delta", {}).get("removed")


def test_cart_vs_order_no_fulfilment_skus_not_false_match():
    names = {"1122085": "Farmina"}
    out = soft_allocation._human_cart_vs_order({"1122085": 1.0}, {}, names)
    assert out.get("match") is False
    assert "no fulfilment skus" in (out.get("detail") or "").lower()


def test_qty_equivalent_pack_vs_base():
    order_line = {"quantity": 20, "sku": {"sku_id": "964144", "units_in_pack": 10}}
    assert soft_allocation._qty_equivalent(2.0, 2.0, order_line)
    assert soft_allocation._qty_equivalent(20.0, 2.0, order_line)
    names = {"964144": "Apoquel", "1122085": "Farmina"}
    out = soft_allocation._human_cart_vs_order(
        {"964144": 1.0, "1122085": 2.0},
        {"1122085": 2.0},
        names,
    )
    assert out.get("match") is False
    assert "Cart still showed" in (out.get("detail") or "")
    assert "Apoquel" in (out.get("detail") or "")
    assert "extra on cart" not in (out.get("detail") or "").lower()


def test_post_order_removal_mrp_history_is_not_high_confidence():
    """MRP churn must not elevate a missing SKU to high-confidence removal."""
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                {
                    "cart_id": "c1",
                    "cart_timestamp": "14 May 2026 11:50:00 IST",
                    "skus_info": {
                        "1122085": {"name": "Farmina", "quantity": 1},
                        "982058": {"name": "Gardasil", "quantity": 1},
                    },
                    "shipments": {
                        "single": [[{"title": "By Today", "skus": ["1122085", "982058"]}]]
                    },
                    "selected_vendors": {},
                    "static_data": {},
                }
            ]
        }
    }
    history = {
        "history": [
            {
                "created": int(placed.timestamp()) + 60,
                "comment": (
                    "Current Price/MRP updated from 1 to 2, Discounted Price/MRP "
                    "changed from 1 to 2 for sku: Gardasil"
                ),
            }
        ]
    }
    out = soft_allocation.build_post_order_sku_removals(order, soft, history)
    assert out["count"] == 0
    assert any(a["sku_id"] == "982058" for a in out["ambiguous_not_on_order"])


def test_post_order_removal_high_confidence_on_deleted_sku_history():
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 1, "789719": 1, "331910": 1},
                    single=True,
                )
            ]
        }
    }
    history = {
        "history": [
            {
                "created": int(placed.timestamp()) + 60,
                "comment": (
                    "Deleted sku '8X Shampoo[789719]' with comment "
                    "'all e consult retry exhausted' and reason "
                    "'removed due to e consult failure'"
                ),
            },
            {
                "created": int(placed.timestamp()) + 60,
                "comment": (
                    "Deleted sku 'Lac Soft C Gel[331910]' with comment "
                    "'all e consult retry exhausted' and reason "
                    "'removed due to e consult failure'"
                ),
            },
        ]
    }
    # Soft snapshot names optional — match primarily by sku_id from Deleted sku history.
    out = soft_allocation.build_post_order_sku_removals(order, soft, history)
    assert out["available"] is True
    assert out["count"] == 2
    by_id = {i["sku_id"]: i for i in out["items"]}
    assert by_id["789719"]["qty_after"] == 0
    assert by_id["789719"]["confidence"] == "high"
    assert by_id["789719"]["evidence"] == "history_deleted_sku"
    assert by_id["331910"]["evidence"] == "history_deleted_sku"


def test_post_order_split_history_skips_moved_sku():
    """Split order request history — not a customer delete / not ambiguous."""
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 1, "982058": 1},
                    single=True,
                )
            ]
        }
    }
    # soft names: ensure Gardasil name is known for split skip matching
    soft["data"]["response"][0]["skus_info"]["982058"]["name"] = "Gardasil"
    history = {
        "history": [
            {
                "created": int(placed.timestamp()) + 60,
                "comment": (
                    "Split order request with skus: "
                    "['Gardasil(quantity = 1)']"
                ),
            }
        ]
    }
    out = soft_allocation.build_post_order_sku_removals(order, soft, history)
    assert out["count"] == 0
    assert not any(a["sku_id"] == "982058" for a in out["ambiguous_not_on_order"])


def test_post_order_removal_ambiguous_without_history():
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 1, "982058": 1},
                    single=True,
                )
            ]
        }
    }
    out = soft_allocation.build_post_order_sku_removals(order, soft, {"history": []})
    assert out["count"] == 0
    assert any(a["sku_id"] == "982058" for a in out["ambiguous_not_on_order"])


def test_post_order_qty_reduction():
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 3},
                    single=True,
                )
            ]
        }
    }
    out = soft_allocation.build_post_order_sku_removals(order, soft, {"history": []})
    assert out["count"] == 1
    assert out["items"][0]["sku_id"] == "1122085"
    assert out["items"][0]["qty_before"] == 3
    assert out["items"][0]["qty_after"] == 1
    assert out["items"][0]["evidence"] == "cart_vs_order_qty"


def test_post_order_removals_never_breaks_on_bad_payload():
    out = soft_allocation.build_post_order_sku_removals(
        {"created": "bad"},
        {"data": "nope"},
        None,
    )
    assert out["count"] == 0
    assert "items" in out


def test_post_order_sku_on_sibling_is_not_removal():
    """Holistic cart: SKU moved to sibling child PO is still on the family."""
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "order_id": "PO_CHILD_A",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    sibling = {
        "order_id": "PO_CHILD_B",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "982058", "name": "Gardasil", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 1, "982058": 1},
                    single=True,
                )
            ]
        }
    }
    out = soft_allocation.build_post_order_sku_removals(
        order,
        soft,
        {"history": []},
        family_orders=[order, sibling],
    )
    assert out["count"] == 0
    assert not any(a["sku_id"] == "982058" for a in out["ambiguous_not_on_order"])


def test_post_order_sku_on_parent_presence_only_not_removal():
    """Fixture-style: family=current only; parent_order used for presence, not qty sum."""
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "order_id": "PO_CHILD",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    parent = {
        "order_id": "PO_PARENT",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "982058", "name": "Gardasil", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 1, "982058": 1},
                    single=True,
                )
            ]
        }
    }
    out = soft_allocation.build_post_order_sku_removals(
        order,
        soft,
        {"history": []},
        parent_order=parent,
        family_orders=[order],
    )
    assert out["count"] == 0
    assert not any(a["sku_id"] == "982058" for a in out["ambiguous_not_on_order"])


def test_post_order_qty_split_across_family_is_not_reduction():
    placed = datetime(2026, 5, 14, 12, 0, 0, tzinfo=IST)
    order = {
        "order_id": "PO_A",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 1,
                "normalized_quantity": 1,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    sibling = {
        "order_id": "PO_B",
        "created": int(placed.timestamp()),
        "order_lines": [
            {
                "quantity": 2,
                "normalized_quantity": 2,
                "sku": {"sku_id": "1122085", "name": "Farmina", "units_in_pack": 1},
            }
        ],
    }
    soft = {
        "data": {
            "response": [
                _snap(
                    cart_id="c1",
                    ts="14 May 2026 11:50:00 IST",
                    skus={"1122085": 3},
                    single=True,
                )
            ]
        }
    }
    out = soft_allocation.build_post_order_sku_removals(
        order,
        soft,
        {"history": []},
        family_orders=[order, sibling],
    )
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_resolve_family_orders_hydrates_sibling_and_failsoft():
    current = {
        "order_id": "PO_PARENT",
        "order_lines": [
            {"quantity": 1, "sku": {"sku_id": "1", "name": "A", "units_in_pack": 1}},
        ],
    }
    sibling_full = {
        "order_id": "PO_CHILD",
        "parent_id": "PO_PARENT",
        "order_lines": [
            {"quantity": 1, "sku": {"sku_id": "2", "name": "B", "units_in_pack": 1}},
        ],
    }

    class _Client:
        async def post(self, *a, **k):
            raise AssertionError("unused")

    async def fake_post_json(client, url, **kw):
        assert "/search" in url
        body = kw.get("json") or {}
        assert body.get("page_size") == sources._FAMILY_SEARCH_PAGE_SIZE
        assert body.get("page_number") == 1
        return {
            "order_details": [
                {"order_id": "PO_PARENT"},
                {"order_id": "PO_CHILD", "parent_id": "PO_PARENT"},  # no lines
            ]
        }

    async def fake_get_json(client, url, **kw):
        assert "PO_CHILD" in url
        return sibling_full

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sources, "_post_json", fake_post_json)
    monkey.setattr(sources, "_get_json", fake_get_json)
    monkey.setattr(sources, "_order_headers", lambda: {})
    try:
        family = await sources._resolve_family_orders(
            _Client(),  # type: ignore[arg-type]
            "https://order.example",
            order=current,
            parent_order=None,
            parent_id=None,
        )
        ids = {str(o.get("order_id")) for o in family}
        assert ids == {"PO_PARENT", "PO_CHILD"}
        child = next(o for o in family if o["order_id"] == "PO_CHILD")
        assert child["order_lines"]

        async def boom_post(*a, **k):
            raise RuntimeError("search down")

        monkey.setattr(sources, "_post_json", boom_post)
        family2 = await sources._resolve_family_orders(
            _Client(),  # type: ignore[arg-type]
            "https://order.example",
            order=current,
            parent_order=None,
            parent_id=None,
        )
        assert [o["order_id"] for o in family2] == ["PO_PARENT"]
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_search_family_order_rows_paginates_until_exhausted():
    """page_size=10, max 5 pages; keep paging while full pages (or via total_pages)."""
    calls: list[int] = []

    async def fake_post_json(client, url, **kw):
        body = kw.get("json") or {}
        page = int(body["page_number"])
        calls.append(page)
        assert body["page_size"] == 10
        assert body["queue_name"] == "search"
        if page == 1:
            return {
                "total_pages": 2,
                "order_details": [{"order_id": f"PO_{i}"} for i in range(10)],
            }
        if page == 2:
            return {
                "total_pages": 2,
                "order_details": [{"order_id": "PO_10"}, {"order_id": "PO_11"}],
            }
        raise AssertionError(f"unexpected page {page}")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sources, "_post_json", fake_post_json)
    monkey.setattr(sources, "_order_headers", lambda: {})
    try:
        rows = await sources._search_family_order_rows(
            object(),  # type: ignore[arg-type]
            "https://order.example",
            "PO_PARENT",
        )
        assert calls == [1, 2]
        assert len(rows) == 12
        assert rows[-1]["order_id"] == "PO_11"
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_search_family_respects_max_pages_cap():
    calls: list[int] = []

    async def fake_post_json(client, url, **kw):
        page = int((kw.get("json") or {})["page_number"])
        calls.append(page)
        # Always full page, no total_pages → hit hard cap.
        return {"order_details": [{"order_id": f"PO_{page}_{i}"} for i in range(10)]}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sources, "_post_json", fake_post_json)
    monkey.setattr(sources, "_order_headers", lambda: {})
    try:
        rows = await sources._search_family_order_rows(
            object(),  # type: ignore[arg-type]
            "https://order.example",
            "PO_PARENT",
        )
        assert calls == [1, 2, 3, 4, 5]
        assert len(rows) == 50
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_search_family_stops_on_short_page_without_total_pages():
    calls: list[int] = []

    async def fake_post_json(client, url, **kw):
        page = int((kw.get("json") or {})["page_number"])
        calls.append(page)
        return {"order_details": [{"order_id": "PO_ONLY"}]}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sources, "_post_json", fake_post_json)
    monkey.setattr(sources, "_order_headers", lambda: {})
    try:
        rows = await sources._search_family_order_rows(
            object(),  # type: ignore[arg-type]
            "https://order.example",
            "PO_PARENT",
        )
        assert calls == [1]
        assert len(rows) == 1
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_build_facts_includes_post_order_sku_removals_block():
    bundle = await sources._fetch_fixtures(CHILD)
    assert isinstance(bundle.get("family_orders"), list)
    assert bundle["family_orders"]
    facts = rules.build_facts(bundle)
    assert "post_order_sku_removals" in facts
    assert "sku_price_increases" in facts
    assert "sku_price_decreases" in facts
    assert isinstance(facts["post_order_sku_removals"].get("items"), list)
