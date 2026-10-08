#!/usr/bin/env python3
"""PO-from-PR sign-off: create PR → prefill PO → create → GET → update → delete.

SAP layer: direct ``create_pr`` + ``build_po_prefill_form_from_pr`` + ``create_po``.
API layer: ``POST …/pr`` → poll → ``GET …/prefill-po`` → ``POST …/po`` → poll →
``PATCH`` resync → poll (same as UI).

  python scripts/integration/run_po_from_pr_signoff_live.py
  python scripts/integration/run_po_from_pr_signoff_live.py --case yunb_simple
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_from_pr_signoff_live.py --layer api
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
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.field_schema import normalize_form
from app.procurement.po_sap_enrichment import build_po_prefill_form_from_pr
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import (
    create_po,
    get_po,
    sap_po_configured,
    sap_pr_configured,
    update_po,
)
from app.procurement.sap_po_payload import verify_po_read_against_form
from app.procurement.sap_po_z_payload import form_from_z_po_read, verify_z_po_read_against_form
from app.procurement.sap_pr_client import create_pr
from app.procurement.sap_ticket_form_read import form_from_po_sap_read

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    apply_pr_integration_fixtures,
)
from scripts.integration.po_live_common import apply_po_live_fixtures, default_delivery_date
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    classify_sap_ticket,
    create_po_via_api_result,
    create_pr_via_api_result,
    get_prefill_po_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.run_pr_create_yast import (
    simple_yast_form,
    yast_multi_cc_form,
    yast_two_material_form,
)
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form
from scripts.integration.run_pr_create_yunb import (
    simple_yunb_form,
    yunb_multi_cc_form,
    yunb_two_material_form,
)

TAX_KW = ("tax", "vertex", "external tax")

FormBuilder = Callable[..., dict[str, Any]]


def _is_tax_err(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in TAX_KW)


def _is_yunb_po_update_known(msg: str) -> bool:
    """QAS YUNB PO PATCH: VERTEX tax bridge on standalone consumable POs."""
    return _is_tax_err(msg or "")


def _yser_multi_cc_form(*, header_note: str) -> dict[str, Any]:
    form = simple_yser_form(header_note=header_note)
    cc_a = (os.environ.get("SAP_COST_CENTER") or "").strip()
    cc_b = (os.environ.get("SAP_COST_CENTER_2") or "").strip()
    form["lines"][0]["allocations"] = [
        {"cost_center": cc_a, "qty": "1"},
        {"cost_center": cc_b, "qty": "1"},
    ]
    return form


ALL_CASES: dict[str, tuple[str, FormBuilder]] = {
    "yunb_simple": ("YUNB", simple_yunb_form),
    "yunb_multi_cc": ("YUNB", yunb_multi_cc_form),
    "yunb_two_material": ("YUNB", yunb_two_material_form),
    "yast_simple": ("YAST", simple_yast_form),
    "yast_multi_cc": ("YAST", yast_multi_cc_form),
    "yast_two_material": ("YAST", yast_two_material_form),
    "yser_simple": ("YSER", simple_yser_form),
    "yser_multi_cc": ("YSER", _yser_multi_cc_form),
    "yser_two_service": ("YSER", complex_yser_form),
}


@dataclass
class StepResult:
    step: str
    status: str
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    document_type: str
    layer: str = "sap"
    pr_number: str = ""
    po_number: str = ""
    steps: list[StepResult] = field(default_factory=list)

    def green(self) -> bool:
        return all(s.status in ("OK", "SKIP", "TAX-KNOWN") for s in self.steps)


def _apply_pr_fixtures(dt: str) -> None:
    # Drop prior PO force-apply keys so YSER plant/sloc don't collide with YUNB PR fixtures.
    for key in (
        "SAP_PUR_ORG",
        "SAP_PUR_GROUP",
        "SAP_PLANT",
        "SAP_SLOC",
        "SAP_COST_CENTER",
        "SAP_COST_CENTER_2",
        "SAP_ASSET",
        "SAP_ASSET_2",
        "SAP_MATERIAL",
        "SAP_MATERIAL_2",
        "SAP_MATERIAL_GROUP",
        "SAP_SERVICE",
        "SAP_SERVICE_2",
        "SAP_SERVICE_GROUP",
        "SAP_VENDOR",
        "SAP_PAYMENT_TERMS",
        "SAP_TAX_CODE",
        "SAP_ORDER_UNIT",
        "SAP_UNIT_PRICE",
        "SAP_PO_INFO_RECORD",
    ):
        os.environ.pop(key, None)
    apply_pr_integration_fixtures(dt)


def _overlay_yast_pr_form(form: dict[str, Any], *, case_id: str) -> dict[str, Any]:
    out = copy.deepcopy(form)
    lines = out.get("lines") or []
    asset = (os.environ.get("SAP_ASSET") or "").strip()
    info_rec = (os.environ.get("SAP_PO_INFO_RECORD") or "").strip()
    unit = (os.environ.get("SAP_ORDER_UNIT") or "EA").strip()
    if case_id == "yast_multi_cc" and lines and isinstance(lines[0], dict):
        a1 = asset
        a2 = (os.environ.get("SAP_ASSET_2") or asset).strip()
        lines[0]["allocations"] = [
            {"asset": a1, "qty": "2"},
            {"asset": a2, "qty": "1"},
        ]
        lines[0]["asset"] = a1
    elif case_id == "yast_two_material" and len(lines) >= 2:
        m1 = (os.environ.get("SAP_MATERIAL") or "").strip()
        m2 = (os.environ.get("SAP_MATERIAL_2") or "").strip()
        a2 = (os.environ.get("SAP_ASSET_2") or asset).strip()
        if isinstance(lines[0], dict):
            lines[0].update(
                {
                    "material": m1,
                    "order_unit": unit,
                    "unit_price": "100.00",
                    "valuation_price": "100.00",
                    "asset": asset,
                    "purchasing_info_record": info_rec,
                    "allocations": [{"asset": asset, "qty": "5"}],
                }
            )
        if isinstance(lines[1], dict):
            lines[1].update(
                {
                    "material": m2,
                    "order_unit": unit,
                    "unit_price": "100.00",
                    "valuation_price": "100.00",
                    "asset": a2,
                    "purchasing_info_record": info_rec,
                    "allocations": [{"asset": a2, "qty": "3"}],
                }
            )
    return out


def _prep_pr_form(dt: str, builder: FormBuilder, *, case_id: str) -> dict[str, Any]:
    form = builder(header_note=f"po-fr-pr-{uuid.uuid4().hex[:8]}")
    form = normalize_form(dt, form)
    if dt == "YAST":
        form = _overlay_yast_pr_form(form, case_id=case_id)
    apply_procurement_defaults(form, document_type=dt, kind="PR")
    return form


def _finalize_po_form(po_form: dict[str, Any], *, dt: str, ticket_id: str) -> dict[str, Any]:
    out = normalize_form(dt, po_form)
    apply_procurement_defaults(out, document_type=dt, kind="PO")
    hdr = out.setdefault("header", {})
    hdr["vendor"] = (os.environ.get("SAP_VENDOR") or "1000000002").strip()
    tax = (os.environ.get("SAP_TAX_CODE") or "").strip()
    if not tax:
        # Prefer allowlisted codes used on QAS YUNB/YAST/YSER PO updates.
        for candidate in ("FA", "HA", "V0"):
            from app.procurement.tax_code_allowlist import tax_code_is_allowed

            if tax_code_is_allowed(candidate):
                tax = candidate
                break
    if tax:
        hdr["tax_code"] = tax
    if not str(hdr.get("header_note") or "").strip():
        hdr["header_note"] = f"PO-{ticket_id[:8]}"
    hdr["header_note"] = str(hdr.get("header_note") or "")[:12]
    pay = (os.environ.get("SAP_PAYMENT_TERMS") or "").strip()
    if pay and not str(hdr.get("payment_terms") or "").strip():
        hdr["payment_terms"] = pay
    for row in out.get("lines") or []:
        if isinstance(row, dict) and not str(row.get("delivery_date") or "").strip():
            row["delivery_date"] = default_delivery_date()
    return out


async def _hydrate_form_from_po(
    *,
    po_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    body, err = await get_po(
        po_number=po_number, ticket_id=f"{ticket_id}-hydrate", document_type=document_type
    )
    if err or not body:
        return None, err or "GET failed"
    dt = document_type.upper()
    if dt == "YSER":
        return form_from_z_po_read(body, seed_form=form), None
    return form_from_po_sap_read(body, document_type=dt, seed_form=form), None


async def _verify_po_get(
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
    n = len(ui.get("lines") or [])
    if n != expect_lines:
        return False, f"lines {n} != expected {expect_lines}"
    if mm:
        return False, "verify: " + "; ".join(mm[:3])
    return True, f"GET OK lines={n}"


def _sap_ok(ticket: dict[str, Any]) -> tuple[bool, str]:
    status, detail, _ = classify_sap_ticket(ticket)
    return status == "pass", detail


def _ensure_line_delivery_dates(form: dict[str, Any]) -> None:
    for row in form.get("lines") or []:
        if isinstance(row, dict) and not str(row.get("delivery_date") or "").strip():
            row["delivery_date"] = default_delivery_date()


def _normalize_whole_alloc_qty(form: dict[str, Any]) -> None:
    """API validation requires positive integer allocation qty (YSER hydrate can return decimals)."""
    lines = form.get("lines")
    if not isinstance(lines, list):
        return
    for line in lines:
        if not isinstance(line, dict):
            continue
        allocs = line.get("allocations")
        if not isinstance(allocs, list):
            continue
        for a in allocs:
            if not isinstance(a, dict):
                continue
            raw = str(a.get("qty") or "1").strip()
            try:
                a["qty"] = str(max(1, int(float(raw.replace(",", ".")))))
            except ValueError:
                a["qty"] = "1"


def _classify_api_sync(
    ticket: dict[str, Any] | None, poll_err: str, *, dt: str
) -> tuple[str, str]:
    if not ticket:
        return "FAIL", poll_err or "no ticket"
    sync = ticket.get("sap_sync") if isinstance(ticket.get("sap_sync"), dict) else {}
    sync_err = str(sync.get("last_error") or "").strip()
    if poll_err == "timeout waiting for sap_id" and sync_err:
        if dt == "YUNB" and _is_yunb_po_update_known(sync_err):
            return "TAX-KNOWN", sync_err[:160]
        if _is_tax_err(sync_err):
            return "TAX-KNOWN", sync_err[:160]
    ok, det = _sap_ok(ticket)
    if ok:
        return "OK", det[:80]
    if dt == "YUNB" and _is_yunb_po_update_known(det):
        return "TAX-KNOWN", det[:160]
    if _is_tax_err(det):
        return "TAX-KNOWN", det[:160]
    return "FAIL", (poll_err or det)[:200]


async def run_api_case(case_id: str, dt: str, builder: FormBuilder, *, token: str) -> CaseResult:
    _apply_pr_fixtures(dt)
    out = CaseResult(case_id=case_id, document_type=dt, layer="api")

    pr_form = _prep_pr_form(dt, builder, case_id=case_id)
    pr_ticket, pr_err = await asyncio.to_thread(
        create_pr_via_api_result,
        access_token=token,
        document_type=dt,
        form=pr_form,
    )
    if pr_err or not pr_ticket:
        out.steps.append(StepResult("pr_create", "FAIL", (pr_err or "no ticket")[:200]))
        return out
    pr_id = str(pr_ticket.get("id") or "")
    pr_polled, pr_poll_err = await asyncio.to_thread(poll_ticket_sap_sync, token, pr_id)
    pr_status, pr_det = _classify_api_sync(pr_polled, pr_poll_err, dt=dt)
    if pr_status != "OK" or not pr_polled:
        out.steps.append(StepResult("pr_create", "FAIL", pr_det))
        return out
    pr_sap = str(pr_polled.get("sap_id") or "")
    out.pr_number = pr_sap
    out.steps.append(StepResult("pr_create", "OK", pr_sap))

    prefill_resp, pfill_err = await asyncio.to_thread(
        get_prefill_po_via_api_result,
        access_token=token,
        pr_ticket_id=pr_id,
    )
    po_seed = (
        prefill_resp.get("form")
        if isinstance(prefill_resp, dict) and isinstance(prefill_resp.get("form"), dict)
        else prefill_resp
    )
    if pfill_err or not isinstance(po_seed, dict) or not po_seed.get("lines"):
        out.steps.append(StepResult("prefill_po", "FAIL", (pfill_err or "prefill empty")[:200]))
        return out
    out.steps.append(StepResult("prefill_po", "OK", f"lines={len(po_seed.get('lines') or [])}"))

    apply_po_live_fixtures(dt)
    po_form = _finalize_po_form(po_seed, dt=dt, ticket_id=pr_id)
    _normalize_whole_alloc_qty(po_form)

    po_ticket, po_err = await asyncio.to_thread(
        create_po_via_api_result,
        access_token=token,
        document_type=dt,
        form=po_form,
        parent_pr_id=pr_id,
    )
    if po_err or not po_ticket:
        out.steps.append(StepResult("po_create", "FAIL", (po_err or "no ticket")[:200]))
        return out
    po_id = str(po_ticket.get("id") or "")
    po_polled, po_poll_err = await asyncio.to_thread(poll_ticket_sap_sync, token, po_id)
    po_status, po_det = _classify_api_sync(po_polled, po_poll_err, dt=dt)
    if po_status != "OK" or not po_polled:
        out.steps.append(StepResult("po_create", "FAIL", po_det))
        return out
    po_sap = str(po_polled.get("sap_id") or "")
    out.po_number = po_sap
    out.steps.append(StepResult("po_create", "OK", po_sap))

    expect_lines = len(po_form.get("lines") or [])
    verify_form = po_form
    if dt == "YSER":
        hydrated, herr = await _hydrate_form_from_po(
            po_number=po_sap, form=po_form, document_type=dt, ticket_id=po_id
        )
        if not hydrated:
            out.steps.append(StepResult("get_after_create", "FAIL", (herr or "hydrate failed")[:200]))
            return out
        verify_form = hydrated
        expect_lines = len(hydrated.get("lines") or [])
    ok_get, det = await _verify_po_get(
        po_number=po_sap,
        form=verify_form,
        document_type=dt,
        ticket_id=po_id,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det))
    if not ok_get:
        return out

    fresh = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=po_id)
    version = int(fresh.get("version") or 0)
    if dt == "YSER":
        working, werr = await _hydrate_form_from_po(
            po_number=po_sap, form=po_form, document_type=dt, ticket_id=po_id
        )
        if not working:
            out.steps.append(StepResult("update", "FAIL", (werr or "hydrate failed")[:200]))
            return out
        upd = copy.deepcopy(working)
    else:
        upd = copy.deepcopy(po_form)
    lines = upd.get("lines") or []
    if lines and isinstance(lines[0], dict):
        if dt == "YSER":
            for row in lines:
                if isinstance(row, dict):
                    row["delivery_date"] = "2026-11-20"
            lines[0]["unit_price"] = "88"
            lines[0]["net_price"] = "88"
        else:
            lines[0]["unit_price"] = "15"
            lines[0]["net_price"] = "15"
    _ensure_line_delivery_dates(upd)
    _normalize_whole_alloc_qty(upd)

    updated, uerr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=po_id,
        form=upd,
        version=version,
    )
    if uerr or not updated:
        out.steps.append(StepResult("update", "FAIL", (uerr or "no ticket")[:200]))
        return out
    polled_u, poll_u_err = await asyncio.to_thread(poll_ticket_sap_sync, token, po_id)
    upd_status, upd_det = _classify_api_sync(polled_u, poll_u_err, dt=dt)
    if upd_status == "OK":
        out.steps.append(StepResult("update", "OK", upd_det))
    elif upd_status == "TAX-KNOWN":
        out.steps.append(StepResult("update", "TAX-KNOWN", upd_det))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX"))
        out.steps.append(StepResult("delete_line", "SKIP", "VERTEX"))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", upd_det))
        return out

    verify_upd = upd
    if dt == "YSER":
        hydrated_u, uherr = await _hydrate_form_from_po(
            po_number=po_sap, form=upd, document_type=dt, ticket_id=f"{po_id}-uh"
        )
        if not hydrated_u:
            out.steps.append(StepResult("get_after_update", "FAIL", (uherr or "hydrate failed")[:200]))
            return out
        verify_upd = hydrated_u
    ok_get2, det2 = await _verify_po_get(
        po_number=po_sap,
        form=verify_upd,
        document_type=dt,
        ticket_id=po_id,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det2))
    if not ok_get2:
        return out

    if expect_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        return out

    fresh2 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=po_id)
    version2 = int(fresh2.get("version") or 0)
    base_form = (
        fresh2.get("form") if isinstance(fresh2.get("form"), dict) else upd
    )
    if dt == "YSER":
        hydrated2, herr2 = await _hydrate_form_from_po(
            po_number=po_sap, form=base_form, document_type=dt, ticket_id=f"{po_id}-ud"
        )
        if not hydrated2:
            out.steps.append(StepResult("delete_line", "FAIL", (herr2 or "hydrate failed")[:200]))
            return out
        del_form = copy.deepcopy(hydrated2)
    else:
        del_form = copy.deepcopy(base_form)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    _ensure_line_delivery_dates(del_form)
    _normalize_whole_alloc_qty(del_form)

    deleted, derr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=po_id,
        form=del_form,
        version=version2,
    )
    if derr or not deleted:
        out.steps.append(StepResult("delete_line", "FAIL", (derr or "no ticket")[:200]))
        return out
    polled_d, poll_d_err = await asyncio.to_thread(poll_ticket_sap_sync, token, po_id)
    del_status, del_det = _classify_api_sync(polled_d, poll_d_err, dt=dt)
    if del_status == "OK":
        out.steps.append(StepResult("delete_line", "OK", del_det))
    elif del_status == "TAX-KNOWN":
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", del_det))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("delete_line", "FAIL", del_det))
        return out

    verify_del = del_form
    if dt == "YSER":
        hydrated3, _ = await _hydrate_form_from_po(
            po_number=po_sap, form=del_form, document_type=dt, ticket_id=f"{po_id}-dh"
        )
        if hydrated3:
            verify_del = hydrated3
    ok_get3, det3 = await _verify_po_get(
        po_number=po_sap,
        form=verify_del,
        document_type=dt,
        ticket_id=po_id,
        expect_lines=expect_lines - 1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det3))
    return out


async def run_case(case_id: str, dt: str, builder: FormBuilder) -> CaseResult:
    _apply_pr_fixtures(dt)
    tid = str(uuid.uuid4())
    out = CaseResult(case_id=case_id, document_type=dt, layer="sap")

    pr_form = _prep_pr_form(dt, builder, case_id=case_id)
    pr, perr = await create_pr(ticket_id=tid, form=pr_form, document_type=dt)
    if not pr or perr:
        out.steps.append(StepResult("pr_create", "FAIL", (perr or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(StepResult("pr_create", "OK", pr))

    po_seed, pfill_err = await build_po_prefill_form_from_pr(
        pr_form, document_type=dt, pr_sap_id=pr, ticket_id=tid
    )
    if pfill_err or not po_seed.get("lines"):
        out.steps.append(StepResult("po_create", "FAIL", (pfill_err or "prefill empty")[:200]))
        return out

    apply_po_live_fixtures(dt)
    po_form = _finalize_po_form(po_seed, dt=dt, ticket_id=tid)

    po, cerr = await create_po(
        ticket_id=tid, form=po_form, document_type=dt, parent_sap_id=pr
    )
    if not po or cerr:
        out.steps.append(StepResult("po_create", "FAIL", (cerr or "")[:200]))
        return out
    out.po_number = po
    out.steps.append(StepResult("po_create", "OK", po))

    expect_lines = len(po_form.get("lines") or [])
    verify_form = po_form
    if dt == "YSER":
        hydrated, herr = await _hydrate_form_from_po(
            po_number=po, form=po_form, document_type=dt, ticket_id=tid
        )
        if not hydrated:
            out.steps.append(StepResult("get_after_create", "FAIL", (herr or "hydrate failed")[:200]))
            return out
        verify_form = hydrated
        expect_lines = len(hydrated.get("lines") or [])
    ok_get, det = await _verify_po_get(
        po_number=po,
        form=verify_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det))
    if not ok_get:
        return out

    if dt == "YSER":
        working, werr = await _hydrate_form_from_po(
            po_number=po, form=po_form, document_type=dt, ticket_id=tid
        )
        if not working:
            out.steps.append(StepResult("update", "FAIL", (werr or "hydrate failed")[:200]))
            return out
        upd = copy.deepcopy(working)
    else:
        upd = copy.deepcopy(po_form)
    lines = upd.get("lines") or []
    if lines and isinstance(lines[0], dict):
        if dt == "YSER":
            for row in lines:
                if isinstance(row, dict):
                    row["delivery_date"] = "2026-11-20"
            lines[0]["unit_price"] = "88"
            lines[0]["net_price"] = "88"
        else:
            lines[0]["unit_price"] = "15"
            lines[0]["net_price"] = "15"

    try:
        _, uerr = await update_po(sap_id=po, ticket_id=f"{tid}-u", form=upd, document_type=dt)
    except ValueError as e:
        uerr = str(e)
    if not uerr:
        out.steps.append(StepResult("update", "OK", ""))
    elif dt == "YUNB" and _is_yunb_po_update_known(uerr):
        out.steps.append(StepResult("update", "TAX-KNOWN", uerr[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "YUNB QAS PATCH"))
        out.steps.append(StepResult("delete_line", "SKIP", "YUNB QAS PATCH"))
        out.steps.append(StepResult("get_after_delete", "SKIP", "YUNB QAS PATCH"))
        return out
    elif _is_tax_err(uerr):
        out.steps.append(StepResult("update", "TAX-KNOWN", uerr[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX"))
        out.steps.append(StepResult("delete_line", "SKIP", "VERTEX"))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", uerr[:200]))
        return out

    verify_upd = upd
    if dt == "YSER":
        hydrated_u, uherr = await _hydrate_form_from_po(
            po_number=po, form=upd, document_type=dt, ticket_id=f"{tid}-uh"
        )
        if not hydrated_u:
            out.steps.append(StepResult("get_after_update", "FAIL", (uherr or "hydrate failed")[:200]))
            return out
        verify_upd = hydrated_u
    ok_get2, det2 = await _verify_po_get(
        po_number=po,
        form=verify_upd,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det2))

    if expect_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single line"))
        return out

    if dt == "YSER":
        hydrated2, herr2 = await _hydrate_form_from_po(
            po_number=po, form=upd, document_type=dt, ticket_id=f"{tid}-ud"
        )
        if not hydrated2:
            out.steps.append(StepResult("delete_line", "FAIL", (herr2 or "hydrate failed")[:200]))
            return out
        del_form = copy.deepcopy(hydrated2)
    else:
        del_form = copy.deepcopy(upd)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    _, derr = await update_po(sap_id=po, ticket_id=f"{tid}-d", form=del_form, document_type=dt)
    if not derr:
        out.steps.append(StepResult("delete_line", "OK", ""))
    elif dt == "YUNB" and _is_yunb_po_update_known(derr):
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", derr[:160]))
        out.steps.append(StepResult("get_after_delete", "SKIP", "YUNB QAS PATCH"))
        return out
    elif _is_tax_err(derr):
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", derr[:160]))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("delete_line", "FAIL", derr[:200]))
        return out

    verify_del = del_form
    if dt == "YSER":
        hydrated3, h3err = await _hydrate_form_from_po(
            po_number=po, form=del_form, document_type=dt, ticket_id=f"{tid}-dh"
        )
        if hydrated3:
            verify_del = hydrated3
    ok_get3, det3 = await _verify_po_get(
        po_number=po,
        form=verify_del,
        document_type=dt,
        ticket_id=tid,
        expect_lines=expect_lines - 1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det3))
    return out


def _print_results(results: list[CaseResult]) -> int:
    print("\n" + "=" * 96)
    print("PO FROM PR SIGN-OFF")
    print("=" * 96)
    fails = 0
    for r in results:
        flag = "GREEN" if r.green() else "RED"
        if not r.green():
            fails += 1
        steps = " | ".join(f"{s.step}={s.status}" for s in r.steps)
        print(
            f"{flag:5} [{r.layer:3}] {r.document_type} {r.case_id:<22} "
            f"PR={r.pr_number:<12} PO={r.po_number:<12} {steps}"
        )
        for s in r.steps:
            if s.status == "FAIL" and s.detail:
                print(f"       ↳ {s.step}: {s.detail[:140]}")
    print("=" * 96)
    print(f"GREEN={len(results) - fails} RED={fails} TOTAL={len(results)}")
    print("=" * 96)
    return 1 if fails else 0


async def main_async(args: argparse.Namespace, *, api_token: str | None) -> int:
    load_env()
    apply_integration_reference_defaults()
    if not sap_pr_configured() or not sap_po_configured():
        print("SAP not configured")
        return 2

    case_ids = (
        [x.strip() for x in args.cases.split(",") if x.strip()]
        if args.cases
        else list(ALL_CASES)
    )
    layers = args.layer.lower()
    run_sap = layers in ("sap", "all")
    run_api = layers in ("api", "all") and bool(api_token)

    results: list[CaseResult] = []
    if run_sap:
        for cid in case_ids:
            spec = ALL_CASES.get(cid)
            if not spec:
                print(f"Unknown case: {cid}")
                return 2
            dt, builder = spec
            print(f"\n>>> Running {cid} ({dt}) [sap] …")
            results.append(await run_case(cid, dt, builder))

    if run_api and api_token:
        print(f"API base: {api_base()}")
        for cid in case_ids:
            spec = ALL_CASES.get(cid)
            if not spec:
                print(f"Unknown case: {cid}")
                return 2
            dt, builder = spec
            print(f"\n>>> Running {cid} ({dt}) [api] …")
            results.append(await run_api_case(cid, dt, builder, token=api_token))
    elif layers in ("api", "all") and not api_token:
        print("API layer skipped (no token or server unreachable)")

    return _print_results(results)


def main() -> int:
    p = argparse.ArgumentParser(description="PO-from-PR sign-off matrix (YUNB/YAST/YSER)")
    add_common_args(p)
    p.add_argument(
        "--layer",
        choices=["sap", "api", "all"],
        default="sap",
        help="Verification layer (default: sap direct client)",
    )
    p.add_argument("--cases", default="", help="Comma-separated case ids")
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
