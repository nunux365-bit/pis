"""Eligibility rules for WhatsApp JIT hold outreach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.order_rca.rules import (
    ordered_quantity_from_line,
    parse_allocation_block,
)
from app.agents.whatsapp_jit_hold.constants import (
    ELIGIBLE_VENDOR_TYPES,
    PACKAGING_STATUS_ID,
    QC_RAPID_SERVICE_IDS,
    fc_type_is_denied,
    fc_type_means_warehouse,
)
from app.agents.whatsapp_jit_hold.order_client import customer_phone
from app.agents.whatsapp_jit_hold.order_status_parser import is_odin_packaging_on_hold
from app.config.settings import settings


class OrderNoLongerEligibleError(Exception):
    """Raised when a conversation step runs but the order fails eligibility re-check."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class JitSkuLine:
    sku_id: str
    name: str
    qty_ordered: int
    qty_stuck: int
    qty_fulfillable: int


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str
    held_skus: list[JitSkuLine]
    ship_skus: list[dict[str, Any]]


def _bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes"}
    return bool(val)


def _is_qc_rapid_order(order: dict[str, Any]) -> bool:
    rapid = order.get("rapid_eligibility_info")
    if not isinstance(rapid, dict):
        return False
    service_id = str(rapid.get("service_id") or rapid.get("rapid_type") or "").lower()
    return service_id in QC_RAPID_SERVICE_IDS


_ON_HOLD_SUB_STATUSES = frozenset({"on-hold", "on hold", "onhold"})


def _normalize_sub_status(raw: Any) -> str:
    return str(raw or "").lower().replace("_", "-").strip()


def _is_on_hold_sub_status(raw: Any) -> bool:
    return _normalize_sub_status(raw) in _ON_HOLD_SUB_STATUSES


def _is_packaging_status(*, status_id: Any = None, status_name: Any = None) -> bool:
    if str(status_id or "").strip() == PACKAGING_STATUS_ID:
        return True
    name = str(status_name or "").lower().replace("_", "-").strip()
    return name == "packaging"


def _is_packaging_on_hold(bundle: dict[str, Any], order: dict[str, Any]) -> bool:
    """
    True only when GET order-details shows packaging on-hold.

    ``status_id`` / ``status`` and ``sub_status`` on the order record are the
    source of truth. Nexus ON_HOLD is a Kafka-only fallback when GET-order
    ``sub_status`` is not yet populated at trigger time.
    """
    if not _is_packaging_status(status_id=order.get("status_id"), status_name=order.get("status")):
        return False

    if _is_on_hold_sub_status(order.get("sub_status")):
        return True

    if order.get("sub_status") not in (None, ""):
        return False

    nexus = bundle.get("nexus_event")
    return isinstance(nexus, dict) and is_odin_packaging_on_hold(nexus)


def _normalize_eligible_vendor_type(raw: Any) -> str | None:
    """Return WAREHOUSE/RETAIL only when the value matches exactly; never invent."""
    vtype = str(raw or "").upper().strip()
    return vtype if vtype in ELIGIBLE_VENDOR_TYPES else None


def _allocated_vendor_type_ok(allocated: dict[str, Any] | None) -> bool:
    """explain_allocation allocated vendor — exact WAREHOUSE/RETAIL only."""
    if not allocated:
        return False
    return _normalize_eligible_vendor_type(allocated.get("vendor_type")) is not None


def _explicit_store_type(order: dict[str, Any]) -> str:
    """Raw ``tags.store_type`` (uppercased), or empty when absent."""
    ship = order.get("shipment_detail")
    if not isinstance(ship, dict):
        return ""
    vendor = ship.get("vendor")
    if not isinstance(vendor, dict):
        return ""
    tags = vendor.get("tags") if isinstance(vendor.get("tags"), dict) else {}
    return str(tags.get("store_type") or "").upper().strip()


def _vendor_type_from_order(order: dict[str, Any]) -> str | None:
    """
    FC/retail from order-details ``shipment_detail.vendor``.

    Canonical field: ``tags.store_type`` ∈ {WAREHOUSE, RETAIL}.
    Secondary (only when store_type is absent): ``tags.fc_type`` is an FC
    fulfilment label (not marketplace / non-fulfilment).
    Do **not** use ``tags.vendor_type`` (values are VMO / Non-VMO, not store kind).
    Do **not** treat unknown / empty / MARKETPLACE as warehouse.
    Explicit non-eligible ``store_type`` (e.g. MARKETPLACE) must not fall through
    to ``fc_type``.
    """
    ship = order.get("shipment_detail")
    if not isinstance(ship, dict):
        return None
    vendor = ship.get("vendor")
    if not isinstance(vendor, dict):
        return None
    tags = vendor.get("tags") if isinstance(vendor.get("tags"), dict) else {}

    raw_store = str(tags.get("store_type") or "").upper().strip()
    if raw_store:
        # Explicit store_type: only WAREHOUSE/RETAIL pass; never consult fc_type.
        return _normalize_eligible_vendor_type(raw_store)

    if fc_type_means_warehouse(tags.get("fc_type")):
        return "WAREHOUSE"
    # tags.vendor_type (VMO/Non-VMO) and vendor.vendor_type are not store kind.
    return None


