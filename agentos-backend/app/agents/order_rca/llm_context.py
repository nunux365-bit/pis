"""Lean payload for OpenAI synthesis (full facts remain for UI)."""

from __future__ import annotations

import re
from typing import Any

from app.agents.order_rca import eta_resolution, msn_adherence, rules
from app.agents.order_rca.redact import redact_free_text
from app.agents.order_rca.time_utils import format_duration_minutes

_SKU_SIGNAL_RE = re.compile(r"^SKU\s+(\S+)\s*:\s*(.+)$", re.IGNORECASE)
_MAX_PRODUCT_NAME_LEN = 120

# Per–virtual-store rejection detail lines (inventory SKU hints, service windows, etc.)
_VIRTUAL_STORE_SIGNALS_MAX = 6


def _sku_for_llm(sku: dict[str, Any]) -> dict[str, Any]:
    """Product lines for narrative — names are required for human-readable synthesis."""
    name = rules._s(sku.get("name")).strip()
    return {
        "name": name or None,
        "quantity": sku.get("quantity"),
    }


def _sku_name_by_id(facts: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sku in facts.get("skus") or []:
        if not isinstance(sku, dict):
            continue
        sid = rules._s(sku.get("sku_id"))
        name = rules._s(sku.get("name")).strip()
        if sid and name:
            out[sid] = name if len(name) <= _MAX_PRODUCT_NAME_LEN else name[: _MAX_PRODUCT_NAME_LEN - 1] + "…"
    return out


def _product_label(sku_id: str, name_by_id: dict[str, str]) -> str:
    name = name_by_id.get(str(sku_id).strip())
    if name:
        return name
    return "Ordered product"


def humanize_signal_for_narrative(signal: str, name_by_id: dict[str, str]) -> str:
    """
    Turn UI/table lines like ``SKU 1122085: Unmapped on vendor`` into narrative-friendly
    ``{product name}: Unmapped on vendor`` (no sku_id in text sent to OpenAI).
    """
    line = (signal or "").strip()
    if not line:
        return line
    m = _SKU_SIGNAL_RE.match(line)
    if m:
        product = _product_label(m.group(1), name_by_id)
        return f"{product}: {m.group(2).strip()}"
    return line


def _humanize_signals(signals: list[Any], name_by_id: dict[str, str]) -> list[str]:
    return [humanize_signal_for_narrative(str(s), name_by_id) for s in signals or [] if str(s).strip()]


def _groot_event_for_llm(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "day": event.get("day"),
        "status": event.get("status"),
        "sub_status": event.get("sub_status"),
        "at": event.get("at"),
        "comments": redact_free_text(event.get("comments")),
    }


def _clickpost_event_for_llm(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": event.get("status"),
        "location": redact_free_text(event.get("location")),
        "at": event.get("at"),
    }


def _signals_for_llm(signals: list[Any], *, name_by_id: dict[str, str]) -> list[str]:
    out: list[str] = []
    for s in signals or []:
        line = str(s)
        if line.startswith("Groot: performers="):
            continue
        out.append(humanize_signal_for_narrative(line, name_by_id))
    return out[:12]


def _promised_delivery_raw_compact(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    compact: dict[str, Any] = {}
    for key, val in raw.items():
        if val is None or val == "" or val == {}:
            continue
        if key == "candidates":
            continue
        compact[key] = val
    timeline = raw.get("eta_timeline")
    if isinstance(timeline, list) and timeline:
        compact["eta_timeline"] = [
            {
                "to": row.get("display"),
                "from": row.get("comms_from_display"),
                "source": row.get("source"),
                "recorded_at": row.get("recorded_at_display"),
            }
            for row in timeline
            if isinstance(row, dict)
        ][:12]
    jumps = raw.get("eta_jumps")
    if isinstance(jumps, list) and jumps:
        compact["eta_jumps"] = [
            {
                "label": row.get("label"),
                "display": row.get("display"),
                "from": row.get("from_display"),
                "recorded_at": row.get("recorded_at_display"),
            }
            for row in jumps
            if isinstance(row, dict)
        ]
    return compact or None


def _store_kind_label(kind: str | None) -> str | None:
    if kind == "retail":
        return "Retail store"
    if kind == "warehouse":
        return "Warehouse"
    return None


def _store_kind_from_vendor(vendor: dict[str, Any] | None) -> str | None:
    if not vendor:
        return None
    return "retail" if rules.is_retail_vendor(vendor) else "warehouse"


def _allocated_store_kind(
    facts: dict[str, Any],
    allocated_row: dict[str, Any] | None,
    allocated_store: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if isinstance(allocated_store, dict) and allocated_store.get("store_kind"):
        kind = str(allocated_store["store_kind"])
    elif allocated_row:
        kind = _store_kind_from_vendor(allocated_row)
    else:
        kinds = facts.get("vendor_types_in_allocation") or []
        if kinds and str(kinds[0]).lower() == "retail":
            kind = "retail"
        elif kinds:
            kind = "warehouse"
        else:
            kind = None
    return kind, _store_kind_label(kind)


def _delivery_sla_for_llm(pf: dict[str, Any]) -> dict[str, Any]:
    """Canonical delivery SLA — shared with extract_order_delivery / Perfect Order."""
    if pf.get("breach_kind") is not None and pf.get("is_eta_breached") is not None:
        return {
            "breach_kind": pf.get("breach_kind"),
            "is_eta_breached": pf.get("is_eta_breached"),
            "breach_minutes": pf.get("breach_minutes"),
            "breach_minutes_display": pf.get("breach_minutes_display"),
            "diagnosed_at": pf.get("diagnosed_at"),
            "delivered": bool(rules._s(pf.get("actual_delivery"))),
            "is_eta_breached_order_api": pf.get("is_eta_breached_order_api"),
        }
    first = pf.get("promised_first") if isinstance(pf.get("promised_first"), dict) else None
    return eta_resolution.compute_delivery_sla(
        promised_first=first,
        actual_delivery=pf.get("actual_delivery"),
        late_minutes=pf.get("late_minutes"),
        is_eta_breached_order_api=pf.get("is_eta_breached_order_api"),
        as_of=None,
    )


def _delivery_eta_for_llm(pf: dict[str, Any]) -> dict[str, Any]:
    """Promised ETA fields for LLM — aligned with first-comms SLA resolution."""
    first = pf.get("promised_first") if isinstance(pf.get("promised_first"), dict) else {}
    current = pf.get("promised_current") if isinstance(pf.get("promised_current"), dict) else {}
    sla = _delivery_sla_for_llm(pf)
    jumps = pf.get("eta_jumps") if isinstance(pf.get("eta_jumps"), list) else []
    compact_jumps = [
        {
            "label": j.get("label"),
            "display": j.get("display"),
            "from": j.get("from_display"),
        }
        for j in jumps
        if isinstance(j, dict) and j.get("display")
    ]
    breach_display = pf.get("breach_minutes_display") or format_duration_minutes(
        pf.get("breach_minutes")
    )
    late_display = pf.get("late_minutes_display") or format_duration_minutes(pf.get("late_minutes"))
    return {
        "sla_anchor": (
            "first customer ETA communication (history |to:|, placement confirmation, or analytics eta_to; "
            "when from and to exist, SLA uses to only). All times IST."
        ),
        "promised_first": first.get("display") or pf.get("promised_delivery"),
        "promised_first_source": first.get("source") or pf.get("promised_delivery_source"),
        "promised_current": current.get("display"),
        "promised_current_source": current.get("source"),
        "promised_first_from": first.get("comms_from_display"),
        "eta_jumps": compact_jumps,
        "late_minutes_vs": pf.get("late_minutes_vs") or "promised_first",
        "late_minutes_display": late_display,
        "breach_minutes_display": breach_display,
        "breach_kind_note": (
            "open_past_promise: overdue not delivered — do not say delivered late; "
            "delivered_late: use breach_minutes_display with actual_delivery; "
            "all vs first promised only. Cite breach_minutes_display in narrative, not raw integers."
        ),
        "breach_kind": sla.get("breach_kind"),
        "is_eta_breached": sla.get("is_eta_breached"),
        "is_eta_breached_order_api": sla.get("is_eta_breached_order_api"),
        "delivered": sla.get("delivered"),
        "diagnosed_at": sla.get("diagnosed_at"),
    }


def _allocated_sla_fields(allocated_row: dict[str, Any] | None) -> dict[str, Any]:
    if not allocated_row:
        return {}
    eta = allocated_row.get("rapid_eta") or allocated_row.get("eta")
    return {
        "standard_available": allocated_row.get("standard_available"),
        "rapid_services": list(allocated_row.get("rapid_services") or []),
        "rapid_eta": eta if isinstance(eta, dict) and eta else None,
    }


def build_perfect_order_llm_summary(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Compact Perfect Order scorecard for LLM synthesis (matches UI pillars)."""
    po = facts.get("perfect_order")
    if not isinstance(po, dict):
        return None
    pillars: list[dict[str, Any]] = []
    for p in po.get("pillars") or []:
        if not isinstance(p, dict):
            continue
        pillars.append(
            {
                "id": p.get("id"),
                "status": p.get("status"),
                "pass": p.get("pass"),
                "label": p.get("label"),
                "detail": p.get("detail"),
                "counts_as_perfect": p.get("counts_as_perfect"),
            }
        )
    failed = rules.rank_failed_pillars(pillars)
    passed = [
        p
        for p in pillars
        if isinstance(p, dict)
        and not p.get("counts_as_perfect")
        and p.get("pass") is True
    ]
    return {
        "overall": po.get("overall"),
        "overall_pass": po.get("overall_pass"),
        "price_hint": po.get("hint"),
        "total_mrp_increase": po.get("total_mrp_increase"),
        "mrp_increase_limit": po.get("mrp_increase_limit"),
        "mrp_increase_source": po.get("mrp_increase_source"),
        "failed_pillars": failed,
        "passed_pillars": passed,
        "pillars": pillars,
    }


def build_hypothesis_seeds(facts: dict[str, Any]) -> list[dict[str, str]]:
    """
    Deterministic hints for LLM hypotheses (not shown in UI).

    All matching seeds are passed to synthesis — no cap — so late ops phases and
    planning gaps are not dropped when many signals exist on one order.
    """
    pf = facts.get("preflight") or {}
    ops = facts.get("operations") or {}
    seeds: list[dict[str, str]] = []
    po = facts.get("perfect_order")
    if isinstance(po, dict) and po.get("overall"):
        inc = po.get("total_mrp_increase")
        cap = po.get("mrp_increase_limit")
        seeds.append(
            {
                "id": "perfect_order",
                "finding_hint": (
                    f"Perfect Order scorecard: {po.get('overall')}"
                    + (
                        f"; total MRP increase vs cart ₹{inc} (cap ₹{cap})"
                        if inc is not None and cap is not None
                        else ""
                    )
                ),
            }
        )
        for pillar in po.get("pillars") or []:
            if not isinstance(pillar, dict) or pillar.get("counts_as_perfect"):
                continue
            if pillar.get("pass") is False:
                pid = str(pillar.get("id") or "").strip() or "pillar"
                seeds.append(
                    {
                        "id": f"perfect_order_{pid}",
                        "finding_hint": (
                            f"Perfect Order — {pillar.get('label')}: {pillar.get('detail')}"
                        ),
                    }
                )
    badge = pf.get("allocation_badge")
    if badge:
        seeds.append({"id": "allocation", "finding_hint": f"Allocation badge: {badge}"})
    reason = pf.get("allocation_badge_reason")
    if reason:
        seeds.append({"id": "allocation_reason", "finding_hint": str(reason)})
    code = pf.get("actual_vendor_code")
    dist = pf.get("distance_km")
    p4_block = facts.get("p4") or {}
    allocated_row = next(
        (r for r in (facts.get("p3") or {}).get("rows") or [] if r.get("outcome") == "allocated"),
        None,
    )
    alloc_kind, alloc_kind_label = _allocated_store_kind(
        facts, allocated_row, p4_block.get("allocated_store")
    )
    if code:
        kind_prefix = f"{alloc_kind_label} " if alloc_kind_label else ""
        seeds.append(
            {
                "id": "allocated_vendor",
                "finding_hint": (
                    f"Allocated to {kind_prefix}{code} at {dist} km"
                    if dist is not None
                    else f"Allocated to {kind_prefix}{code}"
                ),
            }
        )
    store_groups = sorted(
        list(p4_block.get("nearby_stores") or []) + list(p4_block.get("nearby_warehouses") or []),
        key=lambda g: g.get("distance_km") or 9e9,
    )
    if store_groups and code and store_groups[0].get("physical_store") != rules.physical_store_key(code):
        g0 = store_groups[0]
        n_vs = g0.get("virtual_store_count")
        vs_label = f"{n_vs} virtual store{'s' if n_vs != 1 else ''}" if n_vs else "virtual stores"
        g0_kind = _store_kind_label(g0.get("store_kind")) or g0.get("vendor_type")
        parts = [
            f"Nearest rejected {g0_kind} {g0.get('physical_store')} "
            f"at {g0.get('distance_km')} km ({vs_label})"
        ]
        if g0.get("inventory_not_available"):
            parts.append("inventory not available")
        if g0.get("service_not_available"):
            parts.append("service not available")
        if g0.get("location_not_serviceable"):
            parts.append("address not serviceable")
        seeds.append({"id": "nearby_store", "finding_hint": "; ".join(parts)})
    for row in (facts.get("p4") or {}).get("rows") or []:
        if row.get("inventory_not_available"):
            seeds.append(
                {
                    "id": "inventory_rejection",
                    "finding_hint": f"{row.get('vendor_code')} rejected — stock not available",
                }
            )
            break
    for row in (facts.get("p4") or {}).get("rows") or []:
        if row.get("service_not_available"):
            seeds.append(
                {
                    "id": "service_rejection",
                    "finding_hint": f"{row.get('vendor_code')} rejected — no matching delivery window",
                }
            )
            break
    for row in (facts.get("p4") or {}).get("rows") or []:
        if not row.get("implicit_rejection") or row.get("split_enabled") is not False:
            continue
        seeds.append(
            {
                "id": "split_implicit_rejection",
                "finding_hint": (
                    f"{row.get('vendor_code')} not chosen by allocation engine — "
                    "split fulfilment disabled at store"
                ),
            }
        )
        break
    cart_j = facts.get("cart_allocation_journey")
    if isinstance(cart_j, dict) and cart_j.get("available") and not cart_j.get("empty"):
        headline = cart_j.get("headline")
        if isinstance(headline, str) and headline.strip():
            seeds.append(
                {
                    "id": "cart_allocation_journey",
                    "finding_hint": headline.strip(),
                }
            )
        for i, line in enumerate(cart_j.get("insights") or []):
            if isinstance(line, str) and line.strip():
                seeds.append(
                    {
                        "id": f"cart_allocation_insight_{i}",
                        "finding_hint": line.strip(),
                    }
                )
        for step in cart_j.get("steps") or []:
            if not isinstance(step, dict):
                continue
            delta = step.get("shipment_delta")
            if delta:
                seeds.append(
                    {
                        "id": "cart_shipment_delta",
                        "finding_hint": f"Pre-order cart at {step.get('at')}: {delta}",
                    }
                )
                break
    delivery_eta = _delivery_eta_for_llm(pf)
    if pf.get("actual_delivery") and pf.get("order_status"):
        late_part = ""
        if delivery_eta.get("breach_kind") == "delivered_late" and delivery_eta.get("breach_minutes_display"):
            late_part = f"; {delivery_eta['breach_minutes_display']} late vs promised_first"
        elif delivery_eta.get("breach_kind") == "delivered_late" and delivery_eta.get("breach_minutes"):
            late_part = f"; {format_duration_minutes(delivery_eta['breach_minutes'])} late vs promised_first"
        elif delivery_eta.get("breach_kind") == "delivered_on_time":
            late_part = "; on time vs promised_first"
        seeds.append(
            {
                "id": "delivery_outcome",
                "finding_hint": (
                    f"Customer delivery recorded ({pf.get('actual_delivery')}){late_part}; "
                    f"current order status: {pf.get('order_status')}"
                ),
            }
        )
    elif not pf.get("actual_delivery"):
        first_disp = delivery_eta.get("promised_first") or pf.get("promised_delivery")
        kind = delivery_eta.get("breach_kind")
        if kind == "open_past_promise":
            mins_disp = delivery_eta.get("breach_minutes_display") or format_duration_minutes(
                delivery_eta.get("breach_minutes")
            )
            late_bit = f" (~{mins_disp} past first promise)" if mins_disp else ""
            seeds.append(
                {
                    "id": "delivery_past_sla",
                    "finding_hint": (
                        f"Not delivered; first promise was {first_disp}{late_bit} — "
                        "breach_kind open_past_promise (say past promise / overdue, not delivered late)"
                    ),
                }
            )
        elif kind == "pending_within_sla":
            seeds.append(
                {
                    "id": "delivery_pending",
                    "finding_hint": (
                        f"Not delivered yet; still within first promise {first_disp} — "
                        "breach_kind pending_within_sla (do not say breached)"
                    ),
                }
            )
    if pf.get("return_reason"):
        seeds.append(
            {"id": "return_reason", "finding_hint": f"Return/refund context: {pf.get('return_reason')}"}
        )
    if ops.get("return_followed"):
        seeds.append(
            {
                "id": "return_workflow",
                "finding_hint": (
                    "Return/refund milestones after Delivered — post-delivery workflow, not undelivered order"
                ),
            }
        )
    last_mile_mode = ops.get("last_mile_mode") or "none"
    if last_mile_mode == "groot":
        n = len(ops.get("groot_events") or [])
        seeds.append({"id": "groot_timeline", "finding_hint": f"Groot last-mile timeline has {n} events"})
    elif last_mile_mode == "clickpost":
        cp_events = list(ops.get("clickpost_events") or [])
        n = len(cp_events)
        seeds.append({"id": "clickpost_timeline", "finding_hint": f"ClickPost courier timeline has {n} scan buckets"})
        if cp_events:
            oldest, newest = cp_events[-1], cp_events[0]
            seeds.append(
                {
                    "id": "clickpost_span",
                    "finding_hint": (
                        f"Courier scans from {oldest.get('status')} ({oldest.get('at')}) "
                        f"to {newest.get('status')} ({newest.get('at')})"
                    ),
                }
            )
    else:
        seeds.append({"id": "last_mile_empty", "finding_hint": "No Groot or ClickPost last-mile timeline"})
    for tr in ops.get("status_transitions") or []:
        if tr.get("sla_excluded") or tr.get("phase_kind") in ("return", "cancel", "post_delivery"):
            continue
        if tr.get("sla_status") == "late":
            dur = tr.get("duration_display")
            if tr.get("duration_min") is not None:
                dur = format_duration_minutes(tr.get("duration_min")) or dur
            sla = tr.get("default_sla")
            sla_disp = f"{sla} min" if sla not in (None, "", "—") else str(sla)
            seeds.append(
                {
                    "id": f"ops_{tr.get('label', 'phase')}",
                    "finding_hint": (
                        f"{tr.get('label')}: {dur} actual vs {sla_disp} SLA"
                    ),
                }
            )
    msn = ((facts.get("p1") or {}).get("msn_adherence") or {})
    for store in msn.get("stores") or []:
        if not isinstance(store, dict):
            continue
        for sku in store.get("skus") or []:
            if not isinstance(sku, dict):
                continue
            code = sku.get("status_code") or ""
            if code not in msn_adherence.MSN_HYPOTHESIS_STATUS_CODES:
                continue
            seeds.append(
                {
                    "id": f"msn_{store.get('physical_store')}_{sku.get('sku_id')}",
                    "finding_hint": (
                        f"{store.get('physical_store')}: {sku.get('name') or sku.get('sku_id')} — "
                        f"{sku.get('status_label')}"
                    ),
                }
            )
    if msn.get("status") == "pending_api":
        seeds.append(
            {
                "id": "msn_gap",
                "finding_hint": "MSN adherence planning API is not configured (MSN table shows order qty only)",
            }
        )
    split = facts.get("split_insights")
    if split and split.get("insights"):
        seeds.append(
            {
                "id": "split_order",
                "finding_hint": split["insights"][0].get("headline", "Split order"),
            }
        )
    skus = facts.get("skus") or []
    if skus:
        names = [rules._s(s.get("name")) for s in skus if isinstance(s, dict) and rules._s(s.get("name"))]
        if names:
            seeds.append({"id": "order_skus", "finding_hint": f"Order lines: {', '.join(names[:5])}"})
    return seeds


def _rejection_flags(row: dict[str, Any]) -> dict[str, bool]:
    return {
        "location_not_serviceable": bool(row.get("location_not_serviceable")),
        "inventory_not_available": bool(row.get("inventory_not_available")),
        "service_not_available": bool(row.get("service_not_available")),
    }


def build_llm_context(facts: dict[str, Any]) -> dict[str, Any]:
    facts = {**facts, "skus": rules.fulfillment_skus(facts.get("skus") or [])}
    pf = facts.get("preflight") or {}
    ops = facts.get("operations") or {}
    p3 = (facts.get("p3") or {}).get("rows") or []
    p4_block = facts.get("p4") or {}
    store_groups = sorted(
        list(p4_block.get("nearby_stores") or []) + list(p4_block.get("nearby_warehouses") or []),
        key=lambda g: g.get("distance_km") or 9e9,
    )

    name_by_id = _sku_name_by_id(facts)

    def _virtual_store_signals(v: dict[str, Any]) -> list[str]:
        return _humanize_signals(
            (v.get("signals") or [])[:_VIRTUAL_STORE_SIGNALS_MAX],
            name_by_id,
        )

    def _group_ctx(g: dict[str, Any]) -> dict[str, Any]:
        kind = g.get("store_kind")
        return {
            "physical_store": g.get("physical_store"),
            "store_kind": kind,
            "store_kind_label": _store_kind_label(kind),
            "km": g.get("distance_km"),
            "vendor_type": g.get("vendor_type"),
            "virtual_store_count": g.get("virtual_store_count"),
            "rejection": _rejection_flags(g),
            "virtual_stores": [
                {
                    "code": v.get("vendor_code"),
                    "km": v.get("distance_km"),
                    "rejection": _rejection_flags(v),
                    "detail_signals": _virtual_store_signals(v),
                }
                for v in (g.get("virtual_stores") or [])
            ],
        }

    nearest_rejected_stores = [_group_ctx(g) for g in store_groups[:8]]
    nearest_retail_stores = [_group_ctx(g) for g in store_groups if g.get("store_kind") == "retail"][:5]
    allocated_store = p4_block.get("allocated_store")
    notable_rejections = nearest_rejected_stores[:5]

    allocated_row = next((r for r in p3 if r.get("outcome") == "allocated"), None)
    alloc_kind, alloc_kind_label = _allocated_store_kind(facts, allocated_row, allocated_store)
    groot_events = list(ops.get("groot_events") or [])
    clickpost_events = list(ops.get("clickpost_events") or [])
    last_mile_mode = ops.get("last_mile_mode") or "none"
    shipping_summary = ops.get("shipping_summary") if isinstance(ops.get("shipping_summary"), dict) else {}

    preferred_top3 = [
        {
            "rank": p.get("rank"),
            "physical_store": p.get("physical_store"),
            "km": p.get("km"),
            "vendor_type": p.get("vendor_type"),
            "virtual_store_count": p.get("virtual_store_count"),
        }
        for p in (pf.get("preferred_vendors") or [])
        if isinstance(p, dict)
    ]

    msn_block = ((facts.get("p1") or {}).get("msn_adherence") or {})
    msn_summary = msn_adherence.build_msn_adherence_llm_summary(msn_block)

    ctx: dict[str, Any] = {
        "is_split_order": bool(facts.get("parent_id")),
        "order_scope_note": (
            "Split fulfilment — distinguish parent order vs child order in narrative."
            if facts.get("parent_id")
            else "Single order (not a split) — never say parent order or child order; say this order or the order."
        ),
        "order_summary": {
            "placed_at": pf.get("placed_at"),
            "city": pf.get("city"),
            "state": pf.get("state"),
            "pincode": pf.get("pincode"),
            "zone": pf.get("zone"),
            "skus": [_sku_for_llm(s) for s in (facts.get("skus") or []) if isinstance(s, dict)],
            "promised_delivery": pf.get("promised_delivery"),
            "promised_delivery_source": pf.get("promised_delivery_source"),
            "delivery_eta": _delivery_eta_for_llm(pf),
            "promised_delivery_raw": _promised_delivery_raw_compact(pf.get("promised_delivery_raw")),
            "timezone": (facts.get("meta") or {}).get("timezone"),
            "order_status": pf.get("order_status"),
            "order_status_id": pf.get("order_status_id"),
            "actual_delivery": pf.get("actual_delivery"),
            "actual_delivery_source": pf.get("actual_delivery_source"),
            "return_reason": pf.get("return_reason"),
        },
        "allocation_unavailable": facts.get("allocation_unavailable"),
        "allocation_summary": {
            "badge": pf.get("allocation_badge"),
            "allocation_badge_reason": pf.get("allocation_badge_reason"),
            "allocation_tier_label": pf.get("allocation_tier_label"),
            "allocated_vendor_code": pf.get("actual_vendor_code"),
            "allocated_vendor_label": pf.get("actual_vendor"),
            "vendor_type": allocated_row.get("vendor_type") if allocated_row else None,
            "allocated_store_kind": alloc_kind,
            "allocated_store_kind_label": alloc_kind_label,
            "distance_km": pf.get("distance_km"),
            "allocated_to_preferred": pf.get("allocated_to_preferred"),
            "preferred_top3": preferred_top3,
            **_allocated_sla_fields(allocated_row),
        },
        "nearby_context": {
            "store_terminology": {
                "physical_store": "Location code (e.g. 1MG_BRM) — not the store type by itself",
                "virtual_store": "One allocation vendor code at that location (e.g. 1MG_BRM_01)",
                "store_kind_retail": "Retail store — vendor_type RETAIL",
                "store_kind_warehouse": "Warehouse — non-RETAIL vendor_type",
            },
            "allocated": {
                "code": pf.get("actual_vendor_code"),
                "km": pf.get("distance_km"),
                "type": allocated_row.get("vendor_type") if allocated_row else None,
                "store_kind": alloc_kind,
                "store_kind_label": alloc_kind_label,
                **_allocated_sla_fields(allocated_row),
            },
            "nearest_rejected_stores": nearest_rejected_stores,
            "nearest_retail_stores": nearest_retail_stores,
            "notable_rejection_stores": notable_rejections,
            "allocated_store": _group_ctx(allocated_store) if allocated_store else None,
            "rejected_physical_store_count": len(store_groups),
        },
        "rejection_legend": {
            "location_not_serviceable": "Address cannot be served (pincode not mapped and no active delivery window)",
            "inventory_not_available": "Stock cannot fulfil the order or a SKU is missing on that vendor",
            "service_not_available": "No delivery window at order time matching the order speed (see active/inactive columns)",
            "detail_signals_format": (
                "virtual_stores.detail_signals use product names, not sku_id "
                "(e.g. 'Montair-LC Tablet: Unmapped on vendor')"
            ),
            "narrative_sku_rule": (
                "In verdict, primary_cause, contributing_factors, and hypotheses: use product names "
                "from order_summary.skus only — never numeric sku_id or 'SKU 123456' prefixes."
            ),
            "note": "Inactive service entries are schedules, not fulfilment capacity.",
        },
        "copy_glossary": {
            "IDEAL": "Preferred allocation — order went to preferred store or warehouse",
            "CROSS": "Non-preferred allocation — often a farther store was used",
            "Delived": "Stock in depletion pipeline, not on live shelf yet",
        },
        "operations_summary": {
            "is_active_service": (ops.get("ops_sla") or {}).get("is_active_service"),
            "return_followed": ops.get("return_followed"),
            "return_note": ops.get("return_note"),
            "status_transitions": [
                {
                    "label": t.get("label"),
                    "duration_display": t.get("duration_display"),
                    "default_sla": t.get("default_sla"),
                    "sla_status": t.get("sla_status"),
                }
                for t in (ops.get("status_transitions") or [])
                if isinstance(t, dict)
            ],
            "groot_empty": bool(ops.get("groot_empty")),
            "clickpost_empty": bool(ops.get("clickpost_empty")),
            "last_mile_mode": last_mile_mode,
            "fulfillment_path": (
                "hyperlocal"
                if last_mile_mode == "groot"
                else "clickpost"
                if last_mile_mode == "clickpost"
                else "3pl_or_no_groot"
            ),
            "groot_events": [
                _groot_event_for_llm(e) for e in groot_events if isinstance(e, dict)
            ]
            if last_mile_mode == "groot"
            else [],
            "clickpost_events": [
                _clickpost_event_for_llm(e) for e in clickpost_events if isinstance(e, dict)
            ]
            if last_mile_mode == "clickpost"
            else [],
            "shipping_summary": shipping_summary or None,
        },
        "planning_gaps": {
            "p1": (facts.get("p1") or {}).get("status"),
            "p2": (facts.get("p2") or {}).get("status"),
        },
        "signals": _signals_for_llm(facts.get("signals") or [], name_by_id=name_by_id),
        "hypothesis_seeds": build_hypothesis_seeds(facts),
        "warnings": facts.get("warnings") or [],
        "split_insights": facts.get("split_insights"),
        "vendor_types_in_allocation": facts.get("vendor_types_in_allocation") or [],
    }
    if msn_summary:
        ctx["msn_adherence_summary"] = msn_summary
    perfect_order = build_perfect_order_llm_summary(facts)
    if perfect_order:
        ctx["perfect_order_summary"] = perfect_order
    cart_j = facts.get("cart_allocation_journey")
    if isinstance(cart_j, dict) and cart_j.get("available"):
        ctx["cart_allocation_journey"] = {
            "headline": cart_j.get("headline"),
            "insights": cart_j.get("insights") or [],
            "cart_id": cart_j.get("cart_id"),
            "placed_at": cart_j.get("placed_at"),
            "meaningful_changes": cart_j.get("meaningful_changes"),
            "steps": [
                {
                    "at": s.get("at"),
                    "step_label": s.get("step_label"),
                    "layout_label": s.get("layout_label"),
                    "sku_delta": s.get("sku_delta"),
                    "shipment_delta": s.get("shipment_delta"),
                    "shipments": s.get("shipments"),
                }
                for s in (cart_j.get("steps") or [])
                if isinstance(s, dict)
            ],
            "cart_vs_order": cart_j.get("cart_vs_order"),
            "note": cart_j.get("note"),
        }
    return ctx
