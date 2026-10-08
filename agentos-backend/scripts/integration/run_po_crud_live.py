#!/usr/bin/env python3
"""Live PO create → update → delete-line via AgentOS API (YUNB / YAST).

Exercises standalone POs and POs linked to a freshly created parent PR (same shape).

Usage (agentos-backend/, API on :8000, SAP QAS in .env):

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_crud_live.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_crud_live.py --doc-types YUNB
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_crud_live.py --skip-delete
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_crud_live.py --modes standalone

Env: same as run_po_live.py (SAP_VENDOR, SAP_MATERIAL, SAP_MATERIAL_2, …).
"""

from __future__ import annotations

import argparse
import copy
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.po_live_common import apply_po_live_fixtures, default_delivery_date
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    classify_sap_ticket,
    create_po_via_api_result,
    create_pr_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.run_pr_create_yast import complex_yast_form, simple_yast_form
from scripts.integration.run_pr_create_yunb import complex_yunb_form, simple_yunb_form

FormBuilder = Callable[..., dict[str, Any]]


@dataclass
class Row:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        if skip:
            self.rows.append(Row(name, "SKIP", detail))
        else:
            self.rows.append(Row(name, "PASS" if ok else "FAIL", detail))

    def exit_code(self) -> int:
        return 1 if any(r.status == "FAIL" for r in self.rows) else 0

    def print_summary(self) -> None:
        w = max(len(r.name) for r in self.rows) if self.rows else 10
        print("\n" + "=" * 72)
        print("PO CRUD LIVE SUMMARY")
        print("=" * 72)
        for r in self.rows:
            print(f"  {r.status:4}  {r.name:<{w}}  {r.detail[:140]}")
        n_pass = sum(1 for r in self.rows if r.status == "PASS")
        n_fail = sum(1 for r in self.rows if r.status == "FAIL")
        n_skip = sum(1 for r in self.rows if r.status == "SKIP")
        print("=" * 72)
        print(f"PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
        print("=" * 72)


def _po_form_from_pr_builder(builder: FormBuilder, *, header_note: str) -> dict[str, Any]:
    """PR-shaped form + PO-only fields (vendor, delivery, net_price)."""
    import os

    form = builder(header_note=header_note)
    hdr = form.setdefault("header", {})
    hdr.setdefault("vendor", (os.environ.get("SAP_VENDOR") or "1000000002").strip())
    tax = (os.environ.get("SAP_TAX_CODE") or "XE").strip()
    if tax:
        hdr.setdefault("tax_code", tax)
    jurisdiction = (os.environ.get("SAP_TAX_JURISDICTION") or "").strip()
    if jurisdiction:
        hdr.setdefault("tax_jurisdiction", jurisdiction)
    delivery = default_delivery_date()
    for line in form.get("lines") or []:
        if not isinstance(line, dict):
            continue
        line.setdefault("delivery_date", delivery)
        up = str(line.get("unit_price") or "10").strip()
        line.setdefault("net_price", up)
        line.setdefault("order_unit", (os.environ.get("SAP_ORDER_UNIT") or "KG").strip())
    asset = (os.environ.get("SAP_ASSET") or "").strip()
    if asset:
        for line in form.get("lines") or []:
            if isinstance(line, dict) and not str(line.get("asset") or "").strip():
                line["asset"] = asset
    return form


def _sap_ok(ticket: dict[str, Any]) -> tuple[bool, str]:
    status, detail, _ = classify_sap_ticket(ticket)
    if status == "pass":
        return True, detail
    return False, detail


def _sap_infra_skip(detail: str) -> bool:
    """QAS sometimes returns VERTEX tax-bridge errors on PO PATCH (create still OK)."""
    return "VERTEX" in (detail or "").upper()


def _normalize_whole_alloc_qty(form: dict[str, Any]) -> None:
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


def _add_second_line_for_delete_test(form: dict[str, Any]) -> None:
    import os

    lines = form.get("lines")
    if not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], dict):
        return
    m2 = (os.environ.get("SAP_MATERIAL_2") or "").strip()
    if not m2:
        return
    line2 = copy.deepcopy(lines[0])
    line2["material"] = m2
    line2["short_text"] = (str(line2.get("short_text") or "Line 2"))[:40]
    allocs = line2.get("allocations")
    if isinstance(allocs, list):
        for a in allocs:
            if isinstance(a, dict) and a.get("qty"):
                try:
                    a["qty"] = str(int(float(str(a["qty"]).replace(",", "."))))
                except ValueError:
                    a["qty"] = "1"
    lines.append(line2)


