"""Deterministic Order RCA rules (API 5 + order semantics)."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.order_rca.constants import (
    CANCEL_STATUS_IDS,
    GROOT_API_NAIVE_TZ_NAME,
    ORDER_RCA_AMBIGUOUS_TIME_FIELDS,
    ORDER_RCA_DISPLAY_TZ_LABEL,
    ORDER_RCA_DISPLAY_TZ_NAME,
    OPS_EARLY_PHASE_LABELS,
    OPS_RETURN_NOTE,
    OPS_SLA_EARLY_MINUTES,
    OPS_SLA_PACKAGING_ACTIVE_MINUTES,
    OPS_SKIP_TRANSITION_IDS,
    PARENT_PRE_PACKAGING,
    POST_DELIVERY_STATUS_IDS,
    RETURN_STATUS_IDS,
    SEGMENT_OFD_TO_DELIVERED,
    SEGMENT_PACKAGING_TO_TBD,
    SEGMENT_TBD_TO_OFD,
    SERVICE_UNAVAILABLE_DETAIL,
    SERVICE_UNAVAILABLE_SIGNAL,
    SPLIT_OPS_BOUNDARY_STATUS,
    STATUS_LABELS,
    STATUS_TRANSITION_LABELS,
    TOP_PHYSICAL_STORES,
    TOP_WAREHOUSE_STORES,
    PERFECT_ORDER_MRP_INCREASE_LIMIT,
    PERFECT_ORDER_PRICE_HINT,
    POST_ALLOCATION_STATUS_IDS,
    PUSHBACK_ALLOCATION_STATUS_IDS,
    SAMPLING_SKU_PRIORITY,
)
from app.agents.order_rca import msn_adherence
from app.agents.order_rca import soft_allocation
from app.agents.order_rca.redact import redact_free_text
from app.agents.order_rca import time_utils as _tu
from app.config.settings import settings

log = logging.getLogger(__name__)

_MISSING = "—"
# Prod order/status APIs use UTC for naive timestamps (ISO and "YYYY-MM-DD HH:MM:SS").
# Ops UI shows India wall clock to match customer comms (history eta_communicated).
_ORDER_RCA_API_TZ = _tu.ORDER_RCA_API_TZ
_ORDER_RCA_DISPLAY_TZ = _tu.ORDER_RCA_DISPLAY_TZ
_CUSTOMER_ETA_DATE_ONLY_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}$")
_CUSTOMER_ETA_COMMS_RE = re.compile(
    r"^\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}(\s+\d{1,2}:\d{2})?$"
)
_DATE_ONLY_FMTS = ("%d %b, %Y",)
_DATETIME_FMTS = ("%d %b, %Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
_GROOT_DATETIME_FMTS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M")
_GROOT_TZ = ZoneInfo(GROOT_API_NAIVE_TZ_NAME)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip()
        return t or None
    return str(v)


def _display(v: Any, fallback: str = _MISSING) -> str:
    x = _s(v)
    return x if x is not None else fallback


def _bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def physical_store_key(vendor_code: str | None) -> str:
    """Group virtual store codes (e.g. 1MG_ROF_02, 1MG_ROF_219) under one physical store (1MG_ROF)."""
    code = (_s(vendor_code) or "").strip()
    if not code:
        return _MISSING
    if "_" not in code:
        return code
    return code.rsplit("_", 1)[0]


def vendor_type_label(vendor_type: str | None) -> str:
    """Display label: RETAIL → Retail; all other API types → Warehouse."""
    return "Retail" if (vendor_type or "").upper() == "RETAIL" else "Warehouse"


def is_retail_vendor(vendor: dict[str, Any]) -> bool:
    return (vendor.get("vendor_type") or "").upper() == "RETAIL"


def is_warehouse_vendor(vendor: dict[str, Any]) -> bool:
    return bool(vendor.get("vendor_type")) and not is_retail_vendor(vendor)


def _virtual_store_fields(v: dict[str, Any]) -> dict[str, str]:
    retail = is_retail_vendor(v)
    return {
        "vendor_type": vendor_type_label(_s(v.get("vendor_type"))),
        "store_kind": "retail" if retail else "warehouse",
    }


_EMPTY_STORE_VIEWS: dict[str, Any] = {
    "nearby_stores": [],
    "nearby_warehouses": [],
    "allocated_store": None,
    "store_groups": [],
}


def _parse_unix_ts(value: Any) -> datetime | None:
    return _tu.parse_unix_ts(value)


def _parse_eta_to_unix(value: Any) -> datetime | None:
    return _tu.parse_eta_to_unix(value)


def _parse_flexible_datetime(value: Any) -> datetime | None:
    """Parse order API datetimes. Naive strings are UTC (matches status-history alignment in prod)."""
    dt, _ = _parse_ts_value(value)
    return dt


def _parse_ts_value(value: Any) -> tuple[datetime | None, bool]:
    """
    Parse a timestamp; return (instant, has_time_of_day).

    Unix / ISO with time → has_time True. Date-only strings → has_time False.
    """
    if value is None:
        return None, True
    if isinstance(value, (int, float)):
        return _parse_unix_ts(value), True
    s = _s(value)
    if not s:
        return None, True
    if s.isdigit():
        return _parse_unix_ts(s), True
    if _CUSTOMER_ETA_DATE_ONLY_RE.match(s):
        for fmt in _DATE_ONLY_FMTS:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=_ORDER_RCA_API_TZ), False
            except ValueError:
                continue
    dt = _parse_iso(s)
    if dt:
        return (
            dt.replace(tzinfo=_ORDER_RCA_API_TZ) if dt.tzinfo is None else dt.astimezone(_ORDER_RCA_API_TZ),
            True,
        )
    for fmt in _DATE_ONLY_FMTS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_ORDER_RCA_API_TZ), False
        except ValueError:
            continue
    for fmt in _DATETIME_FMTS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_ORDER_RCA_API_TZ), True
        except ValueError:
            continue
    return None, True


def _format_date_ist(dt: datetime) -> str:
    return _tu.format_date_ist(dt)


def _format_instant_ist(dt: datetime, *, date_only: bool = False) -> str:
    return _tu.format_instant_ist(dt, date_only=date_only)


def _format_ist_wall(dt: datetime, *, date_only: bool = False) -> str:
    return _tu.format_ist_wall(dt, date_only=date_only)


def format_duration_minutes(minutes: int | float | None, *, suffix: str = "") -> str | None:
    return _tu.format_duration_minutes(minutes, suffix=suffix)


def _format_ts_display(value: Any, *, fallback: str | None = None) -> str:
    """
    Format any order/status API timestamp for UI (IST).

    Naive ISO and ``YYYY-MM-DD HH:MM:SS`` from prod JSON are interpreted as UTC.
    Date-only values (e.g. ``17 May, 2026``) omit time of day.
    ``history eta_communicated`` strings are already customer IST wall clock.
    """
    if value is None or value == "":
        return fallback if fallback is not None else _MISSING
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return fallback if fallback is not None else _MISSING
        if s.upper().endswith(f" {ORDER_RCA_DISPLAY_TZ_LABEL}"):
            return s
        if _CUSTOMER_ETA_DATE_ONLY_RE.match(s):
            return f"{s} {ORDER_RCA_DISPLAY_TZ_LABEL}"
        if _CUSTOMER_ETA_COMMS_RE.match(s):
            return s if s.upper().endswith(ORDER_RCA_DISPLAY_TZ_LABEL) else f"{s} {ORDER_RCA_DISPLAY_TZ_LABEL}"
    dt, has_time = _parse_ts_value(value)
    if dt is not None:
        return _format_instant_ist(dt, date_only=not has_time)
    s = _s(value)
    return s if s else (fallback if fallback is not None else _MISSING)


def _format_delivery_display(
    dt: datetime | None, fallback: str | None = None, *, date_only: bool = False
) -> str | None:
    if dt is not None:
        return _format_instant_ist(dt, date_only=date_only)
    return fallback


def _parse_groot_ts(value: Any) -> datetime | None:
    """
    Groot ``performed_at`` / ``created_at`` (prod): ``DD-MM-YYYY HH:MM:SS`` as IST wall clock only.
    """
    s = _s(value)
    if not s:
        return None
    for fmt in _GROOT_DATETIME_FMTS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_GROOT_TZ)
        except ValueError:
            continue
    return None


def _format_groot_ts_display(value: Any, *, fallback: str | None = None) -> str:
    dt = _parse_groot_ts(value)
    if dt is not None:
        return _format_ist_wall(dt)
    return fallback if fallback is not None else _display(value)


def _timezone_meta() -> dict[str, Any]:
    return {
        "display_tz": ORDER_RCA_DISPLAY_TZ_NAME,
        "display_label": ORDER_RCA_DISPLAY_TZ_LABEL,
        "order_api_naive": "UTC",
        "order_eta_to_unix": "IST wall clock in unix slot (no +5:30 shift on display)",
        "promised_eta_unix": "UTC instant converted to IST",
        "groot_api_naive": GROOT_API_NAIVE_TZ_NAME,
        "ambiguous_fields": list(ORDER_RCA_AMBIGUOUS_TIME_FIELDS),
    }


_RETURN_REASON_RE = re.compile(
    r"return\s+reason[:\s]+(.+?)(?:\||$)",
    re.IGNORECASE,
)
_RVP_REASON_RE = re.compile(
    r"(?:quality|duplicate|damaged|wrong\s+product)[^|]*",
    re.IGNORECASE,
)


def extract_history_insights(history_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Parse return/cancel hints from order history comments (best-effort)."""
    if not history_payload:
        return {}
    items = history_payload.get("history") or history_payload.get("data") or []
    if not isinstance(items, list):
        return {}
    return_reason: str | None = None
    for entry in items:
        if not isinstance(entry, dict):
            continue
        comment = _s(entry.get("comment")) or ""
        if not comment:
            continue
        low = comment.lower()
        if "request_for_return" in low or "return and refund" in low:
            m = _RETURN_REASON_RE.search(comment)
            if m:
                return_reason = m.group(1).strip()
            elif "quality" in low or "duplicate" in low:
                m2 = _RVP_REASON_RE.search(comment)
                if m2:
                    return_reason = m2.group(0).strip()
        if return_reason:
            break
        if "|return_reason|" in low or "return reason" in low:
            parts = comment.split("|")
            for i, part in enumerate(parts):
                if "return_reason" in part.lower() and i + 1 < len(parts):
                    return_reason = parts[i + 1].strip()
                    break
        if return_reason:
            break
    out: dict[str, Any] = {}
    if return_reason:
        out["return_reason"] = return_reason
    return out


def _eta_from_history(history_payload: dict[str, Any] | None, order_id: str | None = None) -> str | None:
    """Earliest ``eta_communicated`` |to: for the PO (display string)."""
    from app.agents.order_rca.eta_resolution import parse_eta_communicated_comment

    if not history_payload:
        return None
    oid = (order_id or "").strip().upper()
    items = history_payload.get("history") or history_payload.get("data") or []
    if not isinstance(items, list):
        return None
    best_created: int | None = None
    best_to: str | None = None
    for entry in items:
        if not isinstance(entry, dict):
            continue
        comment = _s(entry.get("comment")) or ""
        if "eta_communicated" not in comment or "|to:" not in comment:
            continue
        if oid and oid not in comment:
            continue
        _from_val, to_val = parse_eta_communicated_comment(comment)
        if not to_val:
            continue
        created = _num(entry.get("created"))
        created_i = int(created) if created is not None else 0
        if best_created is None or created_i < best_created:
            best_created = created_i
            best_to = to_val
    return best_to


