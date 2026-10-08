#!/usr/bin/env python3
"""Ablation: YUNB multi-CC doc payload — change/remove one field at a time → SAP POST.

Usage (from agentos-backend/):
  python scripts/integration/run_yunb_multi_cc_ablation.py
  python scripts/integration/run_yunb_multi_cc_ablation.py --only qty,baseline
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_pr_client import _sap_create_pr, sap_pr_configured

BASELINE: dict[str, Any] = {
    "PurchaseRequisition": "",
    "PurchaseRequisitionType": "YUNB",
    "to_PurchaseReqnItem": [
        {
            "PurchaseRequisitionItem": "10",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "A0B",
            "Plant": "H001",
            "CompanyCode": "1MGH",
            "AccountAssignmentCategory": "K",
            "Material": "4200000027",
            "MaterialGroup": "SD05-0001",
            "StorageLocation": "3021",
            "RequestedQuantity": "23.000",
            "PurchaseRequisitionPrice": "1.00",
            "MultipleAcctAssgmtDistribution": "2",
            "to_PurchaseReqnAcctAssgmt": [
                {
                    "PurchaseRequisition": "",
                    "PurchaseRequisitionItem": "10",
                    "PurchaseReqnAcctAssgmtNumber": "1",
                    "CostCenter": "HCO91001H0",
                    "MultipleAcctAssgmtDistrPercent": "40.0",
                    "CostElement": "40020003",
                    "GLAccount": "40020003",
                    "BusinessArea": "1001",
                },
                {
                    "PurchaseRequisition": "",
                    "PurchaseRequisitionItem": "10",
                    "PurchaseReqnAcctAssgmtNumber": "2",
                    "CostCenter": "HCO91001A0",
                    "MultipleAcctAssgmtDistrPercent": "60.0",
                    "CostElement": "40020003",
                    "GLAccount": "40020003",
                    "BusinessArea": "1001",
                },
            ],
        }
    ],
}


@dataclass
class Case:
    case_id: str
    description: str
    mutate: Callable[[dict[str, Any]], None]


def _item(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["to_PurchaseReqnItem"][0]


def _accts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _item(payload)["to_PurchaseReqnAcctAssgmt"]


def _qty_split(payload: dict[str, Any], *, q1: str, q2: str) -> None:
    it = _item(payload)
    it["MultipleAcctAssgmtDistribution"] = "1"
    for a in _accts(payload):
        a.pop("MultipleAcctAssgmtDistrPercent", None)
    _accts(payload)[0]["Quantity"] = q1
    _accts(payload)[1]["Quantity"] = q2


def _percent_split(payload: dict[str, Any], *, p1: str = "40.0", p2: str = "60.0") -> None:
    it = _item(payload)
    it["MultipleAcctAssgmtDistribution"] = "2"
    for a in _accts(payload):
        a.pop("Quantity", None)
    _accts(payload)[0]["MultipleAcctAssgmtDistrPercent"] = p1
    _accts(payload)[1]["MultipleAcctAssgmtDistrPercent"] = p2


def _strip_acct_extras(acct: dict[str, Any]) -> None:
    for k in ("CostElement", "GLAccount", "BusinessArea"):
        acct.pop(k, None)


CASES: list[Case] = [
    Case("baseline_percent_doc", "Exact doc: dist=2, 40/60%, K, CostElement/GL/BA", lambda p: None),
    Case(
        "qty_dist_with_K_and_extras",
        "dist=1, Quantity 9.2+13.8, K, keep CostElement/GL/BA",
        lambda p: _qty_split(p, q1="9.200", q2="13.800"),
    ),
    Case(
        "qty_dist_no_K",
        "dist=1, Quantity 9.2+13.8, AccountAssignmentCategory removed",
        lambda p: (_qty_split(p, q1="9.200", q2="13.800"), _item(p).pop("AccountAssignmentCategory", None)),
    ),
    Case(
        "qty_dist_no_acct_extras",
        "dist=1, Quantity only (no CostElement/GL/BA)",
        lambda p: (
            _qty_split(p, q1="9.200", q2="13.800"),
            [_strip_acct_extras(a) for a in _accts(p)],
        ),
    ),
    Case(
        "qty_dist_K_no_extras",
        "dist=1, K, CC+Qty only",
        lambda p: (
            _qty_split(p, q1="9.200", q2="13.800"),
            [_strip_acct_extras(a) for a in _accts(p)],
        ),
    ),
    Case(
        "remove_AccountAssignmentCategory",
        "percent doc without K",
        lambda p: _item(p).pop("AccountAssignmentCategory", None),
    ),
    Case(
        "change_AccountAssignmentCategory_empty",
        "percent doc, K → empty string",
        lambda p: _item(p).__setitem__("AccountAssignmentCategory", ""),
    ),
    Case(
        "remove_MultipleAcctAssgmtDistribution",
        "percent doc, drop distribution indicator",
        lambda p: _item(p).pop("MultipleAcctAssgmtDistribution", None),
    ),
    Case(
        "percent_no_MultipleAcctAssgmtDistrPercent",
        "dist=2 but no percent on acct rows",
        lambda p: [a.pop("MultipleAcctAssgmtDistrPercent", None) for a in _accts(p)],
    ),
    Case(
        "percent_CC_only",
        "dist=2, acct rows = CostCenter + percent only",
        lambda p: [_strip_acct_extras(a) for a in _accts(p)],
    ),
    Case(
        "remove_CostElement",
        "percent doc, drop CostElement on both acct rows",
        lambda p: [a.pop("CostElement", None) for a in _accts(p)],
    ),
    Case(
        "remove_GLAccount",
        "percent doc, drop GLAccount",
        lambda p: [a.pop("GLAccount", None) for a in _accts(p)],
    ),
    Case(
        "remove_BusinessArea",
        "percent doc, drop BusinessArea",
        lambda p: [a.pop("BusinessArea", None) for a in _accts(p)],
    ),
    Case(
        "remove_all_acct_finance_fields",
        "percent doc, drop CostElement+GL+BA together",
        lambda p: [_strip_acct_extras(a) for a in _accts(p)],
    ),
    Case(
        "remove_MaterialGroup",
        "percent doc, drop MaterialGroup",
        lambda p: _item(p).pop("MaterialGroup", None),
    ),
    Case(
        "remove_CompanyCode",
        "percent doc, drop CompanyCode",
        lambda p: _item(p).pop("CompanyCode", None),
    ),
    Case(
        "remove_StorageLocation",
        "percent doc, drop StorageLocation",
        lambda p: _item(p).pop("StorageLocation", None),
    ),
    Case(
        "remove_PurchaseRequisitionPrice",
        "percent doc, drop price",
        lambda p: _item(p).pop("PurchaseRequisitionPrice", None),
    ),
    Case(
        "remove_acct_PurchaseRequisition_keys",
        "percent doc, drop empty PR keys on acct rows",
        lambda p: [
            (a.pop("PurchaseRequisition", None), a.pop("PurchaseRequisitionItem", None))
            for a in _accts(p)
        ],
    ),
    Case(
        "qty_dist_with_extras_no_percent",
        "dist=1 + extras (AgentOS-like minus K)",
        lambda p: (
            _qty_split(p, q1="9.200", q2="13.800"),
            _item(p).pop("AccountAssignmentCategory", None),
        ),
    ),
    Case(
        "qty_dist_with_K_no_extras_explicit",
        "dist=1, K, no finance fields on acct",
        lambda p: (
            _qty_split(p, q1="9.200", q2="13.800"),
            [_strip_acct_extras(a) for a in _accts(p)],
        ),
    ),
    Case(
        "percent_wrong_total_50_50",
        "dist=2, 50/50% (still sums 100)",
        lambda p: _percent_split(p, p1="50.0", p2="50.0"),
    ),
    Case(
        "single_CC_only",
        "one acct row only (control — not multi)",
        lambda p: _item(p).__setitem__(
            "to_PurchaseReqnAcctAssgmt", [_accts(p)[0]]
        ),
    ),
]


async def run_case(case: Case) -> tuple[str, str | None, str | None]:
    payload = copy.deepcopy(BASELINE)
    case.mutate(payload)
    sap_id, err = await _sap_create_pr(payload=payload, ticket_id=f"abl-{case.case_id[:24]}")
    return case.case_id, sap_id, err


async def main(only: set[str] | None) -> int:
    if not sap_pr_configured():
        print("SAP not configured (PROCUREMENT_SAP_* in .env)", file=sys.stderr)
        return 2

    selected = CASES
    if only:
        selected = [c for c in CASES if any(f in c.case_id for f in only)]
        if not selected:
            print(f"No cases match --only {only!r}", file=sys.stderr)
            return 2

    print(f"SAP: {settings.procurement_sap_base_url}  client={settings.procurement_sap_client}")
    print(f"Running {len(selected)} YUNB multi-CC ablation cases\n")

    results: list[dict[str, Any]] = []
    for case in selected:
        case_id, sap_id, err = await run_case(case)
        status = "PASS" if sap_id else "FAIL"
        row = {
            "case_id": case_id,
            "status": status,
            "sap_id": sap_id,
            "error": (err or "")[:300] if err else None,
            "description": case.description,
        }
        results.append(row)
        mark = "✓" if sap_id else "✗"
        print(f"{mark} {status:4}  {case_id}")
        if sap_id:
            print(f"         PR {sap_id}")
        else:
            print(f"         {(err or '')[:200]}")
        await asyncio.sleep(0.3)

    passed = [r for r in results if r["status"] == "PASS"]
    failed = [r for r in results if r["status"] == "FAIL"]

    print("\n" + "=" * 72)
    print(f"SUMMARY: {len(passed)} pass / {len(failed)} fail")
    print("=" * 72)
    print("\n--- PASS (required fields NOT in this list are optional) ---")
    for r in passed:
        print(f"  {r['case_id']}: {r['description']}")

    print("\n--- FAIL (removed/changed field likely required) ---")
    for r in failed:
        print(f"  {r['case_id']}")
        print(f"    change: {r['description']}")
        print(f"    error:  {(r['error'] or '')[:180]}")

    out = Path("/tmp/yunb_multi_cc_ablation.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nFull log: {out}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--only",
        default="",
        help="Comma filter on case_id substrings (e.g. qty,percent,remove)",
    )
    args = p.parse_args()
    only_set = {x.strip() for x in args.only.split(",") if x.strip()} or None
    raise SystemExit(asyncio.run(main(only_set)))