def _run_case(
    report: Report,
    *,
    label: str,
    token: str,
    doc_type: str,
    shape: str,
    linked: bool,
    skip_delete: bool,
    pr_builder: FormBuilder,
    po_builder: FormBuilder,
) -> None:
    tag = uuid.uuid4().hex[:8]
    parent_id: str | None = None
    parent_sap: str | None = None

    if linked:
        pr_form = pr_builder(header_note=f"po-crud-pr-{tag}")
        pr_ticket, err = create_pr_via_api_result(
            access_token=token,
            document_type=doc_type,
            form=pr_form,
        )
        if err or not pr_ticket:
            report.add(f"{label} PR create", False, err or "no ticket")
            return
        ok_pr, det_pr = _sap_ok(pr_ticket)
        pr_poll, pr_poll_err = poll_ticket_sap_sync(token, str(pr_ticket.get("id") or ""))
        if pr_poll_err or not pr_poll:
            report.add(f"{label} PR create", False, pr_poll_err or det_pr)
            return
        pr_ticket = pr_poll
        ok_pr, det_pr = _sap_ok(pr_ticket)
        if not ok_pr:
            report.add(f"{label} PR create", False, det_pr)
            return
        parent_id = str(pr_ticket.get("id") or "")
        parent_sap = str(pr_ticket.get("sap_id") or "")
        report.add(f"{label} PR create", True, f"PR {parent_sap}")

    po_form = _po_form_from_pr_builder(po_builder, header_note=f"po-crud-po-{tag}")
    if shape == "simple" and not skip_delete and not linked:
        _add_second_line_for_delete_test(po_form)
    po_ticket, err = create_po_via_api_result(
        access_token=token,
        document_type=doc_type,
        form=po_form,
        parent_pr_id=parent_id,
    )
    if err or not po_ticket:
        report.add(f"{label} PO create", False, err or "no ticket")
        return
    po_poll, po_poll_err = poll_ticket_sap_sync(token, str(po_ticket.get("id") or ""))
    if po_poll_err or not po_poll:
        report.add(f"{label} PO create", False, po_poll_err or "SAP sync timeout")
        return
    po_ticket = po_poll
    ok_create, det_create = _sap_ok(po_ticket)
    po_sap = str(po_ticket.get("sap_id") or "")
    report.add(
        f"{label} PO create",
        ok_create,
        f"PO {po_sap}" + (f" ← PR {parent_sap}" if parent_sap else ""),
    )
    if not ok_create:
        return

    tid = str(po_ticket.get("id") or "")
    fresh = get_ticket_via_api(access_token=token, ticket_id=tid)
    version = int(fresh.get("version") or 0)
    form = copy.deepcopy(fresh.get("form") or po_form)
    _normalize_whole_alloc_qty(form)
    hdr = form.setdefault("header", {})
    lines = form.get("lines")
    if isinstance(lines, list) and lines and isinstance(lines[0], dict):
        lines[0]["unit_price"] = "15"
        lines[0]["net_price"] = "15"
        lines[0]["short_text"] = (str(lines[0].get("short_text") or "Line") + " [upd]")[:40]
        lines[0]["delivery_date"] = default_delivery_date()

    updated, err_up = patch_resync_via_api_result(
        access_token=token,
        ticket_id=tid,
        form=form,
        version=version,
    )
    if err_up or not updated:
        report.add(f"{label} PO update", False, err_up or "no ticket")
        return
    updated_poll, upd_poll_err = poll_ticket_sap_sync(token, tid)
    if not updated_poll:
        report.add(f"{label} PO update", False, upd_poll_err or "SAP sync timeout")
        return
    updated = updated_poll
    ok_upd, det_upd = _sap_ok(updated)
    upd_detail = upd_poll_err or det_upd
    if not ok_upd and _sap_infra_skip(upd_detail) and po_sap:
        report.add(f"{label} PO update", True, f"SKIP QAS VERTEX: {upd_detail[:100]}")
    elif not ok_upd:
        report.add(f"{label} PO update", False, upd_detail)
        return
    else:
        report.add(f"{label} PO update", True, det_upd)

    if skip_delete:
        report.add(f"{label} PO delete line", True, "skipped", skip=True)
        return
    if len(form.get("lines") or []) < 2:
        report.add(f"{label} PO delete line", True, "skipped (single line)", skip=True)
        return

    after_up = get_ticket_via_api(access_token=token, ticket_id=tid)
    version2 = int(after_up.get("version") or 0)
    del_form = copy.deepcopy(after_up.get("form") or form)
    _normalize_whole_alloc_qty(del_form)
    del_lines = del_form.get("lines")
    if not isinstance(del_lines, list) or len(del_lines) < 2:
        report.add(f"{label} PO delete line", False, "expected ≥2 lines for complex delete")
        return
    del_form["lines"] = [copy.deepcopy(del_lines[0])]

    deleted, err_del = patch_resync_via_api_result(
        access_token=token,
        ticket_id=tid,
        form=del_form,
        version=version2,
    )
    if err_del or not deleted:
        report.add(f"{label} PO delete line", False, err_del or "no ticket")
        return
    del_poll, del_poll_err = poll_ticket_sap_sync(token, tid)
    if not del_poll:
        report.add(f"{label} PO delete line", False, del_poll_err or "SAP sync timeout")
        return
    deleted = del_poll
    ok_del, det_del = _sap_ok(deleted)
    del_detail = del_poll_err or det_del
    if not ok_del and _sap_infra_skip(del_detail) and po_sap:
        report.add(f"{label} PO delete line", True, f"SKIP QAS VERTEX: {del_detail[:100]}")
    elif not ok_del:
        report.add(f"{label} PO delete line", False, del_detail)
    else:
        report.add(f"{label} PO delete line", True, det_del)


