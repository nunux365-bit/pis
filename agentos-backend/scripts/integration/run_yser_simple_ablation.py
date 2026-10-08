#!/usr/bin/env python3
"""Ablation: YSER simple — doc-success payload vs AgentOS; change one field at a time."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_pr_client import _sap_create_pr, sap_pr_configured

# Successful doc shape (PR Create API-Structure Additional cases.docx) — trimmed create body.
DOC_BASELINE: dict = {
    "PurchaseRequisition": "",
    "PurchaseRequisitionType": "YSER",
    "SourceDetermination": True,
    "PurReqnDescription": "YSER ablation",
    "to_PurchaseReqnItem": [
        {
            "PurchaseRequisitionItem": "10",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "M0B",
            "Plant": "H002",
            "CompanyCode": "1MGH",
            "AccountAssignmentCategory": "",
            "Material": "4200000027",
            "MaterialGroup": "SD05-0001",
            "StorageLocation": "1001",
            "PurchasingDocumentItemCategory": "0",
            "ProductType": "1",
            "ServicePerformer": "",
            "RequestedQuantity": "6.000",
            "BaseUnit": "KG",
            "PurchaseRequisitionPrice": "1471.20",
            "PurchaseRequisitionItemText": "Service line",
            "DeliveryDate": "/Date(1745971200000)/",
            "to_PurchaseReqnAcctAssgmt": [
                {
                    "PurchaseReqnAcctAssgmtNumber": "1",
                    "CostCenter": "HCO91001H0",
                    "Quantity": "6.000",
                }
            ],
        }
    ],
}


def _item(p: dict) -> dict:
    return p["to_PurchaseReqnItem"][0]


def _build_cases() -> list[tuple[str, str, object]]:
    cases: list[tuple[str, str, object]] = []

    def add(case_id: str, desc: str, mutate):
        cases.append((case_id, desc, mutate))

    def noop(_p):
        pass

    def agentos(p):
        it = _item(p)
        it["Material"] = ""
        it["ServicePerformer"] = "1000000000"
        it["ProductType"] = "2"
        it["PurchasingDocumentItemCategory"] = "9"
        it["BaseUnit"] = "QT"
        it["Plant"] = "H001"
        it["StorageLocation"] = "3021"
        it["PurchasingGroup"] = "A0B"
        it["RequestedQuantity"] = "1.000"
        it["PurchaseRequisitionPrice"] = "100"
        it["to_PurchaseReqnAcctAssgmt"][0]["Quantity"] = "1.000"
        p["SourceDetermination"] = False

    add("doc_baseline", "Exact doc-success body", noop)
    add("agentos_equivalent", "Current AgentOS-shaped payload", agentos)
    add("only_ProductType_2", "doc + ProductType 2 only", lambda p: _item(p).__setitem__("ProductType", "2"))
    add("only_ItemCat_9", "doc + Item category 9 only", lambda p: _item(p).__setitem__("PurchasingDocumentItemCategory", "9"))
    add("only_ServicePerformer_DB", "doc + ServicePerformer 1000000000 only", lambda p: _item(p).__setitem__("ServicePerformer", "1000000000"))
    add("only_no_Material", "doc + empty Material only", lambda p: _item(p).__setitem__("Material", ""))
    add("only_SourceDetermination_false", "doc + SourceDetermination false only", lambda p: p.__setitem__("SourceDetermination", False))
    add("only_plant_H001", "doc + Plant H001 only", lambda p: _item(p).__setitem__("Plant", "H001"))
    add("only_BaseUnit_QT", "doc + BaseUnit QT only", lambda p: _item(p).__setitem__("BaseUnit", "QT"))
    add("only_pur_group_A0B", "doc + PurchasingGroup A0B only", lambda p: _item(p).__setitem__("PurchasingGroup", "A0B"))

    def add_validity(p):
        it = _item(p)
        it["PerformancePeriodStartDate"] = "/Date(1745971200000)/"
        it["PerformancePeriodEndDate"] = "/Date(1753747200000)/"

    add("add_PerformancePeriodStart", "doc + PerformancePeriod start/end", add_validity)
    add("remove_DeliveryDate", "doc without DeliveryDate", lambda p: _item(p).pop("DeliveryDate", None))
    add("only_AcctCat_K", "doc + AccountAssignmentCategory K only", lambda p: _item(p).__setitem__("AccountAssignmentCategory", "K"))

    def performer_type2_cat9(p):
        it = _item(p)
        it["ServicePerformer"] = "1000000000"
        it["ProductType"] = "2"
        it["PurchasingDocumentItemCategory"] = "9"

    add("doc_plus_performer_type2_cat9", "doc + performer + type2 + cat9", performer_type2_cat9)
    add("doc_no_material", "doc with Material cleared", lambda p: _item(p).__setitem__("Material", ""))

    return cases


async def run_case(case_id: str, desc: str, mutate) -> dict:
    payload = copy.deepcopy(DOC_BASELINE)
    if mutate:
        mutate(payload)
    sap_id, err = await _sap_create_pr(payload=payload, ticket_id=f"yser-{case_id[:16]}")
    return {
        "case_id": case_id,
        "description": desc,
        "status": "PASS" if sap_id else "FAIL",
        "sap_id": sap_id,
        "error": (err or "")[:280] if err else None,
        "item_snapshot": {k: _item(payload).get(k) for k in (
            "Material", "ServicePerformer", "ProductType", "AccountAssignmentCategory",
            "PurchasingDocumentItemCategory", "BaseUnit", "Plant", "PurchasingGroup",
            "PerformancePeriodStartDate",
        )},
        "SourceDetermination": payload.get("SourceDetermination"),
    }


async def main() -> int:
    if not sap_pr_configured():
        print("SAP not configured", file=sys.stderr)
        return 2
    print(f"SAP: {settings.procurement_sap_base_url}\n")
    results = []
    for case_id, desc, mutate in _build_cases():
        r = await run_case(case_id, desc, mutate)
        results.append(r)
        mark = "✓" if r["status"] == "PASS" else "✗"
        print(f"{mark} {r['status']:4}  {case_id}")
        print(f"         {desc}")
        if r["sap_id"]:
            print(f"         PR {r['sap_id']}")
        else:
            print(f"         {(r['error'] or '')[:200]}")
        await asyncio.sleep(0.2)

    passed = [r for r in results if r["status"] == "PASS"]
    failed = [r for r in results if r["status"] == "FAIL"]
    print(f"\n=== SUMMARY: {len(passed)} pass / {len(failed)} fail ===")
    out = Path("/tmp/yser_simple_ablation.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