def extract_order_delivery(bundle: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve promised vs actual delivery.

    Promised SLA = first customer communication across order, history, and analytics
    (see ``eta_resolution``). ``late_minutes`` uses that first instant.
    """
    from app.agents.order_rca import eta_resolution

    order = bundle.get("order") or {}
    oid = _s(bundle.get("order_id") or order.get("order_id")) or ""
    history = bundle.get("history") or {}
    ship = order.get("shipment_detail") or {}
    eta_o = order.get("eta") or {}

    candidates = eta_resolution.collect_eta_candidates(
        oid,
        order,
        history,
        bundle.get("analytics"),
    )
    resolved = eta_resolution.resolve_promised_eta(candidates)
    promised_dt = eta_resolution.promised_instant_for_sla(resolved)
    first = resolved.get("promised_first") or {}
    current = resolved.get("promised_current") or {}

    promised_display = first.get("display") if isinstance(first, dict) else None
    promised_source = first.get("source") if isinstance(first, dict) else None
    hist_eta = _eta_from_history(history, oid)

    actual_raw = ship.get("delivery_date")
    actual_dt = _parse_flexible_datetime(actual_raw)
    actual_display = _format_delivery_display(actual_dt, _s(actual_raw))

    late_min: int | None = None
    late_vs: str | None = None
    if promised_dt and actual_dt:
        if actual_dt.tzinfo is None:
            actual_dt = actual_dt.replace(tzinfo=_ORDER_RCA_API_TZ)
        compare_actual = actual_dt.astimezone(_ORDER_RCA_DISPLAY_TZ)
        late_min = max(0, int((compare_actual - promised_dt).total_seconds() // 60))
        late_vs = "promised_first"

    sla = eta_resolution.compute_delivery_sla(
        promised_first=first if isinstance(first, dict) else None,
        actual_delivery=actual_display,
        late_minutes=late_min,
        is_eta_breached_order_api=order.get("is_eta_breached"),
    )
    eta_jumps = eta_resolution.build_eta_jumps(
        promised_first=first if isinstance(first, dict) else None,
        promised_current=current if isinstance(current, dict) else None,
        eta_timeline=resolved.get("eta_timeline"),
        actual_display=actual_display,
    )

    late_display = format_duration_minutes(late_min)

    return {
        "promised_delivery": promised_display,
        "promised_delivery_source": promised_source,
        "promised_first": first if isinstance(first, dict) else None,
        "promised_current": current if isinstance(current, dict) else None,
        "eta_jumps": eta_jumps,
        "promised_delivery_raw": {
            "promised_eta": order.get("promised_eta"),
            "eta_to": eta_o.get("eta_to"),
            "to_date": eta_o.get("to_date"),
            "eta_to_label": order.get("eta_to"),
            "history_eta_communicated": _format_ts_display(hist_eta) if hist_eta else None,
            "history_eta_communicated_raw": hist_eta,
            "promised_delivery_sla_end": promised_display,
            "promised_mode": resolved.get("promised_mode"),
            "eta_timeline": resolved.get("eta_timeline"),
            "candidates": resolved.get("candidates"),
        },
        "actual_delivery": actual_display,
        "actual_delivery_source": "order.shipment_detail.delivery_date" if actual_raw else None,
        "late_minutes": late_min,
        "late_minutes_display": late_display,
        "late_minutes_vs": late_vs,
        "is_eta_breached": sla["is_eta_breached"],
        "is_eta_breached_order_api": sla["is_eta_breached_order_api"],
        "breach_kind": sla["breach_kind"],
        "breach_minutes": sla["breach_minutes"],
        "breach_minutes_display": sla.get("breach_minutes_display"),
        "diagnosed_at": sla["diagnosed_at"],
        "delivered": sla["delivered"],
    }


def _p4_virtual_store_row(
    v: dict[str, Any],
    *,
    order_qty_by_sku: dict[str, float] | None = None,
    placed_at: datetime | None = None,
    order: dict[str, Any] | None = None,
    allocated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rej = analyze_vendor_rejection(v, order_qty_by_sku=order_qty_by_sku)
    loc = location_serviceable_fields(v, selected=False, placed_at=placed_at)
    row: dict[str, Any] = {
        "vendor_code": _display(v.get("vendor_code")),
        "label": vendor_label(v),
        "distance_km": _num(v.get("distance")),
        **_virtual_store_fields(v),
        **loc,
        "inventory_not_available": rej["inventory_not_available"],
        "inventory_partial": rej.get("inventory_partial"),
        "inventory_class": rej.get("inventory_class"),
        "inventory_label": rej.get("inventory_label"),
        "inventory_classes": rej.get("inventory_classes"),
        "inventory_labels": rej.get("inventory_labels"),
        "service_not_available": rej["service_not_available"],
        "signals": rej["signals"],
        "reason_short": rej.get("reason_short"),
        "implicit_rejection": rej.get("implicit_rejection"),
        "split_enabled": rej.get("split_enabled"),
    }
    if order is not None:
        row["advertised_rapid_sla"] = advertised_rapid_sla_rows(
            v,
            order=order,
            active_services=loc.get("active_services"),
            inactive_services=loc.get("inactive_services"),
        )
    return row


def compute_allocation_badge(
    allocated: dict[str, Any] | None,
    vendors_all: list[dict[str, Any]],
    preferred: list[dict[str, Any]],
) -> tuple[str, str]:
    """
    IDEAL if: allocated retail, OR (nearest store is a warehouse AND allocated vendor is in
    preferred top-3 warehouses).
    """
    if not allocated:
        return "CROSS", "No fulfilling store returned from allocation data"
    if is_retail_vendor(allocated):
        return "IDEAL", "Allocated to preferred retail store"
    sorted_v = sorted(vendors_all, key=lambda x: _num(x.get("distance")) or 9e9)
    nearest = sorted_v[0] if sorted_v else None
    pref_codes = {v.get("vendor_code") for v in preferred}
    alloc_code = allocated.get("vendor_code")
    if nearest and is_warehouse_vendor(nearest) and alloc_code in pref_codes:
        return "IDEAL", "Nearest location is a warehouse and order used a preferred warehouse"
    if nearest and is_retail_vendor(nearest):
        return "CROSS", "Nearest store is retail but order shipped from a warehouse"
    return "CROSS", "Warehouse used is outside the preferred top 3, or nearest store is retail"


def _summarize_store_group(phys: str, virtual_store_rows: list[dict[str, Any]]) -> dict[str, Any]:
    nearest = virtual_store_rows[0]
    kind = "retail" if (nearest.get("store_kind") or "") == "retail" else "warehouse"
    return {
        "physical_store": phys,
        "distance_km": nearest.get("distance_km"),
        "vendor_type": nearest.get("vendor_type"),
        "store_kind": kind,
        "virtual_store_count": len(virtual_store_rows),
        "location_not_serviceable": any(r.get("location_not_serviceable") for r in virtual_store_rows),
        "inventory_not_available": any(r.get("inventory_not_available") for r in virtual_store_rows),
        "service_not_available": any(r.get("service_not_available") for r in virtual_store_rows),
        "virtual_stores": virtual_store_rows,
    }


def build_ranked_store_views(
    vendors: list[dict[str, Any]],
    *,
    virtual_store_builder: Any,
    allocated: dict[str, Any] | None = None,
    top_all: int = TOP_PHYSICAL_STORES,
    top_warehouse: int = TOP_WAREHOUSE_STORES,
) -> dict[str, Any]:
    """
    Ranked physical-store panels:
    - top N by distance (all store kinds), excluding allocated physical store
    - top M warehouses by distance not already in the first list, excluding allocated
    - allocated physical store shown separately (caller renders at bottom)
    """
    alloc_phys: str | None = None
    if allocated:
        alloc_phys = physical_store_key(_display(allocated.get("vendor_code")))

    pool = [
        v
        for v in vendors
        if isinstance(v, dict)
        and (not alloc_phys or physical_store_key(_display(v.get("vendor_code"))) != alloc_phys)
    ]
    all_groups = build_physical_store_groups(pool, virtual_store_builder=virtual_store_builder, top=None)
    nearby_stores = all_groups[:top_all]
    in_top = {g["physical_store"] for g in nearby_stores}

    nearby_warehouses: list[dict[str, Any]] = []
    for g in all_groups:
        if g.get("store_kind") != "warehouse":
            continue
        if g["physical_store"] in in_top:
            continue
        nearby_warehouses.append(g)
        if len(nearby_warehouses) >= top_warehouse:
            break

    allocated_store: dict[str, Any] | None = None
    if allocated and alloc_phys:
        alloc_vendors = [
            v
            for v in vendors
            if isinstance(v, dict) and physical_store_key(_display(v.get("vendor_code"))) == alloc_phys
        ]
        if alloc_vendors:
            groups = build_physical_store_groups(
                alloc_vendors, virtual_store_builder=virtual_store_builder, top=1
            )
            if groups:
                allocated_store = {**groups[0], "is_allocated": True}

    store_groups = nearby_stores + nearby_warehouses
    return {
        "nearby_stores": nearby_stores,
        "nearby_warehouses": nearby_warehouses,
        "allocated_store": allocated_store,
        "store_groups": store_groups,
    }


def build_physical_store_groups(
    vendors: list[dict[str, Any]],
    *,
    virtual_store_builder: Any,
    top: int = TOP_PHYSICAL_STORES,
    ensure_physical: str | None = None,
) -> list[dict[str, Any]]:
    """Group vendors by physical store (rsplit '_', 1); return nearest `top` stores."""
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in vendors:
        if not isinstance(v, dict):
            continue
        by_store[physical_store_key(_display(v.get("vendor_code")))].append(v)

    groups: list[dict[str, Any]] = []
    for phys, virtual_codes in by_store.items():
        virtual_codes = sorted(virtual_codes, key=lambda x: _num(x.get("distance")) or 9e9)
        virtual_store_rows = [virtual_store_builder(v) for v in virtual_codes]
        groups.append(_summarize_store_group(phys, virtual_store_rows))

    groups.sort(key=lambda g: _num(g.get("distance_km")) or 9e9)
    if ensure_physical:
        pinned = [g for g in groups if g["physical_store"] == ensure_physical]
        rest = [g for g in groups if g["physical_store"] != ensure_physical]
        groups = pinned + rest
    return groups[:top]


def parent_order_summary(parent_order: dict[str, Any] | None) -> dict[str, Any] | None:
    """Lightweight parent PO context from order API (when split child is diagnosed)."""
    if not isinstance(parent_order, dict) or not parent_order:
        return None
    ship = parent_order.get("shipment_detail") or {}
    addr = parent_order.get("delivery_address") or {}
    return {
        "order_id": _display(parent_order.get("order_id")),
        "vendor_id": parent_order.get("vendor_id"),
        "pincode": _display(addr.get("pincode")),
        "city": _display(addr.get("city")),
        "state": _display(addr.get("state")),
        "order_status": _display(parent_order.get("status")),
        "delivery_date": _format_ts_display(ship.get("delivery_date")),
    }


def build_split_insights(
    *,
    parent_id: str | None,
    order_id: str,
    alloc_root: dict[str, Any],
    allocated: dict[str, Any] | None,
    parent_order: dict[str, Any] | None = None,
    child_order: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not parent_id:
        return None
    parent_block = alloc_root.get(parent_id) or {}
    child_block = alloc_root.get(order_id) or {}
    if not parent_block or not child_block:
        return None

    def _alloc_vendor(block: dict[str, Any]) -> dict[str, Any] | None:
        vid = block.get("allocated_vendor")
        for pool in (block.get("selected_vendors") or {}, block.get("rejected_vendors") or {}):
            for k, v in pool.items():
                if str(v.get("vendor_id", k)) == str(vid):
                    return v
        return None

    parent_alloc = _alloc_vendor(parent_block)
    child_alloc = allocated or _alloc_vendor(child_block)
    insights: list[dict[str, str]] = []

    pa_code = _display(parent_alloc.get("vendor_code")) if parent_alloc else None
    ca_code = _display(child_alloc.get("vendor_code")) if child_alloc else None
    pa_type = (parent_alloc.get("vendor_type") or "").upper() if parent_alloc else ""
    ca_type = (child_alloc.get("vendor_type") or "").upper() if child_alloc else ""

    if pa_code and ca_code:
        insights.append(
            {
                "headline": "Split order: different allocation per PO",
                "detail": (
                    f"Parent {parent_id} allocated {pa_code} ({vendor_type_label(pa_type)}); "
                    f"child {order_id} allocated {ca_code} ({vendor_type_label(ca_type)})."
                ),
            }
        )
    if parent_alloc and child_alloc and is_retail_vendor(parent_alloc) and is_warehouse_vendor(child_alloc):
        insights.append(
            {
                "headline": "Parent used retail; child shipped from warehouse",
                "detail": (
                    "Parent order used a nearby retail store; this order shipped from a farther warehouse — "
                    "check stock on the child order lines and how SKUs were split."
                ),
            }
        )
    if pa_code and ca_code and physical_store_key(pa_code) == physical_store_key(ca_code):
        insights.append(
            {
                "headline": "Same building on parent and child",
                "detail": (
                    f"Both orders use location {physical_store_key(pa_code)} "
                    "with different vendor codes at that building."
                ),
            }
        )
    elif pa_code and ca_code:
        near_parent = physical_store_key(pa_code)
        near_child_retail = [
            physical_store_key(v.get("vendor_code"))
            for v in (child_block.get("rejected_vendors") or {}).values()
            if (v.get("vendor_type") or "").upper() == "RETAIL"
        ]
        near_child_retail = sorted(
            set(near_child_retail),
            key=lambda p: min(
                _num(v.get("distance")) or 9e9
                for v in (child_block.get("rejected_vendors") or {}).values()
                if physical_store_key(v.get("vendor_code")) == p
            ),
        )[:3]
        if near_child_retail and near_parent not in near_child_retail:
            insights.append(
                {
                    "headline": "Nearby retail on child had no usable stock",
                    "detail": (
                        f"Parent used {pa_code}; child rejected nearest retail "
                        f"{', '.join(near_child_retail)} (inventory/service) before warehouse allocation."
                    ),
                }
            )

    parent_ctx = parent_order_summary(parent_order)
    if parent_ctx:
        insights.append(
            {
                "headline": "Parent order record",
                "detail": (
                    f"Parent {parent_ctx['order_id']}: status {parent_ctx['order_status']}, "
                    f"pincode {parent_ctx['pincode']}, vendor_id {parent_ctx.get('vendor_id') or '—'}."
                ),
            }
        )
        child_addr = (child_order or {}).get("delivery_address") or {}
        child_pin = _display(child_addr.get("pincode"))
        if child_pin and parent_ctx.get("pincode") and child_pin == parent_ctx["pincode"]:
            insights.append(
                {
                    "headline": "Same delivery pincode on parent and child",
                    "detail": f"Both POs ship to pincode {child_pin} (split shipment, different fulfillment paths).",
                }
            )

    if not insights:
        return None
    out: dict[str, Any] = {"parent_id": parent_id, "child_id": order_id, "insights": insights}
    if parent_ctx:
        out["parent_order"] = parent_ctx
    return out


def _format_status_ts(ts: str | None) -> str:
    return _format_ts_display(ts, fallback=_display(ts))


def _format_clickpost_ts_display(value: Any, *, fallback: str | None = None) -> str:
    """ClickPost API timestamps are naive ISO; treat like order API (UTC) then display IST."""
    return _format_ts_display(value, fallback=fallback)


def parse_clickpost_events(clickpost_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten post-order ClickPost tracking rows (newest-first API order preserved)."""
    payload = clickpost_payload if isinstance(clickpost_payload, dict) else {}
    if payload.get("is_success") is False:
        return []
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_ts = row.get("timestamp")
        out.append(
            {
                "status": _display(row.get("clickpost_status_bucket_description")),
                "location": _display(redact_free_text(row.get("location")), ""),
                "at": _format_clickpost_ts_display(raw_ts),
                "timestamp": _display(raw_ts),
            }
        )
    return out


def build_shipping_summary(order: dict[str, Any] | None) -> dict[str, Any]:
    ship = (order or {}).get("shipment_detail") if isinstance(order, dict) else None
    if not isinstance(ship, dict):
        return {}
    fe_phone = redact_free_text(ship.get("fe_phone_number"))
    summary = {
        "delivery_partners_code": _display(ship.get("delivery_partners_code"), ""),
        "waybill": _display(ship.get("tracking_number"), ""),
        "fe_name": _display(ship.get("fe_name"), ""),
        "fe_phone": fe_phone if fe_phone not in (_MISSING, None) else "",
        "tracking_url": _display(ship.get("tracking_url"), ""),
    }
    if not any(v and v != _MISSING and v != "" for v in summary.values()):
        return {}
    return summary


def resolve_last_mile_mode(
    groot_events: list[dict[str, Any]],
    clickpost_events: list[dict[str, Any]],
) -> str:
    if groot_events:
        return "groot"
    if clickpost_events:
        return "clickpost"
    return "none"


def parse_groot_events(groot_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten Groot timeline API ``data`` day buckets into UI rows (IST timestamps)."""
    data = (groot_payload or {}).get("data") or {}
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for day, evs in sorted(data.items()):
        if not isinstance(evs, list):
            continue
        for e in evs:
            if not isinstance(e, dict):
                continue
            raw_at = e.get("performed_at") or e.get("created_at")
            out.append(
                {
                    "day": day,
                    "status": _display(e.get("status")),
                    "sub_status": _display(e.get("sub_status")),
                    "at": _format_groot_ts_display(raw_at),
                    "comments": _display(redact_free_text(e.get("comments")), ""),
                    "performed_by": _display(e.get("performed_by"), ""),
                }
            )
    return out


def _timeline_with_durations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological status rows with minutes until the next milestone."""
    if not rows:
        return []
    chron = sorted(rows, key=lambda r: _s(r.get("at")) or "")
    out: list[dict[str, Any]] = []
    for i, r in enumerate(chron):
        nxt = chron[i + 1] if i + 1 < len(chron) else None
        dur = _duration_min(r.get("at"), nxt.get("at")) if nxt else None
        out.append({**r, "duration_min": dur})
    return out


def statuses_borrowed_from_parent(parent_rows: list[dict[str, Any]]) -> set[str]:
    """Status ids taken from parent PO on split-child ops trace (through stock allocation)."""
    parent_ids = {str(r.get("status_id")) for r in parent_rows if r.get("status_id")}
    return {sid for sid in PARENT_PRE_PACKAGING if sid in parent_ids}


def merge_order_status_timeline(
    *,
    parent_id: str | None,
    parent_rows: list[dict[str, Any]],
    child_rows: list[dict[str, Any]],
    order_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Build one chronological status chain for ops trace.

    Split child: borrow parent milestones through vendor stock allocation (120);
    all other statuses on this PO come from the child status-history API.
    """
    meta: dict[str, Any] = {
        "split_child": False,
        "parent_id": parent_id,
        "order_id": order_id,
        "borrowed_status_ids": [],
        "boundary_status_id": SPLIT_OPS_BOUNDARY_STATUS,
    }
    if not parent_id or not parent_rows:
        chron = sorted(
            [{**r, "source_po": "this"} for r in child_rows],
            key=lambda r: _s(r.get("at")) or "",
        )
        return chron, meta

    borrow = statuses_borrowed_from_parent(parent_rows)
    meta["split_child"] = True
    meta["borrowed_status_ids"] = sorted(borrow, key=lambda x: int(x) if str(x).isdigit() else 999)

    parent_part = [
        {**r, "source_po": "parent", "source_label": f"Parent {parent_id}"}
        for r in parent_rows
        if str(r.get("status_id")) in borrow
    ]
    child_part = [
        {**r, "source_po": "this", "source_label": f"This order {order_id}"}
        for r in child_rows
        if str(r.get("status_id")) not in borrow
    ]
    merged = sorted(parent_part + child_part, key=lambda r: _s(r.get("at")) or "")
    return merged, meta


def is_rapid_order(order: dict[str, Any]) -> bool:
    """True when the customer selected a rapid/polygon service on the order."""
    rapid = order.get("rapid_eligibility_info")
    if not isinstance(rapid, dict):
        rapid = {}
    service_id = (_s(rapid.get("service_id") or rapid.get("rapid_type")) or "").lower()
    service_type = (_s(rapid.get("service_type")) or "").lower()
    if service_id == "standard" or service_type == "standard":
        return False
    if _bool(rapid.get("rapid_opted")) is True:
        return True
    return bool(service_id and service_id != "standard")


def is_active_service_order(
    order: dict[str, Any],
    allocated: dict[str, Any] | None,
) -> bool:
    """
    Active (rapid/polygon) vs standard pincode path.

    Standard when ``rapid_eligibility_info`` is missing/standard, or selected vendor is
    ``standard_path`` (pincode mapped + empty rapid ``eta``).
    """
    if is_rapid_order(order):
        return True
    if allocated:
        windows = extract_service_windows(allocated)
        if windows.get("standard_path"):
            return False
        if rapid_eta_keys(allocated.get("eta")):
            return True
    return False


def build_ops_sla_context(order: dict[str, Any], *, is_active: bool) -> dict[str, Any]:
    """Cutoff inputs for ops trace (existing HTTP APIs only)."""
    rapid = order.get("rapid_eligibility_info")
    if not isinstance(rapid, dict):
        rapid = {}
    eta_o = order.get("eta") or {}
    delivery_cutoff = _num(rapid.get("delivery_cutoff")) if is_active else None
    eta_to = _num(eta_o.get("eta_to"))
    return {
        "is_active_service": is_active,
        "early_cutoff_min": OPS_SLA_EARLY_MINUTES,
        "packaging_cutoff_min": OPS_SLA_PACKAGING_ACTIVE_MINUTES if is_active else None,
        "delivery_cutoff_unix": delivery_cutoff if delivery_cutoff and delivery_cutoff > 0 else None,
        "eta_to_unix": eta_to if eta_to and eta_to > 0 else None,
    }


def format_duration_display(duration_min: int | None) -> str:
    if duration_min is None:
        return _MISSING
    if duration_min <= 0:
        return "< 1 min"
    return format_duration_minutes(duration_min) or str(duration_min)


def _cutoff_instant_from_unix(
    cutoff_unix: float | None,
    *,
    ist_wall_unix: bool,
) -> datetime | None:
    if cutoff_unix is None or cutoff_unix <= 0:
        return None
    if ist_wall_unix:
        return _parse_eta_to_unix(cutoff_unix)
    end_utc = _parse_unix_ts(cutoff_unix)
    return end_utc.astimezone(_ORDER_RCA_DISPLAY_TZ) if end_utc else None


def _cutoff_minutes_from_segment_start(
    from_at_iso: str | None,
    cutoff_unix: float | None,
    *,
    ist_wall_unix: bool = False,
) -> int | None:
    start = _parse_iso(from_at_iso)
    if not start or cutoff_unix is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=_ORDER_RCA_API_TZ)
    end = _cutoff_instant_from_unix(cutoff_unix, ist_wall_unix=ist_wall_unix)
    if not end:
        return None
    compare_start = start.astimezone(_ORDER_RCA_DISPLAY_TZ)
    if end <= compare_start:
        return 0
    return int((end - compare_start).total_seconds() // 60)


def _sla_status(actual_min: int | None, cutoff_min: int | None) -> str:
    if cutoff_min is None or actual_min is None:
        return "—"
    return "on_time" if actual_min <= cutoff_min else "late"


def _ops_transition_phase_kind(from_id: str, to_id: str) -> str:
    if to_id in RETURN_STATUS_IDS or from_id in RETURN_STATUS_IDS:
        return "return"
    if to_id in CANCEL_STATUS_IDS or from_id in CANCEL_STATUS_IDS:
        return "cancel"
    if to_id in POST_DELIVERY_STATUS_IDS or (from_id == "40" and to_id in POST_DELIVERY_STATUS_IDS):
        return "post_delivery"
    return "forward"


def enrich_ops_transitions(
    transitions: list[dict[str, Any]],
    sla_ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach default SLA (minutes), status, and display fields; drop 120→130."""
    is_active = bool(sla_ctx.get("is_active_service"))
    out: list[dict[str, Any]] = []
    for row in transitions:
        fid = str(row.get("from_status_id") or "")
        tid = str(row.get("to_status_id") or "")
        if (fid, tid) in OPS_SKIP_TRANSITION_IDS:
            continue
        label = str(row.get("label") or "")
        phase_kind = str(row.get("phase_kind") or _ops_transition_phase_kind(fid, tid))
        dur = row.get("duration_min")
        duration_min = int(dur) if dur is not None and isinstance(dur, (int, float)) else None
        default_sla_min: int | None = None
        default_sla_display = _MISSING

        if phase_kind != "forward":
            sla_status = "—"
            out.append(
                {
                    **row,
                    "phase_kind": phase_kind,
                    "duration_display": format_duration_display(duration_min),
                    "default_sla_min": None,
                    "default_sla": _MISSING,
                    "sla_status": sla_status,
                    "sla_excluded": True,
                }
            )
            continue

        if label in OPS_EARLY_PHASE_LABELS:
            default_sla_min = int(sla_ctx.get("early_cutoff_min") or OPS_SLA_EARLY_MINUTES)
            default_sla_display = str(default_sla_min)
        elif label == "Packaging":
            if is_active:
                default_sla_min = int(
                    sla_ctx.get("packaging_cutoff_min") or OPS_SLA_PACKAGING_ACTIVE_MINUTES
                )
                default_sla_display = str(default_sla_min)
        elif label == "Dispatch" and is_active:
            default_sla_min = _cutoff_minutes_from_segment_start(
                row.get("from_at_iso"),
                sla_ctx.get("delivery_cutoff_unix"),
                ist_wall_unix=False,
            )
            if default_sla_min is not None:
                default_sla_display = str(default_sla_min)
        elif label == "Last mile":
            default_sla_min = _cutoff_minutes_from_segment_start(
                row.get("from_at_iso"),
                sla_ctx.get("eta_to_unix"),
                ist_wall_unix=True,
            )
            if default_sla_min is not None:
                default_sla_display = str(default_sla_min)

        sla_status = _sla_status(duration_min, default_sla_min)
        if default_sla_min is None:
            sla_status = "—"

        out.append(
            {
                **row,
                "phase_kind": phase_kind,
                "duration_display": format_duration_display(duration_min),
                "default_sla_min": default_sla_min,
                "default_sla": default_sla_display,
                "sla_status": sla_status,
                "sla_excluded": False,
            }
        )
    return out


def _chronology_has_return(chron: list[dict[str, Any]]) -> bool:
    return any(str(r.get("status_id") or "") in RETURN_STATUS_IDS for r in chron)


def build_status_transitions(chron: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wall-clock segments between consecutive milestones (label = phase completed at ``at``)."""
    if len(chron) < 2:
        return []
    out: list[dict[str, Any]] = []
    for i in range(len(chron) - 1):
        a, b = chron[i], chron[i + 1]
        fid = str(a.get("status_id") or "")
        tid = str(b.get("status_id") or "")
        if (fid, tid) in OPS_SKIP_TRANSITION_IDS:
            continue
        dur = _duration_min(a.get("at"), b.get("at"))
        label = STATUS_TRANSITION_LABELS.get((fid, tid)) or f"{a.get('label')} → {b.get('label')}"
        hover = f"{b.get('label')} at {_format_status_ts(b.get('at'))}"
        if a.get("source_po") != b.get("source_po"):
            hover += f" ({b.get('source_label')})"
        out.append(
            {
                "label": label,
                "duration_min": dur,
                "at": _format_status_ts(b.get("at")),
                "at_iso": b.get("at"),
                "from_at_iso": a.get("at"),
                "hover": hover,
                "from_status_id": fid,
                "to_status_id": tid,
                "from_label": a.get("label"),
                "to_label": b.get("label"),
                "phase_kind": _ops_transition_phase_kind(fid, tid),
            }
        )
    return out


def _segment_from_timeline(
    chron: list[dict[str, Any]],
    from_id: str,
    to_id: str,
    label: str,
) -> dict[str, Any] | None:
    ids = [str(r.get("status_id")) for r in chron]
    try:
        i, j = ids.index(from_id), ids.index(to_id)
    except ValueError:
        return None
    if j <= i:
        return None
    a, b = chron[i], chron[j]
    return {
        "label": label,
        "from": a.get("label"),
        "to": b.get("label"),
        "from_at": _format_ts_display(a.get("at")),
        "to_at": _format_ts_display(b.get("at")),
        "duration_min": _duration_min(a.get("at"), b.get("at")),
    }


def rejection_reason_short(signals: list[str]) -> str:
    if not signals:
        return "No rejection detail available"
    if len(signals) == 1:
        return signals[0]
    return signals[0] if len(signals[0]) > 80 else f"{signals[0]}; {signals[1]}"


def inventory_issue_message(msg: str) -> str:
    """
    Short text after ``SKU {id}:`` in the Allocation store matrix Reason column.

    No leading ``SKU`` — id is only in the prefix. Plain language for ops users.
    """
    m = (msg or "").strip()
    if "(" in m:
        m = m.split("(", 1)[0].strip()
    ml = m.lower()
    if "not mapped" in ml:
        return "Unmapped on vendor"
    if "not available" in ml and "delived" in ml:
        return "Unavailable and Delived"
    if "partial" in ml and "delived" in ml:
        return "Partial and Delived"
    if "delived" in ml and ("pipeline" in ml or "available but" in ml):
        return "Delived pipeline"
    if "partial" in ml:
        return "Partial stock"
    if "out of stock" in ml or ml == "oos":
        return "Out of stock"
    if ml == "jit" or "jit" in ml:
        return "Just-in-time"
    return m


def normalize_rejection_signal(msg: str) -> str:
    """Plain-language rejection line (non-inventory or legacy strings)."""
    m = (msg or "").strip()
    if not m:
        return m
    if "Vendor has no active rapid/standard" in m or m == SERVICE_UNAVAILABLE_DETAIL:
        return ""
    if m.startswith(f"{SERVICE_UNAVAILABLE_SIGNAL}:"):
        return ""
    if m.upper().startswith("SKU ") and ": " in m:
        sku_part, rest = m.split(": ", 1)
        return f"{sku_part}: {inventory_issue_message(rest)}"
    if "location not serviceable" in m.lower() or "address not serviceable" in m.lower():
        return "Address not serviceable"
    if "not mapped" in m.lower():
        return "Unmapped on vendor"
    if "inventory not available" in m.lower() or "stock not available" in m.lower():
        return "Stock not available"
    if m.startswith("Not chosen by allocation engine"):
        return m
    return inventory_issue_message(m)


def format_inventory_rejection_line(line: str) -> str:
    """``SKU {sku_id}: {issue}`` — consistent Reason format for inventory."""
    raw = (line or "").strip()
    if ": " in raw and raw.upper().startswith("SKU "):
        sku_part, msg = raw.split(": ", 1)
        return f"{sku_part}: {inventory_issue_message(msg)}"
    return normalize_rejection_signal(raw)


def dedupe_rejection_signals(signals: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in signals:
        norm = normalize_rejection_signal(raw)
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def location_serviceable(vendor: dict[str, Any], *, selected: bool) -> bool:
    if selected:
        return True
    pin = _bool(vendor.get("pincode_mapped"))
    active = vendor.get("active_services_with_capacity") or {}
    if pin is True:
        return True
    if isinstance(active, dict) and len(active) > 0:
        return True
    return False


def location_not_ok_label(*, pincode_mapped: bool | None, has_active_services: bool) -> str:
    """
    Human-readable Loc OK = No hint.

    Serviceable when pincode is mapped OR there is at least one active service window.
    When not serviceable, both are false in practice; label still distinguishes the cases
    for clarity if only one blocker is shown in data.
    """
    not_pin = pincode_mapped is not True
    no_active = not has_active_services
    if not_pin and no_active:
        return "Address not serviceable — no pincode and no active delivery windows"
    if not_pin:
        return "Address not serviceable — pincode not mapped"
    if no_active:
        return "Address not serviceable — no active delivery windows"
    return "Address not serviceable"


def rapid_eta_keys(eta: Any) -> list[str]:
    if not isinstance(eta, dict) or not eta:
        return []
    return sorted(k for k, val in eta.items() if val not in (None, "", {}))


def standard_available(eta: Any) -> bool:
    if eta is None:
        return True
    if isinstance(eta, dict) and len(eta) == 0:
        return True
    return False


def service_type_label(key: str) -> str:
    """
    Human-readable label from explain_allocation service keys (no static catalog).

    Examples: ``1_hour`` → ``1 hour``, ``30_minute`` → ``30 minute``,
    ``three_day_delivery`` → ``three day delivery``.
    """
    k = (key or "").strip()
    if not k:
        return _MISSING
    if "_" not in k:
        return k
    parts = [p for p in k.split("_") if p]
    if len(parts) == 2 and parts[0].replace(".", "", 1).isdigit():
        return f"{parts[0]} {parts[1]}"
    return " ".join(parts)


_ORDER_SERVICE_LABELS: dict[str, str] = {
    "one_day_delivery": "Zero day",
    "one_hour_delivery": "1 hour",
    "thirty_minute_delivery": "30 min",
    "two_hour_delivery": "2 hour",
    "three_hour_delivery": "3 hour",
    "standard": "Standard",
}


def order_service_type_label(order: dict[str, Any]) -> str | None:
    """CX-style service tag from order payload; ``None`` when unknown."""
    rapid = order.get("rapid_eligibility_info")
    if not isinstance(rapid, dict):
        rapid = {}
    svc = order.get("service_details")
    if not isinstance(svc, dict):
        svc = {}
    for raw in (
        rapid.get("service_id"),
        rapid.get("rapid_type"),
        svc.get("service_type"),
    ):
        key = (_s(raw) or "").strip().lower()
        if not key:
            continue
        if key in _ORDER_SERVICE_LABELS:
            return _ORDER_SERVICE_LABELS[key]
        label = service_type_label(key)
        if label != _MISSING:
            return label
    return None


def _service_windows_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None or v == "":
            continue
        out[str(k)] = str(v)
    return out


_SERVICE_WINDOW_RANGE_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}(?:\s*[AP]M)?)\s*-\s*(?P<end>\d{1,2}:\d{2}(?:\s*[AP]M)?)\s*$",
    re.IGNORECASE,
)

INACTIVITY_BEYOND_TIME = "beyond_time"
INACTIVITY_DISABLED = "disabled"
INACTIVITY_UNKNOWN = "unknown"


def _parse_clock_to_minutes(clock: str) -> int | None:
    """Parse ``07:00``, ``07:00 AM``, ``01:00 PM`` to minutes since midnight."""
    s = (clock or "").strip().upper()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", s)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    elif meridiem is None and hour == 24:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def instant_in_service_window(placed: datetime, window: str) -> bool | None:
    """
    True when ``placed`` IST wall-clock time falls inside ``window`` (e.g. ``07:00 AM-10:50 PM``).

    Returns None when the window string cannot be parsed.
    """
    m = _SERVICE_WINDOW_RANGE_RE.match((window or "").strip())
    if not m:
        return None
    start_m = _parse_clock_to_minutes(m.group("start"))
    end_m = _parse_clock_to_minutes(m.group("end"))
    if start_m is None or end_m is None:
        return None
    local = placed.astimezone(_ORDER_RCA_DISPLAY_TZ)
    now_m = local.hour * 60 + local.minute
    if start_m <= end_m:
        return start_m <= now_m <= end_m
    return now_m >= start_m or now_m <= end_m


def classify_inactive_service_timing(
    inactive_rows: list[dict[str, Any]],
    placed_at: datetime | None,
) -> list[dict[str, Any]]:
    """
    Annotate inactive service rows: beyond operating window vs inactive inside window.

    Assumes allocation only lists services that cover the delivery location.
    ``disabled`` covers polygon/service off or capacity — not distinguished here.
    """
    if not inactive_rows:
        return []
    out: list[dict[str, Any]] = []
    for row in inactive_rows:
        annotated = dict(row)
        window = str(row.get("window") or "").strip()
        kind = INACTIVITY_UNKNOWN
        if placed_at and window:
            inside = instant_in_service_window(placed_at, window)
            if inside is True:
                kind = INACTIVITY_DISABLED
            elif inside is False:
                kind = INACTIVITY_BEYOND_TIME
        annotated["inactivity_kind"] = kind
        out.append(annotated)
    return out


def extract_service_windows(
    vendor: dict[str, Any],
    *,
    placed_at: datetime | None = None,
) -> dict[str, Any]:
    """
    Active/inactive service windows from explain_allocation (API 5).

    Keys like ``1_hour`` are service types; values are schedule windows (e.g. ``07:00 AM-10:50 PM``).
    ``inactive_and_unavailable_capacity`` is schedule-only — not fulfillment capacity.
    """
    active = _service_windows_map(vendor.get("active_services_with_capacity"))
    inactive = _service_windows_map(vendor.get("inactive_and_unavailable_capacity"))
    active_rows = [
        {"service_key": k, "service_label": service_type_label(k), "window": w, "status": "active"}
        for k, w in sorted(active.items())
    ]
    inactive_rows = classify_inactive_service_timing(
        [
            {"service_key": k, "service_label": service_type_label(k), "window": w, "status": "inactive"}
            for k, w in sorted(inactive.items())
        ],
        placed_at,
    )
    eta = vendor.get("eta") or {}
    eta_keys = rapid_eta_keys(eta) if isinstance(eta, dict) else []
    order_sla_labels = [service_type_label(k) for k in eta_keys]
    pin = _bool(vendor.get("pincode_mapped"))
    std = standard_available(eta) if isinstance(eta, dict) else True
    standard_path = bool(pin is True and std and not eta_keys)
    return {
        "active_services": active_rows,
        "inactive_services": inactive_rows,
        "inactive_beyond_time": [r for r in inactive_rows if r.get("inactivity_kind") == INACTIVITY_BEYOND_TIME],
        "inactive_disabled": [r for r in inactive_rows if r.get("inactivity_kind") == INACTIVITY_DISABLED],
        "order_sla_keys": order_sla_labels,
        "standard_path": standard_path,
        "standard_at_store": pin is True,
        "standard_unavailable": pin is not True,
    }


def advertised_rapid_sla_rows(
    vendor: dict[str, Any],
    *,
    order: dict[str, Any],
    active_services: list[dict[str, Any]] | None,
    inactive_services: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """SLA keys from vendor ``eta`` when order is rapid but no active slot at placement."""
    if active_services:
        return []
    if not is_rapid_order(order):
        return []
    eta = vendor.get("eta")
    keys = rapid_eta_keys(eta) if isinstance(eta, dict) else []
    if not keys:
        return []
    inactive_keys = {
        str(r.get("service_key"))
        for r in (inactive_services or [])
        if r.get("service_key")
    }
    keys = [k for k in keys if k not in inactive_keys]
    if not keys:
        return []
    return [
        {"service_key": k, "service_label": service_type_label(k), "status": "advertised"}
        for k in keys
    ]


def location_serviceable_fields(
    vendor: dict[str, Any],
    *,
    selected: bool,
    placed_at: datetime | None = None,
) -> dict[str, Any]:
    """Location serviceable + pincode/active path + service window payload for UI."""
    loc_ok = location_serviceable(vendor, selected=selected)
    pin = _bool(vendor.get("pincode_mapped"))
    windows = extract_service_windows(vendor, placed_at=placed_at)
    has_active = bool(windows["active_services"])
    if selected:
        reason = "Included in shortlisted stores"
    elif loc_ok:
        if pin is True and has_active:
            reason = "Pincode mapped with active delivery windows"
        elif pin is True:
            reason = "Pincode mapped (standard delivery path)"
        elif has_active:
            reason = "Active delivery windows (rapid path)"
        else:
            reason = "Address can be served"
    else:
        reason = location_not_ok_label(pincode_mapped=pin, has_active_services=has_active)
    return {
        "location_serviceable": loc_ok,
        "location_not_serviceable": not loc_ok,
        "location_reason": reason,
        "pincode_mapped": pin,
        "has_active_services": has_active,
        **windows,
    }


def vendor_label(v: dict[str, Any]) -> str:
    cs = v.get("city_and_state") or {}
    city = _s(cs.get("city"))
    state = _s(cs.get("state"))
    code = _display(v.get("vendor_code"))
    if city and state:
        return f"{code} · {city}, {state}"
    return code


def _parse_qty(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _sku_priority_value(sku: dict[str, Any]) -> int | None:
    """Numeric ``sku.priority`` from order-service (e.g. 30 = sampling)."""
    if not isinstance(sku, dict):
        return None
    raw = sku.get("priority")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_sampling_order_line(ln: dict[str, Any]) -> bool:
    """Sampling / freebie lines (priority P30) — display only, not fulfillment analysis."""
    if not isinstance(ln, dict):
        return False
    sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
    return _sku_priority_value(sku) == SAMPLING_SKU_PRIORITY


def fulfillment_order_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        ln
        for ln in (order.get("order_lines") or [])
        if isinstance(ln, dict) and not is_sampling_order_line(ln)
    ]


def fulfillment_order(order: dict[str, Any]) -> dict[str, Any]:
    """Shallow order copy without sampling SKUs — for allocation, MSN, MRP, ETA."""
    return {**order, "order_lines": fulfillment_order_lines(order)}


def fulfillment_skus(skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SKU display rows excluding sampling (for LLM / narrative)."""
    return [s for s in skus if isinstance(s, dict) and not s.get("is_sampling")]


def ordered_quantity_from_line(ln: dict[str, Any]) -> float | None:
    """
    Pack-normalized ordered quantity for allocation, MSN, and inventory comparison.

    Order-service ``order_lines.quantity`` is often total base units (tablets/capsules)
    while allocation, P1 MSN/on-shelf, and vendor ``normalized_quantity`` use pack count
    (strips/bottles). Prefer ``normalized_quantity`` when present; otherwise derive from
    ``quantity`` and ``sku.units_in_pack``.
    """
    if not isinstance(ln, dict):
        return None
    norm = _parse_qty(ln.get("normalized_quantity"))
    if norm is not None:
        return norm

    raw = _parse_qty(ln.get("quantity"))
    if raw is None:
        return None

    sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
    uip = _parse_qty(sku.get("units_in_pack"))
    if uip is None or uip <= 1:
        return raw

    # Exact multiple → quantity is base units; convert to packs.
    if raw >= uip and raw % uip == 0:
        return raw / uip
    # Smaller than a pack or non-divisible → already pack/selling units.
    return raw


def order_qty_by_sku(order: dict[str, Any]) -> dict[str, float]:
    """Ordered pack-normalized quantity per SKU id from order lines."""
    out: dict[str, float] = {}
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        sid = _s(sku.get("sku_id") or ln.get("order_line_id"))
        q = ordered_quantity_from_line(ln)
        if sid and q is not None:
            out[sid] = q
    return out


def classify_sku_inventory(
    sd: dict[str, Any],
    ordered_qty: float | None,
) -> dict[str, Any]:
    """
    Classify inventory using two axes: lifecycle ``state`` and fulfillable qty.

    ``normalized_quantity`` = units that can be allocated (blocked qty already excluded).

    Delive (depletion pipeline) — always evaluate qty first:
      fulfillable <= 0     → not available + Delived
      0 < fulfillable < ordered → partial qty + Delived
      fulfillable >= ordered  → available qty + Delived (still not live shelf stock)

    Live (or other non-delive) — standard rules:
      fulfillable <= 0     → out of stock
      0 < fulfillable < ordered → partial
      else                 → OK
    """
    av = (_s(sd.get("availability")) or "").lower()
    lifecycle = (_s(sd.get("state")) or "").lower()
    fulfillable = _parse_qty(sd.get("normalized_quantity"))
    if fulfillable is None:
        fulfillable = 0.0
    fulfillable = max(0.0, fulfillable)
    ordered = ordered_qty if ordered_qty is not None else 0.0

    if lifecycle == "delive":
        if fulfillable <= 0:
            return {
                "class": "delived_oos",
                "blocks_fulfillment": True,
                "is_partial": False,
                "label": "Unavailable and Delived",
                "detail": "not available and Delived",
            }
        if ordered > 0 and fulfillable < ordered:
            return {
                "class": "delived_partial",
                "blocks_fulfillment": True,
                "is_partial": True,
                "label": "Partial and Delived",
                "detail": "partial qty and Delived",
            }
        return {
            "class": "delived_avail",
            "blocks_fulfillment": True,
            "is_partial": False,
            "label": "Delived pipeline",
            "detail": "in Delived pipeline",
        }

    if av not in ("yes", "maybe"):
        return {
            "class": "oos",
            "blocks_fulfillment": True,
            "is_partial": False,
            "label": "Out of stock",
            "detail": "out of stock",
        }

    if fulfillable <= 0:
        return {
            "class": "oos",
            "blocks_fulfillment": True,
            "is_partial": False,
            "label": "Out of stock",
            "detail": "out of stock",
        }

    if ordered > 0 and fulfillable < ordered:
        return {
            "class": "partial",
            "blocks_fulfillment": True,
            "is_partial": True,
            "label": "Partial stock",
            "detail": "partial inventory",
        }

    return {
        "class": "ok",
        "blocks_fulfillment": False,
        "is_partial": False,
        "label": "OK",
        "detail": "",
    }


INVENTORY_CLASS_PRIORITY: tuple[str, ...] = (
    "unmapped",
    "delived_oos",
    "oos",
    "delived_partial",
    "partial",
    "delived_avail",
    "ok",
)


def _unique_inventory_issues(
    issue_rows: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Distinct stock issue types in severity order (one label per class)."""
    by_class: dict[str, str] = {}
    for cls, label in issue_rows:
        if cls == "ok":
            continue
        if cls not in by_class:
            by_class[cls] = label
    ordered = sorted(
        by_class.keys(),
        key=lambda x: INVENTORY_CLASS_PRIORITY.index(x) if x in INVENTORY_CLASS_PRIORITY else 99,
    )
    return ordered, [by_class[c] for c in ordered]


def _inventory_summary_payload(issue_rows: list[tuple[str, str]]) -> dict[str, Any]:
    """Stock column + flags from per-SKU issue rows."""
    uniq_classes, uniq_labels = _unique_inventory_issues(issue_rows)
    if not uniq_classes:
        return {
            "inventory_class": "ok",
            "inventory_label": "OK",
            "inventory_classes": ["ok"],
            "inventory_labels": ["OK"],
            "inventory_not_available": False,
            "inventory_partial": False,
        }
    worst = uniq_classes[0]
    return {
        "inventory_class": worst,
        "inventory_label": uniq_labels[0],
        "inventory_classes": uniq_classes,
        "inventory_labels": uniq_labels,
        "inventory_not_available": True,
        "inventory_partial": any(c in ("partial", "delived_partial") for c in uniq_classes),
    }


def assess_sku_inventory(
    sd: dict[str, Any],
    ordered_qty: float | None,
) -> tuple[bool, bool, list[str]]:
    """Legacy tuple view of ``classify_sku_inventory``."""
    c = classify_sku_inventory(sd, ordered_qty)
    details = [c["detail"]] if c.get("detail") else []
    return bool(c["blocks_fulfillment"]), bool(c["is_partial"]), details


def _availability_yes_maybe(vendor: dict[str, Any]) -> tuple[int, int]:
    """Count yes/maybe SKUs from availability_count (dict or scalar)."""
    ac = vendor.get("availability_count")
    if isinstance(ac, dict):
        total = len(ac)
        ym = sum(
            1
            for x in ac.values()
            if _s(x) and _s(x).lower() in ("yes", "maybe")
        )
        return ym, total
    if ac in (0, "0", None, "", {}):
        return 0, 0
    return 0, 0


def inventory_not_available(
    vendor: dict[str, Any],
    *,
    order_qty_by_sku: dict[str, float] | None = None,
) -> tuple[bool, list[str]]:
    """True when fulfillable qty cannot cover order or SKU is not mapped on vendor."""
    details: list[str] = []
    qty_supplied = order_qty_by_sku is not None
    qty_map = order_qty_by_sku or {}
    skus = vendor.get("skus") if isinstance(vendor.get("skus"), dict) else {}
    any_block = False

    if qty_supplied:
        if not qty_map:
            return False, []
        for sku_id in qty_map:
            sid = str(sku_id)
            if sid not in skus:
                details.append(f"SKU {sid}: not mapped on vendor")
                any_block = True
        for sku_id in qty_map:
            sid = str(sku_id)
            if sid not in skus:
                continue
            sd = skus.get(sid)
            if not isinstance(sd, dict):
                continue
            ordered = qty_map.get(sid)
            c = classify_sku_inventory(sd, ordered)
            if c["blocks_fulfillment"]:
                any_block = True
            if c.get("detail"):
                details.append(f"SKU {sku_id}: {c['detail']}")
            if _bool(sd.get("jit")):
                details.append(f"SKU {sku_id}: JIT")
        return any_block, details

    if skus:
        any_block = False
        assess_ids = list(qty_map.keys()) if qty_map else list(skus.keys())
        for sku_id in assess_ids:
            sd = skus.get(str(sku_id))
            if not isinstance(sd, dict):
                continue
            ordered = qty_map.get(str(sku_id)) if qty_map else None
            c = classify_sku_inventory(sd, ordered)
            if c["blocks_fulfillment"]:
                any_block = True
            if c.get("detail"):
                details.append(f"SKU {sku_id}: {c['detail']}")
            if _bool(sd.get("jit")):
                details.append(f"SKU {sku_id}: JIT")
        if any_block:
            return True, details
        return False, details

    nus = vendor.get("nu_of_available_skus")
    try:
        nus_i = int(nus) if nus is not None else None
    except (TypeError, ValueError):
        nus_i = None
    if nus_i == 0:
        return True, details
    ym, _ = _availability_yes_maybe(vendor)
    if ym == 0 and vendor.get("availability_count") in (0, "0", {}, None):
        return True, details
    if nus_i is not None and nus_i > 0 and not skus:
        return False, details
    return False, details


def service_not_available(vendor: dict[str, Any], *, location_ok: bool) -> tuple[bool, list[str]]:
    """
    True when no active service window can serve this order.

    Uses active_services_with_capacity / inactive_and_unavailable_capacity (service
    schedules, not fulfillment capacity) and eta SLA keys. Ignores capacity_info (null in samples).
    """
    details: list[str] = []
    active = vendor.get("active_services_with_capacity") or {}
    inactive = vendor.get("inactive_and_unavailable_capacity") or {}
    if not isinstance(active, dict):
        active = {}
    if not isinstance(inactive, dict):
        inactive = {}
    eta = vendor.get("eta") or {}
    eta_keys = set(rapid_eta_keys(eta)) if isinstance(eta, dict) else set()
    active_keys = set(active.keys())
    inactive_keys = set(inactive.keys())
    pin_mapped = _bool(vendor.get("pincode_mapped")) is True

    # Standard (pincode) warehouse: empty rapid eta; pincode_mapped implies standard service path.
    if not eta_keys and pin_mapped:
        return False, details

    if not location_ok:
        if not active_keys and not pin_mapped:
            return True, details
        return False, details

    if not active_keys:
        if inactive_keys or eta_keys:
            if inactive_keys:
                sample = ", ".join(sorted(inactive_keys)[:4])
                details.append(f"No active windows (inactive types: {sample})")
            return True, details
        return False, details

    if eta_keys:
        served = eta_keys & active_keys
        unserved = eta_keys - active_keys
        if not served and unserved:
            sample = ", ".join(sorted(unserved)[:4])
            details.append(f"Advertised SLAs without active window: {sample}")
            return True, details
    return False, details


def _vendor_split_enabled(vendor: dict[str, Any]) -> bool | None:
    """``split_enabled`` from explain_allocation when present."""
    if "split_enabled" not in vendor:
        return None
    return _bool(vendor.get("split_enabled")) is True


_IMPLICIT_REJECTION = "Not chosen by allocation engine"
_IMPLICIT_REJECTION_SPLIT_OFF = (
    f"{_IMPLICIT_REJECTION} (Split fulfilment disabled, Correlate further)"
)
_IMPLICIT_REJECTION_DEFAULT = f"{_IMPLICIT_REJECTION} (Correlate further)"


def _implicit_rejection_signals(vendor: dict[str, Any]) -> list[str]:
    """
    Vendor is in rejected_vendors but none of inv/location/service heuristics fired.
    Common for ranking, split-SKU, or engine tie-break (e.g. 1MG_BRM_01 with stock + active SLAs).
    """
    if _vendor_split_enabled(vendor) is False:
        return [_IMPLICIT_REJECTION_SPLIT_OFF]
    return [_IMPLICIT_REJECTION_DEFAULT]


def vendor_inventory_summary(
    vendor: dict[str, Any],
    *,
    order_qty_by_sku: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Per-vendor stock rollup: unique issue types for Stock column; per-SKU in Reason."""
    qty_supplied = order_qty_by_sku is not None
    qty_map = order_qty_by_sku or {}
    skus = vendor.get("skus") if isinstance(vendor.get("skus"), dict) else {}

    issue_rows: list[tuple[str, str]] = []
    if qty_supplied:
        if not qty_map:
            return {
                "inventory_class": "ok",
                "inventory_label": "OK",
                "inventory_classes": ["ok"],
                "inventory_labels": ["OK"],
                "inventory_not_available": False,
                "inventory_partial": False,
            }
        for sku_id in qty_map:
            sid = str(sku_id)
            if sid not in skus:
                issue_rows.append(("unmapped", "Unmapped"))
                continue
            sd = skus.get(sid)
            if not isinstance(sd, dict):
                continue
            c = classify_sku_inventory(sd, qty_map.get(sid))
            issue_rows.append((str(c["class"]), str(c["label"])))
    else:
        for sku_id, sd in skus.items():
            if not isinstance(sd, dict):
                continue
            c = classify_sku_inventory(sd, None)
            issue_rows.append((str(c["class"]), str(c["label"])))

    if not issue_rows:
        inv_na, _ = inventory_not_available(vendor, order_qty_by_sku=order_qty_by_sku)
        if inv_na:
            return {
                "inventory_class": "oos",
                "inventory_label": "Out of stock",
                "inventory_classes": ["oos"],
                "inventory_labels": ["Out of stock"],
                "inventory_not_available": True,
                "inventory_partial": False,
            }
        return {
            "inventory_class": "ok",
            "inventory_label": "OK",
            "inventory_classes": ["ok"],
            "inventory_labels": ["OK"],
            "inventory_not_available": False,
            "inventory_partial": False,
        }

    return _inventory_summary_payload(issue_rows)


def analyze_vendor_rejection(
    vendor: dict[str, Any],
    *,
    order_qty_by_sku: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Structured rejection reasons for a rejected_vendors row (API 5)."""
    loc_ok = location_serviceable(vendor, selected=False)
    inv_summary = vendor_inventory_summary(vendor, order_qty_by_sku=order_qty_by_sku)
    inv_na = inv_summary["inventory_not_available"]
    _, inv_details = inventory_not_available(vendor, order_qty_by_sku=order_qty_by_sku)
    svc_na, svc_details = service_not_available(vendor, location_ok=loc_ok)

    signals: list[str] = []
    if not loc_ok:
        signals.append("Address not serviceable")
    if inv_na:
        n_before = len(signals)
        for line in inv_details:
            signals.append(format_inventory_rejection_line(line))
        if len(signals) == n_before:
            signals.append("Stock not available")
    # Service gaps: Active / Inactive services columns only (no generic Reason line).
    implicit_rejection = False
    if not signals:
        implicit_rejection = True
        signals.extend(_implicit_rejection_signals(vendor))

    signals = dedupe_rejection_signals(signals)

    return {
        "location_not_serviceable": not loc_ok,
        "inventory_not_available": inv_na,
        "inventory_partial": inv_summary["inventory_partial"],
        "inventory_class": inv_summary["inventory_class"],
        "inventory_label": inv_summary["inventory_label"],
        "inventory_classes": inv_summary.get("inventory_classes") or [inv_summary["inventory_class"]],
        "inventory_labels": inv_summary.get("inventory_labels") or [inv_summary["inventory_label"]],
        "service_not_available": svc_na,
        "implicit_rejection": implicit_rejection,
        "split_enabled": _vendor_split_enabled(vendor),
        "signals": signals,
        "reason_short": rejection_reason_short(signals),
    }


def infer_rejection_signals(v: dict[str, Any]) -> list[str]:
    return list(analyze_vendor_rejection(v).get("signals") or [])


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ORDER_RCA_API_TZ)
        return dt
    except ValueError:
        return None


def _status_rows(payload: dict[str, Any] | None, po: str) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data") or {}
    rows = data.get(po) or []
    out = []
    for r in rows:
        sid = _s(r.get("status"))
        out.append(
            {
                "status_id": sid,
                "label": STATUS_LABELS.get(sid or "", _display(sid, "Unknown")),
                "at": r.get("created"),
            }
        )
    return out


def _duration_min(a: str | None, b: str | None) -> int | None:
    da, db = _parse_iso(a), _parse_iso(b)
    if not da or not db:
        return None
    return int(abs((db - da).total_seconds()) // 60)


def _order_skus(order: dict[str, Any]) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        qty = ordered_quantity_from_line(ln)
        skus.append(
            {
                "sku_id": _display(sku.get("sku_id") or ln.get("order_line_id")),
                "name": _display(sku.get("name")),
                "quantity": qty,
                "is_sampling": is_sampling_order_line(ln),
            }
        )
    return skus


def preferred_top_physical_warehouses(
    vendors_all: list[dict[str, Any]],
    *,
    top: int = TOP_WAREHOUSE_STORES,
) -> list[dict[str, Any]]:
    """Nearest ``top`` distinct physical warehouse stores (one nearest virtual store each)."""
    wh = sorted(
        [v for v in vendors_all if isinstance(v, dict) and is_warehouse_vendor(v)],
        key=lambda x: _num(x.get("distance")) or 9e9,
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for v in wh:
        phys = physical_store_key(_display(v.get("vendor_code")))
        if phys in seen:
            continue
        seen.add(phys)
        out.append(v)
        if len(out) >= top:
            break
    return out


def build_preferred_physical_stores(preferred_warehouses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ranked preferred physical warehouse stores for preflight chips."""
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in preferred_warehouses:
        if not isinstance(v, dict):
            continue
        by_store[physical_store_key(_display(v.get("vendor_code")))].append(v)

    groups: list[dict[str, Any]] = []
    for phys, virtual_codes in by_store.items():
        virtual_codes = sorted(virtual_codes, key=lambda x: _num(x.get("distance")) or 9e9)
        nearest = virtual_codes[0]
        groups.append(
            {
                "physical_store": phys,
                "km": _num(nearest.get("distance")),
                "vendor_type": vendor_type_label(_s(nearest.get("vendor_type"))),
                "virtual_store_count": len(virtual_codes),
                "nearest_virtual_code": _display(nearest.get("vendor_code")),
            }
        )
    groups.sort(key=lambda g: _num(g.get("km")) or 9e9)
    return [{"rank": i + 1, **g} for i, g in enumerate(groups[:TOP_WAREHOUSE_STORES])]


def _allocation_block_present(block: dict[str, Any]) -> bool:
    if not block:
        return False
    if block.get("allocated_vendor") is not None:
        return True
    if block.get("selected_vendors") or block.get("rejected_vendors"):
        return True
    return False


def _days_since_placed(order: dict[str, Any]) -> int | None:
    dt, _ = _parse_ts_value(order.get("created"))
    if not dt:
        return None
    now = datetime.now(tz=_ORDER_RCA_API_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ORDER_RCA_API_TZ)
    else:
        dt = dt.astimezone(_ORDER_RCA_API_TZ)
    return max(0, (now - dt).days)


def build_allocation_unavailable(
    order: dict[str, Any], block: dict[str, Any]
) -> dict[str, Any] | None:
    """explain_allocation succeeded but has no payload for this PO."""
    if _allocation_block_present(block):
        return None
    days = _days_since_placed(order)
    if days is not None:
        message = (
            f"Allocation Data not available for this Order, Order Placed {days} days ago."
        )
    else:
        message = "Allocation Data not available for this Order."
    return {
        "reason": "unavailable",
        "days_placed": days,
        "message": message,
    }


def parse_allocation_block(
    order_id: str, alloc_root: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (allocated, vendors_all, preferred_top3, block) for an order id in allocation API data."""
    oid = (order_id or "").strip().upper()
    block = alloc_root.get(oid) or {}
    selected = block.get("selected_vendors") or {}
    rejected = block.get("rejected_vendors") or {}
    allocated_vid = block.get("allocated_vendor")

    vendors_all: list[dict[str, Any]] = []
    for vid, v in selected.items():
        vendors_all.append({**v, "vendor_id": v.get("vendor_id", vid), "_selected": True})
    for vid, v in rejected.items():
        vendors_all.append({**v, "vendor_id": v.get("vendor_id", vid), "_selected": False})
    vendors_all.sort(key=lambda x: _num(x.get("distance")) or 9e9)

    allocated = None
    for pool in (selected, rejected):
        for vid, v in pool.items():
            if str(v.get("vendor_id", vid)) == str(allocated_vid):
                allocated = {**v, "vendor_id": v.get("vendor_id", vid), "_selected": vid in selected}
                break
        if allocated:
            break
    if not allocated:
        for v in vendors_all:
            if str(v.get("vendor_id")) == str(allocated_vid):
                allocated = v
                break

    preferred = preferred_top_physical_warehouses(vendors_all, top=TOP_WAREHOUSE_STORES)
    return allocated, vendors_all, preferred, block


def is_ideal_bundle(bundle: dict[str, Any]) -> bool:
    """True when allocation badge is IDEAL (order + allocation APIs sufficient)."""
    order = bundle.get("order") or {}
    oid = (bundle.get("order_id") or _s(order.get("order_id")) or "").strip().upper()
    if not oid:
        return False
    alloc_root = (bundle.get("allocation") or {}).get("data") or {}
    allocated, vendors_all, preferred, _block = parse_allocation_block(oid, alloc_root)
    if not allocated:
        return False
    badge, _ = compute_allocation_badge(allocated, vendors_all, preferred)
    return badge == "IDEAL"


def _empty_ops_payload() -> dict[str, Any]:
    return {"data": {}}


def _line_cart_mrp(line: dict[str, Any]) -> float | None:
    """Cart-time unit MRP on an order line (top-level or nested under ``offer``)."""
    direct = _num(line.get("unit_cart_item_mrp"))
    if direct is not None:
        return direct
    offer = line.get("offer")
    if isinstance(offer, dict):
        return _num(offer.get("unit_cart_item_mrp"))
    return None


def compute_order_mrp_increase(order: dict[str, Any]) -> tuple[float, str, bool]:
    """
    Order-level MRP increase vs cart (increases only).

    Returns ``(total_increase, source, line_fields_used)``.
    """
    lines = order.get("order_lines") or []
    total = 0.0
    used_line_fields = False
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        cart = _line_cart_mrp(ln)
        current = _num(ln.get("unit_current_mrp"))
        qty = _num(ln.get("quantity"))
        if cart is None or current is None or qty is None or qty <= 0:
            continue
        used_line_fields = True
        total += max(0.0, current - cart) * qty

    if used_line_fields:
        return total, "order_lines", True

    ps = order.get("payment_summary")
    if isinstance(ps, dict):
        cart_mrp = _num(ps.get("cart_mrp"))
        current_mrp = _num(ps.get("current_mrp"))
        if cart_mrp is not None and current_mrp is not None:
            return max(0.0, current_mrp - cart_mrp), "payment_summary", False

    return 0.0, "none", False


def _empty_sku_price_delta_block(*, total_key: str) -> dict[str, Any]:
    return {total_key: 0.0, "count": 0, "source": "none", "lines": []}


def _build_sku_price_delta_block(
    order: dict[str, Any], *, direction: str
) -> dict[str, Any]:
    """Per-SKU MRP deltas. ``direction`` is ``increase`` or ``decrease``."""
    total_key = f"total_{direction}"
    line_key = f"line_{direction}"
    empty = _empty_sku_price_delta_block(total_key=total_key)
    if not isinstance(order, dict) or direction not in ("increase", "decrease"):
        return empty

    detail: list[dict[str, Any]] = []
    total = 0.0
    used_line_fields = False
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        cart = _line_cart_mrp(ln)
        current = _num(ln.get("unit_current_mrp"))
        qty = _num(ln.get("quantity"))
        if cart is None or current is None or qty is None or qty <= 0:
            continue
        used_line_fields = True
        unit_delta = current - cart
        if direction == "increase" and unit_delta <= 0:
            continue
        if direction == "decrease" and unit_delta >= 0:
            continue
        line_amt = abs(unit_delta) * qty
        total += line_amt
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        detail.append(
            {
                "sku_id": _s(sku.get("sku_id") or ln.get("order_line_id")) or None,
                "name": _s(sku.get("name") or ln.get("name")) or None,
                "qty": qty,
                "unit_cart_mrp": round(cart, 4),
                "unit_current_mrp": round(current, 4),
                line_key: round(line_amt, 4),
            }
        )

    if detail:
        return {
            total_key: round(total, 2),
            "count": len(detail),
            "source": "order_lines",
            "lines": detail,
        }

    # Line MRP fields present but no delta in this direction — do not invent
    # an opposing payment_summary total.
    if used_line_fields:
        return empty

    if direction == "increase":
        total, source, _ = compute_order_mrp_increase(order)
        if source == "payment_summary" and total > 0:
            return {
                total_key: round(total, 2),
                "count": 0,
                "source": source,
                "lines": [],
            }
    else:
        ps = order.get("payment_summary") if isinstance(order.get("payment_summary"), dict) else None
        if isinstance(ps, dict):
            cart_mrp = _num(ps.get("cart_mrp"))
            current_mrp = _num(ps.get("current_mrp"))
            if cart_mrp is not None and current_mrp is not None and current_mrp < cart_mrp:
                return {
                    total_key: round(cart_mrp - current_mrp, 2),
                    "count": 0,
                    "source": "payment_summary",
                    "lines": [],
                }
    return empty


def build_sku_price_increases(order: dict[str, Any]) -> dict[str, Any]:
    """Per-SKU MRP increases. Safe empty dict on bad input."""
    return _build_sku_price_delta_block(order, direction="increase")


def build_sku_price_decreases(order: dict[str, Any]) -> dict[str, Any]:
    """Per-SKU MRP decreases. Safe empty dict on bad input."""
    return _build_sku_price_delta_block(order, direction="decrease")


def detect_allocation_pushback(child_status_rows: list[dict[str, Any]]) -> bool | None:
    """
    Count-based pushback:
    Treat 20 and 120 as one bucket and return True when bucket count >= 2.
    This covers: (20 + 120), (20 + 20), and (120 + 120).
    """
    if not child_status_rows:
        return None
    rows = sorted(child_status_rows, key=lambda r: _s(r.get("created")) or "")
    pushback_count = 0
    for row in rows:
        sid = _s(row.get("status")) or _s(row.get("status_id")) or ""
        if sid in PUSHBACK_ALLOCATION_STATUS_IDS:
            pushback_count += 1
    return pushback_count >= 2


def _delivery_score_state(
    order: dict[str, Any], delivery: dict[str, Any]
) -> tuple[bool, str, str]:
    """Return ``(pass, label, detail)`` for delivery pillar."""
    from app.agents.order_rca import eta_resolution
    from app.agents.order_rca.constants import (
        DELIVERY_BREACH_DELIVERED_LATE,
        DELIVERY_BREACH_OPEN_PAST,
    )

    actual = _s(delivery.get("actual_delivery"))
    breached = delivery.get("is_eta_breached")
    kind = delivery.get("breach_kind")
    breach_min = delivery.get("breach_minutes")
    if breached is None or kind is None:
        late = delivery.get("late_minutes")
        late_min = int(late) if isinstance(late, (int, float)) and late > 0 else None
        sla = eta_resolution.compute_delivery_sla(
            promised_first=delivery.get("promised_first"),
            actual_delivery=actual or None,
            late_minutes=late_min,
            is_eta_breached_order_api=delivery.get("is_eta_breached_order_api")
            or order.get("is_eta_breached"),
        )
        breached = sla["is_eta_breached"]
        kind = sla["breach_kind"]
        breach_min = sla["breach_minutes"]

    if breached:
        parts: list[str] = []
        late_label = format_duration_minutes(breach_min) if isinstance(breach_min, int) and breach_min > 0 else None
        if kind == DELIVERY_BREACH_DELIVERED_LATE and late_label:
            parts.append(f"{late_label} late vs first promise")
        elif kind == DELIVERY_BREACH_OPEN_PAST:
            if late_label:
                parts.append(f"{late_label} past first promise; not delivered")
            else:
                parts.append("Past first customer promise; not delivered")
        else:
            parts.append("Delivery SLA breached")
        return False, "Breached", "; ".join(parts) or "Delivery SLA breached"

    if actual:
        return True, "On time", f"Delivered ({actual})"

    return True, "Pending", "Within first promise window or awaiting delivery"


def perfect_order_pillar_failed(pillar: dict[str, Any]) -> bool:
    """Match overall_pass: any pillar with pass is not True fails (incl. unknown pushback)."""
    if pillar.get("counts_as_perfect"):
        return False
    return pillar.get("pass") is not True


_FAILED_PILLAR_NARRATIVE_ORDER: tuple[str, ...] = (
    "delivery",
    "allocation",
    "pushback",
    "price_integrity",
)


def rank_failed_pillars(pillars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failed pillars for narrative — delivery before allocation when both fail."""
    failed = [p for p in pillars if isinstance(p, dict) and perfect_order_pillar_failed(p)]
    order = {pid: i for i, pid in enumerate(_FAILED_PILLAR_NARRATIVE_ORDER)}
    failed.sort(key=lambda p: order.get(str(p.get("id") or ""), 99))
    return failed


def build_perfect_order_scorecard(
    order: dict[str, Any],
    *,
    allocation_badge: str | None,
    allocation_unavailable: bool,
    child_status_rows: list[dict[str, Any]],
    delivery: dict[str, Any],
) -> dict[str, Any]:
    """Five-pillar Perfect Order scorecard for RCA preflight UI."""
    badge = (allocation_badge or "").strip().upper()
    alloc_pass = badge == "IDEAL" and not allocation_unavailable
    if allocation_unavailable:
        alloc_label, alloc_detail = "N/A", "Allocation data unavailable"
    elif alloc_pass:
        alloc_label, alloc_detail = "Ideal", "Allocated to preferred store or warehouse"
    else:
        alloc_label, alloc_detail = "Non-ideal", badge or "CROSS"

    pushback = detect_allocation_pushback(child_status_rows)
    if pushback is None:
        pb_pass = None
        pb_status = "unknown"
        pb_label, pb_detail = "Unknown", "Status history unavailable"
    elif pushback:
        pb_pass = False
        pb_status = "fail"
        pb_label, pb_detail = (
            "Pushed back",
            "Returned to allocation (status 20 or 120) after fulfilment started",
        )
    else:
        pb_pass = True
        pb_status = "pass"
        pb_label, pb_detail = "None", "No allocation regression on this PO"

    del_pass, del_label, del_detail = _delivery_score_state(order, delivery)

    mrp_inc, mrp_source, _ = compute_order_mrp_increase(order)
    limit = PERFECT_ORDER_MRP_INCREASE_LIMIT
    price_pass = mrp_inc <= limit
    if mrp_inc <= 0:
        price_label = "No increase"
        price_detail = "No MRP increase vs cart"
    elif price_pass:
        price_label = f"≤ ₹{int(limit)}"
        price_detail = f"Total MRP increase ₹{mrp_inc:.2f} ({mrp_source})"
    else:
        price_label = f"> ₹{int(limit)}"
        price_detail = f"Total MRP increase ₹{mrp_inc:.2f} ({mrp_source})"

    pillars: list[dict[str, Any]] = [
        {
            "id": "allocation",
            "pass": alloc_pass,
            "status": "pass" if alloc_pass else "fail",
            "label": alloc_label,
            "detail": alloc_detail,
        },
        {
            "id": "pushback",
            "pass": pb_pass,
            "status": pb_status,
            "label": pb_label,
            "detail": pb_detail,
        },
        {
            "id": "delivery",
            "pass": del_pass,
            "status": "pass" if del_pass else "fail",
            "label": del_label,
            "detail": del_detail,
        },
        {
            "id": "customer_contact",
            "pass": None,
            "status": "unknown",
            "label": "Unknown",
            "detail": "AOR detection not configured",
            "counts_as_perfect": True,
        },
        {
            "id": "price_integrity",
            "pass": price_pass,
            "status": "pass" if price_pass else "fail",
            "label": price_label,
            "detail": price_detail,
        },
    ]

    overall_pass = True
    for p in pillars:
        if p.get("counts_as_perfect"):
            continue
        if p.get("pass") is not True:
            overall_pass = False
            break

    return {
        "overall": "perfect" if overall_pass else "imperfect",
        "overall_pass": overall_pass,
        "hint": PERFECT_ORDER_PRICE_HINT,
        "total_mrp_increase": round(mrp_inc, 2),
        "mrp_increase_limit": limit,
        "mrp_increase_source": mrp_source,
        "pillars": pillars,
    }


def build_facts(bundle: dict[str, Any]) -> dict[str, Any]:
    order = bundle.get("order") or {}
    fulfillment = fulfillment_order(order)
    oid = bundle.get("order_id") or _display(order.get("order_id"))
    parent_id = bundle.get("parent_id")
    alloc_root = (bundle.get("allocation") or {}).get("data") or {}
    allocated, vendors_all, preferred, block = parse_allocation_block(oid, alloc_root)
    selected = block.get("selected_vendors") or {}
    rejected = block.get("rejected_vendors") or {}

    alloc_unavail = build_allocation_unavailable(order, block)
    if alloc_unavail:
        alloc_type = "N/A"
        alloc_badge_reason = alloc_unavail["message"]
    else:
        alloc_type, alloc_badge_reason = compute_allocation_badge(allocated, vendors_all, preferred)

    alloc_code = allocated.get("vendor_code") if allocated else None
    on_preferred = alloc_type == "IDEAL"
    alloc_phys = physical_store_key(_display(alloc_code)) if alloc_code else None

    addr = order.get("delivery_address") or {}
    eta_o = order.get("eta") or {}
    delivery = extract_order_delivery({**bundle, "order": fulfillment})
    promised = delivery.get("promised_delivery")
    actual_del = delivery.get("actual_delivery")
    late_min = delivery.get("late_minutes")

    qty_map = order_qty_by_sku(fulfillment)
    skus = _order_skus(order)
    placed_dt, placed_has_time = _parse_ts_value(order.get("created"))
    placed_at_ist = (
        placed_dt.astimezone(_ORDER_RCA_DISPLAY_TZ)
        if placed_dt is not None and placed_has_time
        else None
    )
    p3_seen: set[str] = set()
    p3_rows: list[dict[str, Any]] = []

    def _p3_row(v: dict[str, Any], outcome: str) -> dict[str, Any]:
        eta = v.get("eta")
        sel = bool(v.get("_selected")) or outcome in ("selected", "allocated")
        row: dict[str, Any] = {
            "vendor_code": _display(v.get("vendor_code")),
            "label": vendor_label(v),
            **_virtual_store_fields(v),
            "distance_km": _num(v.get("distance")),
            "road_km": round((_num(v.get("distance")) or 0) * 1.6, 2),
            **location_serviceable_fields(v, selected=sel, placed_at=placed_at_ist),
            "standard_available": standard_available(eta) if sel else None,
            "rapid_services": rapid_eta_keys(eta) if sel else [],
            "rapid_eta": eta if sel and isinstance(eta, dict) and eta else None,
            "availability_count": v.get("availability_count"),
            "outcome": outcome,
        }
        row["advertised_rapid_sla"] = advertised_rapid_sla_rows(
            v,
            order=order,
            active_services=row.get("active_services"),
            inactive_services=row.get("inactive_services"),
        )
        if outcome == "rejected":
            rej = analyze_vendor_rejection(v, order_qty_by_sku=qty_map)
            row["inventory_not_available"] = rej["inventory_not_available"]
            row["inventory_partial"] = rej.get("inventory_partial")
            row["inventory_class"] = rej.get("inventory_class")
            row["inventory_label"] = rej.get("inventory_label")
            row["inventory_classes"] = rej.get("inventory_classes")
            row["inventory_labels"] = rej.get("inventory_labels")
            row["service_not_available"] = rej["service_not_available"]
            row["signals"] = rej["signals"]
            row["reason_short"] = rej.get("reason_short")
            row["implicit_rejection"] = rej.get("implicit_rejection")
            row["split_enabled"] = rej.get("split_enabled")
        elif outcome == "allocated":
            row["signals"] = ["Allocated to this store"]
            row["reason_short"] = "Allocated to this store"
        return row

    if allocated:
        code = _display(allocated.get("vendor_code"))
        p3_rows.append(_p3_row(allocated, "allocated"))
        p3_seen.add(code)
    for v in vendors_all:
        code = _display(v.get("vendor_code"))
        if code in p3_seen or len(p3_rows) >= 40:
            continue
        outcome = "selected" if v.get("_selected") else "rejected"
        p3_rows.append(_p3_row(v, outcome))
        p3_seen.add(code)

    def _p3_outcome(v: dict[str, Any]) -> str:
        if allocated and _display(v.get("vendor_code")) == _display(allocated.get("vendor_code")):
            return "allocated"
        return "selected" if v.get("_selected") else "rejected"

    p3_views = build_ranked_store_views(
        vendors_all,
        virtual_store_builder=lambda v: _p3_row(v, _p3_outcome(v)),
        allocated=allocated,
    )
    p4_rows = [
        _p4_virtual_store_row(
            v,
            order_qty_by_sku=qty_map,
            placed_at=placed_at_ist,
            order=order,
            allocated=allocated,
        )
        for v in sorted(rejected.values(), key=lambda x: _num(x.get("distance")) or 9e9)[:25]
    ]
    p4_views = build_ranked_store_views(
        list(rejected.values()),
        virtual_store_builder=lambda v: _p4_virtual_store_row(
            v, order_qty_by_sku=qty_map, placed_at=placed_at_ist, order=order, allocated=allocated
        ),
        allocated=allocated,
    )
    p1_fixture_path = (
        Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "order_rca" / "p1_msn.json"
    )
    if (settings.order_rca_fixture_dir or "").strip():
        p1_fixture_path = Path(settings.order_rca_fixture_dir) / "p1_msn.json"
    p1_api_configured = bool((settings.order_rca_p1_msn_base_url or "").strip()) or (
        settings.order_rca_use_fixtures and p1_fixture_path.is_file()
    )
    msn_block = msn_adherence.build_msn_adherence_block(
        matrix_store_groups=msn_adherence.collect_matrix_physical_stores(p3_views),
        order_skus=_order_skus(fulfillment),
        asked_qty_by_sku=qty_map,
        p1_msn_raw=bundle.get("p1_msn") or {},
        p1_api_configured=p1_api_configured,
    )
    split_insights = build_split_insights(
        parent_id=parent_id,
        order_id=oid,
        alloc_root=alloc_root,
        allocated=allocated,
        parent_order=bundle.get("parent_order"),
        child_order=order,
    )
    vendor_types = sorted(
        {vendor_type_label(_s(v.get("vendor_type"))) for v in vendors_all if _s(v.get("vendor_type"))}
    )

    child_status = _status_rows(bundle.get("status"), oid)
    parent_status = _status_rows(bundle.get("parent_status"), parent_id) if parent_id else []
    status_chronology, timeline_meta = merge_order_status_timeline(
        parent_id=parent_id,
        parent_rows=parent_status,
        child_rows=child_status,
        order_id=oid,
    )
    is_active = is_active_service_order(order, allocated)
    ops_sla = build_ops_sla_context(order, is_active=is_active)
    status_transitions = enrich_ops_transitions(
        build_status_transitions(status_chronology),
        ops_sla,
    )
    status_chronology = [
        {**r, "at_display": _format_ts_display(r.get("at"))} for r in status_chronology
    ]
    split_child = bool(timeline_meta.get("split_child"))

    segments = [
        x
        for x in [
            _segment_from_timeline(status_chronology, "130", "25", SEGMENT_PACKAGING_TO_TBD),
            _segment_from_timeline(status_chronology, "25", "30", SEGMENT_TBD_TO_OFD),
            _segment_from_timeline(status_chronology, "30", "40", SEGMENT_OFD_TO_DELIVERED),
        ]
        if x
    ]

    groot_events = parse_groot_events(bundle.get("groot"))
    clickpost_raw = bundle.get("clickpost") if isinstance(bundle.get("clickpost"), dict) else {}
    clickpost_events = parse_clickpost_events(clickpost_raw) if not groot_events else []
    last_mile_mode = resolve_last_mile_mode(groot_events, clickpost_events)
    shipping_summary = build_shipping_summary(order)
    meta_sources = ["order", "allocation", "status", "history", "analytics", "groot", "soft_allocation"]
    if last_mile_mode == "clickpost":
        meta_sources.append("clickpost")

    warnings: list[str] = []
    if not groot_events and isinstance(clickpost_raw, dict) and clickpost_raw.get("is_success") is False:
        err = clickpost_raw.get("error")
        msg = err.get("message") if isinstance(err, dict) else None
        if msg:
            warnings.append(f"ClickPost tracking unavailable: {msg}")
        else:
            warnings.append("ClickPost tracking unavailable for this delivery partner")
    elif (
        not groot_events
        and not clickpost_events
        and shipping_summary.get("waybill")
        and shipping_summary.get("delivery_partners_code")
        and not any("ClickPost" in w for w in warnings)
    ):
        warnings.append(
            "ClickPost tracking not available (empty response, missing post-order URL, or unsupported partner)"
        )
    if alloc_unavail:
        warnings.append(alloc_unavail["message"])
    if not child_status:
        warnings.append("No status history for fulfilling PO")
    history_payload = bundle.get("history") or {}
    hist_items = history_payload.get("history") or []
    hist_total = history_payload.get("total_count")
    try:
        if hist_total is not None and int(hist_total) > len(hist_items):
            warnings.append(
                f"Order history incomplete: loaded {len(hist_items)} of {hist_total} entries "
                "(pagination may have failed)"
            )
    except (TypeError, ValueError):
        pass

    hist_insights = extract_history_insights(history_payload)
    if parent_id and not bundle.get("parent_order"):
        warnings.append("Parent order API record unavailable — split context uses allocation only")

    if alloc_unavail:
        note = alloc_unavail["message"]
        p3_views = {**p3_views, "panel_note": note, "status": "unavailable"}
        p4_views = {**p4_views, "panel_note": note, "status": "unavailable"}
        msn_block = {**msn_block, "panel_note": note, "status": "unavailable", "stores": []}

    on_preferred = False if alloc_unavail else on_preferred

    perfect_order = build_perfect_order_scorecard(
        fulfillment,
        allocation_badge=alloc_type,
        allocation_unavailable=bool(alloc_unavail),
        child_status_rows=child_status,
        delivery=delivery,
    )
    if perfect_order.get("mrp_increase_source") == "none" and fulfillment.get("order_lines"):
        warnings.append(
            "Perfect Order price: MRP cart/current fields missing on order API — counted as no increase"
        )

    try:
        sku_price_increases = build_sku_price_increases(fulfillment)
        sku_price_decreases = build_sku_price_decreases(fulfillment)
    except Exception:
        log.exception("sku_price_deltas failed — skipping blocks")
        sku_price_increases = _empty_sku_price_delta_block(total_key="total_increase")
        sku_price_decreases = _empty_sku_price_delta_block(total_key="total_decrease")

    soft_payload = (
        bundle.get("soft_allocation") if isinstance(bundle.get("soft_allocation"), dict) else None
    )
    history_payload = bundle.get("history") if isinstance(bundle.get("history"), dict) else None
    parent_order = (
        bundle.get("parent_order") if isinstance(bundle.get("parent_order"), dict) else None
    )
    raw_family = bundle.get("family_orders")
    family_orders = (
        [o for o in raw_family if isinstance(o, dict)] if isinstance(raw_family, list) else None
    )
    post_order_sku_removals = soft_allocation.build_post_order_sku_removals(
        order,
        soft_payload,
        history_payload,
        parent_order=parent_order,
        family_orders=family_orders,
    )

    return {
        "order_id": oid,
        "parent_id": parent_id,
        "schema_version": 1,
        "analysis_mode": "allocation_unavailable" if alloc_unavail else "full",
        "analysis_skipped": False,
        "allocation_unavailable": alloc_unavail,
        "warnings": warnings,
        "perfect_order": perfect_order,
        "sku_price_increases": sku_price_increases,
        "sku_price_decreases": sku_price_decreases,
        "post_order_sku_removals": post_order_sku_removals,
        "meta": {
            "sources": meta_sources,
            "timezone": _timezone_meta(),
        },
        "preflight": {
            "placed_at": _format_ts_display(order.get("created")),
            "city": _display(addr.get("city")),
            "state": _display(addr.get("state")),
            "pincode": _display(addr.get("pincode")),
            "zone": _display((eta_o.get("zone") or {}).get("name") if isinstance(eta_o.get("zone"), dict) else None),
            "promised_delivery": promised,
            "promised_delivery_source": delivery.get("promised_delivery_source"),
            "promised_first": delivery.get("promised_first"),
            "promised_current": delivery.get("promised_current"),
            "eta_jumps": delivery.get("eta_jumps"),
            "promised_delivery_raw": delivery.get("promised_delivery_raw"),
            "actual_delivery": actual_del,
            "actual_delivery_source": delivery.get("actual_delivery_source"),
            "late_minutes": late_min,
            "late_minutes_display": delivery.get("late_minutes_display"),
            "late_minutes_vs": delivery.get("late_minutes_vs"),
            "is_eta_breached": delivery.get("is_eta_breached"),
            "is_eta_breached_order_api": delivery.get("is_eta_breached_order_api"),
            "breach_kind": delivery.get("breach_kind"),
            "breach_minutes": delivery.get("breach_minutes"),
            "breach_minutes_display": delivery.get("breach_minutes_display"),
            "diagnosed_at": delivery.get("diagnosed_at"),
            "order_status": _display(order.get("status")),
            "order_status_id": _s(order.get("status_id")),
            "service_type": order_service_type_label(order),
            "return_reason": hist_insights.get("return_reason"),
            "actual_vendor": vendor_label(allocated) if allocated else _MISSING,
            "actual_vendor_code": _display(alloc_code),
            "allocation_badge": alloc_type,
            "allocation_badge_reason": alloc_badge_reason,
            "allocated_to_preferred": on_preferred,
            "allocation_tier_label": (
                _MISSING
                if alloc_unavail
                else ("Preferred" if on_preferred else "Non-preferred")
            ),
            "preferred_vendors": build_preferred_physical_stores(preferred),
            "distance_km": _num(allocated.get("distance")) if allocated else None,
            "escalation_reason": None,
        },
        "skus": skus,
        "cart_allocation_journey": soft_allocation.build_cart_allocation_journey(
            order,
            soft_payload,
        ),
        "p1": {
            "status": msn_block["status"],
            "msn_adherence": msn_block,
            "rows": [],
        },
        "p2": {
            "status": "not_configured",
            "rows": [],
            "status_message": (
                "Procurement funnel (drop and replenish) is not connected yet. "
                "This panel will show planning metrics when the API is available."
            ),
        },
        "p3": {"rows": p3_rows, **p3_views},
        "p4": {"rows": p4_rows, **p4_views},
        "split_insights": split_insights,
        "vendor_types_in_allocation": vendor_types,
        "operations": {
            "status_chronology": status_chronology,
            "status_transitions": status_transitions,
            "ops_sla": ops_sla,
            "segments": segments,
            "groot_events": groot_events,
            "groot_empty": len(groot_events) == 0,
            "clickpost_events": clickpost_events,
            "clickpost_empty": len(clickpost_events) == 0,
            "last_mile_mode": last_mile_mode,
            "shipping_summary": shipping_summary,
            "history_count": len((bundle.get("history") or {}).get("history") or []),
            **timeline_meta,
            "return_followed": _chronology_has_return(status_chronology),
            "return_note": OPS_RETURN_NOTE if _chronology_has_return(status_chronology) else None,
            "timeline_note": (
                f"Split child: milestones through Vendor Stock Allocation use parent {parent_id}; "
                f"Packaging onward use this PO ({oid}). Durations are wall-clock between steps. "
                f"All clock times are {ORDER_RCA_DISPLAY_TZ_LABEL}."
                if split_child and parent_id
                else f"Status milestones and durations from this order only. "
                f"All clock times are {ORDER_RCA_DISPLAY_TZ_LABEL}."
            ),
        },
        "signals": _compact_signals(
            alloc_type,
            allocated,
            p4_rows,
            segments,
            groot_events,
            clickpost_events,
            last_mile_mode,
            warnings,
        ),
    }


def _groot_signals(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["Groot: no hyperlocal timeline"]
    out = [f"Groot: {len(events)} events"]
    statuses = [str(e.get("status") or "").lower() for e in events]
    if any("on_the_way" in s or s == "on_the_way" for s in statuses):
        out.append("Groot: out-for-delivery / on-the-way present")
    if any("completed" in s for s in statuses):
        out.append("Groot: delivery completed")
    cancelish = sum(1 for e in events if "cancel" in str(e.get("comments", "")).lower())
    if cancelish:
        out.append(f"Groot: {cancelish} cancel/retry-related comments")
    riders = {e.get("performed_by") for e in events if e.get("performed_by")}
    if riders:
        out.append(f"Groot: performers={', '.join(sorted(riders)[:3])}")
    return out


def _clickpost_signals(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return []
    out = [f"ClickPost: {len(events)} courier scan buckets"]
    statuses = [str(e.get("status") or "") for e in events]
    if any("delivered" in s.lower() for s in statuses):
        out.append("ClickPost: delivered scan present")
    if any("out for delivery" in s.lower() for s in statuses):
        out.append("ClickPost: out-for-delivery scan present")
    oldest, newest = events[-1], events[0]
    if oldest.get("status") and newest.get("status") and oldest is not newest:
        out.append(
            f"ClickPost: span {oldest.get('status')} ({oldest.get('at')}) "
            f"→ {newest.get('status')} ({newest.get('at')})"
        )
    return out


def _last_mile_signals(
    mode: str,
    groot_events: list[dict[str, Any]],
    clickpost_events: list[dict[str, Any]],
) -> list[str]:
    if mode == "groot":
        return _groot_signals(groot_events)
    if mode == "clickpost":
        return _clickpost_signals(clickpost_events)
    return ["Last mile: no Groot or ClickPost timeline"]


def _compact_signals(
    alloc_type: str,
    allocated: dict[str, Any] | None,
    p4: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    groot_events: list[dict[str, Any]],
    clickpost_events: list[dict[str, Any]],
    last_mile_mode: str,
    warnings: list[str],
) -> list[str]:
    out = [f"Allocation: {alloc_type}"]
    if allocated:
        out.append(f"Allocated: {allocated.get('vendor_code')} ({allocated.get('vendor_type')})")
    if p4:
        out.append(f"Rejected virtual vendors considered: {len(p4)}")
        implicit = [
            r
            for r in p4
            if r.get("implicit_rejection")
            or any("Not chosen by allocation engine" in str(s) for s in (r.get("signals") or []))
        ]
        if implicit:
            codes = [_display(r.get("vendor_code")) for r in implicit[:4]]
            out.append(
                "Rejected without explicit inv/loc/svc flags: "
                + ", ".join(c for c in codes if c != _MISSING)
                + " (same physical store may have other virtual stores with explicit reasons)"
            )
    for seg in segments:
        dm = seg.get("duration_min")
        out.append(f"{seg.get('label')}: {dm} min" if dm is not None else seg.get("label", "segment"))
    out.extend(_last_mile_signals(last_mile_mode, groot_events, clickpost_events))
    out.extend(warnings)
    return out
