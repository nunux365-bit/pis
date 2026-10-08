#!/usr/bin/env python3
"""Live YSER PO CRUD via AgentOS API — standalone + PR-linked, simple + multi-CC.

Usage (agentos-backend/, API + SAP in .env):

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_yser_po_crud_live.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_yser_po_crud_live.py --case standalone-simple
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

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_po_client import get_po, sap_po_configured
from app.procurement.sap_po_z_payload import form_from_z_po_read, normalize_service_performer_code

from scripts.integration.po_live_common import apply_po_live_fixtures, simple_po_form
from scripts.integration.procurement_api_common import (
    add_common_args,
    classify_sap_ticket,
    create_po_via_api_result,
    create_pr_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)


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
        print("\n" + "=" * 72)
        print("YSER PO CRUD LIVE SUMMARY")
        print("=" * 72)
        for r in self.rows:
            print(f"  {r.status:4}  {r.name:<40}  {r.detail[:120]}")
        n_pass = sum(1 for r in self.rows if r.status == "PASS")
        n_fail = sum(1 for r in self.rows if r.status == "FAIL")
        print("=" * 72)
        print(f"PASS={n_pass} FAIL={n_fail}")
        print("=" * 72)


def _complex_yser_form(*, header_note: str) -> dict[str, Any]:
    form = simple_po_form("YSER", header_note=header_note)
    cc1 = os.environ.get("SAP_COST_CENTER", "HCO91001H0")
    cc2 = os.environ.get("SAP_COST_CENTER_2", "HCO91001A0")
    delivery = form["lines"][0].get("delivery_date")
    form["lines"] = [
        {
            "service": os.environ.get("SAP_SERVICE", "10000000006"),
            "short_text": "YSER PO service A",
            "delivery_date": delivery,
            "unit_price": os.environ.get("SAP_UNIT_PRICE", "100"),
            "allocations": [
                {"cost_center": cc1, "qty": "1"},
                {"cost_center": cc2, "qty": "1"},
            ],
        },
        {
            "service": os.environ.get("SAP_SERVICE_2", "10000000007"),
            "short_text": "YSER PO service B",
            "delivery_date": delivery,
            "unit_price": os.environ.get("SAP_UNIT_PRICE_2", "50"),
            "allocations": [{"cost_center": cc1, "qty": "2"}],
        },
    ]
    return form


def _simple_yser_pr_form(*, header_note: str) -> dict[str, Any]:
    from scripts.integration.run_pr_create_yser import simple_yser_form

    return simple_yser_form(header_note=header_note)


def _sap_ok(ticket: dict[str, Any]) -> tuple[bool, str]:
    status, detail, _ = classify_sap_ticket(ticket)
    return status == "pass", detail


async def _verify_sap_get(po_number: str, *, expect_lines: int) -> tuple[bool, str]:
    body, err = await get_po(po_number=po_number, ticket_id="yser-verify", document_type="YSER")
    if err or not body:
        return False, err or "GET failed"
    form = form_from_z_po_read(body)
    lines = form.get("lines") or []
    if len(lines) != expect_lines:
        return False, f"expected {expect_lines} lines, got {len(lines)}"
    for line in lines:
        svc = normalize_service_performer_code(str(line.get("service") or ""))
        if not svc:
            return False, "missing service on read"
    return True, f"GET ok lines={len(lines)}"


def _create_expect_lines(form: dict[str, Any]) -> int:
    """After create + reconcile, SAP should persist every submitted service line."""
    n = len(form.get("lines") or [])
    return max(n, 1)


def _run_case(
    report: Report,
    *,
    token: str,
    case_id: str,
    form: dict[str, Any],
    parent_pr_id: str | None,
    skip_update: bool,
    skip_delete: bool,
    expect_create_blocked: bool = False,
) -> None:
    tag = uuid.uuid4().hex[:8]
    form = copy.deepcopy(form)
    form.setdefault("header", {})["header_note"] = f"Y{case_id[:6]}{tag[:4]}"[:12]

    po_ticket, err = create_po_via_api_result(
        access_token=token,
        document_type="YSER",
        form=form,
        parent_pr_id=parent_pr_id,
    )
    if err or not po_ticket:
        if expect_create_blocked and err and "Account assignment cannot be changed" in err:
            report.add(f"{case_id}/create-blocked", True, "PR-linked multi-CC rejected as expected")
        else:
            report.add(f"{case_id}/create", False, err or "no ticket")
        return
    tid = str(po_ticket.get("id") or "")
    po_poll, poll_err = poll_ticket_sap_sync(token, tid)
    if poll_err or not po_poll:
        report.add(f"{case_id}/create", False, poll_err or "sync timeout")
        return
    ok, detail = _sap_ok(po_poll)
    sap_id = str(po_poll.get("sap_id") or "")
    report.add(f"{case_id}/create", ok, f"PO {sap_id}" if ok else detail)
    if not ok or not sap_id:
        return

    get_ok, get_detail = asyncio.run(
        _verify_sap_get(sap_id, expect_lines=_create_expect_lines(form))
    )
    report.add(f"{case_id}/get", get_ok, get_detail)

    working_form = copy.deepcopy(form)
    if skip_update:
        report.add(f"{case_id}/update", True, "skipped", skip=True)
    else:
        working_form["lines"][0]["unit_price"] = str(
            int(float(str(working_form["lines"][0].get("unit_price") or "10"))) + 5
        )
        working_form["lines"][0]["net_price"] = working_form["lines"][0]["unit_price"]
        fresh = get_ticket_via_api(access_token=token, ticket_id=tid) or {}
        version = int(fresh.get("version") or 0)
        _, upd_err = patch_resync_via_api_result(
            access_token=token,
            ticket_id=tid,
            form=working_form,
            version=version,
        )
        if upd_err:
            report.add(f"{case_id}/update", False, upd_err)
        else:
            upd_poll, upd_poll_err = poll_ticket_sap_sync(token, tid)
            if upd_poll_err or not upd_poll:
                report.add(f"{case_id}/update", False, upd_poll_err or "sync timeout")
            else:
                ok_u, det_u = _sap_ok(upd_poll)
                report.add(f"{case_id}/update", ok_u, det_u)

    if skip_delete or len(form.get("lines") or []) < 2:
        report.add(f"{case_id}/delete-line", True, "skipped", skip=True)
        return

    del_form = copy.deepcopy(working_form)
    del_form["lines"] = del_form["lines"][:1]
    fresh = get_ticket_via_api(access_token=token, ticket_id=tid) or {}
    version = int(fresh.get("version") or 0)
    _, del_err = patch_resync_via_api_result(
        access_token=token,
        ticket_id=tid,
        form=del_form,
        version=version,
    )
    if del_err:
        report.add(f"{case_id}/delete-line", False, del_err)
        return
    del_poll, del_poll_err = poll_ticket_sap_sync(token, tid)
    if del_poll_err or not del_poll:
        report.add(f"{case_id}/delete-line", False, del_poll_err or "sync timeout")
        return
    ok_d, det_d = _sap_ok(del_poll)
    report.add(f"{case_id}/delete-line", ok_d, det_d)
    if ok_d:
        asyncio.run(_verify_sap_get(str(del_poll.get("sap_id") or ""), expect_lines=1))


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Live YSER PO CRUD matrix")
    add_common_args(parser)
    parser.add_argument("--case", default="", help="Run one case id")
    parser.add_argument("--skip-update", action="store_true")
    parser.add_argument("--skip-delete", action="store_true")
    args = parser.parse_args()

    if not sap_po_configured():
        print("SAP not configured — set PROCUREMENT_SAP_* in .env")
        return 2

    apply_po_live_fixtures("YSER")
    token = resolve_token_from_args(args)
    report = Report()

    cases: list[tuple[str, dict[str, Any], str | None]] = [
        ("standalone-simple", simple_po_form("YSER"), None),
        ("standalone-complex", _complex_yser_form(header_note="complex"), None),
    ]

    pr_note = f"YSER PO parent {uuid.uuid4().hex[:8]}"
    parent_pr_id: str | None = None
    if args.case in ("", "pr-linked-simple"):
        pr_ticket, pr_err = create_pr_via_api_result(
            access_token=token,
            document_type="YSER",
            form=_simple_yser_pr_form(header_note=pr_note[:12]),
        )
        if pr_err or not pr_ticket:
            report.add("pr-bootstrap", False, pr_err or "no PR ticket")
        else:
            pr_poll, pr_poll_err = poll_ticket_sap_sync(token, str(pr_ticket.get("id") or ""))
            if pr_poll_err or not pr_poll:
                report.add("pr-bootstrap", False, pr_poll_err or "PR sync timeout")
            else:
                ok_pr, det_pr = _sap_ok(pr_poll)
                report.add("pr-bootstrap", ok_pr, det_pr)
                if ok_pr:
                    parent_pr_id = str(pr_poll.get("id") or "")
                    po_simple = simple_po_form("YSER", header_note="from-pr-sim")
                    for line in po_simple.get("lines") or []:
                        if isinstance(line, dict):
                            line["purchase_requisition_item"] = "10"
                    cases.append(("pr-linked-simple", po_simple, parent_pr_id))

    if args.case in ("", "pr-linked-multi-cc"):
        pr_note2 = f"YSER parent2 {uuid.uuid4().hex[:6]}"[:12]
        pr_ticket2, pr_err2 = create_pr_via_api_result(
            access_token=token,
            document_type="YSER",
            form=_simple_yser_pr_form(header_note=pr_note2),
        )
        if pr_err2 or not pr_ticket2:
            report.add("pr-bootstrap-multi", False, pr_err2 or "no PR ticket")
        else:
            pr_poll2, pr_poll_err2 = poll_ticket_sap_sync(token, str(pr_ticket2.get("id") or ""))
            if pr_poll_err2 or not pr_poll2:
                report.add("pr-bootstrap-multi", False, pr_poll_err2 or "PR sync timeout")
            else:
                ok_pr2, det_pr2 = _sap_ok(pr_poll2)
                report.add("pr-bootstrap-multi", ok_pr2, det_pr2)
                if ok_pr2:
                    parent2 = str(pr_poll2.get("id") or "")
                    po_multi_cc = simple_po_form("YSER", header_note="from-pr-mcc")
                    line = po_multi_cc["lines"][0]
                    if isinstance(line, dict):
                        line["purchase_requisition_item"] = "10"
                        cc1 = os.environ.get("SAP_COST_CENTER", "HCO91001H0")
                        cc2 = os.environ.get("SAP_COST_CENTER_2", "HCO91001A0")
                        line["allocations"] = [
                            {"cost_center": cc1, "qty": "1"},
                            {"cost_center": cc2, "qty": "1"},
                        ]
                    cases.append(("pr-linked-multi-cc", po_multi_cc, parent2))

    for case_id, form, parent in cases:
        if args.case and args.case != case_id:
            continue
        _run_case(
            report,
            token=token,
            case_id=case_id,
            form=form,
            parent_pr_id=parent,
            skip_update=args.skip_update,
            skip_delete=args.skip_delete,
            expect_create_blocked=case_id == "pr-linked-multi-cc",
        )

    report.print_summary()
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
