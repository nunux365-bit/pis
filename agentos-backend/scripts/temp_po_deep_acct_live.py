#!/usr/bin/env python3
"""Live PO create: PR-aligned deep to_AccountAssignment. Temp script."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import load_env
from scripts.integration.sap_po_doc_fixtures import apply_po_doc_master_env
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import create_po, get_po
from app.procurement.sap_ticket_form_read import form_from_po_sap_read

load_env()
apply_po_doc_master_env()

CASES = {
    "yunb_simple": (
        "YUNB",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1001",
                "material_group": "S002-0001",
                "vendor": "1000000002",
                "tax_code": "XE",
            },
            "lines": [
                {
                    "material": "4100000012",
                    "short_text": "YUNB simple",
                    "delivery_date": "2026-08-15",
                    "unit_price": "100.00",
                    "order_unit": "PC",
                    "allocations": [{"cost_center": "HCO91001H0", "qty": "5"}],
                }
            ],
        },
    ),
    "yunb_multi_cc": (
        "YUNB",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1001",
                "material_group": "S002-0001",
                "vendor": "1000000002",
                "tax_code": "XE",
            },
            "lines": [
                {
                    "material": "4100000012",
                    "short_text": "YUNB multi-CC",
                    "delivery_date": "2026-08-15",
                    "unit_price": "100.00",
                    "order_unit": "PC",
                    "allocations": [
                        {"cost_center": "HCO91001H0", "qty": "2"},
                        {"cost_center": "HBM11001A0", "qty": "3"},
                    ],
                }
            ],
        },
    ),
    "yunb_two_line": (
        "YUNB",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1001",
                "material_group": "S002-0001",
                "vendor": "1000000002",
                "tax_code": "XE",
            },
            "lines": [
                {
                    "material": "4100000012",
                    "short_text": "line 10",
                    "delivery_date": "2026-08-01",
                    "unit_price": "100.00",
                    "order_unit": "PC",
                    "allocations": [{"cost_center": "HCO91001H0", "qty": "5"}],
                },
                {
                    "material": "4100000018",
                    "short_text": "line 20",
                    "delivery_date": "2026-09-01",
                    "unit_price": "1500.00",
                    "order_unit": "KG",
                    "allocations": [{"cost_center": "HCO91001H0", "qty": "5"}],
                },
            ],
        },
    ),
    "yast_simple": (
        "YAST",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1003",
                "material_group": "M003-0043",
                "vendor": "1000000002",
                "tax_code": "XE",
                "gl_account": "29000001",
                "controlling_area": "T1MG",
            },
            "lines": [
                {
                    "material": "4100000018",
                    "short_text": "YAST simple",
                    "delivery_date": "2026-08-15",
                    "unit_price": "1500.00",
                    "order_unit": "KG",
                    "asset": "7100001182",
                    "purchasing_info_record": "5300000009",
                    "allocations": [{"cost_center": "", "qty": "5"}],
                }
            ],
        },
    ),
    "yast_multi_cc": (
        "YAST",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1003",
                "material_group": "M003-0043",
                "vendor": "1000000002",
                "tax_code": "XE",
                "gl_account": "29000001",
                "controlling_area": "T1MG",
            },
            "lines": [
                {
                    "material": "4100000018",
                    "short_text": "YAST multi-CC",
                    "delivery_date": "2026-08-15",
                    "unit_price": "1500.00",
                    "order_unit": "KG",
                    "asset": "7100001182",
                    "purchasing_info_record": "5300000009",
                    "allocations": [
                        {"cost_center": "HCO91001H0", "qty": "2"},
                        {"cost_center": "HBM11001A0", "qty": "3"},
                    ],
                }
            ],
        },
    ),
    "yast_two_line": (
        "YAST",
        {
            "header": {
                "purchasing_org": "1LFS",
                "purchasing_group": "A0B",
                "plant": "L001",
                "storage_location": "1003",
                "material_group": "M003-0043",
                "vendor": "1000000002",
                "tax_code": "XE",
                "gl_account": "29000001",
                "controlling_area": "T1MG",
            },
            "lines": [
                {
                    "material": "4100000018",
                    "short_text": "YAST A",
                    "delivery_date": "2026-08-15",
                    "unit_price": "1500.00",
                    "order_unit": "KG",
                    "asset": "7100001182",
                    "purchasing_info_record": "5300000009",
                    "allocations": [{"cost_center": "", "qty": "5"}],
                },
                {
                    "material": "4100000012",
                    "short_text": "YAST B",
                    "delivery_date": "2026-09-15",
                    "unit_price": "100.00",
                    "order_unit": "PC",
                    "asset": "7100001182",
                    "purchasing_info_record": "5300000009",
                    "allocations": [{"cost_center": "", "qty": "5"}],
                },
            ],
        },
    ),
}


async def run(name: str, dt: str, form_raw: dict) -> dict:
    form = normalize_form(dt, form_raw)
    apply_procurement_defaults(form, document_type=dt, kind="PO")
    po, err = await create_po(ticket_id=str(uuid.uuid4()), form=form, document_type=dt)
    out = {"case": name, "create": "OK" if po and not err else "FAIL", "po": po, "error": (err or "")[:500]}
    if po and not err:
        body, gerr = await get_po(po_number=po, ticket_id="read")
        if gerr:
            out["get"] = "FAIL"
            out["get_error"] = gerr[:200]
        else:
            ui = form_from_po_sap_read(body or {}, document_type=dt)
            out["get"] = "OK"
            out["lines"] = []
            for ln in ui.get("lines") or []:
                out["lines"].append(
                    {
                        "material": ln.get("material"),
                        "delivery": ln.get("delivery_date"),
                        "asset": ln.get("asset"),
                        "ccs": [a.get("cost_center") for a in (ln.get("allocations") or []) if a.get("cost_center")],
                    }
                )
    return out


async def main() -> None:
    results = []
    for name, (dt, form) in CASES.items():
        print(f"Running {name}...", flush=True)
        results.append(await run(name, dt, form))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
