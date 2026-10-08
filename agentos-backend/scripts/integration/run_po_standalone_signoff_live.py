#!/usr/bin/env python3
"""Standalone PO sign-off: YUNB / YAST / YSER — create → GET → update → delete.

Master data from ``pr_po_reference_values`` (same org/plant/sloc/CC/vendor derivation as UI).
No parent PR.

Layers:
  --layer sap   Production SAP client only (default)
  --layer api   AgentOS API → background SAP sync (requires API on :8000)
  --layer all   Both

Usage (agentos-backend/):

  python scripts/integration/run_po_standalone_signoff_live.py
  python scripts/integration/run_po_standalone_signoff_live.py --layer all
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_standalone_signoff_live.py --layer api
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import create_po, get_po, sap_po_configured, update_po
from app.procurement.sap_po_payload import verify_po_read_against_form
from app.procurement.sap_po_z_payload import form_from_z_po_read, verify_z_po_read_against_form
from app.procurement.sap_ticket_form_read import form_from_po_sap_read

from scripts.integration.po_live_common import (
    build_po_matrix_cases,
    distinct_integration_material,
    distinct_integration_service,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    classify_sap_ticket,
    create_po_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.integration_reference_defaults import load_po_db_context

TAX_KW = ("tax", "vertex", "external tax")


def _is_tax_err(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in TAX_KW)


@dataclass
class StepResult:
    step: str
    status: str  # OK | TAX-KNOWN | FAIL | SKIP
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    document_type: str
    layer: str
    po_number: str = ""
    steps: list[StepResult] = field(default_factory=list)

    def green(self) -> bool:
        allowed = frozenset({"OK", "SKIP", "TAX-KNOWN"})
        return all(s.status in allowed for s in self.steps)


def _prep_form(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    dt = spec["document_type"]
    form = normalize_form(dt, copy.deepcopy(spec["form"]))
    apply_procurement_defaults(form, document_type=dt, kind="PO")
    return dt, form


def _inject_second_line_for_delete(form: dict[str, Any], *, dt: str) -> None:
    lines = form.get("lines")
    if not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], dict):
        return
    line1 = lines[0]
    line2 = copy.deepcopy(line1)
    line2["short_text"] = (str(line2.get("short_text") or "Line") + " B")[:40]
    if dt == "YSER":
        svc1 = str(line1.get("service") or "").strip()
        svc2 = distinct_integration_service(exclude=svc1)
        if not svc2:
            return
        line2["service"] = svc2
        cc1 = ""
        a1 = line1.get("allocations")
        if isinstance(a1, list) and a1 and isinstance(a1[0], dict):
            cc1 = str(a1[0].get("cost_center") or "").strip()
        line2["allocations"] = [{"cost_center": cc1, "qty": "1"}]
    else:
        mat1 = str(line1.get("material") or "").strip()
        mat2 = distinct_integration_material(exclude=mat1)
        if not mat2:
            return
        line2["material"] = mat2
        if dt == "YAST":
            a2 = (os.environ.get("SAP_ASSET_2") or os.environ.get("SAP_ASSET") or "").strip()
            line2["asset"] = a2
            line2["allocations"] = [{"asset": a2, "qty": "3"}]
    lines.append(line2)


async def _hydrate_form_from_po(
    *,
    po_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """GET + map to UI form (production resubmit must start from SAP state)."""
    body, err = await get_po(
        po_number=po_number, ticket_id=f"{ticket_id}-hydrate", document_type=document_type
    )
    if err or not body:
        return None, err or "GET failed"
    dt = document_type.upper()
    if dt == "YSER":
        return form_from_z_po_read(body, seed_form=form), None
    return form_from_po_sap_read(body, document_type=dt, seed_form=form), None


async def _verify_sap_get(
    *,
    po_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
    expect_lines: int,
) -> tuple[bool, str]:
    body, err = await get_po(
        po_number=po_number, ticket_id=f"{ticket_id}-get", document_type=document_type
    )
    if err or not body:
        return False, err or "GET failed"
    dt = document_type.upper()
    if dt == "YSER":
        ui = form_from_z_po_read(body, seed_form=form)
        mm = verify_z_po_read_against_form(body, form=form, ticket_id=ticket_id)
    else:
        ui = form_from_po_sap_read(body, document_type=dt, seed_form=form)
        mm = verify_po_read_against_form(
            body, form=form, document_type=dt, ticket_id=ticket_id
        )
    lines = ui.get("lines") or []
    if len(lines) != expect_lines:
        return False, f"UI lines {len(lines)} != expected {expect_lines}"
    if mm:
        return False, "verify: " + "; ".join(mm[:3])
    return True, f"GET OK {len(lines)} line(s)"


async def run_sap_case(case_id: str, spec: dict[str, Any]) -> CaseResult:
    dt, form = _prep_form(spec)
    tid = str(uuid.uuid4())
    out = CaseResult(case_id=case_id, document_type=dt, layer="sap")
    delete_form = copy.deepcopy(form)
    if (
        len(delete_form.get("lines") or []) < 2
        and case_id.endswith("simple")
        and not case_id.startswith("yunb_")
    ):
        _inject_second_line_for_delete(delete_form, dt=dt)

    po, err = await create_po(ticket_id=tid, form=delete_form, document_type=dt)
    if not po or err:
        out.steps.append(StepResult("create", "FAIL", (err or "no PO")[:200]))
        return out
    out.po_number = po
    out.steps.append(StepResult("create", "OK", po))

    expect_lines = len(delete_form.get("lines") or [])
    verify_form = delete_form
    if dt == "YSER":
        hydrated, herr = await _hydrate_form_from_po(
            po_number=po,
            form=delete_form,
            document_type=dt,
            ticket_id=tid,
        )
        if not hydrated:
            out.steps.append(StepResult("get_after_create", "FAIL", (herr or "hydrate failed")[:200]))
            return out
        verify_form = hydrated
        expect_lines = len(hydrated.get("lines") or [])
    ok_get, det_get = await _verify_sap_get(
        po_number=po,
        form=verify_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det_get))

    if dt == "YSER":
        working, werr = await _hydrate_form_from_po(
            po_number=po,
            form=delete_form,
            document_type=dt,
            ticket_id=tid,
        )
        if not working:
            out.steps.append(StepResult("update", "FAIL", (werr or "hydrate failed")[:200]))
            return out
        upd_form = copy.deepcopy(working)
    else:
        upd_form = copy.deepcopy(delete_form)
    lines = upd_form.get("lines") or []
    if lines and isinstance(lines[0], dict):
        lines[0]["unit_price"] = "15"
        lines[0]["net_price"] = "15"
    _, uerr = await update_po(sap_id=po, ticket_id=f"{tid}-u", form=upd_form, document_type=dt)
    if not uerr:
        out.steps.append(StepResult("update", "OK", ""))
    elif _is_tax_err(uerr):
        out.steps.append(StepResult("update", "TAX-KNOWN", uerr[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX blocked PATCH"))
        if expect_lines < 2:
            out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        else:
            out.steps.append(StepResult("delete_line", "TAX-KNOWN", "VERTEX blocked"))
            out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX blocked"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", uerr[:200]))
        return out

    ok_get2, det_get2 = await _verify_sap_get(
        po_number=po,
        form=upd_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det_get2))

    if expect_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        return out

    if dt == "YSER":
        hydrated2, herr2 = await _hydrate_form_from_po(
            po_number=po,
            form=upd_form,
            document_type=dt,
            ticket_id=f"{tid}-ud",
        )
        if not hydrated2:
            out.steps.append(StepResult("delete_line", "FAIL", (herr2 or "hydrate failed")[:200]))
            return out
        del_form = copy.deepcopy(hydrated2)
    else:
        del_form = copy.deepcopy(upd_form)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    _, derr = await update_po(sap_id=po, ticket_id=f"{tid}-d", form=del_form, document_type=dt)
    if not derr:
        out.steps.append(StepResult("delete_line", "OK", ""))
    elif _is_tax_err(derr):
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", derr[:160]))
    else:
        out.steps.append(StepResult("delete_line", "FAIL", derr[:200]))
        return out

    ok_get3, det_get3 = await _verify_sap_get(
        po_number=po,
        form=del_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det_get3))
    return out


def _sap_ok(ticket: dict[str, Any]) -> tuple[bool, str]:
    status, detail, _ = classify_sap_ticket(ticket)
    return status == "pass", detail


async def run_api_case(
    case_id: str,
    spec: dict[str, Any],
    *,
    token: str,
    form: dict[str, Any] | None = None,
) -> CaseResult:
    if form is None:
        dt, form = _prep_form(spec)
    else:
        dt = str(spec["document_type"])
    out = CaseResult(case_id=case_id, document_type=dt, layer="api")
    delete_form = copy.deepcopy(form)
    if (
        len(delete_form.get("lines") or []) < 2
        and case_id.endswith("simple")
        and not case_id.startswith("yunb_")
    ):
        _inject_second_line_for_delete(delete_form, dt=dt)

    ticket, err = await asyncio.to_thread(
        create_po_via_api_result,
        access_token=token,
        document_type=dt,
        form=delete_form,
    )
    if err or not ticket:
        out.steps.append(StepResult("create", "FAIL", (err or "no ticket")[:200]))
        return out
    tid = str(ticket.get("id") or "")
    polled, perr = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    if not polled:
        out.steps.append(StepResult("create", "FAIL", perr or "sync timeout"))
        return out
    ok, det = _sap_ok(polled)
    po = str(polled.get("sap_id") or "")
    out.po_number = po
    out.steps.append(StepResult("create", "OK" if ok else "FAIL", f"PO {po} {det}"[:120]))
    if not ok:
        return out

    verify_form = delete_form
    if dt == "YSER":
        hydrated, herr = await _hydrate_form_from_po(
            po_number=po,
            form=delete_form,
            document_type=dt,
            ticket_id=tid,
        )
        if not hydrated:
            out.steps.append(StepResult("get_after_create", "FAIL", (herr or "hydrate failed")[:200]))
            return out
        verify_form = hydrated
    ok_get, det_get = await _verify_sap_get(
        po_number=po,
        form=verify_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=len(verify_form.get("lines") or []),
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det_get))

    fresh = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    version = int(fresh.get("version") or 0)
    if dt == "YSER":
        working, werr = await _hydrate_form_from_po(
            po_number=po,
            form=delete_form,
            document_type=dt,
            ticket_id=tid,
        )
        if not working:
            out.steps.append(StepResult("update", "FAIL", (werr or "hydrate failed")[:200]))
            return out
        upd_form = copy.deepcopy(working)
    else:
        upd_form = copy.deepcopy(delete_form)
    lines = upd_form.get("lines") or []
    if lines and isinstance(lines[0], dict):
        lines[0]["unit_price"] = "15"
        lines[0]["net_price"] = "15"
    updated, uerr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=tid,
        form=upd_form,
        version=version,
    )
    if uerr or not updated:
        out.steps.append(StepResult("update", "FAIL", (uerr or "no ticket")[:200]))
        return out
    polled2, _ = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    ok2, det2 = _sap_ok(polled2 or {})
    expect_lines = len(verify_form.get("lines") or [])
    if ok2:
        out.steps.append(StepResult("update", "OK", det2[:80]))
    elif _is_tax_err(det2):
        out.steps.append(StepResult("update", "TAX-KNOWN", det2[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX blocked PATCH"))
        if expect_lines < 2:
            out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        else:
            out.steps.append(StepResult("delete_line", "TAX-KNOWN", "VERTEX blocked"))
            out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX blocked"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", det2[:200]))
        return out

    ok_get2, det_get2 = await _verify_sap_get(
        po_number=po,
        form=upd_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det_get2))

    if expect_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        return out

    fresh2 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    version2 = int(fresh2.get("version") or 0)
    if dt == "YSER":
        hydrated2, herr2 = await _hydrate_form_from_po(
            po_number=po,
            form=upd_form,
            document_type=dt,
            ticket_id=f"{tid}-ud",
        )
        if not hydrated2:
            out.steps.append(StepResult("delete_line", "FAIL", (herr2 or "hydrate failed")[:200]))
            return out
        del_form = copy.deepcopy(hydrated2)
    else:
        del_form = copy.deepcopy(upd_form)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    deleted, derr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=tid,
        form=del_form,
        version=version2,
    )
    if derr or not deleted:
        out.steps.append(StepResult("delete_line", "FAIL", (derr or "no ticket")[:200]))
        return out
    polled3, _ = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    ok3, det3 = _sap_ok(polled3 or {})
    if ok3:
        out.steps.append(StepResult("delete_line", "OK", det3[:80]))
    elif _is_tax_err(det3):
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", det3[:160]))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX blocked"))
        return out
    else:
        out.steps.append(StepResult("delete_line", "FAIL", det3[:200]))
        return out

    ok_get3, det_get3 = await _verify_sap_get(
        po_number=po,
        form=del_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det_get3))
    return out


def _print_results(results: list[CaseResult]) -> int:
    print("\n" + "=" * 88)
    print("STANDALONE PO SIGN-OFF (no PR)")
    print("=" * 88)
    fails = 0
    for r in results:
        flag = "GREEN" if r.green() else "RED"
        if not r.green():
            fails += 1
        steps = " | ".join(f"{s.step}={s.status}" for s in r.steps)
        print(f"{flag:5} [{r.layer:3}] {r.document_type} {r.case_id:<22} PO={r.po_number:<12} {steps}")
        for s in r.steps:
            if s.status == "FAIL" and s.detail:
                print(f"       ↳ {s.step}: {s.detail[:140]}")
    print("=" * 88)
    n_green = sum(1 for r in results if r.green())
    print(f"GREEN={n_green} RED={fails} TOTAL={len(results)}")
    print("=" * 88)
    return 1 if fails else 0


async def main_async(args: argparse.Namespace, *, api_token: str | None) -> int:
    load_env()
    ctx = load_po_db_context(apply_env=True)
    all_cases = build_po_matrix_cases(ctx)
    print(
        "PO DB context: "
        f"org={ctx.purchasing_org!r} plant={ctx.plant!r} vendor={ctx.vendor!r} "
        f"pay={ctx.payment_terms!r} mat={ctx.material!r} cc={ctx.cost_center!r}"
    )
    if not sap_po_configured():
        print("SAP not configured")
        return 2

    case_ids = [x.strip() for x in args.cases.split(",") if x.strip()] if args.cases else list(all_cases)
    layers = args.layer.lower()
    run_sap = layers in ("sap", "all")
    run_api = layers in ("api", "all") and bool(api_token)

    results: list[CaseResult] = []

    if run_sap:
        for cid in case_ids:
            spec = all_cases.get(cid)
            if not spec:
                print(f"Unknown case: {cid}")
                return 2
            results.append(await run_sap_case(cid, spec))

    if run_api and api_token:
        print(f"API base: {api_base()}")
        for cid in case_ids:
            spec = all_cases.get(cid)
            if spec:
                results.append(await run_api_case(cid, spec, token=api_token))
    elif layers in ("api", "all") and not api_token:
        print("API layer skipped (no token or server unreachable)")

    return _print_results(results)


def main() -> int:
    p = argparse.ArgumentParser(description="Standalone PO sign-off matrix (YUNB/YAST/YSER)")
    add_common_args(p)
    p.add_argument(
        "--layer",
        choices=["sap", "api", "all"],
        default="sap",
        help="Verification layer (default: sap direct client)",
    )
    p.add_argument(
        "--cases",
        default="",
        help="Comma-separated case ids (default: all 9 cases)",
    )
    args = p.parse_args()
    api_token: str | None = None
    if args.layer.lower() in ("api", "all"):
        try:
            api_token = resolve_token_from_args(args)
        except SystemExit as e:
            print(f"API auth skipped: {e}")
    return asyncio.run(main_async(args, api_token=api_token))


if __name__ == "__main__":
    raise SystemExit(main())
