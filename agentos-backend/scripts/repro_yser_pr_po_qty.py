#!/usr/bin/env python3
"""Reproduce YSER PR→PO qty behaviour (16 in form vs 19 in SAP). QA only."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from typing import Any

from app.procurement import sap_sync
from app.procurement.sap_po_client import get_po
from app.procurement.sap_po_z_payload import (
    build_z_yser_po_post_body,
    form_from_z_po_read,
    parse_z_po_items_from_read,
)
from app.procurement.sap_pr_z_client import get_yser_pr
from app.procurement.sap_pr_z_payload import (
    build_z_yser_pr_payload,
    form_from_z_pr_read,
)


def _alloc_sum(form: dict[str, Any]) -> float:
    allocs = form.get("lines", [{}])[0].get("allocations", [])
    return sum(float(a.get("qty") or 0) for a in allocs if isinstance(a, dict))


def _sap_pr_qty_summary(body: dict[str, Any]) -> dict[str, Any]:
    form = form_from_z_pr_read(body, document_type="YSER")
    line = form["lines"][0]
    from app.procurement.sap_odata_utils import odata_results_list, odata_entity_properties, odata_text

    root = body.get("d") or body
    svc_rows: list[dict[str, str]] = []
    for item in odata_results_list(root.get("to_PRItemSet") or root.get("to_Items")):
        ip = odata_entity_properties(item)
        item_val = odata_text(ip.get("ValuationPrice"))
        for svc in odata_results_list(item.get("to_ServiceSet") or ip.get("to_ServiceSet")):
            sp = odata_entity_properties(svc)
            svc_rows.append(
                {
                    "cc": odata_text(sp.get("CostCenter")),
                    "quantity": odata_text(sp.get("Quantity")),
                    "distr_qty": odata_text(sp.get("DistrQuantity")),
                    "gross_price": odata_text(sp.get("GrossPrice")),
                }
            )
    return {
        "form_alloc_sum": _alloc_sum(form),
        "form_allocations": line.get("allocations"),
        "form_unit_price": line.get("unit_price"),
        "form_valuation": line.get("valuation_price"),
        "sap_item_valuation": item_val if svc_rows else "",
        "sap_service_rows": svc_rows,
        "sap_service_qty": svc_rows[0]["quantity"] if svc_rows else "",
    }


def _sap_po_qty_summary(body: dict[str, Any]) -> dict[str, Any]:
    form = form_from_z_po_read(body)
    _, snaps = parse_z_po_items_from_read(body)
    svc = snaps[0].services[0] if snaps and snaps[0].services else None
    acct = []
    if svc:
        for seg in svc.acct_segments:
            acct.append({"cc": seg.cost_center, "qty": seg.quantity})
    return {
        "form_alloc_sum": _alloc_sum(form),
        "form_allocations": form["lines"][0].get("allocations"),
        "sap_confirmed_qty": svc.confirmed_quantity if svc else "",
        "sap_line_total": svc.net_price_amount if svc else "",
        "sap_acct_qtys": acct,
        "pr_link": snaps[0].purchase_requisition if snaps else "",
    }


PR_HEADER = {
    "plant": "H002",
    "vendor": "",
    "tax_code": "",
    "po_remarks": "",
    "header_note": "",
    "company_code": "1MGH",
    "po_deadlines": "",
    "payment_terms": "",
    "service_group": "S082-0001",
    "material_group": "",
    "purchasing_org": "1MGH",
    "requestor_email": "",
    "purchasing_group": "A0D",
    "storage_location": "H002|1002",
    "tax_jurisdiction": "",
    "purchasing_doc_type": "",
    "po_terms_of_delivery": "",
}

PR_LINE_BASE = {
    "asset": "",
    "service": "000000001000000061",
    "material": "",
    "line_kind": "service",
    "net_price": "",
    "order_unit": "EA",
    "short_text": "Rent for premises REPRO",
    "unit_price": "100",
    "delivery_date": "2026-07-16",
    "item_category": "9",
    "service_group": "S082-0001",
    "material_group": "",
    "account_assignment_cat": "K",
    "purchase_requisition_item": "10",
}

ALLOCATIONS_16 = [
    {"qty": "1", "cost_center": "HBM11002A0"},
    {"qty": "1", "cost_center": "HBM13001I0"},
    {"qty": "6", "cost_center": "HBM11011A0"},
    {"qty": "8", "cost_center": "HBM13002H0"},
]

PO_HEADER = {
    "plant": "H002",
    "vendor": "1000000487",
    "tax_code": "FB",
    "po_remarks": "remark repro",
    "header_note": "",
    "company_code": "1MGH",
    "po_deadlines": "deadline repro",
    "payment_terms": "YI05",
    "service_group": "S082-0001",
    "material_group": "S082-0001",
    "purchasing_org": "1MGH",
    "requestor_email": "pravin.chahal@1mg.com",
    "purchasing_group": "A0D",
    "storage_location": "H002|1002",
    "tax_jurisdiction": "",
    "purchasing_doc_type": "",
    "po_terms_of_delivery": "terms repro",
}


async def run() -> None:
    run_id = uuid.uuid4().hex[:8]
    pr_ticket = f"repro-pr-{run_id}"
    po_ticket = f"repro-po-{run_id}"

    pr_line = copy.deepcopy(PR_LINE_BASE)
    pr_line["allocations"] = copy.deepcopy(ALLOCATIONS_16)
    # Match original case: 16 qty but line valuation 1900 (16×100=1600 would be consistent).
    pr_line["valuation_price"] = "1900"
    pr_line["gross_price"] = "1900"

    pr_form = {"header": copy.deepcopy(PR_HEADER), "lines": [pr_line]}
    po_line = copy.deepcopy(pr_line)
    po_line.update(
        {
            "short_text": "Rent for premises REPRO",
            "net_price": "1900.00",
            "unit_price": "100.00",
            "gross_price": "1900.00",
            "valuation_price": "1900.00",
            "allocations": copy.deepcopy(ALLOCATIONS_16),
        }
    )
    po_form = {"header": copy.deepcopy(PO_HEADER), "lines": [po_line]}

    print("=" * 60)
    print("REPRO RUN", run_id)
    print("PR form alloc sum:", _alloc_sum(pr_form), "valuation:", pr_line["valuation_price"])
    print("PO form alloc sum:", _alloc_sum(po_form))

    pr_payload = build_z_yser_pr_payload(
        form=pr_form, document_type="YSER", ticket_id=pr_ticket
    )
    first_svc = pr_payload["to_Items"][0]["to_Services"][0]
    print("\n--- AgentOS PR CREATE payload (first CC row) ---")
    print("  Item ValuationPrice:", repr(pr_payload["to_Items"][0].get("ValuationPrice")))
    print(
        "  Service:",
        {
            k: first_svc.get(k)
            for k in ("Quantity", "GrossPrice", "DistrQuantity", "CostCenter")
        },
    )

    pr_no, pr_err = await sap_sync.create_pr(
        ticket_id=pr_ticket, form=pr_form, document_type="YSER"
    )
    print("\n--- PR create ---")
    print("  sap_id:", pr_no, "error:", pr_err)
    if not pr_no:
        return

    pr_body, pr_read_err = await get_yser_pr(pr_number=pr_no)
    if pr_read_err or not pr_body:
        print("  PR read failed:", pr_read_err)
        return
    pr_summary = _sap_pr_qty_summary(pr_body)
    print("\n--- SAP PR after create ---")
    print(json.dumps(pr_summary, indent=2))

    po_payload = build_z_yser_po_post_body(
        form=po_form,
        parent_pr_number=pr_no,
        ticket_id=po_ticket,
        creator_email="pravin.chahal@1mg.com",
    )
    po_item = po_payload["d"]["to_PurchaseOrderItem"]["results"][0]
    po_svc = po_item["to_Services"]["results"][0]
    print("\n--- AgentOS PO CREATE payload ---")
    print("  ConfirmedQuantity:", po_svc.get("ConfirmedQuantity"))
    print(
        "  acct qtys:",
        [
            (a.get("CostCenter"), a.get("Quantity"))
            for a in po_svc["to_AccountAssignment"]["results"]
        ],
    )

    po_no, po_err = await sap_sync.create_po(
        ticket_id=po_ticket,
        form=po_form,
        document_type="YSER",
        parent_sap_id=pr_no,
        creator_email="pravin.chahal@1mg.com",
    )
    print("\n--- PO create ---")
    print("  sap_id:", po_no, "error:", po_err)
    if not po_no:
        return

    po_body, po_read_err = await get_po(po_number=po_no, document_type="YSER")
    if po_read_err or not po_body:
        print("  PO read failed:", po_read_err)
        return
    po_summary = _sap_po_qty_summary(po_body)
    print("\n--- SAP PO after create ---")
    print(json.dumps(po_summary, indent=2))

    print("\n--- REPRO VERDICT ---")
    pr_qty = float(pr_summary.get("sap_service_qty") or 0)
    po_qty = float(po_summary.get("sap_confirmed_qty") or 0)
    form_16 = _alloc_sum(pr_form) == 16.0
    sap_19 = abs(pr_qty - 19.0) < 0.01 and abs(po_qty - 19.0) < 0.01
    print(f"  Form sent 16 qty: {form_16}")
    print(f"  SAP PR qty: {pr_qty}")
    print(f"  SAP PO qty: {po_qty}")
    print(f"  Reproduced 16→19: {form_16 and sap_19}")
    print(f"  PR {pr_no}  PO {po_no}")


if __name__ == "__main__":
    asyncio.run(run())
