#!/usr/bin/env python3
"""Live QA: YSER PR→PO with two subline services on one PR item, different cost centres.

Mirrors PO-4030011266 / ops report: grouped services, distinct CC per service, same SAP PR item.

  python scripts/integration/run_yser_pr_to_po_multi_svc_cc_live.py
  python scripts/integration/run_yser_pr_to_po_multi_svc_cc_live.py --layer api
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.po_sap_enrichment import (
    build_po_prefill_form_from_pr,
    po_allocations_match_sap_pr,
)
from app.procurement.sap_po_client import create_po, get_po, sap_po_configured, sap_pr_configured
from app.procurement.sap_po_z_payload import form_from_z_po_read, verify_z_po_read_against_form
from app.procurement.sap_pr_client import create_pr
from app.procurement.sap_pr_items import fetch_pr_items

from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults
from scripts.integration.po_live_common import apply_po_live_fixtures, default_delivery_date
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    create_po_via_api_result,
    create_pr_via_api_result,
    get_prefill_po_via_api_result,
    load_env,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.run_yser_pr_e2e_signoff_live import yser_duplicate_service_form


def po4030011266_pr_form(*, header_note: str) -> dict[str, Any]:
    """Two different services, different CCs, same service group (one SAP PR item)."""
    sg = (os.environ.get("SAP_SERVICE_GROUP") or "S089-0001").strip()
    s1 = (os.environ.get("SAP_SERVICE") or "").strip()
    s2 = (os.environ.get("SAP_SERVICE_2") or "").strip()
    cc1 = (os.environ.get("SAP_COST_CENTER") or "").strip()
    cc2 = (os.environ.get("SAP_COST_CENTER_2") or "").strip()
    if not s1 or not s2 or not cc1 or not cc2:
        raise SystemExit("Need SAP_SERVICE, SAP_SERVICE_2, SAP_COST_CENTER, SAP_COST_CENTER_2")
    return {
        "header": {
            "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": os.environ.get("SAP_PUR_GROUP", "S0Z"),
            "plant": os.environ.get("SAP_PLANT", "H002"),
            "storage_location": os.environ.get("SAP_SLOC", "1001"),
            "service_group": sg,
            "header_note": header_note,
            "fixed_vendor": (os.environ.get("SAP_VENDOR") or "0000008005").strip(),
        },
        "lines": [
            {
                "service": s1,
                "service_group": sg,
                "short_text": "QA svc A diff CC",
                "delivery_date": default_delivery_date(),
                "unit_price": "100",
                "net_price": "100",
                "allocations": [{"cost_center": cc1, "qty": "1"}],
            },
            {
                "service": s2,
                "service_group": sg,
                "short_text": "QA svc B diff CC",
                "delivery_date": default_delivery_date(),
                "unit_price": "120",
                "net_price": "120",
                "allocations": [{"cost_center": cc2, "qty": "1"}],
            },
        ],
    }


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class RunOut:
    case_id: str
    layer: str
    pr_number: str = ""
    po_number: str = ""
    steps: list[Step] = field(default_factory=list)

    def green(self) -> bool:
        return all(s.ok for s in self.steps)


def _print_run(r: RunOut) -> None:
    flag = "PASS" if r.green() else "FAIL"
    print(f"\n{flag} [{r.layer}] {r.case_id} PR={r.pr_number} PO={r.po_number}")
    for s in r.steps:
        mark = "OK" if s.ok else "FAIL"
        print(f"  {mark:4} {s.name:<28} {s.detail[:120]}")


async def _verify_po_services(
    *,
    po_number: str,
    po_form: dict[str, Any],
    ticket_id: str,
    min_lines: int = 2,
    min_ccs: int = 2,
) -> tuple[bool, str]:
    body, err = await get_po(po_number=po_number, ticket_id=ticket_id, document_type="YSER")
    if err or not body:
        return False, err or "PO GET failed"
    ui = form_from_z_po_read(body, seed_form=po_form)
    mm = verify_z_po_read_against_form(body, form=po_form, ticket_id=ticket_id)
    n = len(ui.get("lines") or [])
    if n < min_lines:
        return False, f"expected >={min_lines} PO lines, got {n}"
    ccs: list[str] = []
    for row in ui.get("lines") or []:
        if isinstance(row, dict):
            for a in row.get("allocations") or []:
                if isinstance(a, dict) and a.get("cost_center"):
                    ccs.append(str(a["cost_center"]))
    if len(set(ccs)) < min_ccs:
        return False, f"expected >={min_ccs} distinct CCs, got {ccs}"
    if mm:
        return False, "; ".join(mm[:3])
    return True, f"lines={n} ccs={ccs}"


async def run_sap_case(case_id: str, pr_form: dict[str, Any]) -> RunOut:
    from app.procurement.field_schema import normalize_form
    from app.procurement.sap_defaults import apply_procurement_defaults

    out = RunOut(case_id=case_id, layer="sap")
    tid = str(uuid.uuid4())
    form = normalize_form("YSER", copy.deepcopy(pr_form))
    apply_procurement_defaults(form, document_type="YSER", kind="PR")

    pr, perr = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or perr:
        out.steps.append(Step("pr_create", False, perr or ""))
        return out
    out.pr_number = pr
    out.steps.append(Step("pr_create", True, pr))

    items, ierr = await fetch_pr_items(pr_number=pr, ticket_id=tid, document_type="YSER")
    if ierr or not items:
        out.steps.append(Step("fetch_pr_items", False, ierr or "no items"))
        return out
    n_cc = sum(len(i.acct_rows) for i in items)
    out.steps.append(
        Step(
            "fetch_pr_items",
            True,
            f"n={len(items)} sap_pr_items={sorted({i.sap_pr_item or i.item_no for i in items})} "
            f"ccs={[r.cost_center for i in items for r in i.acct_rows]}",
        )
    )
    if case_id == "P4030011266_two_svc_diff_cc" and len(items) < 2:
        out.steps.append(Step("fetch_shape", False, f"expected >=2 service rows, got {len(items)}"))
        return out

    po_seed, perr = await build_po_prefill_form_from_pr(
        form, document_type="YSER", pr_sap_id=pr, ticket_id=tid
    )
    if perr or not po_seed.get("lines"):
        out.steps.append(Step("po_prefill", False, perr or "empty lines"))
        return out
    out.steps.append(Step("po_prefill", True, f"lines={len(po_seed.get('lines') or [])}"))

    items2, _ = await fetch_pr_items(pr_number=pr, ticket_id=tid, document_type="YSER")
    alloc_err = po_allocations_match_sap_pr(po_seed, document_type="YSER", sap_items=items2)
    if alloc_err:
        out.steps.append(Step("alloc_validation", False, alloc_err))
        return out
    out.steps.append(Step("alloc_validation", True, "linked PO CC check passed"))

    apply_po_live_fixtures("YSER")
    po_form = copy.deepcopy(po_seed)
    from scripts.integration.run_po_from_pr_signoff_live import _finalize_po_form

    po_form = _finalize_po_form(po_form, dt="YSER", ticket_id=tid)

    po, cerr = await create_po(
        ticket_id=tid, form=po_form, document_type="YSER", parent_sap_id=pr
    )
    if not po or cerr:
        out.steps.append(Step("po_create", False, cerr or ""))
        return out
    out.po_number = po
    out.steps.append(Step("po_create", True, po))

    verify_kwargs = (
        {"min_lines": 2, "min_ccs": 2}
        if case_id == "P4030011266_two_svc_diff_cc"
        else {"min_lines": 1, "min_ccs": 2}
    )
    ok, det = await _verify_po_services(
        po_number=po, po_form=po_form, ticket_id=tid, **verify_kwargs
    )
    out.steps.append(Step("po_sap_verify", ok, det))
    return out


async def run_api_case(case_id: str, pr_form: dict[str, Any], *, token: str) -> RunOut:
    out = RunOut(case_id=case_id, layer="api")
    pr_ticket, pr_err = await asyncio.to_thread(
        create_pr_via_api_result,
        access_token=token,
        document_type="YSER",
        form=pr_form,
    )
    if pr_err or not pr_ticket:
        out.steps.append(Step("pr_create_api", False, pr_err or ""))
        return out
    pr_id = str(pr_ticket.get("id") or "")
    polled, poll_err = await asyncio.to_thread(poll_ticket_sap_sync, token, pr_id)
    if poll_err or not polled or not (polled.get("sap_id") or "").strip():
        out.steps.append(Step("pr_create_api", False, poll_err or "no sap_id"))
        return out
    out.pr_number = str(polled.get("sap_id") or "")
    out.steps.append(Step("pr_create_api", True, out.pr_number))

    prefill, pfill_err = await asyncio.to_thread(
        get_prefill_po_via_api_result,
        access_token=token,
        pr_ticket_id=pr_id,
    )
    form = (
        prefill.get("form")
        if isinstance(prefill, dict) and isinstance(prefill.get("form"), dict)
        else prefill
    )
    if pfill_err or not isinstance(form, dict) or not form.get("lines"):
        out.steps.append(Step("prefill_po_api", False, pfill_err or "empty"))
        return out
    out.steps.append(Step("prefill_po_api", True, f"lines={len(form.get('lines') or [])}"))

    items, ierr = await fetch_pr_items(
        pr_number=out.pr_number, ticket_id=pr_id, document_type="YSER"
    )
    alloc_err = po_allocations_match_sap_pr(form, document_type="YSER", sap_items=items)
    if alloc_err:
        out.steps.append(Step("alloc_validation_api", False, alloc_err))
        return out
    out.steps.append(Step("alloc_validation_api", True, "passed"))

    apply_po_live_fixtures("YSER")
    from scripts.integration.run_po_from_pr_signoff_live import _finalize_po_form

    po_form = _finalize_po_form(copy.deepcopy(form), dt="YSER", ticket_id=pr_id)

    po_ticket, po_err = await asyncio.to_thread(
        create_po_via_api_result,
        access_token=token,
        parent_pr_id=pr_id,
        document_type="YSER",
        form=po_form,
    )
    if po_err or not po_ticket:
        out.steps.append(Step("po_create_api", False, po_err or ""))
        return out
    po_id = str(po_ticket.get("id") or "")
    po_polled, po_poll_err = await asyncio.to_thread(poll_ticket_sap_sync, token, po_id)
    if po_poll_err or not po_polled:
        out.steps.append(Step("po_create_api", False, po_poll_err or "sync failed"))
        return out
    sync_err = str((po_polled.get("sap_sync") or {}).get("last_error") or "")
    out.po_number = str(po_polled.get("sap_id") or "")
    if not out.po_number:
        out.steps.append(Step("po_create_api", False, sync_err or "no po sap_id"))
        return out
    out.steps.append(Step("po_create_api", True, out.po_number))

    ok, det = await _verify_po_services(
        po_number=out.po_number,
        po_form=po_form,
        ticket_id=po_id,
        min_lines=2,
        min_ccs=2,
    )
    out.steps.append(Step("po_sap_verify_api", ok, det))
    return out


async def main_async(args: argparse.Namespace, *, token: str | None) -> int:
    load_env()
    apply_integration_reference_defaults()
    if not sap_pr_configured() or not sap_po_configured():
        print("SAP not configured")
        return 2

    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "P4030011266_two_svc_diff_cc",
            po4030011266_pr_form(header_note=f"qa-p403-{uuid.uuid4().hex[:8]}"),
        ),
        (
            "duplicate_svc_diff_cc",
            yser_duplicate_service_form(header_note=f"qa-dup-{uuid.uuid4().hex[:8]}"),
        ),
    ]
    if args.case:
        cases = [(c, f) for c, f in cases if c == args.case]
        if not cases:
            print(f"Unknown case: {args.case}")
            return 2

    results: list[RunOut] = []
    if args.layer in ("sap", "all"):
        for cid, form in cases:
            print(f"\n>>> SAP {cid}")
            results.append(await run_sap_case(cid, form))

    if args.layer in ("api", "all") and token:
        print(f"\nAPI base: {api_base()}")
        for cid, form in cases:
            print(f"\n>>> API {cid}")
            results.append(await run_api_case(cid, form, token=token))

    print("\n" + "=" * 72)
    print("YSER PR→PO MULTI-SERVICE / MULTI-CC LIVE QA")
    print("=" * 72)
    fails = 0
    for r in results:
        _print_run(r)
        if not r.green():
            fails += 1
    print("=" * 72)
    print(f"PASS={len(results) - fails} FAIL={fails} TOTAL={len(results)}")
    return 1 if fails else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Live QA: YSER PR→PO multi-service multi-CC")
    add_common_args(p)
    p.add_argument("--layer", choices=["sap", "api", "all"], default="all")
    p.add_argument(
        "--case",
        default="",
        help="Run one case id (P4030011266_two_svc_diff_cc | duplicate_svc_diff_cc)",
    )
    args = p.parse_args()
    token = None
    if args.layer in ("api", "all"):
        try:
            token = resolve_token_from_args(args)
        except SystemExit as e:
            print(f"API auth skipped: {e}")
            if args.layer == "api":
                return 2
    return asyncio.run(main_async(args, token=token))


if __name__ == "__main__":
    raise SystemExit(main())
