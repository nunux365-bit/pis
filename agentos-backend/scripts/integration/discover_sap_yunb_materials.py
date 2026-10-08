#!/usr/bin/env python3
"""Probe SAP QAS for YUNB materials valid on a plant (for two-material integration tests).

DB ``pr_po_reference_values`` (domain ``material``) uses codes like ``3000000004`` / ``4000000027``
that often do **not** match SAP's activated material numbers on QAS (e.g. ``4200000027``).

Usage (from agentos-backend/, SAP configured in .env):

  python scripts/integration/discover_sap_yunb_materials.py
  python scripts/integration/discover_sap_yunb_materials.py --plant H001 --sloc 3021
  python scripts/integration/discover_sap_yunb_materials.py --scan-range 1 100
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_pr_client import _sap_create_pr, sap_pr_configured


def _probe_payload(*, material: str, plant: str, sloc: str, material_group: str) -> dict:
    return {
        "PurchaseRequisition": "",
        "PurchaseRequisitionType": "YUNB",
        "PurReqnDescription": "MatDiscovery",
        "to_PurchaseReqnItem": [
            {
                "PurchaseRequisitionItem": "10",
                "PurchasingOrganization": "1MGH",
                "PurchasingGroup": "A0B",
                "Plant": plant,
                "CompanyCode": "1MGH",
                "AccountAssignmentCategory": "K",
                "Material": material,
                "MaterialGroup": material_group,
                "StorageLocation": sloc,
                "RequestedQuantity": "1.000",
                "PurchaseRequisitionPrice": "1.00",
                "to_PurchaseReqnAcctAssgmt": [
                    {
                        "PurchaseReqnAcctAssgmtNumber": "1",
                        "CostCenter": "HCO91001H0",
                        "Quantity": "1.000",
                    }
                ],
            }
        ],
    }


async def _probe_one(
    *, material: str, plant: str, sloc: str, material_group: str
) -> tuple[bool, str | None]:
    payload = _probe_payload(
        material=material, plant=plant, sloc=sloc, material_group=material_group
    )
    sap_id, err = await _sap_create_pr(payload=payload, ticket_id=f"disc-{material[-6:]}")
    if sap_id:
        return True, sap_id
    err_s = (err or "").lower()
    if "does not exist" in err_s or "not activated" in err_s or "not maintained in plant" in err_s:
        return False, None
    return False, (err or "")[:120]


async def main() -> int:
    p = argparse.ArgumentParser(description="Discover SAP-valid YUNB materials for a plant")
    p.add_argument("--plant", default="H001")
    p.add_argument("--sloc", default="3021")
    p.add_argument("--material-group", default="SD05-0001")
    p.add_argument("--scan-range", nargs=2, type=int, metavar=("START", "END"), default=(1, 100))
    p.add_argument("--prefix", default="42000000", help="Material prefix before 2-digit suffix")
    args = p.parse_args()

    if not sap_pr_configured():
        print("SAP not configured (PROCUREMENT_SAP_* in .env)", file=sys.stderr)
        return 2

    print(f"SAP: {settings.procurement_sap_base_url}  plant={args.plant}  sloc={args.sloc}")
    start, end = args.scan_range
    valid: list[str] = []
    for i in range(start, end + 1):
        mat = f"{args.prefix}{i:02d}"
        ok, detail = await _probe_one(
            material=mat,
            plant=args.plant,
            sloc=args.sloc,
            material_group=args.material_group,
        )
        if ok:
            valid.append(mat)
            print(f"  OK  {mat}  (created PR {detail})")
        await asyncio.sleep(0.08)

    print(f"\nValid on {args.plant}: {valid}")
    if len(valid) >= 2:
        print("\nSuggested env for two-material YUNB tests:")
        print(f"  export SAP_PLANT={args.plant}")
        print(f"  export SAP_SLOC={args.sloc}")
        print(f"  export SAP_MATERIAL={valid[0]}")
        print(f"  export SAP_MATERIAL_2={valid[1]}")
        print(f"  export SAP_MATERIAL_GROUP={args.material_group}")
    elif len(valid) == 1:
        print("\nOnly one material found — need a second valid code for two-line tests.")
    else:
        print("\nNo materials passed — check plant/sloc or widen --scan-range.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
