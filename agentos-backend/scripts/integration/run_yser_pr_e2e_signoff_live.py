#!/usr/bin/env python3
"""YSER PR end-to-end sign-off: simple + complex × create/get/update/delete-line/full-delete.

Exercises production code paths:
  - SAP layer: normalize_form → apply_procurement_defaults → create_pr/update_pr/get_pr
  - UI hydrate: load_form_from_sap (same as GET /api/procurement/tickets/{id})
  - API layer (optional): POST/PATCH tickets when API is up

Usage (from agentos-backend):
  python scripts/integration/run_yser_pr_e2e_signoff_live.py
  python scripts/integration/run_yser_pr_e2e_signoff_live.py --layer api
  python scripts/integration/run_yser_pr_e2e_signoff_live.py --case yser_complex
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
from app.procurement.sap_pr_z_client import delete_yser_pr
from app.procurement.sap_odata_utils import (
    odata_entity_properties,
    odata_norm as _norm,
    odata_results_list,
    odata_text,
)
from app.procurement.sap_pr_z_payload import (
    count_z_pr_sap_items,
    form_from_z_pr_read,
    verify_yser_read_matches_form,
    yser_effective_line_blocks,
)
from app.procurement.sap_ticket_form_read import load_form_from_sap

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    create_pr_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form

FormBuilder = Callable[..., dict[str, Any]]


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _line(svc: str, grp: str, *, ccs: list[tuple[str, str]], short: str = "svc") -> dict:
    return {
        "service": svc,
        "service_group": grp,
        "short_text": short,
        "unit_price": "100",
        "valuation_price": "100",
        "delivery_date": "2026-08-15",
        "allocations": [{"cost_center": cc, "qty": qty} for cc, qty in ccs],
    }


def yser_multi_cc_form(*, header_note: str) -> dict:
    form = simple_yser_form(header_note=header_note)
    form["lines"][0]["allocations"] = [
        {"cost_center": _env("SAP_COST_CENTER"), "qty": "1"},
        {"cost_center": _env("SAP_COST_CENTER_2"), "qty": "1"},
    ]
    return form


def yser_grouped_two_service_form(*, header_note: str) -> dict:
    sg = _env("SAP_SERVICE_GROUP", "S089-0001")
    s1, s2 = _env("SAP_SERVICE"), _env("SAP_SERVICE_2")
    cc = _env("SAP_COST_CENTER")
    return {
        "header": {
            "purchasing_org": _env("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": _env("SAP_PUR_GROUP", "S0Z"),
            "plant": _env("SAP_PLANT", "H002"),
            "storage_location": _env("SAP_SLOC", "1001"),
            "service_group": sg,
            "header_note": header_note,
            "fixed_vendor": _env("SAP_VENDOR", "0000008005"),
        },
        "lines": [
            _line(s1, sg, ccs=[(cc, "1")], short="Svc A"),
            _line(s2, sg, ccs=[(cc, "1")], short="Svc B"),
        ],
    }


def yser_complex_form(*, header_note: str) -> dict:
    g1 = _env("SAP_SERVICE_GROUP", "S089-0001")
    g2 = _env("SAP_SERVICE_GROUP_2", "SD05-0001")
    s1, s2 = _env("SAP_SERVICE"), _env("SAP_SERVICE_2")
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    ccs = [(cc1, "1"), (cc2, "1")]
    return {
        "header": {
            "purchasing_org": _env("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": _env("SAP_PUR_GROUP", "S0Z"),
            "plant": _env("SAP_PLANT", "H002"),
            "storage_location": _env("SAP_SLOC", "1001"),
            "service_group": g1,
            "header_note": header_note,
            "fixed_vendor": _env("SAP_VENDOR", "0000008005"),
        },
        "lines": [
            _line(s1, g1, ccs=ccs, short="G1 A"),
            _line(s2, g1, ccs=ccs, short="G1 B"),
            _line(s1, g2, ccs=ccs, short="G2 A"),
            _line(s2, g2, ccs=ccs, short="G2 B"),
        ],
    }


def yser_duplicate_service_form(*, header_note: str) -> dict:
    sg = _env("SAP_SERVICE_GROUP", "S089-0001")
    svc = _env("SAP_SERVICE")
    cc1, cc2 = _env("SAP_COST_CENTER"), _env("SAP_COST_CENTER_2")
    return {
        "header": {
            "purchasing_org": _env("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": _env("SAP_PUR_GROUP", "S0Z"),
            "plant": _env("SAP_PLANT", "H002"),
            "storage_location": _env("SAP_SLOC", "1001"),
            "service_group": sg,
            "header_note": header_note,
            "fixed_vendor": _env("SAP_VENDOR", "0000008005"),
        },
        "lines": [
            _line(svc, sg, ccs=[(cc1, "1")], short="Dup line 1"),
            _line(svc, sg, ccs=[(cc2, "1")], short="Dup line 2"),
        ],
    }


def yser_two_groups_form(*, header_note: str) -> dict:
    """Two lines in different service_groups → two SAP items (legacy delete path)."""
    g1 = _env("SAP_SERVICE_GROUP", "S089-0001")
    g2 = _env("SAP_SERVICE_GROUP_2", "SD05-0001")
    svc = _env("SAP_SERVICE")
    cc = _env("SAP_COST_CENTER")
    return {
        "header": {
            "purchasing_org": _env("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": _env("SAP_PUR_GROUP", "S0Z"),
            "plant": _env("SAP_PLANT", "H002"),
            "storage_location": _env("SAP_SLOC", "1001"),
            "service_group": g1,
            "header_note": header_note,
            "fixed_vendor": _env("SAP_VENDOR", "0000008005"),
        },
        "lines": [
            _line(svc, g1, ccs=[(cc, "1")], short="Group 1"),
            _line(svc, g2, ccs=[(cc, "1")], short="Group 2"),
        ],
    }


CASES: dict[str, tuple[FormBuilder, int | None, str]] = {
    # case_id -> (builder, expected_ui_lines_after_create, delete_service_mode)
    # delete_service_mode: none | legacy_item | grouped_skip
    "yser_simple": (simple_yser_form, 1, "none"),
    "yser_multi_cc": (yser_multi_cc_form, 1, "none"),
    "yser_two_service": (complex_yser_form, 2, "grouped_skip"),
    "yser_grouped_2svc": (yser_grouped_two_service_form, 2, "grouped_skip"),
    "yser_complex": (yser_complex_form, 4, "grouped_skip"),
    "yser_duplicate_svc": (yser_duplicate_service_form, 1, "grouped_skip"),
    "yser_two_groups": (yser_two_groups_form, 2, "legacy_item"),
}


@dataclass
class Step:
    name: str
    status: str  # OK | FAIL | SKIP
    detail: str = ""


@dataclass
class CaseOut:
    case_id: str
    layer: str
    pr_number: str = ""
    steps: list[Step] = field(default_factory=list)

    def ok(self) -> bool:
        ran = [s for s in self.steps if s.status != "SKIP"]
        return bool(ran) and all(s.status == "OK" for s in ran)


def _prep(builder: FormBuilder) -> dict[str, Any]:
    form = builder(header_note=f"yser-e2e-{uuid.uuid4().hex[:8]}")
    form = normalize_form("YSER", form)
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    return form


def _normalize_alloc_qty(form: dict[str, Any]) -> None:
    for line in form.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for alloc in line.get("allocations") or []:
            if not isinstance(alloc, dict):
                continue
            try:
                alloc["qty"] = str(max(1, int(float(str(alloc.get("qty") or "1").replace(",", ".")))))
            except ValueError:
                alloc["qty"] = "1"


def _sap_shape_summary(body: dict[str, Any]) -> str:
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    items = odata_results_list(root.get("to_Items"))
    parts: list[str] = []
    for item in items:
        ip = odata_entity_properties(item)
        pr_item = odata_text(ip.get("PRItem"))
        mg = odata_text(ip.get("MaterialGroup"))
        svcs = odata_results_list(ip.get("to_Services"))
        rows: list[str] = []
        for s in svcs:
            sp = odata_entity_properties(s)
            rows.append(
                f"{odata_text(sp.get('Service'))[-6:]}:"
                f"{odata_text(sp.get('CostCenter'))[-4:]}:"
                f"acct{odata_text(sp.get('PRAcctAssgmtNumber'))}"
            )
        parts.append(f"item{pr_item}({mg})[{len(svcs)}svcs:{','.join(rows)}]")
    return " | ".join(parts)


async def _ui_hydrate(
    *, pr_number: str, seed: dict[str, Any], ticket_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    form, source, err = await load_form_from_sap(
        kind="PR",
        document_type="YSER",
        sap_id=pr_number,
        ticket_id=ticket_id,
        seed_form=seed,
    )
    if err or not form or source != "sap":
        return None, err or f"hydrate source={source}"
    return form, None


async def _verify_create_with_duplicate_tolerance(
    *,
    case_id: str,
    pr_number: str,
    form: dict[str, Any],
    ticket_id: str,
    expect_lines: int | None,
) -> tuple[bool, str]:
    ok, det = await _verify(pr_number=pr_number, form=form, ticket_id=ticket_id, expect_lines=expect_lines)
    if ok or case_id != "yser_duplicate_svc":
        return ok, det
    body, _ = await get_pr(pr_number=pr_number, ticket_id=f"{ticket_id}-dup", document_type="YSER")
    if not body:
        return ok, det
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
    if len(ui.get("lines") or []) != 1:
        return ok, det
    got_ccs = {
        _norm(a.get("cost_center"))
        for a in (ui["lines"][0].get("allocations") or [])
        if isinstance(a, dict) and _norm(a.get("cost_center"))
    }
    exp_ccs = {
        _norm(a.get("cost_center"))
        for line in form.get("lines") or []
        if isinstance(line, dict)
        for a in line.get("allocations") or []
        if isinstance(a, dict) and _norm(a.get("cost_center"))
    }
    if got_ccs == exp_ccs:
        return True, (
            "SAP merged duplicate performer → 1 line / multi-CC "
            f"(create sent {len(form.get('lines') or [])} services)"
        )
    return ok, det


async def _verify(
    *,
    pr_number: str,
    form: dict[str, Any],
    ticket_id: str,
    expect_lines: int | None,
) -> tuple[bool, str]:
    body, err = await get_pr(
        pr_number=pr_number, ticket_id=f"{ticket_id}-get", document_type="YSER"
    )
    if err or not body:
        return False, err or "GET failed"
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=form)
    mm = verify_yser_read_matches_form(body, form=form)
    n = len(ui.get("lines") or [])
    if expect_lines is not None and n != expect_lines:
        mm = [f"lines {n} != expected {expect_lines}"] + mm
    shape = _sap_shape_summary(body)
    if mm:
        return False, "; ".join(mm[:4]) + f" | SAP: {shape}"
    return True, f"lines={n} items={count_z_pr_sap_items(body)} | SAP: {shape}"


def _apply_update_edits(form: dict[str, Any], *, case_id: str) -> None:
    lines = form.get("lines") or []
    if not lines or not isinstance(lines[0], dict):
        return
    lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " upd")[:40]
    lines[0]["delivery_date"] = "2026-11-20"
    if case_id in ("yser_multi_cc", "yser_complex"):
        lines[0]["unit_price"] = "120"
        lines[0]["valuation_price"] = "240"
    if case_id == "yser_two_service" and len(lines) > 1:
        lines[1]["short_text"] = (str(lines[1].get("short_text") or "Svc") + " B upd")[:40]


def _pick_grouped_delete_form(form: dict[str, Any]) -> dict[str, Any]:
    """Keep one service line for grouped delete.

    When both single-CC and multi-CC siblings exist, keep the single-CC line so the
    e2e deletes the multi-CC service (reliable ``IsDeleted`` path). Deleting the
    single-CC sibling while multi remains uses ``IsAcctAssgmtDeleted`` in the planner.
    """
    out = copy.deepcopy(form)
    lines = [l for l in (out.get("lines") or []) if isinstance(l, dict)]
    if len(lines) < 2:
        out["lines"] = lines[:1]
        return out

    def _cc_count(line: dict[str, Any]) -> int:
        return sum(
            1
            for a in (line.get("allocations") or [])
            if isinstance(a, dict) and (a.get("cost_center") or "").strip()
        )

    single = next((l for l in lines if _cc_count(l) <= 1), None)
    multi = next((l for l in lines if _cc_count(l) > 1), None)
    if single is not None and multi is not None:
        out["lines"] = [copy.deepcopy(single)]
    else:
        out["lines"] = [copy.deepcopy(lines[0])]
    return out


async def run_sap_case(case_id: str, builder: FormBuilder, *, expect_lines: int | None, del_mode: str) -> CaseOut:
    out = CaseOut(case_id=case_id, layer="sap")
    tid = str(uuid.uuid4())
    form = _prep(builder)
    expected = expect_lines or len(yser_effective_line_blocks(form))

    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr or err:
        out.steps.append(Step("create", "FAIL", (err or "")[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    ok, det = await _verify_create_with_duplicate_tolerance(
        case_id=case_id,
        pr_number=pr,
        form=form,
        ticket_id=tid,
        expect_lines=expected,
    )
    out.steps.append(Step("get_after_create", "OK" if ok else "FAIL", det))
    if not ok:
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out

    hydrated, herr = await _ui_hydrate(pr_number=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("ui_hydrate", "FAIL", (herr or "")[:200]))
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out
    out.steps.append(Step("ui_hydrate", "OK", f"lines={len(hydrated.get('lines') or [])}"))

    upd = copy.deepcopy(hydrated)
    _normalize_alloc_qty(upd)
    _apply_update_edits(upd, case_id=case_id)
    _, uerr = await update_pr(sap_id=pr, ticket_id=tid, form=upd, document_type="YSER")
    if uerr:
        out.steps.append(Step("update", "FAIL", uerr[:200]))
        await delete_yser_pr(sap_id=pr, ticket_id=tid)
        return out
    out.steps.append(Step("update", "OK", ""))

    ok2, det2 = await _verify(pr_number=pr, form=upd, ticket_id=tid, expect_lines=expected)
    out.steps.append(Step("get_after_update", "OK" if ok2 else "FAIL", det2))

    if del_mode == "legacy_item" and len(upd.get("lines") or []) >= 2:
        refreshed, _ = await _ui_hydrate(pr_number=pr, seed=upd, ticket_id=f"{tid}-del")
        del_form = copy.deepcopy(refreshed or upd)
        del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
        _normalize_alloc_qty(del_form)
        _, derr = await update_pr(sap_id=pr, ticket_id=tid, form=del_form, document_type="YSER")
        if derr:
            out.steps.append(Step("delete_service", "FAIL", derr[:200]))
        else:
            ok3, det3 = await _verify(
                pr_number=pr, form=del_form, ticket_id=tid, expect_lines=1
            )
            out.steps.append(Step("delete_service", "OK" if ok3 else "FAIL", det3))
    elif del_mode == "grouped_skip":
        refreshed, _ = await _ui_hydrate(pr_number=pr, seed=upd, ticket_id=f"{tid}-del")
        if refreshed and len(refreshed.get("lines") or []) >= 2:
            del_form = _pick_grouped_delete_form(refreshed)
            _normalize_alloc_qty(del_form)
            _, derr = await update_pr(sap_id=pr, ticket_id=tid, form=del_form, document_type="YSER")
            if derr:
                out.steps.append(Step("delete_service", "FAIL", derr[:200]))
            else:
                ok3, det3 = await _verify(
                    pr_number=pr, form=del_form, ticket_id=tid, expect_lines=1
                )
                out.steps.append(Step("delete_service", "OK" if ok3 else "FAIL", det3))
        else:
            out.steps.append(Step("delete_service", "SKIP", "need 2+ hydrated lines"))
    else:
        out.steps.append(Step("delete_service", "SKIP", "single line case"))

    ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
    out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


async def run_api_case(
    case_id: str, builder: FormBuilder, *, expect_lines: int | None, del_mode: str, token: str
) -> CaseOut:
    out = CaseOut(case_id=case_id, layer="api")
    form = _prep(builder)
    expected = expect_lines or len(yser_effective_line_blocks(form))

    ticket, err = await asyncio.to_thread(
        create_pr_via_api_result,
        access_token=token,
        document_type="YSER",
        form=form,
    )
    if err or not ticket:
        out.steps.append(Step("create", "FAIL", (err or "no ticket")[:200]))
        return out
    tid = str(ticket.get("id") or "")
    polled, perr = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    if not polled:
        out.steps.append(Step("create", "FAIL", perr or "sync timeout"))
        return out
    pr = str(polled.get("sap_id") or "")
    sync_err = str(((polled.get("sap_sync") or {}).get("last_error") or ""))
    if not pr:
        out.steps.append(Step("create", "FAIL", sync_err[:200]))
        return out
    out.pr_number = pr
    out.steps.append(Step("create", "OK", pr))

    ok, det = await _verify_create_with_duplicate_tolerance(
        case_id=case_id,
        pr_number=pr,
        form=form,
        ticket_id=tid,
        expect_lines=expected,
    )
    out.steps.append(Step("get_after_create", "OK" if ok else "FAIL", det))
    if not ok:
        return out

    ui_ticket = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    ui_lines = len((ui_ticket.get("form") or {}).get("lines") or [])
    src = ui_ticket.get("form_source", "?")
    out.steps.append(Step("api_get_hydrate", "OK" if ui_lines == expected else "FAIL", f"source={src} lines={ui_lines}"))

    hydrated, herr = await _ui_hydrate(pr_number=pr, seed=form, ticket_id=tid)
    if not hydrated:
        out.steps.append(Step("ui_hydrate", "FAIL", (herr or "")[:200]))
        return out
    upd = copy.deepcopy(hydrated)
    _normalize_alloc_qty(upd)
    _apply_update_edits(upd, case_id=case_id)

    fresh = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
    version = int(fresh.get("version") or 0)
    patched, perr = await asyncio.to_thread(
        patch_resync_via_api_result,
        access_token=token,
        ticket_id=tid,
        form=upd,
        version=version,
    )
    if perr or not patched:
        out.steps.append(Step("update", "FAIL", (perr or "patch failed")[:200]))
        return out
    polled2, _ = await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
    sync_err2 = str((((polled2 or {}).get("sap_sync") or {}).get("last_error") or ""))
    if sync_err2:
        out.steps.append(Step("update", "FAIL", sync_err2[:200]))
        return out
    out.steps.append(Step("update", "OK", ""))

    ok2, det2 = await _verify(pr_number=pr, form=upd, ticket_id=tid, expect_lines=expected)
    out.steps.append(Step("get_after_update", "OK" if ok2 else "FAIL", det2))

    if del_mode == "legacy_item" and len(upd.get("lines") or []) >= 2:
        refreshed, _ = await _ui_hydrate(pr_number=pr, seed=upd, ticket_id=f"{tid}-del")
        del_form = copy.deepcopy(refreshed or upd)
        del_form["lines"] = [copy.deepcopy(del_form["lines"][0])]
        _normalize_alloc_qty(del_form)
        fresh2 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
        v2 = int(fresh2.get("version") or 0)
        _, derr = await asyncio.to_thread(
            patch_resync_via_api_result,
            access_token=token,
            ticket_id=tid,
            form=del_form,
            version=v2,
        )
        if derr:
            out.steps.append(Step("delete_service", "FAIL", derr[:200]))
        else:
            await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
            ok3, det3 = await _verify(pr_number=pr, form=del_form, ticket_id=tid, expect_lines=1)
            out.steps.append(Step("delete_service", "OK" if ok3 else "FAIL", det3))
    elif del_mode == "grouped_skip":
        refreshed, _ = await _ui_hydrate(pr_number=pr, seed=upd, ticket_id=f"{tid}-del")
        if refreshed and len(refreshed.get("lines") or []) >= 2:
            del_form = _pick_grouped_delete_form(refreshed)
            _normalize_alloc_qty(del_form)
            fresh2 = await asyncio.to_thread(get_ticket_via_api, access_token=token, ticket_id=tid)
            v2 = int(fresh2.get("version") or 0)
            _, derr = await asyncio.to_thread(
                patch_resync_via_api_result,
                access_token=token,
                ticket_id=tid,
                form=del_form,
                version=v2,
            )
            if derr:
                out.steps.append(Step("delete_service", "FAIL", derr[:200]))
            else:
                await asyncio.to_thread(poll_ticket_sap_sync, token, tid)
                ok3, det3 = await _verify(pr_number=pr, form=del_form, ticket_id=tid, expect_lines=1)
                out.steps.append(Step("delete_service", "OK" if ok3 else "FAIL", det3))
        else:
            out.steps.append(Step("delete_service", "SKIP", "need 2+ hydrated lines"))
    else:
        out.steps.append(Step("delete_service", "SKIP", "single line"))

    ok_del, del_err = await delete_yser_pr(sap_id=pr, ticket_id=tid)
    out.steps.append(Step("full_delete", "OK" if ok_del else "FAIL", (del_err or "")[:120]))
    return out


def _print_report(results: list[CaseOut]) -> int:
    fails = 0
    print("\n" + "=" * 72)
    print("YSER PR E2E SIGN-OFF")
    print("=" * 72)
    for r in results:
        mark = "PASS" if r.ok() else "FAIL"
        if not r.ok():
            fails += 1
        print(f"\n[{mark}] {r.case_id} ({r.layer}) PR={r.pr_number or '-'}")
        for s in r.steps:
            print(f"  {s.status:4} {s.name:22} {s.detail[:100]}")
    print("\n" + "=" * 72)
    print(f"TOTAL: {len(results) - fails}/{len(results)} passed")
    return 1 if fails else 0


async def main_async(args: argparse.Namespace, *, api_token: str | None = None) -> int:
    load_env()
    applied = apply_integration_reference_defaults()
    os.environ.setdefault("SAP_VENDOR", "0000008005")
    os.environ.setdefault("SAP_PUR_ORG", "1MGH")
    os.environ.setdefault("SAP_PLANT", "H002")
    os.environ.setdefault("SAP_SLOC", "1001")
    print(integration_defaults_summary(applied))

    if not sap_pr_configured():
        print("SAP not configured.", file=sys.stderr)
        return 2

    case_ids = [args.case] if args.case else list(CASES)
    layer_mode = args.layer.lower()
    run_sap = layer_mode in ("sap", "all")
    run_api = layer_mode in ("api", "all") and bool(api_token)
    if layer_mode in ("api", "all") and api_token:
        try:
            import httpx

            with httpx.Client(timeout=5.0) as c:
                c.get(f"{api_base()}/api/auth/me", headers={"Authorization": f"Bearer {api_token}"})
        except Exception as e:
            print(f"API layer skipped: {e}")
            run_api = False
    elif layer_mode in ("api", "all") and not api_token:
        print("API layer skipped (no token or server unreachable)")

    results: list[CaseOut] = []
    for cid in case_ids:
        if cid not in CASES:
            print(f"Unknown case {cid}", file=sys.stderr)
            return 2
        builder, expect_lines, del_mode = CASES[cid]
        if run_sap:
            results.append(await run_sap_case(cid, builder, expect_lines=expect_lines, del_mode=del_mode))
        if run_api:
            results.append(
                await run_api_case(
                    cid, builder, expect_lines=expect_lines, del_mode=del_mode, token=api_token or ""
                )
            )

    return _print_report(results)


def main() -> int:
    p = argparse.ArgumentParser(description="YSER PR E2E sign-off")
    add_common_args(p)
    p.add_argument("--layer", choices=["sap", "api", "all"], default="sap")
    p.add_argument("--case", help="Run one case id")
    args = p.parse_args()
    api_token: str | None = None
    if args.layer in ("api", "all"):
        try:
            api_token = resolve_token_from_args(args)
        except SystemExit as e:
            print(f"API auth skipped: {e}")
    return asyncio.run(main_async(args, api_token=api_token))


if __name__ == "__main__":
    raise SystemExit(main())
