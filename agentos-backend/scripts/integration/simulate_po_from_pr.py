#!/usr/bin/env python3
"""Probe SAP PO create (API_PURCHASEORDER_PROCESS_SRV) from existing YUNB/YAST PRs on QAS.

Uses the same ``build_po_payload`` / ``create_po`` path as production. YSER excluded.

Usage (agentos-backend/, SAP in .env):

  python scripts/integration/simulate_po_from_pr.py
  python scripts/integration/simulate_po_from_pr.py --case yunb-simple
  python scripts/integration/simulate_po_from_pr.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_po_client import create_po, sap_po_configured
from app.procurement.sap_po_payload import build_po_payload
from app.procurement.sap_pr_client import (
    _credentials_or_error,
    _sap_error_message,
    sap_gateway_query_params,
    sap_pr_configured,
)

CASES: dict[str, dict[str, str]] = {
    "yunb-simple": {"doc_type": "YUNB", "pr": "1040000063", "label": "1 mat 1 CC"},
    "yunb-multi-cc": {"doc_type": "YUNB", "pr": "1040000064", "label": "1 mat 2 CC"},
    "yunb-two-material": {"doc_type": "YUNB", "pr": "1040000065", "label": "2 mat 2 CC each"},
    "yast-simple": {"doc_type": "YAST", "pr": "1030000102", "label": "1 mat 1 CC"},
    "yast-multi-cc": {"doc_type": "YAST", "pr": "1030000103", "label": "1 mat 2 CC"},
    "yast-two-material": {"doc_type": "YAST", "pr": "1030000104", "label": "2 mat 2 CC each"},
}

DEFAULT_SUPPLIER = "1000000002"
DEFAULT_PUR_ORG = "1MGH"


@dataclass
class PrItem:
    item_no: str
    material: str
    plant: str
    sloc: str
    material_group: str
    qty: str
    unit: str
    price: str
    pur_group: str
    pur_org: str
    item_cat: str
    acct_cat: str
    short_text: str
    acct_rows: list[dict[str, str]]
    master_asset: str = ""


async def _fetch_pr_items(pr_number: str) -> list[PrItem]:
    creds, err = _credentials_or_error()
    if err:
        raise RuntimeError(err)
    base, user, password = creds
    params = sap_gateway_query_params()
    params["$filter"] = f"PurchaseRequisition eq '{pr_number}'"
    params["$expand"] = "to_PurchaseReqnAcctAssgmt"
    url = base.rstrip("/") + "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionItem"
    async with httpx.AsyncClient(
        verify=bool(settings.procurement_sap_verify_ssl),
        timeout=httpx.Timeout(120.0),
        auth=(user, password),
    ) as client:
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(_sap_error_message(resp))
    out: list[PrItem] = []
    for it in resp.json().get("d", {}).get("results", []):
        accts: list[dict[str, str]] = []
        master_asset = ""
        for a in it.get("to_PurchaseReqnAcctAssgmt", {}).get("results", []):
            asset = str(a.get("MasterFixedAsset") or "").strip()
            if asset and not master_asset:
                master_asset = asset
            accts.append(
                {
                    "seq": str(a.get("PurchaseReqnAcctAssgmtNumber") or ""),
                    "cc": str(a.get("CostCenter") or "").strip(),
                    "qty": str(a.get("Quantity") or "").strip(),
                    "asset": asset,
                }
            )
        out.append(
            PrItem(
                item_no=str(it.get("PurchaseRequisitionItem") or "10").strip(),
                material=str(it.get("Material") or "").strip(),
                plant=str(it.get("Plant") or "H001").strip(),
                sloc=str(it.get("StorageLocation") or "3021").strip(),
                material_group=str(it.get("MaterialGroup") or "SD05-0001").strip(),
                qty=str(it.get("RequestedQuantity") or "1.000").strip(),
                unit=str(it.get("BaseUnit") or "KG").strip(),
                price=str(it.get("PurchaseRequisitionPrice") or "10.00").strip(),
                pur_group=str(it.get("PurchasingGroup") or "A0B").strip(),
                pur_org=str(it.get("PurchasingOrganization") or "").strip(),
                item_cat=str(it.get("PurchasingDocumentItemCategory") or "0").strip(),
                acct_cat=str(it.get("AccountAssignmentCategory") or "K").strip(),
                short_text=str(it.get("PurchaseRequisitionItemText") or "PO sim").strip()[:40],
                acct_rows=accts,
                master_asset=master_asset,
            )
        )
    return sorted(out, key=lambda x: int(x.item_no or "0"))


def _agentos_form_from_pr(
    *,
    doc_type: str,
    pur_org: str,
    pur_group: str,
    supplier: str,
    pr_number: str,
    items: list[PrItem],
    link_pr: bool,
    tax_code: str | None,
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for row in items:
        allocs = [
            {"cost_center": a["cc"], "qty": a["qty"] or row.qty}
            for a in row.acct_rows
            if a.get("cc")
        ]
        if doc_type.upper() == "YAST":
            allocs = [
                {
                    "asset": (a.get("asset") or row.master_asset or ""),
                    "qty": a["qty"] or row.qty,
                }
                for a in row.acct_rows
                if a.get("asset") or a.get("qty") or row.master_asset
            ]
            if not allocs and row.master_asset:
                allocs = [{"asset": row.master_asset, "qty": row.qty}]
        elif not allocs:
            allocs = [{"cost_center": "", "qty": row.qty}]
        line: dict[str, Any] = {
                "material": row.material,
                "short_text": row.short_text,
                "unit_price": row.price,
                "net_price": row.price,
                "order_unit": row.unit,
                "purchase_requisition_item": row.item_no,
                "item_category": row.item_cat,
                "account_assignment_cat": row.acct_cat,
                "allocations": allocs,
            }
        if doc_type.upper() == "YAST" and row.master_asset:
            line["asset"] = row.master_asset
        lines.append(line)
    header: dict[str, Any] = {
        "purchasing_org": pur_org,
        "purchasing_group": pur_group,
        "plant": items[0].plant if items else "H001",
        "storage_location": items[0].sloc if items else "3021",
        "material_group": items[0].material_group if items else "SD05-0001",
        "vendor": supplier,
        "header_note": f"PO sim from PR {pr_number}",
    }
    if tax_code:
        header["tax_code"] = tax_code
    return {
        "header": header,
        "lines": lines,
    }


def _build_payload(
    *,
    doc_type: str,
    pur_org: str,
    pur_group: str,
    supplier: str,
    pr_number: str,
    items: list[PrItem],
    link_pr: bool,
    tax_code: str | None,
) -> dict[str, Any]:
    form = _agentos_form_from_pr(
        doc_type=doc_type,
        pur_org=pur_org,
        pur_group=pur_group,
        supplier=supplier,
        pr_number=pr_number,
        items=items,
        link_pr=link_pr,
        tax_code=tax_code,
    )
    return build_po_payload(
        form=form,
        document_type=doc_type,
        ticket_id=f"po-sim-{pr_number}",
        parent_pr_number=pr_number if link_pr else None,
        kind="PO",
    )


async def run_case(case_id: str, meta: dict[str, str]) -> list[dict[str, Any]]:
    pr = meta["pr"]
    doc_type = meta["doc_type"]
    items = await _fetch_pr_items(pr)
    if not items:
        return [{"case": case_id, "variant": "—", "status": "SKIP", "error": "no PR items on SAP"}]

    pur_org = items[0].pur_org or os.environ.get("SAP_PUR_ORG", DEFAULT_PUR_ORG)
    pur_group = items[0].pur_group or "A0B"
    supplier = os.environ.get("SAP_VENDOR", DEFAULT_SUPPLIER)

    variants = [
        ("linked_min", True, None),
        ("linked_tax_XE", True, "XE"),
        ("linked_acct", True, None),
        ("standalone_min", False, None),
        ("standalone_tax_XE", False, "XE"),
    ]

    results: list[dict[str, Any]] = []
    for vname, link_pr, tax in variants:
        form = _agentos_form_from_pr(
            doc_type=doc_type,
            pur_org=pur_org,
            pur_group=pur_group,
            supplier=supplier,
            pr_number=pr,
            items=items,
            link_pr=link_pr,
            tax_code=tax,
        )
        po_id, err = await create_po(
            ticket_id=f"po-{case_id}-{vname[:8]}",
            form=form,
            document_type=doc_type,
            parent_sap_id=pr if link_pr else None,
        )
        results.append(
            {
                "case": case_id,
                "pr": pr,
                "doc_type": doc_type,
                "label": meta.get("label"),
                "variant": vname,
                "link_pr": link_pr,
                "tax_code": tax or "",
                "item_count": len(items),
                "status": "PASS" if po_id and not err else "FAIL",
                "po_id": po_id or "",
                "error": (err or "")[:400],
            }
        )
        print(
            f"{case_id:20} {vname:18} -> {'PASS ' + po_id if po_id and not err else 'FAIL ' + (err or '')[:120]}"
        )
    return results


async def dry_run_case(case_id: str, meta: dict[str, str]) -> dict[str, Any]:
    pr = meta["pr"]
    items = await _fetch_pr_items(pr)
    payload = _build_payload(
        doc_type=meta["doc_type"],
        pur_org=os.environ.get("SAP_PUR_ORG", DEFAULT_PUR_ORG),
        pur_group=items[0].pur_group if items else "A0B",
        supplier=os.environ.get("SAP_VENDOR", DEFAULT_SUPPLIER),
        pr_number=pr,
        items=items,
        link_pr=True,
        tax_code=None,
    )
    return {
        "case": case_id,
        "pr": pr,
        "label": meta.get("label"),
        "item_count": len(items),
        "recommended_variant": "linked_acct",
        "payload": payload,
    }


async def main() -> int:
    p = argparse.ArgumentParser(description="Simulate PO create from existing YUNB/YAST PRs")
    p.add_argument("--case", choices=[*CASES.keys(), "all"], default="all")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payloads from SAP PR only (no PO POST); use when PO OData not authorized",
    )
    p.add_argument("--json-out", default="/tmp/po_simulation_from_pr.json")
    args = p.parse_args()

    if not sap_pr_configured():
        print("SAP not configured", file=sys.stderr)
        return 1
    if not args.dry_run and not sap_po_configured():
        print("SAP PO not configured", file=sys.stderr)
        return 1

    ids = list(CASES.keys()) if args.case == "all" else [args.case]

    if args.dry_run:
        out: list[dict[str, Any]] = []
        for cid in ids:
            block = await dry_run_case(cid, CASES[cid])
            out.append(block)
            print(f"{cid}: PR {block['pr']} -> {block['item_count']} item(s), payload keys OK")
        Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\nDry-run payloads written to {args.json_out}")
        return 0

    all_results: list[dict[str, Any]] = []
    for cid in ids:
        print(f"\n--- {cid} PR {CASES[cid]['pr']} ({CASES[cid]['label']}) ---")
        all_results.extend(await run_case(cid, CASES[cid]))

    passed = [r for r in all_results if r["status"] == "PASS"]
    Path(args.json_out).write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nDone: {len(passed)}/{len(all_results)} PASS. Wrote {args.json_out}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