def _explicit_fc_type(order: dict[str, Any]) -> str:
    ship = order.get("shipment_detail")
    if not isinstance(ship, dict):
        return ""
    vendor = ship.get("vendor")
    if not isinstance(vendor, dict):
        return ""
    tags = vendor.get("tags") if isinstance(vendor.get("tags"), dict) else {}
    return str(tags.get("fc_type") or "").strip()


def _vendor_eligible(order: dict[str, Any], allocated: dict[str, Any] | None) -> bool:
    """Prefer order-details shipment vendor; fall back to explain_allocation allocated vendor."""
    raw_store = _explicit_store_type(order)
    if raw_store and raw_store not in ELIGIBLE_VENDOR_TYPES:
        # Hard deny — do not fall through to fc_type (already blocked) or allocation.
        return False
    fc_raw = _explicit_fc_type(order)
    if fc_raw and fc_type_is_denied(fc_raw):
        return False
    vtype = _vendor_type_from_order(order)
    if vtype:
        return True
    # Allocation only when the order itself has no store_type / fc_type signal.
    if raw_store or fc_raw:
        return False
    return _allocated_vendor_type_ok(allocated)


def _fulfilled_quantity_from_line(ln: dict[str, Any]) -> int | None:
    """
    Pack-fulfilled qty from order-service at packaging on-hold.

    Reads ``order_lines.fulfilled_quantity`` first, then
    ``order_lines.sku.sku_meta_data.fulfilled_quantity``.
    """
    if not isinstance(ln, dict):
        return None
    for raw in (ln.get("fulfilled_quantity"),):
        if raw is None:
            continue
        try:
            qty = int(float(raw))
        except (TypeError, ValueError):
            continue
        return max(0, qty)

    sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
    meta = sku.get("sku_meta_data")
    if isinstance(meta, dict):
        raw = meta.get("fulfilled_quantity")
        if raw is not None:
            try:
                return max(0, int(float(raw)))
            except (TypeError, ValueError):
                return None
    return None


def _is_jit_line(ln: dict[str, Any]) -> bool:
    if _bool(ln.get("jit")):
        return True
    sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
    return _bool(sku.get("jit"))


def _pack_size_from_line(ln: dict[str, Any]) -> int:
    """Tablets per pack, same as order-service JIT split: quantity / normalized_quantity."""
    try:
        raw = float(ln.get("quantity"))
        packs = float(ln.get("normalized_quantity"))
    except (TypeError, ValueError):
        return 1
    if raw <= 0 or packs <= 0:
        return 1
    size = int(raw / packs)
    return size if size >= 1 else 1


def _line_tablets(ln: dict[str, Any], *, ordered_packs: int, pack_size: int) -> int:
    """OS split subtracts retain from ``order_lines.quantity`` (tablets)."""
    try:
        raw = float(ln.get("quantity"))
    except (TypeError, ValueError):
        raw = 0
    if raw > 0:
        return int(raw)
    return max(0, ordered_packs * pack_size)


def _retain_tablets(
    ln: dict[str, Any],
    *,
    ordered_packs: int,
    keep_packs: int,
    pack_size: int,
) -> int:
    """Tablets to keep on the parent. Full line → ``quantity``; partial JIT → packs × pack size."""
    total = _line_tablets(ln, ordered_packs=ordered_packs, pack_size=pack_size)
    keep = max(0, min(int(keep_packs), int(ordered_packs)))
    if keep <= 0 or total <= 0:
        return 0
    if keep >= ordered_packs:
        return total
    return min(total, keep * pack_size)


def _ship_row(
    *,
    sku_id: str,
    name: str,
    qty_packs: int,
    jit: bool,
    qty_retain: int,
) -> dict[str, Any]:
    """WhatsApp ``qty`` is packs; ``qty_retain`` is tablets kept on the parent."""
    return {
        "sku_id": sku_id,
        "name": name,
        "qty": qty_packs,
        "jit": jit,
        "qty_retain": qty_retain,
    }


def retain_skus_for_split(result: EligibilityResult) -> list[dict[str, str | int]]:
    """Tablets to keep on the parent (available / ship-now lines)."""
    out: list[dict[str, str | int]] = []
    for s in result.ship_skus:
        sku = str(s.get("sku_id") or "")
        raw = s.get("qty_retain")
        if raw is None:
            raw = s.get("qty")
        try:
            qty = int(raw or 0)
        except (TypeError, ValueError):
            qty = 0
        if sku and qty > 0:
            out.append({"sku_id": sku, "qty": qty})
    return out


