#!/usr/bin/env python3
"""Live integration: create PR via AgentOS API — workflow YAST (Asset).

Test shapes:
  - simple:         1 material, 1 asset (line/alloc — back-compat)
  - multi-asset:    1 material, 2 assets (qty splits)
  - two-material:   2 materials, 1 asset each

Materials/plant/asset from ``pr_po_reference_values`` / QAS fixtures.

Usage:

  python scripts/integration/run_pr_create_yast.py --shape multi-asset
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import (
    add_common_args,
    create_pr_via_api,
    load_env,
    preview_sap_payload,
    print_ticket_result,
    resolve_token_from_args,
)
from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)

DOCUMENT_TYPE = "YAST"


def _header(*, header_note: str, material_group: str) -> dict[str, Any]:
    plant = (os.environ.get("SAP_PLANT") or "").strip()
    sloc = (os.environ.get("SAP_SLOC") or "1001").strip()
    if plant and sloc and "|" not in sloc:
        sloc = f"{plant}|{sloc}"
    return {
        "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
        "purchasing_group": os.environ.get("SAP_PUR_GROUP", "S0Z"),
        "plant": plant,
        "storage_location": sloc,
        "material_group": material_group,
        "header_note": header_note or "Integration YAST",
    }


def _line(
    *,
    material: str,
    short_text: str,
    allocations: list[dict[str, str]],
    asset: str = "",
    delivery_date: str = "2026-07-01",
    unit_price: str = "100",
    valuation_price: str = "100",
) -> dict[str, Any]:
    return {
        "material": material,
        "short_text": short_text,
        "delivery_date": delivery_date,
        "unit_price": unit_price,
        "valuation_price": valuation_price,
        "asset": asset,
        "allocations": allocations,
    }


def _asset_a() -> str:
    return (os.environ.get("SAP_ASSET") or "").strip()


def _asset_b() -> str:
    return (os.environ.get("SAP_ASSET_2") or os.environ.get("SAP_ASSET") or "").strip()


def _mat_1() -> str:
    return (os.environ.get("SAP_MATERIAL") or "").strip()


def _mat_2() -> str:
    return (os.environ.get("SAP_MATERIAL_2") or "").strip()


def _require_two_materials() -> tuple[str, str]:
    m1, m2 = _mat_1(), _mat_2()
    if not m1 or not m2 or m1 == m2:
        raise SystemExit("Invalid SAP_MATERIAL / SAP_MATERIAL_2 — import reference master or set env")
    return m1, m2


def simple_yast_form(*, header_note: str) -> dict:
    """One material, one asset (line-level + alloc — back-compat)."""
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    asset = _asset_a()
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=_mat_1(),
                short_text="YAST simple — 1 mat 1 asset",
                asset=asset,
                allocations=[{"asset": asset, "qty": "1"}],
            )
        ],
    }


def yast_multi_asset_form(*, header_note: str) -> dict:
    """One material, two assets (qty splits)."""
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    a1, a2 = _asset_a(), _asset_b()
    if not a1 or not a2:
        raise SystemExit("SAP_ASSET / SAP_ASSET_2 required for multi-asset shape")
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=_mat_1(),
                short_text="YAST multi-asset — 1 mat 2 assets",
                asset=a1,
                valuation_price="200",
                allocations=[
                    {"asset": a1, "qty": "2"},
                    {"asset": a2, "qty": "1"},
                ],
            )
        ],
    }


# Backward-compatible alias for scripts that still pass multi-cc.
yast_multi_cc_form = yast_multi_asset_form


def yast_two_material_form(*, header_note: str) -> dict:
    """Two materials; each line one asset."""
    m1, m2 = _require_two_materials()
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    a1, a2 = _asset_a(), _asset_b()
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=m1,
                short_text="YAST two-mat line A — 1 asset",
                asset=a1,
                valuation_price="200",
                allocations=[{"asset": a1, "qty": "2"}],
            ),
            _line(
                material=m2,
                short_text="YAST two-mat line B — 1 asset",
                asset=a2 or a1,
                delivery_date="2026-07-15",
                unit_price="50",
                valuation_price="50",
                allocations=[{"asset": a2 or a1, "qty": "3"}],
            ),
        ],
    }


complex_yast_form = yast_two_material_form

YAST_SHAPES = {
    "simple": simple_yast_form,
    "multi-asset": yast_multi_asset_form,
    "multi-cc": yast_multi_asset_form,  # alias
    "two-material": yast_two_material_form,
}


def main() -> int:
    load_env()
    applied = apply_integration_reference_defaults()
    print(integration_defaults_summary(applied))

    p = argparse.ArgumentParser(description="Create PR (YAST) through AgentOS API → SAP")
    add_common_args(p)
    p.add_argument(
        "--shape",
        choices=list(YAST_SHAPES.keys()),
        default="simple",
    )
    p.add_argument("--complex", action="store_true", help="Deprecated: use --shape two-material")
    args = p.parse_args()
    shape = "two-material" if args.complex else args.shape
    form = YAST_SHAPES[shape](header_note=args.note)

    if args.preview_sap or args.dry_run:
        preview_sap_payload(DOCUMENT_TYPE, form)
    if args.dry_run:
        return 0

    token = resolve_token_from_args(args)
    ticket = create_pr_via_api(
        access_token=token,
        document_type=DOCUMENT_TYPE,
        form=form,
    )
    return print_ticket_result(ticket, label=f"YAST {shape}")


if __name__ == "__main__":
    raise SystemExit(main())