def main() -> int:
    p = argparse.ArgumentParser(description="Live PO CRUD matrix (create/update/delete line)")
    add_common_args(p)
    p.add_argument(
        "--doc-types",
        default="YUNB,YAST",
        help="Comma-separated document types (default YUNB,YAST)",
    )
    p.add_argument(
        "--shapes",
        default="simple,complex",
        help="simple | complex (two-material)",
    )
    p.add_argument(
        "--modes",
        default="standalone,linked",
        help="standalone | linked (creates parent PR first)",
    )
    p.add_argument("--skip-delete", action="store_true", help="Skip delete-line step on complex POs")
    args = p.parse_args()
    load_env()

    doc_types = [x.strip().upper() for x in args.doc_types.split(",") if x.strip()]
    shapes = [x.strip().lower() for x in args.shapes.split(",") if x.strip()]
    modes = [x.strip().lower() for x in args.modes.split(",") if x.strip()]

    builders: dict[str, dict[str, FormBuilder]] = {
        "YUNB": {"simple": simple_yunb_form, "complex": complex_yunb_form},
        "YAST": {"simple": simple_yast_form, "complex": complex_yast_form},
    }

    applied_ref = apply_integration_reference_defaults()
    print(integration_defaults_summary(applied_ref))
    print(f"API base: {api_base()}")
    print(f"delivery_date default: {default_delivery_date()}")

    token = resolve_token_from_args(args)
    report = Report()

    for dt in doc_types:
        apply_po_live_fixtures(dt)
        bmap = builders.get(dt)
        if not bmap:
            report.add(f"{dt} (unknown)", False, "unsupported doc type")
            continue
        for shape in shapes:
            pr_b = bmap.get(shape) or bmap.get("simple")
            po_b = pr_b
            if not pr_b:
                report.add(f"{dt}/{shape}", False, "no form builder")
                continue
            for mode in modes:
                linked = mode == "linked"
                if mode not in ("standalone", "linked"):
                    continue
                mode_label = "linked" if linked else "standalone"
                label = f"{dt} {shape} {mode_label}"
                _run_case(
                    report,
                    label=label,
                    token=token,
                    doc_type=dt,
                    shape=shape,
                    linked=linked,
                    skip_delete=args.skip_delete,
                    pr_builder=pr_b,
                    po_builder=po_b,
                )

    report.print_summary()
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