def _jit_lines_from_order(order: dict[str, Any]) -> tuple[list[JitSkuLine], list[dict[str, Any]]]:
    """
    Return (held_jit_skus, shippable_skus) from order-details lines only.

    WhatsApp hold/ship qty is packs (``normalized_quantity`` / ``fulfilled_quantity``).
    Split retain is tablets on **ship** lines (available stock stays on the parent).
    """
    held: list[JitSkuLine] = []
    ship: list[dict[str, Any]] = []

    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        sku = ln.get("sku") if isinstance(ln.get("sku"), dict) else {}
        sku_id = str(sku.get("sku_id") or ln.get("order_line_id") or "")
        if not sku_id:
            continue
        ordered_f = ordered_quantity_from_line(ln)
        if ordered_f is None or ordered_f <= 0:
            continue
        ordered = int(ordered_f)
        name = str(sku.get("name") or sku_id)
        pack_size = _pack_size_from_line(ln)

        if not _is_jit_line(ln):
            ship.append(
                _ship_row(
                    sku_id=sku_id,
                    name=name,
                    qty_packs=ordered,
                    jit=False,
                    qty_retain=_retain_tablets(
                        ln,
                        ordered_packs=ordered,
                        keep_packs=ordered,
                        pack_size=pack_size,
                    ),
                )
            )
            continue

        fulfillable_i = _fulfilled_quantity_from_line(ln)
        if fulfillable_i is None:
            fulfillable_i = 0
        fulfillable_i = min(fulfillable_i, ordered)
        stuck = max(0, ordered - fulfillable_i)
        if stuck > 0:
            held.append(
                JitSkuLine(
                    sku_id=sku_id,
                    name=name,
                    qty_ordered=ordered,
                    qty_stuck=stuck,
                    qty_fulfillable=fulfillable_i,
                )
            )
        if fulfillable_i > 0:
            ship.append(
                _ship_row(
                    sku_id=sku_id,
                    name=name,
                    qty_packs=fulfillable_i,
                    jit=True,
                    qty_retain=_retain_tablets(
                        ln,
                        ordered_packs=ordered,
                        keep_packs=fulfillable_i,
                        pack_size=pack_size,
                    ),
                )
            )

    return held, ship


def _total_ordered_from_order(order: dict[str, Any]) -> int:
    """Sum ordered qty across all order lines (JIT and non-JIT)."""
    total = 0
    for ln in order.get("order_lines") or []:
        if not isinstance(ln, dict):
            continue
        ordered_f = ordered_quantity_from_line(ln)
        if ordered_f is None or ordered_f <= 0:
            continue
        total += int(ordered_f)
    return total


def _jit_qty_bounds() -> tuple[int, int, int]:
    """(ordered_min, stuck_min, stuck_max) from settings — stuck_max is never below stuck_min."""
    ordered_min = int(settings.whatsapp_jit_hold_jit_ordered_qty_min)
    stuck_min = int(settings.whatsapp_jit_hold_jit_stuck_qty_min)
    stuck_max = max(stuck_min, int(settings.whatsapp_jit_hold_jit_stuck_qty_max))
    return ordered_min, stuck_min, stuck_max


def _order_jit_totals_pass_gate(order: dict[str, Any], held: list[JitSkuLine]) -> bool:
    """
    Order-level eligibility on JIT held qty.

    - ``total_ordered``: all lines (JIT + non-JIT).
    - ``total_stuck``: sum of stuck on JIT held lines only.
    """
    if not held:
        return False
    ordered_min, stuck_min, stuck_max = _jit_qty_bounds()
    total_ordered = _total_ordered_from_order(order)
    total_stuck = sum(h.qty_stuck for h in held)
    return total_ordered >= ordered_min and stuck_min <= total_stuck <= stuck_max


def require_eligible(bundle: dict[str, Any]) -> EligibilityResult:
    """Re-check eligibility; raise if the order no longer qualifies."""
    result = evaluate_eligibility(bundle)
    if not result.eligible:
        raise OrderNoLongerEligibleError(result.reason)
    return result


def evaluate_eligibility(bundle: dict[str, Any]) -> EligibilityResult:
    order = bundle.get("order")
    if not isinstance(order, dict):
        return EligibilityResult(False, "order_missing", [], [])

    parent_id = str(order.get("parent_id") or "").strip().upper()
    child_id = str(bundle.get("order_id") or order.get("order_id") or "").strip().upper()
    if parent_id and parent_id != child_id:
        return EligibilityResult(False, "already_split_child", [], [])

    if _is_qc_rapid_order(order):
        return EligibilityResult(False, "qc_rapid_30_60", [], [])

    if not _is_packaging_on_hold(bundle, order):
        return EligibilityResult(False, "not_packaging_on_hold", [], [])

    oid = str(bundle.get("order_id") or "").strip().upper()
    alloc_root = (bundle.get("allocation") or {}).get("data") or {}
    allocated, _, _, _ = parse_allocation_block(oid, alloc_root)
    if not _vendor_eligible(order, allocated):
        return EligibilityResult(False, "vendor_not_fc_wh", [], [])

    phone = customer_phone(order)
    if not phone:
        return EligibilityResult(False, "no_phone", [], [])

    held, ship = _jit_lines_from_order(order)
    if not _order_jit_totals_pass_gate(order, held):
        return EligibilityResult(False, "no_eligible_jit_skus", held, ship)
    if not ship:
        # Option A copy says remaining items are ready to ship — refuse if nothing can ship.
        return EligibilityResult(False, "no_ship_now_skus", held, [])

    return EligibilityResult(True, "eligible", held, ship)
