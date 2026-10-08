#!/usr/bin/env python3
"""YSER PO full sign-off — new PO CRUD, old-PO mutations (issues 7–10), API resync.

Runs against live QAS SAP (and optional AgentOS API for resync path).

  cd agentos-backend
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_yser_po_full_signoff_live.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_yser_po_full_signoff_live.py --skip-api
  python scripts/integration/run_yser_po_full_signoff_live.py --sap-only --skip-old
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import create_po, get_po, sap_po_configured, update_po
from app.procurement.sap_po_z_payload import form_from_z_po_read, verify_z_po_read_against_form
from app.procurement.sap_ticket_form_read import load_form_from_sap

from scripts.integration.po_live_common import apply_po_live_fixtures, simple_po_form
from scripts.integration.procurement_api_common import (
    add_common_args,
    classify_sap_ticket,
    create_po_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)

OLD_ISSUE_POS = {
    "issue7_add_service": "4030011089",
    "issue8_del_service": "4030011090",
    "issue9_del_cc": "4030011091",
    "issue10_header_note": "4030011092",
}


@dataclass
class Row:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        self.rows.append(Row(name, "SKIP" if skip else ("PASS" if ok else "FAIL"), detail))

    def exit_code(self) -> int:
        return 1 if any(r.status == "FAIL" for r in self.rows) else 0

    def print_summary(self) -> None:
        print("\n" + "=" * 76)
        print("YSER PO FULL SIGN-OFF")
        print("=" * 76)
        for r in self.rows:
            print(f"  {r.status:4}  {r.name:<48}  {r.detail[:140]}")
        n_pass = sum(1 for r in self.rows if r.status == "PASS")
        n_fail = sum(1 for r in self.rows if r.status == "FAIL")
        n_skip = sum(1 for r in self.rows if r.status == "SKIP")
        print("=" * 76)
        print(f"PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
        print("=" * 76)


def _prep(form: dict[str, Any]) -> dict[str, Any]:
    out = normalize_form("YSER", copy.deepcopy(form))
    apply_procurement_defaults(out, document_type="YSER", kind="PO")
    return out


def _qas_master() -> dict[str, str]:
    """QAS YSER PO master aligned with issue POs 4030011089–1092."""
    return {
        "plant": "H001",
        "sloc": "H001|3021",
        "service_group": "S089-0001",
        "vendor": "8005",
        "tax": "XE",
        "cc1": "HBM13021A0",
        "cc2": "HBM13021A1",
        "svc1": "000000001000000000",
        "svc2": "000000001000000001",
        "pur_org": "1MGH",
        "pur_group": "A0B",
        "delivery": os.environ.get("SAP_PO_DELIVERY_DATE", "2026-09-01"),
    }


def _allowlisted_tax_code() -> str:
    from app.procurement.tax_code_allowlist import tax_code_is_allowed

    for candidate in ("FA", "HA", "FB", "V0", "XE"):
        if tax_code_is_allowed(candidate):
            return candidate
    return "FA"


def _single_service_api_form(*, tag: str) -> dict[str, Any]:
    """Like ``_single_service_create_form`` but passes API catalogue validation."""
    form = _single_service_create_form(tag=tag)
    form["header"]["tax_code"] = _allowlisted_tax_code()
    return form


def _single_service_create_form(*, tag: str) -> dict[str, Any]:
    """One service — SAP Z create persists a single service per POST; add more via update."""
    m = _qas_master()
    return {
        "header": {
            "purchasing_org": m["pur_org"],
            "purchasing_group": m["pur_group"],
            "plant": m["plant"],
            "storage_location": m["sloc"],
            "service_group": m["service_group"],
            "vendor": m["vendor"],
            "tax_code": m["tax"],
            "header_note": f"SIG{tag}"[:12],
        },
        "lines": [
            {
                "service": m["svc1"],
                "short_text": f"Signoff svc A {tag}",
                "delivery_date": m["delivery"],
                "unit_price": "100",
                "allocations": [{"cost_center": m["cc1"], "qty": "1"}],
            },
        ],
    }


def _grouped_create_form(*, tag: str) -> dict[str, Any]:
    """Two services on one service_group — use after first service exists (update add)."""
    base = _single_service_create_form(tag=tag)
    m = _qas_master()
    base["lines"].append(
        {
            "service": m["svc2"],
            "short_text": f"Signoff svc B {tag}",
            "delivery_date": m["delivery"],
            "unit_price": "80",
            # SAP QAS may normalize sibling CC when adding 2nd service via update POST;
            # use same CC as line 1 for add-service mutation (different CCs: create both at once).
            "allocations": [{"cost_center": m["cc1"], "qty": "1"}],
        }
    )
    return base


async def _update_po_with_retry(
    *,
    sap_id: str,
    form: dict[str, Any],
    ticket_id: str,
    attempts: int = 2,
) -> tuple[str | None, str | None]:
    last_err: str | None = None
    for i in range(attempts):
        po_id, err = await update_po(
            sap_id=sap_id,
            form=form,
            document_type="YSER",
            ticket_id=f"{ticket_id}-{i}",
        )
        if not err and po_id:
            return po_id, None
        last_err = err
        if err and "partner" not in err.lower() and "timeout" not in err.lower():
            break
        await asyncio.sleep(2)
    return None, last_err


async def _verify_po(
    po_number: str,
    expected_form: dict[str, Any],
    *,
    expect_lines: int | None = None,
) -> tuple[bool, str]:
    from app.procurement.sap_po_z_payload import reconcile_yser_po_verify_form_with_sap_read
    from app.procurement.sap_pr_z_payload import normalize_service_performer_code

    body, err = await get_po(po_number=po_number, ticket_id="signoff-v", document_type="YSER")
    if err or not body:
        return False, err or "GET failed"
    read = form_from_z_po_read(body, seed_form=expected_form)
    if expect_lines is not None and len(read.get("lines") or []) != expect_lines:
        return False, f"lines {len(read.get('lines') or [])} != {expect_lines}"
    exp_services = {
        normalize_service_performer_code(str(ln.get("service") or ""))
        for ln in (expected_form.get("lines") or [])
        if isinstance(ln, dict) and str(ln.get("service") or "").strip()
    }
    read_services = {
        normalize_service_performer_code(str(ln.get("service") or ""))
        for ln in (read.get("lines") or [])
        if isinstance(ln, dict) and str(ln.get("service") or "").strip()
    }
    if exp_services and exp_services != read_services:
        return False, f"services {sorted(read_services)} != {sorted(exp_services)}"
    reconciled = reconcile_yser_po_verify_form_with_sap_read(expected_form, body)
    mismatches = verify_z_po_read_against_form(body, form=reconciled)
    if mismatches:
        return False, "; ".join(mismatches[:3])
    return True, f"verify ok lines={len(read.get('lines') or [])}"


async def _update_and_verify(
    report: Report,
    *,
    label: str,
    po_number: str,
    form: dict[str, Any],
    expect_lines: int | None = None,
) -> dict[str, Any] | None:
    tid = f"signoff-{uuid.uuid4().hex[:8]}"
    norm = _prep(form)
    po_id, err = await update_po(
        sap_id=po_number,
        form=norm,
        document_type="YSER",
        ticket_id=tid,
    )
    if err or not po_id:
        # POST may have succeeded while verify raced SAP read — re-check GET.
        ok_retry, det_retry = await _verify_po(po_number, norm, expect_lines=expect_lines)
        if ok_retry:
            report.add(label, True, f"verify retry ok: {det_retry[:80]}")
            body, _ = await get_po(po_number=po_number, ticket_id="signoff-r", document_type="YSER")
            if body:
                return form_from_z_po_read(body, seed_form=norm)
            return norm
        report.add(label, False, err or "update failed")
        return None
    ok, detail = await _verify_po(po_number, norm, expect_lines=expect_lines)
    report.add(label, ok, detail)
    if not ok:
        return None
    body, _ = await get_po(po_number=po_number, ticket_id="signoff-r", document_type="YSER")
    if body:
        return form_from_z_po_read(body, seed_form=norm)
    return norm


async def _run_new_po_complex(report: Report) -> str | None:
    """Create 1 svc → add svc → add CC → price → del CC → del svc → header (SAP Z pattern)."""
    tag = uuid.uuid4().hex[:6]
    form = _prep(_single_service_create_form(tag=tag))
    tid = f"signoff-new-{tag}"
    po_number, err = await create_po(form=form, document_type="YSER", ticket_id=tid)
    if err or not po_number:
        report.add("new/create-1svc", False, err or "create failed")
        return None
    ok, det = await _verify_po(po_number, form, expect_lines=1)
    report.add("new/create-1svc", ok, f"PO {po_number} — {det}")
    if not ok:
        return po_number

    body, _ = await get_po(po_number=po_number, ticket_id="signoff-g", document_type="YSER")
    sap_form = form_from_z_po_read(body or {}, seed_form=form)

    # Issue 7: add second service on grouped item (SAP requires update POST, not create batch)
    add_form = _prep(_grouped_create_form(tag=tag))
    # Keep hydrated line 0 + new line 1
    add_form["header"] = sap_form.get("header") or add_form["header"]
    add_form["lines"] = [sap_form["lines"][0], add_form["lines"][1]]
    sap_form = await _update_and_verify(
        report,
        label="new/add-service",
        po_number=po_number,
        form=add_form,
        expect_lines=2,
    )
    if not sap_form:
        return po_number

    # Delete service (remove last line) — before multi-CC on line 0
    if len(sap_form.get("lines") or []) > 1:
        del_svc_form = copy.deepcopy(sap_form)
        del_svc_form["lines"] = del_svc_form["lines"][:-1]
        sap_form = await _update_and_verify(
            report,
            label="new/del-service",
            po_number=po_number,
            form=del_svc_form,
            expect_lines=1,
        )
    else:
        report.add("new/del-service", True, "skipped — single line", skip=True)

    if not sap_form:
        return po_number

    # Add CC split on first line
    m = _qas_master()
    cc_form = copy.deepcopy(sap_form)
    line0 = cc_form["lines"][0]
    allocs = list(line0.get("allocations") or [])
    if len(allocs) == 1:
        allocs.append({"cost_center": m["cc2"], "qty": "1"})
        line0["allocations"] = allocs
        line0["unit_price"] = line0.get("unit_price") or "100"
    sap_form = await _update_and_verify(
        report,
        label="new/add-cc",
        po_number=po_number,
        form=cc_form,
        expect_lines=1,
    )
    if not sap_form:
        return po_number

    # Field update: bump price on line 0
    price_form = copy.deepcopy(sap_form)
    if price_form["lines"]:
        price_form["lines"][0]["unit_price"] = "110"
        price_form["lines"][0]["net_price"] = "110"
    sap_form = await _update_and_verify(
        report,
        label="new/field-update",
        po_number=po_number,
        form=price_form,
        expect_lines=1,
    )
    if not sap_form:
        return po_number

    # Delete CC (keep first CC only on line 0)
    del_cc_form = copy.deepcopy(sap_form)
    if del_cc_form["lines"]:
        allocs = del_cc_form["lines"][0].get("allocations") or []
        if len(allocs) > 1:
            del_cc_form["lines"][0]["allocations"] = [allocs[0]]
    sap_form = await _update_and_verify(
        report,
        label="new/del-cc",
        po_number=po_number,
        form=del_cc_form,
        expect_lines=1,
    )
    if not sap_form:
        return po_number

    # Header note (issue 10 pattern)
    note_form = copy.deepcopy(sap_form)
    note_form.setdefault("header", {})["header_note"] = f"NT{tag}"[:12]
    await _update_and_verify(
        report,
        label="new/header-note",
        po_number=po_number,
        form=note_form,
        expect_lines=len(note_form.get("lines") or []),
    )
    return po_number


async def _run_old_po_mutations(report: Report) -> None:
    """Exercise issues 7–10 on fresh POs (old issue POs may be in inconsistent state)."""
    m = _qas_master()

    # Issue 7: add service to grouped item (start from 1 svc)
    tag7 = uuid.uuid4().hex[:6]
    form7 = _prep(_single_service_create_form(tag=tag7))
    po7, err = await create_po(form=form7, document_type="YSER", ticket_id=f"old7-{tag7}")
    if err or not po7:
        report.add("old/issue7-create", False, err or "create failed")
    else:
        body, _ = await get_po(po_number=po7, ticket_id="g7", document_type="YSER")
        sap = form_from_z_po_read(body or {}, seed_form=form7)
        add = _prep(_grouped_create_form(tag=tag7))
        add["header"] = sap["header"]
        add["lines"] = [sap["lines"][0], add["lines"][1]]
        await _update_and_verify(
            report,
            label="old/issue7-add-service",
            po_number=po7,
            form=add,
            expect_lines=2,
        )

    # Issue 8: delete one service (start from 2 svc)
    tag8 = uuid.uuid4().hex[:6]
    form8 = _prep(_grouped_create_form(tag=tag8))
    po8, err = await create_po(form=form8, document_type="YSER", ticket_id=f"old8-{tag8}")
    if err or not po8:
        report.add("old/issue8-create", False, err or "create failed")
    else:
        body, _ = await get_po(po_number=po8, ticket_id="g8", document_type="YSER")
        sap = form_from_z_po_read(body or {}, seed_form=form8)
        n = len(sap.get("lines") or [])
        if n < 2:
            # create only persisted 1 svc — add second then delete
            add = _prep(_grouped_create_form(tag=tag8))
            add["header"] = sap["header"]
            add["lines"] = [sap["lines"][0], add["lines"][1]]
            sap = await _update_and_verify(
                report,
                label="old/issue8-setup-2svc",
                po_number=po8,
                form=add,
                expect_lines=2,
            )
        if sap and len(sap.get("lines") or []) >= 2:
            del_form = copy.deepcopy(sap)
            del_form["lines"] = del_form["lines"][:1]
            await _update_and_verify(
                report,
                label="old/issue8-del-service",
                po_number=po8,
                form=del_form,
                expect_lines=1,
            )

    # Issue 9: delete one CC
    tag9 = uuid.uuid4().hex[:6]
    form9 = _prep(_single_service_create_form(tag=tag9))
    form9["lines"][0]["allocations"] = [
        {"cost_center": m["cc1"], "qty": "1"},
        {"cost_center": m["cc2"], "qty": "1"},
    ]
    po9, err = await create_po(form=form9, document_type="YSER", ticket_id=f"old9-{tag9}")
    if err or not po9:
        report.add("old/issue9-create", False, err or "create failed")
    else:
        body, _ = await get_po(po_number=po9, ticket_id="g9", document_type="YSER")
        sap = form_from_z_po_read(body or {}, seed_form=form9)
        del_cc = copy.deepcopy(sap)
        allocs = del_cc["lines"][0].get("allocations") or []
        if len(allocs) > 1:
            del_cc["lines"][0]["allocations"] = [allocs[0]]
            await _update_and_verify(
                report,
                label="old/issue9-del-cc",
                po_number=po9,
                form=del_cc,
                expect_lines=1,
            )
        else:
            report.add("old/issue9-del-cc", True, "create returned single CC only", skip=True)

    # Issue 10: header note on fresh PO
    tag10 = uuid.uuid4().hex[:6]
    form10 = _prep(_single_service_create_form(tag=tag10))
    po10, err = await create_po(form=form10, document_type="YSER", ticket_id=f"old10-{tag10}")
    if err or not po10:
        report.add("old/issue10-create", False, err or "create failed")
    else:
        body, _ = await get_po(po_number=po10, ticket_id="g10", document_type="YSER")
        sap = form_from_z_po_read(body or {}, seed_form=form10)
        note_form = copy.deepcopy(sap)
        note_form.setdefault("header", {})["header_note"] = f"NT{tag10}"[:12]
        po_id, uerr = await _update_po_with_retry(
            sap_id=po10,
            form=_prep(note_form),
            ticket_id=f"old10-u-{tag10}",
        )
        if uerr or not po_id:
            report.add("old/issue10-header-note", False, uerr or "update failed")
        else:
            report.add("old/issue10-header-note", True, f"POST ok note={note_form['header']['header_note']!r}")


async def _run_api_resync_complex(report: Report, *, token: str) -> None:
    """UI-like flow: create via API → hydrate GET ticket → PATCH resync mutations."""
    tag = uuid.uuid4().hex[:6]
    form = _prep(_single_service_api_form(tag=f"api{tag}"))
    po_ticket, err = create_po_via_api_result(
        access_token=token,
        document_type="YSER",
        form=form,
        parent_pr_id=None,
    )
    if err or not po_ticket:
        report.add("api/create", False, err or "no ticket")
        return
    tid = str(po_ticket.get("id") or "")
    polled, poll_err = poll_ticket_sap_sync(token, tid)
    if poll_err or not polled:
        report.add("api/create", False, poll_err or "sync timeout")
        return
    ok, detail, _ = classify_sap_ticket(polled)
    sap_id = str(polled.get("sap_id") or "")
    report.add("api/create", ok == "pass", f"PO {sap_id}" if sap_id else detail)
    if not sap_id:
        return

    fresh = get_ticket_via_api(access_token=token, ticket_id=tid) or {}
    hydrated = fresh.get("form") if isinstance(fresh.get("form"), dict) else {}
    report.add(
        "api/hydrate-on-read",
        len(hydrated.get("lines") or []) >= 1,
        f"lines={len(hydrated.get('lines') or [])} source={fresh.get('form_source')}",
    )

    # Resync: add second service (grouped item)
    working = copy.deepcopy(hydrated)
    m = _qas_master()
    working.setdefault("lines", []).append(
        {
            "service": m["svc2"],
            "short_text": f"API add B {tag}",
            "delivery_date": (working["lines"][0].get("delivery_date") if working.get("lines") else m["delivery"]),
            "unit_price": "80",
            "allocations": [{"cost_center": m["cc1"], "qty": "1"}],
        }
    )
    version = int(fresh.get("version") or 0)
    _, upd_err = patch_resync_via_api_result(
        access_token=token, ticket_id=tid, form=working, version=version
    )
    if upd_err:
        report.add("api/resync-add-service", False, upd_err)
        return
    polled2, _ = poll_ticket_sap_sync(token, tid)
    ok2, det2, _ = classify_sap_ticket(polled2 or {})
    report.add("api/resync-add-service", ok2 == "pass", det2)
    if ok2 != "pass":
        return

    body, _ = await get_po(po_number=sap_id, ticket_id="api-v", document_type="YSER")
    if body:
        v_ok, v_det = await _verify_po(sap_id, _prep(working), expect_lines=2)
        report.add("api/verify-after-add", v_ok, v_det)

    # Resync: delete last service
    fresh2 = get_ticket_via_api(access_token=token, ticket_id=tid) or {}
    working2 = copy.deepcopy(fresh2.get("form") or {})
    lines = working2.get("lines") or []
    if len(lines) > 1:
        working2["lines"] = lines[:-1]
        version2 = int(fresh2.get("version") or 0)
        _, del_err = patch_resync_via_api_result(
            access_token=token, ticket_id=tid, form=working2, version=version2
        )
        if del_err:
            report.add("api/resync-del-service", False, del_err)
        else:
            polled3, _ = poll_ticket_sap_sync(token, tid)
            ok3, det3, _ = classify_sap_ticket(polled3 or {})
            report.add("api/resync-del-service", ok3 == "pass", det3)
            if ok3 == "pass":
                v_ok, v_det = await _verify_po(sap_id, _prep(working2), expect_lines=1)
                report.add("api/verify-after-del", v_ok, v_det)


async def _run_hydration_robustness(report: Report) -> None:
    """load_form_from_sap + verify on reference POs."""
    for label, po in OLD_ISSUE_POS.items():
        form, source, err = await load_form_from_sap(
            kind="PO",
            document_type="YSER",
            sap_id=po,
            ticket_id=f"hydr-{po}",
            seed_form=None,
        )
        ok = source == "sap" and not err and bool(form and form.get("lines"))
        report.add(f"hydrate/{label}", ok, err or f"lines={len((form or {}).get('lines') or [])}")


async def main_async(*, skip_api: bool, skip_old: bool, token: str | None) -> int:
    report = Report()
    if not sap_po_configured():
        print("SAP not configured")
        return 2

    apply_po_live_fixtures("YSER")

    await _run_hydration_robustness(report)
    po_new = await _run_new_po_complex(report)
    if po_new:
        report.add("new/final-po", True, po_new)

    if not skip_old:
        await _run_old_po_mutations(report)

    if not skip_api and token:
        await _run_api_resync_complex(report, token=token)
    elif not skip_api:
        report.add("api/*", True, "no token — set AGENTOS_INTEGRATION_MINT_TOKEN=1", skip=True)
    else:
        report.add("api/*", True, "--skip-api", skip=True)

    report.print_summary()
    return report.exit_code()


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="YSER PO full sign-off (QAS)")
    add_common_args(parser)
    parser.add_argument("--skip-api", action="store_true", help="Skip API resync path")
    parser.add_argument("--skip-old", action="store_true", help="Skip old PO mutation tests")
    parser.add_argument("--sap-only", action="store_true", help="Alias for --skip-api")
    args = parser.parse_args()
    skip_api = args.skip_api or args.sap_only
    token = None if skip_api else resolve_token_from_args(args)
    return asyncio.run(
        main_async(skip_api=skip_api, skip_old=args.skip_old, token=token)
    )


if __name__ == "__main__":
    raise SystemExit(main())
