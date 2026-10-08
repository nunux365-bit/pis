#!/usr/bin/env python3
"""Probe SAP QAS for YSER item cat 9 — which ServicePerformer codes create a PR.

Uses the cat-9 / no-material / acct-K shape that passes structural checks but fails on
invalid performers (e.g. DB ``1000000000`` or material ``4200000027``).

Usage (from agentos-backend/, SAP in .env):

  python scripts/integration/discover_sap_yser_service_performers.py
  python scripts/integration/discover_sap_yser_service_performers.py --limit 40
  python scripts/integration/discover_sap_yser_service_performers.py --codes 1000000000,4200000027
  python scripts/integration/discover_sap_yser_service_performers.py --scan-prefix 10000000 --start 0 --end 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_pr_client import _sap_create_pr, sap_pr_configured

# SAP QAS: next workday for cat-9 probes (from live error messages).
WORKDAY_DEL = "/Date(1782748800000)/"
PERIOD_END = "/Date(1785100800000)/"


def _probe_payload(*, performer: str, plant: str, sloc: str, pur_group: str, cost_center: str) -> dict:
    return {
        "PurchaseRequisition": "",
        "PurchaseRequisitionType": "YSER",
        "SourceDetermination": True,
        "PurReqnDescription": "YSER perf probe",
        "to_PurchaseReqnItem": [
            {
                "PurchaseRequisitionItem": "10",
                "PurchasingOrganization": "1MGH",
                "PurchasingGroup": pur_group,
                "Plant": plant,
                "CompanyCode": "1MGH",
                "AccountAssignmentCategory": "K",
                "Material": "",
                "MaterialGroup": "SD05-0001",
                "StorageLocation": sloc,
                "PurchasingDocumentItemCategory": "9",
                "ProductType": "2",
                "ServicePerformer": performer,
                "RequestedQuantity": "1.000",
                "PurchaseRequisitionPrice": "100.00",
                "PurchaseRequisitionItemText": "Performer probe",
                "DeliveryDate": WORKDAY_DEL,
                "PerformancePeriodStartDate": WORKDAY_DEL,
                "PerformancePeriodEndDate": PERIOD_END,
                "to_PurchaseReqnAcctAssgmt": [
                    {
                        "PurchaseReqnAcctAssgmtNumber": "1",
                        "CostCenter": cost_center,
                        "Quantity": "1.000",
                    }
                ],
            }
        ],
    }


def _classify(err: str | None) -> str:
    e = (err or "").lower()
    if not e:
        return "unknown"
    if "does not exist" in e:
        return "performer_missing"
    if "maintain services or limits" in e:
        return "services_limits"
    if "delivery date" in e or "workday" in e:
        return "delivery"
    if "item category" in e:
        return "item_category"
    return "other"


async def _probe_one(
    *,
    performer: str,
    plant: str,
    sloc: str,
    pur_group: str,
    cost_center: str,
) -> tuple[str, str | None, str | None]:
    payload = _probe_payload(
        performer=performer,
        plant=plant,
        sloc=sloc,
        pur_group=pur_group,
        cost_center=cost_center,
    )
    sap_id, err = await _sap_create_pr(payload=payload, ticket_id=f"yperf-{performer[-8:]}")
    if sap_id:
        return "pass", sap_id, None
    return _classify(err), None, (err or "")[:200]


def _load_db_service_codes(*, limit: int) -> list[str]:
    from scripts.integration.integration_reference_defaults import _connect

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT code FROM pr_po_reference_values
            WHERE domain = 'service' AND code <> ''
            ORDER BY sort_order, code
            LIMIT %s
            """,
            (limit,),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if str(r[0]).strip()]
    finally:
        conn.close()


async def main() -> int:
    p = argparse.ArgumentParser(description="Discover SAP-valid YSER ServicePerformer codes (cat 9)")
    p.add_argument("--plant", default="H002")
    p.add_argument("--sloc", default="1001")
    p.add_argument("--pur-group", default="M0B")
    p.add_argument("--cost-center", default="HCO91001H0")
    p.add_argument("--limit", type=int, default=80, help="Max service codes from DB")
    p.add_argument("--codes", default="", help="Comma-separated performers to probe")
    p.add_argument("--scan-prefix", default="", help="Numeric scan e.g. 10000000")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=0, help="Exclusive end for scan (suffix width from prefix)")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--json-out", default="/tmp/yser_service_performer_probe.json")
    args = p.parse_args()

    if not sap_pr_configured():
        print("SAP not configured", file=sys.stderr)
        return 1

    candidates: list[str] = []
    if args.codes.strip():
        candidates.extend(c.strip() for c in args.codes.split(",") if c.strip())
    else:
        try:
            candidates.extend(_load_db_service_codes(limit=args.limit))
        except Exception as e:
            print(f"DB service list skipped: {e}", file=sys.stderr)

    if args.scan_prefix:
        width = max(0, 10 - len(args.scan_prefix))
        end = args.end if args.end > args.start else args.start + 30
        for i in range(args.start, end):
            candidates.append(f"{args.scan_prefix}{i:0{width}d}")

    # De-dupe preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict] = []

    async def run_one(code: str) -> None:
        async with sem:
            status, sap_id, err = await _probe_one(
                performer=code,
                plant=args.plant,
                sloc=args.sloc,
                pur_group=args.pur_group,
                cost_center=args.cost_center,
            )
            row = {"performer": code, "status": status, "sap_id": sap_id, "error": err}
            results.append(row)
            mark = "PASS" if status == "pass" else status.upper()
            print(f"{code}: {mark}" + (f" -> {sap_id}" if sap_id else (f" — {err}" if err else "")))

    await asyncio.gather(*[run_one(c) for c in unique])

    passed = [r for r in results if r["status"] == "pass"]
    limits = [r for r in results if r["status"] == "services_limits"]
    missing = [r for r in results if r["status"] == "performer_missing"]

    out = {
        "plant": args.plant,
        "sloc": args.sloc,
        "passed": passed,
        "services_limits": limits,
        "performer_missing_count": len(missing),
        "all": sorted(results, key=lambda x: x["performer"]),
    }
    Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"\nProbed {len(unique)} code(s). PASS={len(passed)} services_limits={len(limits)} missing={len(missing)}")
    if passed:
        print("Valid performers (created PR):")
        for r in passed:
            print(f"  {r['performer']} -> {r['sap_id']}")
    if limits:
        print("Performer accepted structurally but services/limits missing:")
        for r in limits[:15]:
            print(f"  {r['performer']}")
        if len(limits) > 15:
            print(f"  ... +{len(limits) - 15} more")
    print(f"Full results: {args.json_out}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
