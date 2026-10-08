"""Pre-order soft allocation cart journey (#19) — parse snapshots and build RCA facts."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from app.agents.order_rca import rules
from app.agents.order_rca.constants import ORDER_RCA_DISPLAY_TZ_LABEL
from app.agents.order_rca.time_utils import (
    ORDER_RCA_API_TZ,
    ORDER_RCA_DISPLAY_TZ,
    format_instant_ist,
    parse_num,
    parse_str,
    parse_unix_ts,
)

log = logging.getLogger(__name__)

_CART_TS_RE = re.compile(
    r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(IST)?$",
    re.I,
)

_STEP_KIND_LABELS = {
    "start": "Cart snapshot",
    "skus": "SKU edit",
    "layout": "Layout change",
    "options": "Options change",
    "fastest_eta": "ETA refresh",
    "shipment_options": "Shipment refresh",
}


def parse_cart_timestamp(value: Any) -> datetime | None:
    """Soft-alloc ``cart_timestamp`` — IST wall clock string or unix epoch (UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
        return parse_unix_ts(value)
    s = parse_str(value)
    if not s:
        return None
    m = _CART_TS_RE.match(s.strip())
    if m:
        day, mon, year, hh, mm, ss, _ = m.groups()
        try:
            dt = datetime.strptime(f"{day} {mon} {year} {hh}:{mm}:{ss}", "%d %B %Y %H:%M:%S")
            return dt.replace(tzinfo=ORDER_RCA_DISPLAY_TZ)
        except ValueError:
            pass
    dt = parse_unix_ts(s)
    if dt:
        return dt
    for fmt in ("%d %B %Y %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(s.replace(f" {ORDER_RCA_DISPLAY_TZ_LABEL}", "").strip(), fmt).replace(
                tzinfo=ORDER_RCA_DISPLAY_TZ
            )
        except ValueError:
            continue
    return None


def order_phone(order: dict[str, Any]) -> str | None:
    user = order.get("user") if isinstance(order.get("user"), dict) else {}
    num = parse_str(user.get("number"))
    if num:
        return num
    addr = order.get("delivery_address") if isinstance(order.get("delivery_address"), dict) else {}
    return parse_str(addr.get("contact_number"))


def order_placed_at(order: dict[str, Any]) -> datetime | None:
    """Place-order instant from ``order.created`` (UTC epoch in prod)."""
    return parse_unix_ts(order.get("created"))


def order_sku_qty_map(order: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for ln in rules.fulfillment_order_lines(order):
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        sid = parse_str(sku.get("sku_id") or ln.get("order_line_id"))
        qty = rules.ordered_quantity_from_line(ln)
        if sid and qty is not None:
            out[sid] = out.get(sid, 0.0) + qty
    return out


def _cart_skus(snapshot: dict[str, Any]) -> dict[str, float]:
    info = snapshot.get("skus_info") if isinstance(snapshot.get("skus_info"), dict) else {}
    out: dict[str, float] = {}
    for sid, block in info.items():
        if not isinstance(block, dict):
            continue
        q = parse_num(block.get("quantity"))
        if q is not None and q > 0:
            out[str(sid)] = q
    return out


def _sku_overlap_score(cart: dict[str, float], order_skus: dict[str, float]) -> float:
    if not order_skus:
        return 0.0
    matched = sum(1 for sid in order_skus if sid in cart)
    return matched / len(order_skus)


def _shipment_groups(shipments: dict[str, Any] | None) -> tuple[str, list[list[dict[str, Any]]]]:
    if not isinstance(shipments, dict):
        return "none", []
    single = shipments.get("single") or []
    multi = shipments.get("multi") or []
    has_single = bool(single)
    has_multi = bool(multi)
    if has_single and not has_multi:
        layout = "single"
        raw = single
    elif has_multi and not has_single:
        layout = "multi"
        raw = multi
    elif has_single and has_multi:
        layout = "mixed"
        raw = single + multi
    else:
        return "none", []

    groups: list[list[dict[str, Any]]] = []
    for block in raw:
        if isinstance(block, list):
            opts = [o for o in block if isinstance(o, dict)]
            if opts:
                groups.append(opts)
        elif isinstance(block, dict):
            groups.append([block])
    return layout, groups


def _layout_label(layout: str, group_count: int) -> str:
    if layout == "single":
        return "Single shipment"
    if layout == "multi":
        n = group_count or 0
        return f"Split shipment ({n} group{'s' if n != 1 else ''})"
    if layout == "mixed":
        return "Single + split options"
    return "No shipment options"


def _short_eta(eta_to: str | None) -> str | None:
    if not eta_to:
        return None
    s = eta_to.replace(f" {ORDER_RCA_DISPLAY_TZ_LABEL}", "").strip()
    if " " in s:
        parts = s.split()
        if len(parts) >= 4:
            return f"{parts[0]} {parts[1]}, {parts[3][:5]}"
    return s


def _option_key(opt: dict[str, Any]) -> tuple:
    return (
        parse_str(opt.get("title")) or "",
        parse_str(opt.get("eta_to")) or "",
        parse_str(opt.get("service_name")) or "",
        tuple(str(x) for x in (opt.get("sku_ids") or [])),
    )


def _option_view(opt: dict[str, Any], *, is_fastest: bool) -> dict[str, Any]:
    title = parse_str(opt.get("title"))
    eta = parse_str(opt.get("eta_to"))
    return {
        "title": title,
        "eta_to": eta,
        "eta_short": _short_eta(eta),
        "service_name": parse_str(opt.get("service_name")),
        "vendor_code": parse_str(opt.get("vendor_code")),
        "sku_ids": [str(x) for x in (opt.get("sku_ids") or [])],
        "is_fastest": is_fastest,
    }


def _sku_labels(sku_ids: list[str], names: dict[str, str]) -> str:
    if not sku_ids:
        return ""
    labels = [names.get(sid) or sid for sid in sku_ids]
    return ", ".join(labels)


def _option_slot_key(opt: dict[str, Any]) -> tuple:
    """Stable slot identity — same SKUs + service, ignoring title/ETA."""
    return (
        tuple(sorted(str(x) for x in (opt.get("sku_ids") or []))),
        parse_str(opt.get("service_name")) or "",
    )


def _format_option_change(prev: dict[str, Any], cur: dict[str, Any]) -> str | None:
    pt = parse_str(prev.get("title"))
    ct = parse_str(cur.get("title"))
    pe = parse_str(prev.get("eta_to"))
    ce = parse_str(cur.get("eta_to"))
    if pt and ct and pt != ct:
        if pe != ce and pe and ce:
            return f"{pt} → {ct} ({_short_eta(pe)} → {_short_eta(ce)})"
        return f"{pt} → {ct}"
    if pt and pe != ce:
        return f"{pt}: {_short_eta(pe)} → {_short_eta(ce)}"
    if ct and pe != ce:
        return f"{ct}: {_short_eta(pe)} → {_short_eta(ce)}"
    return None


def _diff_option_group(
    prev_group: list[dict[str, Any]],
    group: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return ``added``, ``removed``, ``changed``, ``eta_changed`` for one shipment group."""
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    eta_changed: list[str] = []

    prev_by_title = {parse_str(o.get("title")): o for o in prev_group if parse_str(o.get("title"))}
    cur_by_title = {parse_str(o.get("title")): o for o in group if parse_str(o.get("title"))}
    paired_prev: set[int] = set()
    paired_cur: set[int] = set()

    # Position pairing when option count is stable (typical ETA refresh).
    if len(prev_group) == len(group):
        for i, (po, co) in enumerate(zip(prev_group, group)):
            if _option_slot_key(po) == _option_slot_key(co):
                paired_prev.add(i)
                paired_cur.add(i)
                label = _format_option_change(po, co)
                if label:
                    changed.append(label)

    # Slot pairing when counts differ (new/removed option in group).
    for j, co in enumerate(group):
        if j in paired_cur:
            continue
        cslot = _option_slot_key(co)
        for k, po in enumerate(prev_group):
            if k in paired_prev:
                continue
            if _option_slot_key(po) == cslot:
                paired_prev.add(k)
                paired_cur.add(j)
                label = _format_option_change(po, co)
                if label:
                    changed.append(label)
                break

    # Same-title ETA updates for options not already paired by slot position.
    for j, o in enumerate(group):
        if j in paired_cur:
            continue
        title = parse_str(o.get("title"))
        if not title or title not in prev_by_title:
            continue
        prev_o = prev_by_title[title]
        if parse_str(prev_o.get("eta_to")) != parse_str(o.get("eta_to")):
            pe = parse_str(prev_o.get("eta_to"))
            ce = parse_str(o.get("eta_to"))
            eta_changed.append(f"{title}: {_short_eta(pe)} → {_short_eta(ce)}")

    for i, o in enumerate(prev_group):
        if i in paired_prev:
            continue
        title = parse_str(o.get("title"))
        if title and title not in cur_by_title:
            removed.append(title)

    for i, o in enumerate(group):
        if i in paired_cur:
            continue
        title = parse_str(o.get("title"))
        if title and title not in prev_by_title:
            added.append(title)

    return added, removed, changed, eta_changed


def _group_view(
    group: list[dict[str, Any]],
    index: int,
    *,
    names: dict[str, str],
    prev_group: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    options = [_option_view(o, is_fastest=(i == 0)) for i, o in enumerate(group)]
    sku_ids = options[0]["sku_ids"] if options else []
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    eta_changed: list[str] = []
    if prev_group is not None:
        added, removed, changed, eta_changed = _diff_option_group(prev_group, group)

    fastest = options[0] if options else {}
    return {
        "group_index": index + 1,
        "sku_ids": sku_ids,
        "sku_labels": _sku_labels(sku_ids, names),
        "option_count": len(options),
        "options": options,
        "fastest_title": fastest.get("title"),
        "fastest_eta": fastest.get("eta_to"),
        "service_name": fastest.get("service_name"),
        "options_delta": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "eta_changed": eta_changed,
            "prev_count": len(prev_group) if prev_group is not None else None,
            "cur_count": len(group),
        },
    }


def _removed_group_view(
    group: list[dict[str, Any]],
    index: int,
    *,
    names: dict[str, str],
) -> dict[str, Any]:
    options = [_option_view(o, is_fastest=(i == 0)) for i, o in enumerate(group)]
    sku_ids = options[0]["sku_ids"] if options else []
    removed = [parse_str(o.get("title")) for o in group if parse_str(o.get("title"))]
    return {
        "group_index": index + 1,
        "group_status": "removed",
        "sku_ids": sku_ids,
        "sku_labels": _sku_labels(sku_ids, names),
        "option_count": 0,
        "options": [],
        "fastest_title": None,
        "fastest_eta": None,
        "service_name": None,
        "options_delta": {
            "added": [],
            "removed": removed,
            "changed": [],
            "eta_changed": [],
            "prev_count": len(group),
            "cur_count": 0,
        },
    }


def _groups_views(
    groups: list[list[dict[str, Any]]],
    *,
    names: dict[str, str],
    prev_groups: list[list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    prev = prev_groups or []
    out: list[dict[str, Any]] = []
    max_len = max(len(groups), len(prev))
    for i in range(max_len):
        g = groups[i] if i < len(groups) else None
        pg = prev[i] if i < len(prev) else None
        if g is not None:
            view = _group_view(g, i, names=names, prev_group=pg)
            if pg is None and prev:
                view["group_status"] = "new"
            out.append(view)
        elif pg is not None:
            out.append(_removed_group_view(pg, i, names=names))
    return out


def _shipment_signature(shipments: dict[str, Any] | None) -> tuple:
    layout, groups = _shipment_groups(shipments)
    sig_groups = []
    for g in groups:
        sig_groups.append(tuple(_option_key(o) for o in g))
    return (layout, tuple(sig_groups))


def _sku_delta_parts(
    prev: dict[str, float],
    cur: dict[str, float],
    names: dict[str, str],
) -> tuple[str | None, list[dict[str, Any]]]:
    if prev == cur:
        return None, []
    parts: list[str] = []
    items: list[dict[str, Any]] = []
    for sid in sorted(set(prev) | set(cur)):
        label = names.get(sid) or sid
        p, c = prev.get(sid), cur.get(sid)
        if p is None and c is not None:
            q = int(c) if c == int(c) else c
            parts.append(f"Added {label} ×{q}")
            items.append({"kind": "added", "label": label, "qty": c})
        elif p is not None and c is None:
            parts.append(f"Removed {label}")
            items.append({"kind": "removed", "label": label})
        elif p is not None and c is not None and p != c:
            pq = int(p) if p == int(p) else p
            cq = int(c) if c == int(c) else c
            parts.append(f"{label} qty {pq}→{cq}")
            items.append({"kind": "qty", "label": label, "from_qty": p, "to_qty": c})
    if not parts:
        return "Cart items changed", [{"kind": "changed", "label": "Cart items changed"}]
    return ", ".join(parts), items


def _sku_delta_label(prev: dict[str, float], cur: dict[str, float], names: dict[str, str]) -> str | None:
    label, _ = _sku_delta_parts(prev, cur, names)
    return label


def _option_count_changed(prev_groups: list[list[dict[str, Any]]], cur_groups: list[list[dict[str, Any]]]) -> bool:
    if len(prev_groups) != len(cur_groups):
        return True
    for pg, cg in zip(prev_groups, cur_groups):
        if len(pg) != len(cg):
            return True
    return False


def _shipment_delta_label(
    prev_sig: tuple,
    cur_sig: tuple,
    prev_layout: str,
    cur_layout: str,
    *,
    prev_groups: list[list[dict[str, Any]]],
    cur_groups: list[list[dict[str, Any]]],
) -> str | None:
    if prev_sig == cur_sig:
        return None
    if prev_layout != cur_layout:
        return f"{_layout_label(prev_layout, len(prev_sig[1]))} → {_layout_label(cur_layout, len(cur_sig[1]))}"
    if _option_count_changed(prev_groups, cur_groups):
        prev_n = sum(len(g) for g in prev_groups)
        cur_n = sum(len(g) for g in cur_groups)
        return f"Delivery options shown: {prev_n} → {cur_n}"
    for i, (pg, cg) in enumerate(zip(prev_groups, cur_groups)):
        if pg and cg and parse_str(pg[0].get("title")) != parse_str(cg[0].get("title")):
            pt = parse_str(pg[0].get("title"))
            ct = parse_str(cg[0].get("title"))
            if pt and ct:
                prefix = f"Shipment {i + 1} " if len(cur_groups) > 1 else ""
                return f"{prefix}Fastest shown: {pt} → {ct}"
    return "Shipment options refreshed"


def _step_kind(changes: list[str], *, is_first: bool) -> str:
    if is_first:
        return "start"
    for key in ("layout", "skus", "options", "fastest_eta", "shipment_options"):
        if key in changes:
            return key
    return "shipment_options"


def _format_sku_summary(skus: dict[str, float], names: dict[str, str]) -> str:
    if not skus:
        return "Empty cart"
    parts = []
    for sid, qty in sorted(skus.items()):
        name = names.get(sid) or sid
        q = int(qty) if qty == int(qty) else qty
        parts.append(f"{name} ×{q}")
    return ", ".join(parts)


def _collect_sku_names(order: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        sid = parse_str(sku.get("sku_id") or ln.get("order_line_id"))
        if sid:
            names[sid] = parse_str(sku.get("name")) or parse_str(ln.get("name")) or sid
    for snap in snapshots:
        info = snap.get("skus_info") if isinstance(snap.get("skus_info"), dict) else {}
        for sid, block in info.items():
            if not isinstance(block, dict):
                continue
            if sid not in names:
                names[str(sid)] = parse_str(block.get("name")) or str(sid)
    return names


def _collect_snapshots(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("response")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def pick_cart_id(
    snapshots: list[dict[str, Any]],
    *,
    placed_at: datetime,
    order_skus: dict[str, float],
) -> str | None:
    placed_utc = placed_at.astimezone(ORDER_RCA_API_TZ)
    gated: list[tuple[datetime, dict[str, Any], float]] = []
    for snap in snapshots:
        cid = parse_str(snap.get("cart_id"))
        if not cid:
            continue
        ts = parse_cart_timestamp(snap.get("cart_timestamp"))
        if ts is None:
            continue
        if ts.astimezone(ORDER_RCA_API_TZ) > placed_utc:
            continue
        score = _sku_overlap_score(_cart_skus(snap), order_skus)
        if score <= 0:
            continue
        gated.append((ts, snap, score))

    if not gated:
        return None

    by_cart: dict[str, list[tuple[datetime, float]]] = {}
    for ts, snap, score in gated:
        cid = parse_str(snap.get("cart_id"))
        if not cid:
            continue
        by_cart.setdefault(cid, []).append((ts, score))

    best_cid: str | None = None
    best_key: tuple[float, float] = (-1.0, -1.0)
    for cid, entries in by_cart.items():
        max_ts = max(t for t, _ in entries)
        max_score = max(s for _, s in entries)
        key = (max_score, max_ts.timestamp())
        if key > best_key:
            best_key = key
            best_cid = cid
    return best_cid


def _last_cart_skus(
    snapshots: list[dict[str, Any]],
    *,
    cart_id: str,
    placed_at: datetime,
) -> dict[str, float]:
    placed_utc = placed_at.astimezone(ORDER_RCA_API_TZ)
    best_ts: datetime | None = None
    best_snap: dict[str, Any] | None = None
    for snap in snapshots:
        if parse_str(snap.get("cart_id")) != cart_id:
            continue
        ts = parse_cart_timestamp(snap.get("cart_timestamp"))
        if ts is None:
            continue
        if ts.astimezone(ORDER_RCA_API_TZ) > placed_utc:
            continue
        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_snap = snap
    return _cart_skus(best_snap) if best_snap else {}


def build_journey_steps(
    snapshots: list[dict[str, Any]],
    *,
    cart_id: str,
    placed_at: datetime,
    sku_names: dict[str, str],
) -> list[dict[str, Any]]:
    placed_utc = placed_at.astimezone(ORDER_RCA_API_TZ)
    rows = []
    for snap in snapshots:
        if parse_str(snap.get("cart_id")) != cart_id:
            continue
        ts = parse_cart_timestamp(snap.get("cart_timestamp"))
        if ts is None or ts.astimezone(ORDER_RCA_API_TZ) > placed_utc:
            continue
        rows.append((ts, snap))
    rows.sort(key=lambda x: x[0])

    steps: list[dict[str, Any]] = []
    prev_skus: dict[str, float] = {}
    prev_sig: tuple | None = None
    prev_layout = "none"
    prev_groups: list[list[dict[str, Any]]] = []

    for ts, snap in rows:
        skus = _cart_skus(snap)
        shipments = snap.get("shipments") if isinstance(snap.get("shipments"), dict) else {}
        layout, groups = _shipment_groups(shipments)
        sig = _shipment_signature(shipments)
        sku_delta = _sku_delta_label(prev_skus, skus, sku_names) if prev_skus else None
        _, sku_changes = _sku_delta_parts(prev_skus, skus, sku_names) if prev_skus else (None, [])
        ship_delta = (
            _shipment_delta_label(
                prev_sig, sig, prev_layout, layout, prev_groups=prev_groups, cur_groups=groups
            )
            if prev_sig is not None
            else None
        )

        if prev_sig is not None and not sku_delta and not ship_delta and sig == prev_sig:
            continue

        is_first = prev_sig is None
        changes: list[str] = []
        if sku_delta:
            changes.append("skus")
        if prev_layout != layout:
            changes.append("layout")
        elif prev_sig is not None and _option_count_changed(prev_groups, groups):
            changes.append("options")
        elif ship_delta and "Fastest" in (ship_delta or ""):
            changes.append("fastest_eta")
        elif ship_delta:
            changes.append("shipment_options")

        kind = _step_kind(changes, is_first=is_first)
        group_views = _groups_views(groups, names=sku_names, prev_groups=prev_groups if not is_first else None)

        steps.append(
            {
                "at": format_instant_ist(ts.astimezone(ORDER_RCA_API_TZ)),
                "step_kind": kind,
                "step_label": _STEP_KIND_LABELS.get(kind, "Update"),
                "layout": layout,
                "layout_label": _layout_label(layout, len(groups)),
                "sku_summary": _format_sku_summary(skus, sku_names) if is_first or sku_delta else None,
                "sku_delta": sku_delta,
                "sku_changes": sku_changes or None,
                "shipment_delta": ship_delta,
                "shipments": group_views,
                "show_all_options": is_first or bool(changes),
                "changes": changes,
            }
        )
        prev_skus, prev_sig, prev_layout, prev_groups = skus, sig, layout, groups

    return steps


def _order_line_for_sku(order: dict[str, Any], sid: str) -> dict[str, Any] | None:
    for ln in rules.fulfillment_order_lines(order):
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        if parse_str(sku.get("sku_id") or ln.get("order_line_id")) == sid:
            return ln
    return None


def _qty_equivalent(cart_qty: float, order_qty: float, order_line: dict[str, Any] | None) -> bool:
    """True when cart ``skus_info`` qty matches pack-normalized order qty."""
    if not order_line:
        return cart_qty == order_qty
    norm = rules.ordered_quantity_from_line(order_line)
    target = norm if norm is not None else order_qty
    if cart_qty == target:
        return True
    raw = parse_num(order_line.get("quantity"))
    sku = order_line.get("sku") if isinstance(order_line.get("sku"), dict) else {}
    uip = parse_num(sku.get("units_in_pack"))
    if raw is not None and uip and uip > 1:
        pack_from_raw = raw / uip if raw % uip == 0 else None
        if pack_from_raw is not None:
            if cart_qty == pack_from_raw and target == pack_from_raw:
                return True
            if cart_qty == raw and target == pack_from_raw:
                return True
    if raw is not None and cart_qty == raw and (uip is None or uip <= 1):
        return target == raw
    return False


def _human_cart_vs_order(
    last_skus: dict[str, float],
    order_skus: dict[str, float],
    names: dict[str, str],
    *,
    order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not last_skus:
        return {
            "match": False,
            "detail": "No cart SKUs in the last pre-order snapshot.",
            "placed_summary": _format_sku_summary(order_skus, names) if order_skus else None,
            "cart_summary": None,
        }
    if not order_skus:
        cart_summary = _format_sku_summary(last_skus, names)
        return {
            "match": False,
            "detail": (
                f"Cart still had items before place-order ({cart_summary}) "
                "but no fulfilment SKUs on order to compare."
            ),
            "placed_summary": None,
            "cart_summary": cart_summary,
        }

    not_placed = [names.get(s, s) for s in last_skus if s not in order_skus]
    missing_on_cart = [names.get(s, s) for s in order_skus if s not in last_skus]
    qty_notes: list[str] = []
    for sid, oq in order_skus.items():
        if sid not in last_skus:
            continue
        cq = last_skus[sid]
        ln = _order_line_for_sku(order, sid) if order else None
        if not _qty_equivalent(cq, oq, ln):
            label = names.get(sid) or sid
            cqi = int(cq) if cq == int(cq) else cq
            oqi = int(oq) if oq == int(oq) else oq
            qty_notes.append(f"{label}: cart had ×{cqi}, order placed ×{oqi}")

    placed_summary = _format_sku_summary(order_skus, names)
    cart_summary = _format_sku_summary(last_skus, names)
    match = not not_placed and not missing_on_cart and not qty_notes

    parts: list[str] = []
    if not_placed:
        n = len(not_placed)
        preview = ", ".join(not_placed[:3])
        suffix = f" (+{n - 3} more)" if n > 3 else ""
        parts.append(f"Cart still showed {n} item(s) not on this order ({preview}{suffix})")
    if missing_on_cart:
        parts.append(f"On order but absent from last cart view: {', '.join(missing_on_cart)}")
    if qty_notes:
        parts.append("; ".join(qty_notes))
    if match:
        detail = "Last cart view matches placed order SKUs and quantities."
    elif not_placed and not missing_on_cart and qty_notes:
        detail = "; ".join(qty_notes)
    elif parts:
        detail = ". ".join(parts)
    else:
        detail = "Cart and order differ."

    return {
        "match": match,
        "detail": detail,
        "placed_summary": placed_summary,
        "cart_summary": cart_summary,
    }


def build_narrative(
    *,
    cart_id: str | None,
    placed_display: str | None,
    steps: list[dict[str, Any]],
    cart_vs_order: dict[str, Any],
) -> dict[str, Any]:
    if not cart_id or not steps:
        return {
            "headline": "No pre-order cart snapshots before place-order.",
            "insights": [],
        }

    n = max(0, len(steps) - 1)
    last = steps[-1]
    headline = (
        f"Before place-order ({placed_display or '—'}), cart {cart_id} had "
        f"{n} meaningful update(s); final view was {last.get('layout_label') or 'unknown'}."
    )

    insights: list[str] = []
    for step in steps[1:]:
        delta = step.get("shipment_delta")
        if delta and delta not in insights:
            insights.append(str(delta))
        sd = step.get("sku_delta")
        if sd and sd not in insights:
            insights.append(f"Cart edit: {sd}")

    cvo = cart_vs_order.get("detail")
    cart_mismatch = bool(cvo and not cart_vs_order.get("match"))
    if cart_mismatch:
        if len(insights) >= 2:
            insights = [insights[0], str(cvo)]
        else:
            insights.append(str(cvo))
    insights = insights[:2]

    if not insights:
        groups = last.get("shipments") or []
        fastest = [g.get("fastest_title") for g in groups if isinstance(g, dict) and g.get("fastest_title")]
        if fastest:
            insights.append(f"Fastest option(s) shown: {'; '.join(fastest[:2])}.")

    return {"headline": headline, "insights": insights}


def build_cart_allocation_journey(
    order: dict[str, Any],
    soft_alloc_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build ``facts.cart_allocation_journey`` block."""
    placed = order_placed_at(order)
    placed_display = format_instant_ist(placed) if placed else None
    empty: dict[str, Any] = {
        "available": False,
        "empty": True,
        "empty_reason": "no_data",
        "placed_at": placed_display,
        "cart_id": None,
        "headline": "No pre-order soft-allocation data.",
        "insights": [],
        "steps": [],
        "cart_vs_order": None,
        "note": "Options shown — customer selection not recorded.",
    }

    if not isinstance(soft_alloc_payload, dict) or not soft_alloc_payload:
        return empty

    if placed is None:
        return {**empty, "empty_reason": "no_place_time"}

    snapshots = _collect_snapshots(soft_alloc_payload)
    if not snapshots:
        return {**empty, "empty_reason": "no_snapshots"}

    order_skus = order_sku_qty_map(order)
    sku_names = _collect_sku_names(order, snapshots)

    cart_id = pick_cart_id(snapshots, placed_at=placed, order_skus=order_skus)
    if not cart_id:
        return {
            **empty,
            "available": True,
            "empty_reason": "no_match_before_place",
            "headline": f"No cart snapshot at or before place-order ({placed_display}) matched order SKUs.",
            "insights": ["Soft-allocation may require pagination back to the place-order day."],
        }

    steps = build_journey_steps(snapshots, cart_id=cart_id, placed_at=placed, sku_names=sku_names)
    last_skus = _last_cart_skus(snapshots, cart_id=cart_id, placed_at=placed)
    cart_vs = _human_cart_vs_order(last_skus, order_skus, sku_names, order=order)
    narrative = build_narrative(
        cart_id=cart_id,
        placed_display=placed_display,
        steps=steps,
        cart_vs_order=cart_vs,
    )

    return {
        "available": True,
        "empty": len(steps) == 0,
        "empty_reason": None if steps else "no_steps",
        "placed_at": placed_display,
        "cart_id": cart_id,
        "headline": narrative["headline"],
        "insights": narrative["insights"],
        "meaningful_changes": max(0, len(steps) - 1),
        "steps": steps,
        "cart_vs_order": cart_vs,
        "note": "Options shown — customer selection not recorded.",
    }


_HISTORY_DELETED_SKU_RE = re.compile(
    r"Deleted\s+sku\s+['\"]([^'\"]+?)\[([^\]]+)\]['\"]",
    re.I,
)
_HISTORY_SPLIT_SKUS_RE = re.compile(
    r"with skus:\s*\[([^\]]*)\]",
    re.I,
)
_HISTORY_SPLIT_ITEM_RE = re.compile(
    r"['\"]?([^\]'\"]+?)\(quantity\s*=\s*[^)]+\)['\"]?",
    re.I,
)


def _empty_post_order_sku_removals(*, available: bool = False) -> dict[str, Any]:
    return {
        "available": available,
        "count": 0,
        "items": [],
        "ambiguous_not_on_order": [],
    }


def _history_post_place_deletions(
    history_payload: dict[str, Any] | None,
    *,
    placed_at: datetime | None,
) -> tuple[set[str], set[str], set[str]]:
    """Return (deleted_sku_ids, deleted_names_lower, names_moved_via_split).

    High-confidence removals require explicit ``Deleted sku 'Name[id]'`` history —
    not MRP/price churn comments.
    """
    deleted_ids: set[str] = set()
    deleted_names: set[str] = set()
    split_moved: set[str] = set()
    if not isinstance(history_payload, dict):
        return deleted_ids, deleted_names, split_moved
    items = history_payload.get("history") or history_payload.get("data") or []
    if not isinstance(items, list):
        return deleted_ids, deleted_names, split_moved

    placed_epoch: float | None = None
    if placed_at is not None:
        try:
            placed_epoch = placed_at.astimezone(ORDER_RCA_API_TZ).timestamp()
        except Exception:
            placed_epoch = None

    for entry in items:
        if not isinstance(entry, dict):
            continue
        created = parse_num(entry.get("created"))
        if placed_epoch is not None and created is not None and float(created) < placed_epoch:
            continue
        comment = parse_str(entry.get("comment")) or ""
        if not comment:
            continue
        low = comment.lower()
        for dm in _HISTORY_DELETED_SKU_RE.finditer(comment):
            name = (dm.group(1) or "").strip().lower()
            sid = (dm.group(2) or "").strip()
            if sid:
                deleted_ids.add(sid)
            if name:
                deleted_names.add(name)
        sm = _HISTORY_SPLIT_SKUS_RE.search(comment)
        if sm and "split order request" in low:
            for im in _HISTORY_SPLIT_ITEM_RE.finditer(sm.group(1) or ""):
                name = (im.group(1) or "").strip().lower()
                if name:
                    split_moved.add(name)
    return deleted_ids, deleted_names, split_moved


def build_post_order_sku_removals(
    order: dict[str, Any],
    soft_alloc_payload: dict[str, Any] | None,
    history_payload: dict[str, Any] | None = None,
    *,
    parent_order: dict[str, Any] | None = None,
    family_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Cart@place vs family PO lines, using history to mark post-order removals.

    Uses soft-alloc ``sku_id`` keys. Never raises to callers — returns empty on failure.
    """
    try:
        return _build_post_order_sku_removals(
            order,
            soft_alloc_payload,
            history_payload,
            parent_order=parent_order,
            family_orders=family_orders,
        )
    except Exception:
        log.exception("post_order_sku_removals failed — skipping block")
        return _empty_post_order_sku_removals()


def _family_sku_qty_map(
    family_orders: list[dict[str, Any]] | None,
    *,
    order: dict[str, Any],
    parent_order: dict[str, Any] | None,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Sum qty by sku across parent+children. Falls back to current (+ parent)."""
    orders: list[dict[str, Any]] = []
    if isinstance(family_orders, list):
        orders = [o for o in family_orders if isinstance(o, dict)]
    if not orders:
        orders = [order]
        if isinstance(parent_order, dict):
            orders.append(parent_order)
    out: dict[str, float] = {}
    for o in orders:
        for sid, qty in order_sku_qty_map(o).items():
            out[sid] = out.get(sid, 0.0) + qty
    return out, orders


def _build_post_order_sku_removals(
    order: dict[str, Any],
    soft_alloc_payload: dict[str, Any] | None,
    history_payload: dict[str, Any] | None,
    *,
    parent_order: dict[str, Any] | None,
    family_orders: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(order, dict):
        return _empty_post_order_sku_removals()

    placed = order_placed_at(order)
    snapshots = _collect_snapshots(soft_alloc_payload if isinstance(soft_alloc_payload, dict) else None)
    if placed is None or not snapshots:
        return _empty_post_order_sku_removals()

    order_skus = order_sku_qty_map(order)
    sku_names = _collect_sku_names(order, snapshots)
    cart_id = pick_cart_id(snapshots, placed_at=placed, order_skus=order_skus)
    if not cart_id:
        return _empty_post_order_sku_removals(available=True)

    last_skus = _last_cart_skus(snapshots, cart_id=cart_id, placed_at=placed)
    if not last_skus:
        return _empty_post_order_sku_removals(available=True)

    history_deleted_ids, history_deleted_names, split_moved_names = (
        _history_post_place_deletions(history_payload, placed_at=placed)
    )
    family_skus, family = _family_sku_qty_map(
        family_orders, order=order, parent_order=parent_order
    )
    parent_skus = (
        order_sku_qty_map(parent_order) if isinstance(parent_order, dict) else {}
    )
    parent_oid = (
        str(parent_order.get("order_id") or "").strip().upper()
        if isinstance(parent_order, dict)
        else ""
    )
    family_oids = {
        str(o.get("order_id") or "").strip().upper() for o in family if isinstance(o, dict)
    }
    # Fixture parent stubs clone child lines — presence-only, never sum into family qty.
    parent_presence_only = bool(parent_oid and parent_oid not in family_oids)

    items: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for sid, cart_qty in last_skus.items():
        name = sku_names.get(sid) or sid
        name_key = name.strip().lower()

        # Split moved off this PO — not a customer/CC deletion.
        if name_key in split_moved_names:
            continue

        family_qty = family_skus.get(sid)
        if family_qty is None:
            if parent_presence_only and sid in parent_skus:
                continue
            # Gone from family — high confidence primarily via Deleted sku id.
            deleted = sid in history_deleted_ids
            if not deleted and name_key in history_deleted_names:
                # Name fallback only when soft-alloc name uniquely matches history.
                name_hits = [
                    s
                    for s, q in last_skus.items()
                    if (sku_names.get(s) or s).strip().lower() == name_key
                ]
                deleted = len(name_hits) == 1 and name_hits[0] == sid
            if deleted:
                items.append(
                    {
                        "sku_id": sid,
                        "name": name,
                        "qty_before": cart_qty,
                        "qty_after": 0,
                        "evidence": "history_deleted_sku",
                        "confidence": "high",
                    }
                )
            else:
                ambiguous.append(
                    {
                        "sku_id": sid,
                        "name": name,
                        "qty_at_place": cart_qty,
                        "note": "in_cart_at_place_but_no_post_place_evidence",
                    }
                )
            continue

        ln = None
        for fo in family:
            ln = _order_line_for_sku(fo, sid)
            if ln is not None:
                break
        if _qty_equivalent(cart_qty, family_qty, ln):
            continue
        # Still on family with lower pack qty → post-order reduction.
        if family_qty < cart_qty:
            items.append(
                {
                    "sku_id": sid,
                    "name": name,
                    "qty_before": cart_qty,
                    "qty_after": family_qty,
                    "evidence": "cart_vs_order_qty",
                    "confidence": "high",
                }
            )

    return {
        "available": True,
        "count": len(items),
        "items": items,
        "ambiguous_not_on_order": ambiguous,
    }


def pagination_cutoff(placed_at: datetime, *, lookback_hours: int = 168) -> datetime:
    """Earliest cart snapshot to fetch — default 7 days before place-order."""
    return placed_at.astimezone(ORDER_RCA_API_TZ) - timedelta(hours=lookback_hours)
