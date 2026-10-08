#!/usr/bin/env python3
"""Probe YSER Z API: delete item (IsDeleted=X) then recreate with reduced services/CCs.

Tests workarounds for grouped partial-service delete and per-CC delete.

Usage (from agentos-backend):
  python scripts/integration/probe_yser_delete_recreate_live.py
  python scripts/integration/probe_yser_delete_recreate_live.py --case grouped_service
  python scripts/integration/probe_yser_delete_recreate_live.py --case multi_cc
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_pr_client import create_pr, get_pr, sap_pr_configured, update_pr
from app.procurement.sap_pr_z_client import delete_yser_pr, get_yser_pr
from app.procurement.sap_pr_z_payload import (
    build_z_yser_item_delete_post_body,
    build_z_yser_pr_payload,
    finalize_z_yser_update_post_body,
    form_from_z_pr_read,
    verify_yser_read_matches_form,
    z_pr_collection_url,
)
from app.procurement.sap_pr_client import (
    _credentials_or_error,
    _httpx_timeout,
    _request_with_csrf_retry,
    sap_post_create_headers,
    sap_service_root_url,
)
from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import load_env
from scripts.integration.run_pr_create_yser import complex_yser_form

import httpx

from app.config.settings import settings


def _env(k: str, default: str = "") -> str:
    return (os.environ.get(k) or default).strip()


def multi_cc_form(*, header_note: str) -> dict:
    sg = _env("SAP_SERVICE_GROUP", "S089-0001")
    svc = _env("SAP_SERVICE")
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    return {
        "header": {
            "purchasing_org": _env("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": _env("SAP_PUR_GROUP", "A0B"),
            "plant": _env("SAP_PLANT", "H001"),
            "storage_location": _env("SAP_SLOC", "1001"),
            "service_group": sg,
            "header_note": header_note,
            "fixed_vendor": _env("SAP_VENDOR", "0000008005"),
        },
        "lines": [
            {
                "service": svc,
                "service_group": sg,
                "short_text": "Multi CC probe",
                "unit_price": "100",
                "valuation_price": "300",
                "delivery_date": "2026-08-15",
                "allocations": [
                    {"cost_center": cc1, "qty": "1"},
                    {"cost_center": cc2, "qty": "2"},
                ],
            }
        ],
    }


@dataclass
class ProbeStep:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ProbeResult:
    case_id: str
    pr_number: str = ""
    steps: list[ProbeStep] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(ProbeStep(name, ok, detail))


async def _z_post_update(
    *,
    ticket_id: str,
    inner: dict[str, Any],
) -> tuple[bool, str]:
    creds, err = _credentials_or_error()
    if err:
        return False, err
    base, user, password = creds
    post_body = finalize_z_yser_update_post_body(inner)
    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            resp = await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=ticket_id,
                method="POST",
                url=z_pr_collection_url(base),
                json_body=post_body,
                headers_builder=sap_post_create_headers,
                csrf_service_root=sap_service_root_url(base),
            )
            if resp.status_code >= 400:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text[:500]
                return False, f"HTTP {resp.status_code}: {str(err_body)[:400]}"
    except Exception as e:
        return False, str(e)
    return True, ""


async def _verify_form(*, pr: str, form: dict, tid: str) -> tuple[bool, str]:
    body, err = await get_pr(pr_number=pr, ticket_id=f"{tid}-v", document_type="YSER")
    if err or not body:
        return False, err or "GET failed"
    mm = verify_yser_read_matches_form(body, form=form)
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
    n = len(ui.get("lines") or [])
    if mm:
        return False, "; ".join(mm[:3]) + f" | lines={n}"
    return True, f"lines={n} verify ok"


async def _mark_item_deleted(*, pr: str, item_no: str, tid: str) -> tuple[bool, str]:
    creds, err = _credentials_or_error()
    if err:
        return False, err
    base, user, password = creds
    post_body = build_z_yser_item_delete_post_body(pr_number=pr, item_numbers=[item_no])
    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            resp = await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=tid,
                method="POST",
                url=z_pr_collection_url(base),
                json_body=post_body,
                headers_builder=sap_post_create_headers,
                csrf_service_root=sap_service_root_url(base),
            )
            if resp.status_code >= 400:
                return False, resp.text[:400]
    except Exception as e:
        return False, str(e)
    return True, ""


async def _hydrate(pr: str, seed: dict, tid: str) -> dict | None:
    from app.procurement.sap_ticket_form_read import load_form_from_sap

    form, src, err = await load_form_from_sap(
        kind="PR", document_type="YSER", sap_id=pr, ticket_id=tid, seed_form=seed
    )
    if err or not form or src != "sap":
        return None
    return form


async def probe_grouped_service_delete_recreate() -> ProbeResult:
    """2 services same group → delete item → recreate with 1 service."""
    out = ProbeResult(case_id="grouped_service_delete_recreate")
    tid = str(uuid.uuid4())
    tag = f"probe-grp-{uuid.uuid4().hex[:6]}"
    seed = complex_yser_form(header_note=tag)
    form = normalize_form("YSER", seed)
    apply_procurement_defaults(form, document_type="YSER", kind="PR")

    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.add("create", False, err or "")
        return out
    out.pr_number = pr
    out.add("create", True, pr)

    ok, det = await _verify_form(pr=pr, form=form, tid=tid)
    out.add("verify_after_create", ok, det)
    if not ok:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out

    # Target: keep line A only (first service)
    target = copy.deepcopy(form)
    target["lines"] = [copy.deepcopy(form["lines"][0])]

    hydrated = await _hydrate(pr, form, tid)
    if not hydrated:
        out.add("hydrate", False, "failed")
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out
    out.add("hydrate", True, f"lines={len(hydrated.get('lines') or [])}")

    item_no = str((hydrated.get("lines") or [{}])[0].get("sap_pr_item") or "10")
    out.add("sap_item_no", True, item_no)

    # Strategy A: two-step — IsDeleted then recreate via update_pr
    ok_del, del_err = await _mark_item_deleted(pr=pr, item_no=item_no, tid=f"{tid}-del")
    out.add("step1_item_isdeleted", ok_del, del_err or item_no)
    if not ok_del:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out

    upd_form = copy.deepcopy(target)
    for line in upd_form.get("lines") or []:
        if isinstance(line, dict):
            line["sap_pr_item"] = item_no
            line["purchase_requisition_item"] = item_no
    _, uerr = await update_pr(sap_id=pr, ticket_id=f"{tid}-rec", form=upd_form, document_type="YSER")
    if uerr:
        out.add("step2_recreate_update_pr", False, uerr[:300])
    else:
        ok2, det2 = await _verify_form(pr=pr, form=target, tid=f"{tid}-rec")
        out.add("step2_recreate_update_pr", ok2, det2)

    # Strategy B: single POST — IsDeleted stub + recreate item in same body
    pr2, err2 = await create_pr(
        ticket_id=f"{tid}-b", form=form, document_type="YSER"
    )
    if not pr2 or err2:
        out.add("create_for_atomic", False, err2 or "")
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out
    out.add("create_for_atomic", True, pr2)

    hydrated2 = await _hydrate(pr2, form, f"{tid}-b")
    item2 = str((hydrated2 or {}).get("lines", [{}])[0].get("sap_pr_item") or "10")
    target_b = copy.deepcopy(target)
    for line in target_b.get("lines") or []:
        if isinstance(line, dict):
            line["sap_pr_item"] = item2
            line["purchase_requisition_item"] = item2

    inner = build_z_yser_pr_payload(
        form=target_b,
        document_type="YSER",
        pr_number=pr2,
        ticket_id=f"{tid}-atomic",
        for_update=True,
        sap_item_count=1,
    )
    # Prepend IsDeleted stub for same item
    delete_stub = build_z_yser_item_delete_post_body(pr_number=pr2, item_numbers=[item2])
    del_items = delete_stub.get("to_Items") or []
    recreate_items = inner.get("to_Items") or []
    combined = {
        "PRNumber": pr2,
        "DocumentType": "YSER",
        "to_Items": list(del_items) + list(recreate_items),
    }
    ok_atom, atom_err = await _z_post_update(ticket_id=f"{tid}-atom", inner=combined)
    if ok_atom:
        ok3, det3 = await _verify_form(pr=pr2, form=target, tid=f"{tid}-atom")
        out.add("atomic_delete_plus_recreate", ok3, det3)
    else:
        out.add("atomic_delete_plus_recreate", False, atom_err[:300])

    await delete_yser_pr(sap_id=pr, ticket_id=tid)
    await delete_yser_pr(sap_id=pr2, ticket_id=f"{tid}-b")
    return out


async def probe_multi_cc_delete_recreate() -> ProbeResult:
    """1 service 2 CCs → delete item → recreate with 1 CC."""
    out = ProbeResult(case_id="multi_cc_delete_recreate")
    tid = str(uuid.uuid4())
    tag = f"probe-cc-{uuid.uuid4().hex[:6]}"
    seed = multi_cc_form(header_note=tag)
    form = normalize_form("YSER", seed)
    apply_procurement_defaults(form, document_type="YSER", kind="PR")

    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.add("create", False, err or "")
        return out
    out.pr_number = pr
    out.add("create", True, pr)

    ok, det = await _verify_form(pr=pr, form=form, tid=tid)
    out.add("verify_after_create", ok, det)
    if not ok:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out

    target = copy.deepcopy(form)
    target["lines"][0]["allocations"] = [copy.deepcopy(form["lines"][0]["allocations"][0])]
    target["lines"][0]["valuation_price"] = "100"
    target["lines"][0]["unit_price"] = "100"

    hydrated = await _hydrate(pr, form, tid)
    if not hydrated:
        out.add("hydrate", False, "failed")
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out
    item_no = str((hydrated.get("lines") or [{}])[0].get("sap_pr_item") or "10")

    ok_del, del_err = await _mark_item_deleted(pr=pr, item_no=item_no, tid=f"{tid}-del")
    out.add("step1_item_isdeleted", ok_del, del_err or item_no)
    if not ok_del:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out

    upd_form = copy.deepcopy(target)
    line = upd_form["lines"][0]
    line["sap_pr_item"] = item_no
    line["purchase_requisition_item"] = item_no
    # Preserve acct serial from hydrate if present
    hline = (hydrated.get("lines") or [{}])[0]
    if isinstance(hline, dict):
        allocs_h = hline.get("allocations") or []
        if allocs_h and isinstance(allocs_h[0], dict):
            line["allocations"][0]["pr_acct_assgmt_number"] = allocs_h[0].get(
                "pr_acct_assgmt_number", "01"
            )

    _, uerr = await update_pr(sap_id=pr, ticket_id=f"{tid}-rec", form=upd_form, document_type="YSER")
    if uerr:
        out.add("step2_recreate_update_pr", False, uerr[:300])
    else:
        ok2, det2 = await _verify_form(pr=pr, form=target, tid=f"{tid}-rec")
        out.add("step2_recreate_update_pr", ok2, det2)

    await delete_yser_pr(sap_id=pr, ticket_id=tid)
    return out


async def probe_omit_only_baseline() -> ProbeResult:
    """Baseline: omit service/CC without delete — expect fail for grouped."""
    out = ProbeResult(case_id="omit_only_baseline")
    tid = str(uuid.uuid4())
    form = normalize_form("YSER", complex_yser_form(header_note=f"probe-omit-{uuid.uuid4().hex[:6]}"))
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.add("create", False, err or "")
        return out
    out.pr_number = pr
    one_line = copy.deepcopy(form)
    one_line["lines"] = [one_line["lines"][0]]
    hydrated = await _hydrate(pr, form, tid)
    upd = copy.deepcopy(hydrated or one_line)
    upd["lines"] = [upd["lines"][0]]
    _, uerr = await update_pr(sap_id=pr, ticket_id=tid, form=upd, document_type="YSER")
  # omit only — update_pr may succeed verify fail
    if uerr:
        out.add("omit_only_update", False, uerr[:200])
    else:
        ok, det = await _verify_form(pr=pr, form=one_line, tid=tid)
        out.add("omit_only_update", ok, "expected_fail" if not ok else f"unexpected_pass: {det}")
    await delete_yser_pr(sap_id=pr, ticket_id=tid)
    return out


def _print_results(results: list[ProbeResult]) -> int:
    fails = 0
    print("\n" + "=" * 72)
    print("YSER DELETE+RECREATE PROBE")
    print("=" * 72)
    for r in results:
        case_fails = [s for s in r.steps if not s.ok]
        if case_fails:
            fails += 1
        mark = "PASS" if not case_fails else "MIXED/FAIL"
        print(f"\n[{mark}] {r.case_id} PR={r.pr_number or '-'}")
        for s in r.steps:
            print(f"  {'OK' if s.ok else 'FAIL':4} {s.name:32} {s.detail[:100]}")
    print("\n" + "=" * 72)
    return 1 if fails else 0


async def main_async(args: argparse.Namespace) -> int:
    load_env()
    print(integration_defaults_summary(apply_integration_reference_defaults()))
    if not sap_pr_configured():
        print("SAP not configured", file=sys.stderr)
        return 2

    cases = {
        "grouped_service": probe_grouped_service_delete_recreate,
        "multi_cc": probe_multi_cc_delete_recreate,
        "omit_baseline": probe_omit_only_baseline,
    }
    ids = [args.case] if args.case else list(cases)
    results: list[ProbeResult] = []
    for cid in ids:
        if cid not in cases:
            print(f"Unknown case {cid}", file=sys.stderr)
            return 2
        results.append(await cases[cid]())
    return _print_results(results)


def main() -> int:
    p = argparse.ArgumentParser(description="Probe YSER delete+recreate workarounds")
    p.add_argument("--case", choices=["grouped_service", "multi_cc", "omit_baseline"])
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
