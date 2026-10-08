#!/usr/bin/env python3
"""Live integration: create PR via AgentOS API — workflow YUNB (Consumable).

Mimics UI: POST /api/procurement/tickets/pr → server normalize → SAP.

Test shapes (see docs/procurement_sap_integration_playbook.md):
  - simple:         1 material, 1 cost centre
  - multi-cc:       1 material, 2 cost centres (qty split)
  - two-material:   2 distinct materials, each line 2 cost centres

Usage:

  python scripts/integration/run_pr_create_yunb.py
  python scripts/integration/run_pr_create_yunb.py --shape multi-cc

Materials/plant/CC from ``pr_po_reference_values`` (see ``integration_reference_defaults``).
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

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    create_pr_via_api,
    load_env,
    preview_sap_payload,
    print_ticket_result,
    resolve_token_from_args,
)

DOCUMENT_TYPE = "YUNB"


def _header(*, header_note: str, material_group: str) -> dict[str, Any]:
    return {
        "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
        "purchasing_group": os.environ.get("SAP_PUR_GROUP", "S0Z"),
        "plant": os.environ.get("SAP_PLANT", ""),
        "storage_location": os.environ.get("SAP_SLOC", "1001"),
        "material_group": material_group,
        "header_note": header_note or "Integration YUNB",
    }


def _line(
    *,
    material: str,
    short_text: str,
    allocations: list[dict[str, str]],
    delivery_date: str = "2026-07-01",
    unit_price: str = "10",
    valuation_price: str = "10",
) -> dict[str, Any]:
    return {
        "material": material,
        "short_text": short_text,
        "delivery_date": delivery_date,
        "unit_price": unit_price,
        "valuation_price": valuation_price,
        "allocations": allocations,
    }


def _cc_a() -> str:
    return (os.environ.get("SAP_COST_CENTER") or "HBM13291G0").strip()


def _cc_b() -> str:
    return (os.environ.get("SAP_COST_CENTER_2") or "HBM13291H0").strip()


def _mat_1() -> str:
    return (os.environ.get("SAP_MATERIAL") or "").strip()


def _mat_2() -> str:
    return (os.environ.get("SAP_MATERIAL_2") or "").strip()


def _require_two_materials() -> tuple[str, str]:
    m1, m2 = _mat_1(), _mat_2()
    if not m1 or not m2:
        raise SystemExit("SAP_MATERIAL / SAP_MATERIAL_2 missing — run via integration scripts (sap_qas_fixtures)")
    if m1 == m2:
        raise SystemExit(
            f"SAP_MATERIAL_2 must differ from SAP_MATERIAL for two-material tests (both {m1!r})"
        )
    return m1, m2


def simple_yunb_form(*, header_note: str) -> dict:
    """One material, one cost centre."""
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=_mat_1(),
                short_text="YUNB simple — 1 mat 1 CC",
                allocations=[{"cost_center": _cc_a(), "qty": "1"}],
            )
        ],
    }


def yunb_multi_cc_form(*, header_note: str) -> dict:
    """One material, two cost centres on a single line (multi account assignment)."""
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=_mat_1(),
                short_text="YUNB multi-cc — 1 mat 2 CC",
                unit_price="10",
                valuation_price="30",
                allocations=[
                    {"cost_center": _cc_a(), "qty": "2"},
                    {"cost_center": _cc_b(), "qty": "1"},
                ],
            )
        ],
    }


def yunb_two_material_form(*, header_note: str) -> dict:
    """Two distinct materials; each line has two cost centres."""
    m1, m2 = _require_two_materials()
    mg = (os.environ.get("SAP_MATERIAL_GROUP") or "").strip()
    return {
        "header": _header(header_note=header_note, material_group=mg),
        "lines": [
            _line(
                material=m1,
                short_text="YUNB two-mat line A — 2 CC",
                unit_price="10",
                valuation_price="30",
                allocations=[
                    {"cost_center": _cc_a(), "qty": "2"},
                    {"cost_center": _cc_b(), "qty": "1"},
                ],
            ),
            _line(
                material=m2,
                short_text="YUNB two-mat line B — 2 CC",
                delivery_date="2026-07-10",
                unit_price="25",
                valuation_price="50",
                allocations=[
                    {"cost_center": _cc_a(), "qty": "3"},
                    {"cost_center": _cc_b(), "qty": "2"},
                ],
            ),
        ],
    }


# Workshop matrix / resubmit scripts still import this name for “complex” YUNB.
complex_yunb_form = yunb_two_material_form

YUNB_SHAPES = {
    "simple": simple_yunb_form,
    "multi-cc": yunb_multi_cc_form,
    "two-material": yunb_two_material_form,
}


def main() -> int:
    load_env()
    applied = apply_integration_reference_defaults()
    print(integration_defaults_summary(applied))

    p = argparse.ArgumentParser(description="Create PR (YUNB) through AgentOS API → SAP")
    add_common_args(p)
    p.add_argument(
        "--shape",
        choices=list(YUNB_SHAPES.keys()),
        default="simple",
        help="Test shape (default: simple)",
    )
    p.add_argument(
        "--complex",
        action="store_true",
        help="Deprecated: use --shape two-material",
    )
    args = p.parse_args()
    shape = "two-material" if args.complex else args.shape
    builder = YUNB_SHAPES[shape]
    form = builder(header_note=args.note)

    if args.preview_sap or args.dry_run:
        preview_sap_payload(DOCUMENT_TYPE, form)

    if args.dry_run:
        print(f"Dry-run: {shape} — no API call.")
        return 0

    token = resolve_token_from_args(args)
    ticket = create_pr_via_api(
        access_token=token,
        document_type=DOCUMENT_TYPE,
        form=form,
    )
    return print_ticket_result(ticket, label=f"YUNB {shape}")


if __name__ == "__main__":
    raise SystemExit(main())
