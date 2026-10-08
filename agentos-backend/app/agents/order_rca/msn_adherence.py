"""MSN adherence table — P1 planning fields vs order qty (no allocation rejection labels)."""

from __future__ import annotations

from typing import Any

_MISSING = "—"


def physical_store_key(vendor_code: str | None) -> str:
    c = (vendor_code or "").strip()
    if not c:
        return ""
    if "_" in c:
        return c.rsplit("_", 1)[0]
    return c


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_qty(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# SKU status codes (MSN-only; no not-mapped / delived).
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_MSN_CAN_FULFIL = "no_msn_can_fulfil"
STATUS_NO_MSN_PARTIAL = "no_msn_partial"
STATUS_NO_MSN_OOS = "no_msn_oos"
STATUS_CAN_FULFIL_MSN_MET = "can_fulfil_msn_met"
STATUS_CAN_FULFIL_BELOW_MSN = "can_fulfil_below_msn"
STATUS_PARTIAL_MSN_MET = "partial_msn_met"
STATUS_PARTIAL_BELOW_MSN = "partial_below_msn"
STATUS_OOS_BELOW_MSN = "oos_below_msn"

_BLOCKING_CODES = frozenset(
    {
        STATUS_NO_MSN_PARTIAL,
        STATUS_NO_MSN_OOS,
        STATUS_PARTIAL_MSN_MET,
        STATUS_PARTIAL_BELOW_MSN,
        STATUS_OOS_BELOW_MSN,
    }
)
_BELOW_MSN_CODES = frozenset({STATUS_CAN_FULFIL_BELOW_MSN, STATUS_PARTIAL_BELOW_MSN, STATUS_OOS_BELOW_MSN})

# Statuses surfaced to LLM hypothesis seeds (includes warn-path below MSN).
MSN_HYPOTHESIS_STATUS_CODES = _BLOCKING_CODES | frozenset({STATUS_CAN_FULFIL_BELOW_MSN})

_LLM_SUMMARY_MAX_LINES = 12


def _has_msn(m: float | None) -> bool:
    return m is not None and m > 0


def classify_msn_adherence_status(
    *,
    effective_msn: float | None,
    on_shelf_qty: float | None,
    asked_qty: float | None,
) -> dict[str, Any]:
    """
    Derive MSN-only status from P1 fields.

    Returns status_code, status_label, status_tone (ok|warn|bad|slate).
    """
    if on_shelf_qty is None or asked_qty is None:
        return {
            "status_code": STATUS_UNAVAILABLE,
            "status_label": _MISSING,
            "status_tone": "slate",
        }

    s = max(0.0, float(on_shelf_qty))
    a = max(0.0, float(asked_qty))
    m = _parse_qty(effective_msn) if effective_msn is not None else None

    if not _has_msn(m):
        if s >= a:
            return {
                "status_code": STATUS_NO_MSN_CAN_FULFIL,
                "status_label": "No MSN. Can fulfil order",
                "status_tone": "ok",
            }
        if s > 0:
            return {
                "status_code": STATUS_NO_MSN_PARTIAL,
                "status_label": "No MSN. Partial vs order",
                "status_tone": "bad",
            }
        return {
            "status_code": STATUS_NO_MSN_OOS,
            "status_label": "No MSN. Out of stock vs order",
            "status_tone": "bad",
        }

    m_f = float(m)
    if s >= a and s >= m_f:
        return {
            "status_code": STATUS_CAN_FULFIL_MSN_MET,
            "status_label": "Can fulfil · MSN met",
            "status_tone": "ok",
        }
    if s >= a and s < m_f:
        return {
            "status_code": STATUS_CAN_FULFIL_BELOW_MSN,
            "status_label": "Can fulfil · below MSN",
            "status_tone": "warn",
        }
    if 0 < s < a and s >= m_f:
        return {
            "status_code": STATUS_PARTIAL_MSN_MET,
            "status_label": "Partial vs order · MSN met",
            "status_tone": "bad",
        }
    if 0 < s < a and s < m_f:
        return {
            "status_code": STATUS_PARTIAL_BELOW_MSN,
            "status_label": "Partial vs order · below MSN",
            "status_tone": "bad",
        }
    return {
        "status_code": STATUS_OOS_BELOW_MSN,
        "status_label": "Out of stock vs order · below MSN",
        "status_tone": "bad",
    }


def _sku_sort_key(line: dict[str, Any]) -> tuple[int, str]:
    code = str(line.get("status_code") or "")
    if code in _BLOCKING_CODES:
        tier = 0
    elif code in _BELOW_MSN_CODES:
        tier = 1
    elif code in (STATUS_NO_MSN_CAN_FULFIL, STATUS_CAN_FULFIL_MSN_MET):
        tier = 2
    else:
        tier = 3
    return (tier, str(line.get("name") or line.get("sku_id") or ""))


def rollup_store_status(sku_lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Worst SKU wins for store header."""
    if not sku_lines:
        return {
            "rollup_status_code": "empty",
            "rollup_label": _MISSING,
            "rollup_tone": "slate",
        }
    codes = [str(l.get("status_code") or "") for l in sku_lines]
    if any(c in _BLOCKING_CODES for c in codes):
        n = sum(1 for c in codes if c in _BLOCKING_CODES)
        total = len(sku_lines)
        return {
            "rollup_status_code": "cannot_serve",
            "rollup_label": f"{n} of {total} SKUs cannot fulfil" if n < total else "Cannot serve order",
            "rollup_tone": "bad",
        }
    below = sum(1 for c in codes if c in _BELOW_MSN_CODES)
    if below:
        return {
            "rollup_status_code": "below_msn",
            "rollup_label": f"Can fulfil · {below} below MSN" if below > 1 else "Can fulfil · below MSN",
            "rollup_tone": "warn",
        }
    if all(c == STATUS_UNAVAILABLE for c in codes):
        return {
            "rollup_status_code": "pending",
            "rollup_label": "P1 data pending",
            "rollup_tone": "slate",
        }
    return {
        "rollup_status_code": "all_ok",
        "rollup_label": "All SKUs OK",
        "rollup_tone": "ok",
    }


def normalize_p1_msn_lookup(raw: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    """
    physical_store -> sku_id -> {effective_msn, on_shelf_qty, sku_sub_grade}.

    Accepts ``stores`` as dict keyed by physical store or as a list of store blocks.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    stores = raw.get("stores")
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def _ingest_sku(phys: str, sku_id: str, row: dict[str, Any]) -> None:
        if not phys or not sku_id or not isinstance(row, dict):
            return
        key = str(phys).strip()
        sid = str(sku_id).strip()
        out.setdefault(key, {})[sid] = {
            "effective_msn": _parse_qty(row.get("effective_msn")),
            "on_shelf_qty": _parse_qty(
                row.get("on_shelf_qty") if row.get("on_shelf_qty") is not None else row.get("on_shelf")
            ),
            "sku_sub_grade": row.get("sku_sub_grade") or row.get("sub_grade"),
        }

    if isinstance(stores, dict):
        for phys, block in stores.items():
            if not isinstance(block, dict):
                continue
            skus = block.get("skus")
            if isinstance(skus, dict):
                for sid, row in skus.items():
                    if isinstance(row, dict):
                        _ingest_sku(str(phys), str(sid), row)
            elif isinstance(skus, list):
                for row in skus:
                    if isinstance(row, dict):
                        _ingest_sku(str(phys), str(row.get("sku_id") or ""), row)
    elif isinstance(stores, list):
        for block in stores:
            if not isinstance(block, dict):
                continue
            phys = str(block.get("physical_store") or "")
            skus = block.get("skus")
            if isinstance(skus, dict):
                for sid, row in skus.items():
                    if isinstance(row, dict):
                        _ingest_sku(phys, str(sid), row)
            elif isinstance(skus, list):
                for row in skus:
                    if isinstance(row, dict):
                        _ingest_sku(phys, str(row.get("sku_id") or ""), row)
    return out


def collect_matrix_physical_stores(p3_views: dict[str, Any]) -> list[dict[str, Any]]:
    """Same physical stores as allocation matrix: top retail + warehouses + allocated."""
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for g in p3_views.get("store_groups") or []:
        if not isinstance(g, dict):
            continue
        phys = str(g.get("physical_store") or "").strip()
        if not phys or phys in seen:
            continue
        seen.add(phys)
        ordered.append(g)
    alloc = p3_views.get("allocated_store")
    if isinstance(alloc, dict):
        phys = str(alloc.get("physical_store") or "").strip()
        if phys and phys not in seen:
            ordered.append(alloc)
    return sorted(ordered, key=lambda g: _num(g.get("distance_km")) or 9e9)


def _vendor_type_label(group: dict[str, Any]) -> str:
    kind = (group.get("store_kind") or "").lower()
    if kind == "retail":
        return "Retail"
    if kind == "warehouse":
        return "Warehouse"
    vt = str(group.get("vendor_type") or "")
    return vt[:1].upper() + vt[1:].lower() if vt else _MISSING


def _display_qty(v: float | None) -> str | None:
    if v is None:
        return None
    if v == int(v):
        return str(int(v))
    return str(v)


def build_msn_adherence_block(
    *,
    matrix_store_groups: list[dict[str, Any]],
    order_skus: list[dict[str, Any]],
    p1_msn_raw: dict[str, Any] | None,
    p1_api_configured: bool,
    asked_qty_by_sku: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build MSN adherence payload for facts.p1.msn_adherence."""
    lookup = normalize_p1_msn_lookup(p1_msn_raw)
    has_p1_rows = bool(lookup)

    store_rows: list[dict[str, Any]] = []
    for group in matrix_store_groups:
        phys = str(group.get("physical_store") or "").strip()
        if not phys:
            continue
        p1_store = lookup.get(phys) or lookup.get(physical_store_key(phys)) or {}
        sku_lines: list[dict[str, Any]] = []

        for sku in order_skus:
            if not isinstance(sku, dict):
                continue
            sid = str(sku.get("sku_id") or "").strip()
            if not sid:
                continue
            asked = None
            if asked_qty_by_sku:
                asked = _parse_qty(asked_qty_by_sku.get(sid))
            if asked is None:
                asked = _parse_qty(sku.get("quantity"))
            p1_sku = p1_store.get(sid) or {}
            msn = _parse_qty(p1_sku.get("effective_msn"))
            on_shelf = _parse_qty(p1_sku.get("on_shelf_qty"))
            sub_grade = p1_sku.get("sku_sub_grade")
            status = classify_msn_adherence_status(
                effective_msn=msn,
                on_shelf_qty=on_shelf,
                asked_qty=asked,
            )
            sku_lines.append(
                {
                    "sku_id": sid,
                    "name": sku.get("name"),
                    "sku_sub_grade": sub_grade if sub_grade not in (None, "") else None,
                    "effective_msn": msn,
                    "effective_msn_display": _display_qty(msn),
                    "on_shelf_qty": on_shelf,
                    "on_shelf_display": _display_qty(on_shelf),
                    "asked_qty": asked,
                    "asked_display": _display_qty(asked),
                    **status,
                }
            )

        sku_lines.sort(key=_sku_sort_key)
        rollup = rollup_store_status(sku_lines)
        store_rows.append(
            {
                "physical_store": phys,
                "distance_km": _num(group.get("distance_km")),
                "vendor_type": _vendor_type_label(group),
                "store_kind": group.get("store_kind"),
                "skus": sku_lines,
                **rollup,
            }
        )

    if has_p1_rows:
        status = "connected"
    elif p1_api_configured:
        status = "pending_data"
    else:
        status = "pending_api"

    return {
        "status": status,
        "p1_api_configured": p1_api_configured,
        "stores": store_rows,
    }


def _sku_has_p1_fields(sku_line: dict[str, Any]) -> bool:
    """True when P1 returned at least one planning field for this store × SKU."""
    if sku_line.get("sku_sub_grade") not in (None, ""):
        return True
    if sku_line.get("effective_msn") is not None:
        return True
    if sku_line.get("on_shelf_qty") is not None:
        return True
    return False


def build_msn_adherence_llm_summary(msn_block: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Compact MSN context for synthesis when P1 supplied any per-SKU planning fields.

    Does not depend on block-level ``connected`` — only on populated SKU cells.
    """
    if not msn_block or not isinstance(msn_block, dict):
        return None

    notable: list[dict[str, Any]] = []
    for store in msn_block.get("stores") or []:
        if not isinstance(store, dict):
            continue
        phys = store.get("physical_store")
        for sku in store.get("skus") or []:
            if not isinstance(sku, dict) or not _sku_has_p1_fields(sku):
                continue
            code = str(sku.get("status_code") or "")
            if code not in MSN_HYPOTHESIS_STATUS_CODES and code not in (
                STATUS_CAN_FULFIL_MSN_MET,
                STATUS_NO_MSN_CAN_FULFIL,
            ):
                continue
            notable.append(
                {
                    "store": phys,
                    "product": sku.get("name"),
                    "msn": sku.get("effective_msn_display"),
                    "on_shelf": sku.get("on_shelf_display"),
                    "asked": sku.get("asked_display"),
                    "status": sku.get("status_label"),
                }
            )

    if not notable:
        return None

    below = sum(1 for n in notable if "below MSN" in str(n.get("status") or ""))
    blocked = sum(
        1
        for n in notable
        if any(x in str(n.get("status") or "") for x in ("Partial", "Out of stock"))
    )
    return {
        "stores_compared": len(msn_block.get("stores") or []),
        "lines_with_p1_data": len(notable),
        "below_msn_count": below,
        "cannot_fulfil_count": blocked,
        "notable_lines": notable[:_LLM_SUMMARY_MAX_LINES],
    }
