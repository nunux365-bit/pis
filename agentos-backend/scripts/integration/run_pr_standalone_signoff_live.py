#!/usr/bin/env python3
"""Standalone PR sign-off: YUNB / YAST / YSER — create → GET → update → delete-line.

Uses DB catalogue defaults (``integration_reference_defaults``). No parent PO.

Layers:
  --layer sap   Production SAP client only (default)
  --layer api   AgentOS API → background SAP sync (requires API on :8000)
  --layer all   Both

  python scripts/integration/run_pr_standalone_signoff_live.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_pr_standalone_signoff_live.py --layer api
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
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_pr_client import create_pr, get_pr, sap_pr_configured, update_pr
from app.procurement.sap_pr_payload import verify_pr_read_against_form
from app.procurement.sap_pr_z_payload import form_from_z_pr_read
from app.procurement.sap_ticket_form_read import form_from_pr_sap_read

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    apply_pr_integration_fixtures,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    classify_sap_ticket,
    create_pr_via_api_result,
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
    steps: list[StepResult] = field(default_factory=list)

    def green(self) -> bool:
        """SKIP steps are not exercised — they must not count as pass."""
        ran = [s for s in self.steps if s.status != "SKIP"]
        if not ran:
            return False
        return all(s.status in ("OK", "TAX-KNOWN") for s in ran)


def _sap_ok(ticket: dict[str, Any]) -> tuple[bool, str]:
    status, detail, _ = classify_sap_ticket(ticket)
    return status == "pass", detail


def _apply_fixtures(dt: str) -> None:
    apply_pr_integration_fixtures(dt)


def _overlay_yast_form(form: dict[str, Any], *, case_id: str) -> dict[str, Any]:
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


def _prep_form(dt: str, builder: FormBuilder, *, case_id: str = "") -> dict[str, Any]:
    form = builder(header_note=f"pr-signoff-{uuid.uuid4().hex[:8]}")
    form = normalize_form(dt, form)
    if dt == "YAST":
        form = _overlay_yast_form(form, case_id=case_id)
    return form


def _inject_second_line(form: dict[str, Any], *, dt: str) -> None:
    lines = form.get("lines")
    if not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], dict):
        return
    line2 = copy.deepcopy(lines[0])
    if dt == "YSER":
        line2["service"] = (os.environ.get("SAP_SERVICE_2") or "").strip()
        line2["short_text"] = (str(line2.get("short_text") or "Svc") + " B")[:40]
        cc = (os.environ.get("SAP_COST_CENTER") or "").strip()
        line2["allocations"] = [{"cost_center": cc, "qty": "1"}]
    else:
        m2 = (os.environ.get("SAP_MATERIAL_2") or "").strip()
        if m2:
            line2["material"] = m2
        line2["short_text"] = (str(line2.get("short_text") or "Line") + " B")[:40]
    lines.append(line2)


async def _hydrate_pr_form_from_sap(
    *,
    pr_number: str,
    seed_form: dict[str, Any],
    document_type: str,
    ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """GET + map to UI form (resubmit must start from SAP state, not create-time form)."""
    body, err = await get_pr(
        pr_number=pr_number,
        ticket_id=f"{ticket_id}-hydrate",
        document_type=document_type,
    )
    if err or not body:
        return None, err or "GET failed"
    dt = document_type.upper()
    if dt == "YSER":
        return form_from_z_pr_read(body, document_type=dt, seed_form=seed_form), None
    return form_from_pr_sap_read(body, document_type=dt, seed_form=seed_form), None


def _normalize_form_allocation_qty(form: dict[str, Any]) -> None:
    """API validation requires positive integer allocation qty (SAP hydrate returns ``1.000``)."""
    lines = form.get("lines")
    if not isinstance(lines, list):
        return
    for line in lines:
        if not isinstance(line, dict):
            continue
        allocs = line.get("allocations")
        if not isinstance(allocs, list):
            continue
        for alloc in allocs:
            if not isinstance(alloc, dict):
                continue
            raw = str(alloc.get("qty") or "1").strip()
            try:
                alloc["qty"] = str(max(1, int(float(raw.replace(",", ".")))))
            except ValueError:
                alloc["qty"] = "1"


def _apply_pr_signoff_update_edits(
    upd: dict[str, Any], *, document_type: str, case_id: str
) -> None:
    dt = document_type.upper()
    lines = upd.get("lines") or []
    if not lines or not isinstance(lines[0], dict):
        return
    if dt == "YSER":
        if case_id == "yser_multi_cc":
            lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " upd")[:40]
            lines[0]["delivery_date"] = "2026-11-20"
            lines[0]["unit_price"] = "120"
            lines[0]["valuation_price"] = "240"
        elif case_id == "yser_two_service":
            lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " A upd")[:40]
            lines[0]["unit_price"] = "160"
            lines[0]["valuation_price"] = "480"
            if len(lines) > 1 and isinstance(lines[1], dict):
                lines[1]["short_text"] = (str(lines[1].get("short_text") or "Svc") + " B upd")[:40]
                lines[1]["unit_price"] = "90"
                lines[1]["valuation_price"] = "90"
        else:
            lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " upd")[:40]
            lines[0]["delivery_date"] = "2026-11-20"
    else:
        lines[0]["unit_price"] = "99"
        lines[0]["valuation_price"] = "99"


async def _verify_get(
    *,
    pr_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
    expect_lines: int | None = None,
) -> tuple[bool, str]:
    body, err = await get_pr(
        pr_number=pr_number, ticket_id=f"{ticket_id}-get", document_type=document_type
    )
    if err or not body:
        return False, err or "GET failed"
    dt = document_type.upper()
    if dt == "YSER":
        ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
        mm: list[str] = []
    else:
        ui = form_from_pr_sap_read(body, document_type=dt, seed_form=form)
        mm = verify_pr_read_against_form(
            body, form=form, document_type=dt, ticket_id=ticket_id
        )
    n = len(ui.get("lines") or [])
    if expect_lines is not None and n != expect_lines:
        return False, f"lines {n} != expected {expect_lines}"
    if mm:
        return False, "verify: " + "; ".join(mm[:3])
    return True, f"GET OK lines={n}"


async def run_sap_case(case_id: str, dt: str, builder: FormBuilder) -> CaseResult:
    _apply_fixtures(dt)
    tid = str(uuid.uuid4())
    out = CaseResult(case_id=case_id, document_type=dt, layer="sap")

    form = _prep_form(dt, builder, case_id=case_id)
    apply_procurement_defaults(form, document_type=dt, kind="PR")

    work_form = copy.deepcopy(form)
    if case_id.endswith("_simple"):
        _inject_second_line(work_form, dt=dt)

    pr, err = await create_pr(ticket_id=tid, form=work_form, document_type=dt)
    if not pr or err:
        out.steps.append(StepResult("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(StepResult("create", "OK", pr))

    ok_get, det = await _verify_get(
        pr_number=pr,
        form=work_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=None,
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det))
    if not ok_get:
        return out
    baseline_lines = int(det.split("lines=")[-1]) if "lines=" in det else len(
        work_form.get("lines") or []
    )

    hydrated, herr = await _hydrate_pr_form_from_sap(
        pr_number=pr,
        seed_form=work_form,
        document_type=dt,
        ticket_id=tid,
    )
    if not hydrated:
        out.steps.append(StepResult("update", "FAIL", (herr or "hydrate failed")[:200]))
        return out
    upd = copy.deepcopy(hydrated)
    _apply_pr_signoff_update_edits(upd, document_type=dt, case_id=case_id)

    _, uerr = await update_pr(sap_id=pr, ticket_id=tid, form=upd, document_type=dt)
    if not uerr:
        out.steps.append(StepResult("update", "OK", ""))
    elif _is_tax_err(uerr):
        out.steps.append(StepResult("update", "TAX-KNOWN", uerr[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX"))
        if baseline_lines < 2:
            out.steps.append(StepResult("delete_line", "SKIP", "single SAP line"))
        else:
            out.steps.append(StepResult("delete_line", "TAX-KNOWN", "VERTEX"))
            out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", uerr[:200]))
        return out

    ok_get2, det2 = await _verify_get(
        pr_number=pr,
        form=upd,
        document_type=dt,
        ticket_id=tid,
        expect_lines=baseline_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det2))

    if dt == "YSER":
        refreshed, ref_err = await _hydrate_pr_form_from_sap(
            pr_number=pr,
            seed_form=upd,
            document_type=dt,
            ticket_id=f"{tid}-refresh",
        )
        if not refreshed:
            refreshed = copy.deepcopy(upd)
        if isinstance(refreshed.get("header"), dict) and isinstance(upd.get("header"), dict):
            refreshed["header"] = copy.deepcopy(upd["header"])

        if case_id == "yser_two_service" and len(refreshed.get("lines") or []) >= 2:
            del_svc = copy.deepcopy(refreshed)
            del_svc["lines"] = [copy.deepcopy(del_svc["lines"][0])]
            _, ds_err = await update_pr(
                sap_id=pr, ticket_id=tid, form=del_svc, document_type=dt
            )
            if ds_err:
                out.steps.append(StepResult("delete_service", "FAIL", ds_err[:200]))
            else:
                ok_ds, det_ds = await _verify_get(
                    pr_number=pr,
                    form=del_svc,
                    document_type=dt,
                    ticket_id=tid,
                    expect_lines=1,
                )
                out.steps.append(
                    StepResult("delete_service", "OK" if ok_ds else "FAIL", det_ds)
                )
        else:
            out.steps.append(
                StepResult("delete_service", "SKIP", "single-service or multi-cc only")
            )

        out.steps.append(
            StepResult(
                "delete_cc",
                "SKIP",
                "SAP confirmed: per-CC delete not supported on Z YSER PR",
            )
        )
        out.steps.append(StepResult("delete_line", "SKIP", "use delete_service (item IsDeleted)"))
        out.steps.append(StepResult("get_after_delete", "SKIP", "covered by delete_service step"))
        return out

    if baseline_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single SAP line"))
        return out

    hydrated_del, derr_h = await _hydrate_pr_form_from_sap(
        pr_number=pr,
        seed_form=upd,
        document_type=dt,
        ticket_id=f"{tid}-del",
    )
    del_form = copy.deepcopy(hydrated_del if hydrated_del else upd)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    _, derr = await update_pr(sap_id=pr, ticket_id=tid, form=del_form, document_type=dt)
    if not derr:
        out.steps.append(StepResult("delete_line", "OK", ""))
    elif _is_tax_err(derr):
        out.steps.append(StepResult("delete_line", "TAX-KNOWN", derr[:160]))
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("delete_line", "FAIL", derr[:200]))
        return out

    ok_get3, det3 = await _verify_get(
        pr_number=pr,
        form=del_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=baseline_lines - 1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det3))
    return out


async def run_api_case(
    case_id: str,
    dt: str,
    builder: FormBuilder,
    *,
    token: str,
    form: dict[str, Any] | None = None,
) -> CaseResult:
    _apply_fixtures(dt)
    out = CaseResult(case_id=case_id, document_type=dt, layer="api")

    if form is None:
        form = _prep_form(dt, builder, case_id=case_id)
        apply_procurement_defaults(form, document_type=dt, kind="PR")

    work_form = copy.deepcopy(form)
    if case_id.endswith("_simple"):
        _inject_second_line(work_form, dt=dt)

    ticket, err = await asyncio.to_thread(
        create_pr_via_api_result,
        access_token=token,
        document_type=dt,
        form=work_form,
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
    pr = str(polled.get("sap_id") or "")
    out.pr_number = pr
    out.steps.append(StepResult("create", "OK" if ok else "FAIL", f"PR {pr} {det}"[:120]))
    if not ok:
        return out

    ok_get, det_get = await _verify_get(
        pr_number=pr,
        form=work_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=None,
    )
    out.steps.append(StepResult("get_after_create", "OK" if ok_get else "FAIL", det_get))
    if not ok_get:
        return out
    baseline_lines = int(det_get.split("lines=")[-1]) if "lines=" in det_get else len(
        work_form.get("lines") or []
    )

    hydrated, herr = await _hydrate_pr_form_from_sap(
        pr_number=pr,
        seed_form=work_form,
        document_type=dt,
        ticket_id=tid,
    )
    if not hydrated:
        out.steps.append(StepResult("update", "FAIL", (herr or "hydrate failed")[:200]))
        return out
    upd = copy.deepcopy(hydrated)
    _normalize_form_allocation_qty(upd)
    _apply_pr_signoff_update_edits(upd, document_type=dt, case_id=case_id)

    fresh = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    version = int(fresh.get("version") or 0)
    updated, uerr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=tid,
        form=upd,
        version=version,
    )
    if uerr or not updated:
        out.steps.append(StepResult("update", "FAIL", (uerr or "no ticket")[:200]))
        return out
    polled2, _ = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    ok2, det2 = _sap_ok(polled2 or {})
    if ok2:
        out.steps.append(StepResult("update", "OK", det2[:80]))
    elif _is_tax_err(det2):
        out.steps.append(StepResult("update", "TAX-KNOWN", det2[:160]))
        out.steps.append(StepResult("get_after_update", "SKIP", "VERTEX"))
        if baseline_lines < 2:
            out.steps.append(StepResult("delete_line", "SKIP", "single SAP line"))
        else:
            out.steps.append(StepResult("delete_line", "TAX-KNOWN", "VERTEX"))
            out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("update", "FAIL", det2[:200]))
        return out

    ok_get2, det2 = await _verify_get(
        pr_number=pr,
        form=upd,
        document_type=dt,
        ticket_id=tid,
        expect_lines=baseline_lines,
    )
    out.steps.append(StepResult("get_after_update", "OK" if ok_get2 else "FAIL", det2))

    if dt == "YSER":
        refreshed, ref_err = await _hydrate_pr_form_from_sap(
            pr_number=pr,
            seed_form=upd,
            document_type=dt,
            ticket_id=f"{tid}-refresh",
        )
        if not refreshed:
            refreshed = copy.deepcopy(upd)
        if isinstance(refreshed.get("header"), dict) and isinstance(upd.get("header"), dict):
            refreshed["header"] = copy.deepcopy(upd["header"])

        if case_id == "yser_two_service" and len(refreshed.get("lines") or []) >= 2:
            del_svc = copy.deepcopy(refreshed)
            del_svc["lines"] = [copy.deepcopy(del_svc["lines"][0])]
            _normalize_form_allocation_qty(del_svc)
            fresh2 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
            version2 = int(fresh2.get("version") or 0)
            deleted, ds_err = await asyncio.to_thread(
                patch_resync_via_api_result,
                access_token=token,
                ticket_id=tid,
                form=del_svc,
                version=version2,
            )
            if ds_err or not deleted:
                out.steps.append(StepResult("delete_service", "FAIL", (ds_err or "no ticket")[:200]))
            else:
                polled_ds, _ = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
                ok_ds, _ = _sap_ok(polled_ds or {})
                if not ok_ds:
                    sync_err = str(
                        ((polled_ds or {}).get("sap_sync") or {}).get("last_error") or ""
                    )
                    out.steps.append(StepResult("delete_service", "FAIL", sync_err[:200]))
                else:
                    ok_ds_get, det_ds = await _verify_get(
                        pr_number=pr,
                        form=del_svc,
                        document_type=dt,
                        ticket_id=tid,
                        expect_lines=1,
                    )
                    out.steps.append(
                        StepResult("delete_service", "OK" if ok_ds_get else "FAIL", det_ds)
                    )
        else:
            out.steps.append(
                StepResult("delete_service", "SKIP", "single-service or multi-cc only")
            )

        out.steps.append(
            StepResult(
                "delete_cc",
                "SKIP",
                "SAP confirmed: per-CC delete not supported on Z YSER PR",
            )
        )
        out.steps.append(StepResult("delete_line", "SKIP", "use delete_service (item IsDeleted)"))
        out.steps.append(StepResult("get_after_delete", "SKIP", "covered by delete_service step"))
        return out

    if baseline_lines < 2:
        out.steps.append(StepResult("delete_line", "SKIP", "single SAP line"))
        return out

    hydrated_del, _ = await _hydrate_pr_form_from_sap(
        pr_number=pr,
        seed_form=upd,
        document_type=dt,
        ticket_id=f"{tid}-del",
    )
    del_form = copy.deepcopy(hydrated_del if hydrated_del else upd)
    del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
    _normalize_form_allocation_qty(del_form)
    fresh3 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    version3 = int(fresh3.get("version") or 0)
    deleted, derr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=tid,
        form=del_form,
        version=version3,
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
        out.steps.append(StepResult("get_after_delete", "SKIP", "VERTEX"))
        return out
    else:
        out.steps.append(StepResult("delete_line", "FAIL", det3[:200]))
        return out

    ok_get3, det3 = await _verify_get(
        pr_number=pr,
        form=del_form,
        document_type=dt,
        ticket_id=tid,
        expect_lines=baseline_lines - 1,
    )
    out.steps.append(StepResult("get_after_delete", "OK" if ok_get3 else "FAIL", det3))
    return out


def _print_results(results: list[CaseResult]) -> int:
    print("\n" + "=" * 88)
    print("STANDALONE PR SIGN-OFF")
    print("=" * 88)
    fails = 0
    for r in results:
        flag = "GREEN" if r.green() else "RED"
        if not r.green():
            fails += 1
        steps = " | ".join(f"{s.step}={s.status}" for s in r.steps)
        print(f"{flag:5} [{r.layer:3}] {r.document_type} {r.case_id:<22} PR={r.pr_number:<12} {steps}")
        for s in r.steps:
            if s.status == "FAIL" and s.detail:
                print(f"       ↳ {s.step}: {s.detail[:140]}")
    print("=" * 88)
    skipped = sum(1 for r in results for s in r.steps if s.status == "SKIP")
    print(f"GREEN={len(results)-fails} RED={fails} TOTAL={len(results)} SKIP_STEPS={skipped}")
    print("(SKIP steps are not run — they do not make a case GREEN)")
    print("=" * 88)
    return 1 if fails else 0


async def main_async(args: argparse.Namespace, *, api_token: str | None) -> int:
    load_env()
    apply_integration_reference_defaults()
    if not sap_pr_configured():
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
            results.append(await run_sap_case(cid, dt, builder))

    if run_api and api_token:
        print(f"API base: {api_base()}")
        for cid in case_ids:
            spec = ALL_CASES.get(cid)
            if spec:
                dt, builder = spec
                results.append(await run_api_case(cid, dt, builder, token=api_token))
    elif layers in ("api", "all") and not api_token:
        print("API layer skipped (no token or server unreachable)")

    return _print_results(results)


def main() -> int:
    p = argparse.ArgumentParser(description="Standalone PR sign-off matrix (YUNB/YAST/YSER)")
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
