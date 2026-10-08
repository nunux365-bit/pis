"""Fixture overlays for WhatsApp JIT hold dev/test."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "whatsapp_jit_hold"


def _overlay_path(order_id: str) -> Path | None:
    oid = order_id.strip().upper()
    if not oid or "/" in oid or "\\" in oid or ".." in oid:
        return None
    path = (_FIXTURE_ROOT / f"overlay_{oid}.json").resolve()
    root = _FIXTURE_ROOT.resolve()
    if not path.is_relative_to(root):
        return None
    return path


def apply_fixture_overlay(bundle: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge optional overlay JSON onto an order RCA fixture bundle."""
    oid = str(bundle.get("order_id") or "").strip().upper()
    path = _overlay_path(oid)
    if path is None or not path.is_file():
        return _default_jit_overlay(bundle)
    overlay = json.loads(path.read_text())
    return _deep_merge(copy.deepcopy(bundle), overlay)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, val in patch.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            base[key] = _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _default_jit_overlay(bundle: dict[str, Any]) -> dict[str, Any]:
    """
    When no overlay file exists, patch bundle minimally so eligibility can pass in tests.

    JIT stuck/ship qty comes from order line ``jit`` + ``fulfilled_quantity``.
    Allocation is only patched for FC/WH vendor type fallback.
    """
    out = copy.deepcopy(bundle)
    order = out.get("order")
    if not isinstance(order, dict):
        return out
    oid = str(out.get("order_id") or order.get("order_id") or "").strip().upper()
    lines = order.get("order_lines") or []
    if not lines:
        return out
    ln0 = lines[0] if isinstance(lines[0], dict) else {}
    ordered = int(ln0.get("normalized_quantity") or ln0.get("quantity") or 9)
    if ordered < 9:
        ordered = 9
        ln0["normalized_quantity"] = ordered
    fulfillable = max(0, ordered - 2)
    ln0["jit"] = True
    ln0["fulfilled_quantity"] = fulfillable
    lines[0] = ln0
    order["order_lines"] = lines

    order["sub_status"] = "on-hold"
    order["status_id"] = 130
    order["status"] = "Packaging"

    # Kafka eligibility uses GET-order vendor tags (allocation is empty on that path).
    # RCA parent fixture stubs ship with ``shipment_detail: {}``. Only invent WH when
    # store_type and fc_type are both absent — never overwrite MARKETPLACE / other labels.
    ship = order.get("shipment_detail") if isinstance(order.get("shipment_detail"), dict) else {}
    vendor = ship.get("vendor") if isinstance(ship.get("vendor"), dict) else {}
    tags = vendor.get("tags") if isinstance(vendor.get("tags"), dict) else {}
    has_store = bool(str(tags.get("store_type") or "").strip())
    has_fc = bool(str(tags.get("fc_type") or "").strip())
    if not has_store and not has_fc:
        tags["store_type"] = "WAREHOUSE"
        tags["fc_type"] = "Fulfilment Center"
    vendor["tags"] = tags
    ship["vendor"] = vendor
    order["shipment_detail"] = ship

    rapid = order.get("rapid_eligibility_info")
    if not isinstance(rapid, dict):
        rapid = {}
        order["rapid_eligibility_info"] = rapid
    rapid["service_id"] = "three_day_delivery"
    rapid["rapid_opted"] = False

    out["status"] = {
        "data": {
            oid: [
                {"status": "130", "created": "2026-06-09T10:00:00+05:30", "sub_status": "on-hold"},
            ]
        }
    }

    alloc_root = (out.get("allocation") or {}).get("data") or {}
    block = alloc_root.get(oid) or {}
    fulfillable = max(0, ordered - 2)
    block.update(
        {
            "allocated_vendor": 8212,
            "selected_vendors": {
                "8212": {
                    "vendor_id": 8212,
                    "vendor_code": "1MG_BRM_01",
                    "vendor_type": "WAREHOUSE",
                    "distance": 5.0,
                }
            },
        }
    )
    alloc_root[oid] = block
    if "allocation" not in out:
        out["allocation"] = {}
    out["allocation"]["data"] = alloc_root
    return out
