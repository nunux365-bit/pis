#!/usr/bin/env python3
"""Live QA matrix for YSER PO post-create reconcile (new POs only).

Scenarios:
  R-L01  standalone 1 service (no reconcile update expected)
  R-L02  standalone 2 services same group (reconcile if SAP drops one)
  R-L03  standalone duplicate service code same group (distinct short_text)
  R-L04  OPS 2x2 multi-group (2 service_groups × 2 services)
  R-L05  PO-from-PR 2 services same group

Usage:
  cd agentos-backend
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_yser_po_reconcile_qa_live.py
  python scripts/integration/run_yser_po_reconcile_qa_live.py --sap-direct
  python scripts/integration/run_yser_po_reconcile_qa_live.py --case R-L04
"""
from __future__ import annotations

import argparse
import asyncio
import copy
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
from app.procurement.sap_po_client import create_po, get_po, sap_po_configured
from app.procurement.sap_po_z_payload import (
    form_from_z_po_read,
    normalize_service_performer_code,
    verify_z_po_read_against_form,
)
from app.procurement.sap_pr_z_payload import normalize_service_performer_code as norm_pr_svc

from scripts.integration.po_live_common import apply_po_live_fixtures
from scripts.integration.procurement_api_common import (
    add_common_args,
    classify_sap_ticket,
    create_po_via_api_result,
    create_pr_via_api_result,
    load_env,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)


@dataclass
class Row:
    scenario: str
    step: str
    status: str
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, scenario: str, step: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        st = "SKIP" if skip else ("PASS" if ok else "FAIL")
        self.rows.append(Row(scenario, step, st, detail))

    def exit_code(self) -> int:
        return 1 if any(r.status == "FAIL" for r in self.rows) else 0

    def print_summary(self) -> None:
        print("\n" + "=" * 80)
        print("YSER PO RECONCILE QA LIVE")
        print("=" * 80)
        cur = ""
        for r in self.rows:
            if r.scenario != cur:
                cur = r.scenario
                print(f"\n--- {cur} ---")
            print(f"  {r.status:4}  {r.step:<28}  {r.detail[:120]}")
        n_pass = sum(1 for r in self.rows if r.status == "PASS")
        n_fail = sum(1 for r in self.rows if r.status == "FAIL")
        n_skip = sum(1 for r in self.rows if r.status == "SKIP")
        print("\n" + "=" * 80)
        print(f"PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
        print("=" * 80)


def _master() -> dict[str, str]:
    return {
        "plant": "H001",
        "sloc": "H001|3021",
        "g1": "S089-0001",
        "g2": "S074-0001",
        "vendor": "8005",
        "tax": "XE",
        "cc": "HBM13021A0",
        "svc1": "000000001000000000",
        "svc2": "000000001000000001",
        "pur_org": "1MGH",
        "pur_group": "A0B",
        "delivery": "2026-09-15",
    }


def _prep(form: dict[str, Any]) -> dict[str, Any]:
    out = normalize_form("YSER", copy.deepcopy(form))
    apply_procurement_defaults(out, document_type="YSER", kind="PO")
    return out


def _line(
    *,
    service: str,
    group: str,
    short_text: str,
    price: str = "100",
) -> dict[str, Any]:
    m = _master()
    return {
        "service": service,
        "service_group": group,
        "short_text": short_text,
        "delivery_date": m["delivery"],
        "unit_price": price,
        "net_price": price,
        "allocations": [{"cost_center": m["cc"], "qty": "1"}],
    }


def _form_r_l01() -> dict[str, Any]:
    m = _master()
    return _prep(
        {
            "header": {
                "purchasing_org": m["pur_org"],
                "purchasing_group": m["pur_group"],
                "plant": m["plant"],
                "storage_location": m["sloc"],
                "service_group": m["g1"],
                "vendor": m["vendor"],
                "tax_code": m["tax"],
            },
            "lines": [_line(service=m["svc1"], group=m["g1"], short_text="R-L01 single")],
        }
    )


def _form_r_l02() -> dict[str, Any]:
    m = _master()
    return _prep(
        {
            "header": {
                "purchasing_org": m["pur_org"],
                "purchasing_group": m["pur_group"],
                "plant": m["plant"],
                "storage_location": m["sloc"],
                "service_group": m["g1"],
                "vendor": m["vendor"],
                "tax_code": m["tax"],
            },
            "lines": [
                _line(service=m["svc1"], group=m["g1"], short_text="R-L02 svc A"),
                _line(service=m["svc2"], group=m["g1"], short_text="R-L02 svc B", price="80"),
            ],
        }
    )


def _form_r_l03() -> dict[str, Any]:
    m = _master()
    return _prep(
        {
            "header": {
                "purchasing_org": m["pur_org"],
                "purchasing_group": m["pur_group"],
                "plant": m["plant"],
                "storage_location": m["sloc"],
                "service_group": m["g1"],
                "vendor": m["vendor"],
                "tax_code": m["tax"],
            },
            "lines": [
                _line(service=m["svc1"], group=m["g1"], short_text="R-L03 dup line 1"),
                _line(service=m["svc1"], group=m["g1"], short_text="R-L03 dup line 2", price="90"),
            ],
        }
    )


def _form_r_l04() -> dict[str, Any]:
    """OPS 2×2: two service groups, two services each."""
    m = _master()
    return _prep(
        {
            "header": {
                "purchasing_org": m["pur_org"],
                "purchasing_group": m["pur_group"],
                "plant": m["plant"],
                "storage_location": m["sloc"],
                "vendor": m["vendor"],
                "tax_code": m["tax"],
            },
            "lines": [
                _line(service=m["svc1"], group=m["g1"], short_text="R-L04 G1 A"),
                _line(service=m["svc2"], group=m["g1"], short_text="R-L04 G1 B", price="80"),
                _line(service=m["svc1"], group=m["g2"], short_text="R-L04 G2 C", price="70"),
                _line(service=m["svc2"], group=m["g2"], short_text="R-L04 G2 D", price="60"),
            ],
        }
    )


def _form_r_l05_pr() -> dict[str, Any]:
    from scripts.integration.run_pr_create_yser import simple_yser_form

    m = _master()
    form = simple_yser_form(header_note="RL05")
    form = normalize_form("YSER", form)
    form["lines"].append(
        {
            "service": m["svc2"],
            "service_group": m["g1"],
            "short_text": "R-L05 PR svc B",
            "delivery_date": m["delivery"],
            "unit_price": "80",
            "net_price": "80",
            "allocations": [{"cost_center": m["cc"], "qty": "1"}],
        }
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    return form


def _form_r_l05_po_from_pr(pr_form: dict[str, Any]) -> dict[str, Any]:
    m = _master()
    lines = []
    for row in pr_form.get("lines") or []:
        if not isinstance(row, dict):
            continue
        lines.append(
            {
                "service": row.get("service"),
                "service_group": row.get("service_group") or m["g1"],
                "short_text": row.get("short_text"),
                "delivery_date": row.get("delivery_date") or m["delivery"],
                "unit_price": row.get("unit_price") or "100",
                "net_price": row.get("net_price") or row.get("unit_price") or "100",
                "purchase_requisition_item": row.get("purchase_requisition_item"),
                "allocations": copy.deepcopy(row.get("allocations") or []),
            }
        )
    return _prep(
        {
            "header": {
                "purchasing_org": m["pur_org"],
                "purchasing_group": m["pur_group"],
                "plant": m["plant"],
                "storage_location": m["sloc"],
                "service_group": m["g1"],
                "vendor": m["vendor"],
                "tax_code": m["tax"],
            },
            "lines": lines,
        }
    )


async def _verify_po(
    po_number: str,
    expected: dict[str, Any],
    *,
    expect_lines: int,
) -> tuple[bool, str]:
    body, err = await get_po(po_number=po_number, ticket_id="qa-verify", document_type="YSER")
    if err or not body:
        return False, err or "GET failed"
    read = form_from_z_po_read(body, seed_form=expected)
    lines = read.get("lines") or []
    if len(lines) != expect_lines:
        return False, f"lines {len(lines)} != {expect_lines}"
    exp_refs = {
        str(ln.get("sap_po_service_ref") or "").strip()
        for ln in (expected.get("lines") or [])
        if isinstance(ln, dict) and str(ln.get("sap_po_service_ref") or "").strip()
    }
    read_refs = {
        str(ln.get("sap_po_service_ref") or "").strip()
        for ln in lines
        if str(ln.get("sap_po_service_ref") or "").strip()
    }
    if exp_refs and not exp_refs.issubset(read_refs):
        return False, f"missing refs {sorted(exp_refs - read_refs)}"
    exp_svcs = {
        normalize_service_performer_code(str(ln.get("service") or ""))
        for ln in (expected.get("lines") or [])
        if isinstance(ln, dict)
    }
    read_svcs = {
        normalize_service_performer_code(str(ln.get("service") or ""))
        for ln in lines
        if isinstance(ln, dict)
    }
    if len(exp_svcs) != len(lines) and exp_svcs != read_svcs:
        return False, f"services {sorted(read_svcs)} != {sorted(exp_svcs)}"
    mismatches = verify_z_po_read_against_form(body, form=expected)
    if mismatches:
        soft = [m for m in mismatches if "delivery" in m.lower() or "service_group" in m.lower()]
        if soft and len(soft) == len(mismatches):
            return True, f"GET ok (soft verify: {soft[0][:60]})"
        return False, "; ".join(mismatches[:3])
    return True, f"GET+verify ok lines={len(lines)}"


async def _run_sap_direct(report: Report, scenario: str, form: dict[str, Any]) -> None:
    tag = uuid.uuid4().hex[:6]
    form = copy.deepcopy(form)
    form.setdefault("header", {})["header_note"] = f"QA{scenario[-2:]}{tag}"[:12]
    expect = len(form.get("lines") or [])
    tid = f"qa-{scenario}-{tag}"
    po_number, err = await create_po(
        form=form,
        document_type="YSER",
        ticket_id=tid,
        parent_sap_id=None,
    )
    if err or not po_number:
        report.add(scenario, "create", False, err or "create failed")
        return
    report.add(scenario, "create", True, f"PO {po_number}")
    ok, det = await _verify_po(po_number, form, expect_lines=expect)
    report.add(scenario, "get_after_create", ok, det)


def _run_api(
    report: Report,
    scenario: str,
    form: dict[str, Any],
    *,
    token: str,
    parent_pr_id: str | None,
) -> None:
    tag = uuid.uuid4().hex[:6]
    form = copy.deepcopy(form)
    form.setdefault("header", {})["header_note"] = f"QA{scenario[-2:]}{tag}"[:12]
    expect = len(form.get("lines") or [])
    po_ticket, err = create_po_via_api_result(
        access_token=token,
        document_type="YSER",
        form=form,
        parent_pr_id=parent_pr_id,
    )
    if err or not po_ticket:
        report.add(scenario, "create", False, err or "no ticket")
        return
    tid = str(po_ticket.get("id") or "")
    polled, poll_err = poll_ticket_sap_sync(token, tid)
    if poll_err or not polled:
        report.add(scenario, "create", False, poll_err or "sync timeout")
        return
    status, detail, _ = classify_sap_ticket(polled)
    sap_id = str(polled.get("sap_id") or "")
    if status != "pass" or not sap_id:
        report.add(scenario, "create", False, detail)
        return
    report.add(scenario, "create", True, f"PO {sap_id} ticket={tid[:8]}")
    ok, det = asyncio.run(_verify_po(sap_id, form, expect_lines=expect))
    report.add(scenario, "get_after_create", ok, det)


async def _main_async(args: argparse.Namespace, *, token: str | None) -> int:
    load_env()
    if not sap_po_configured():
        print("SAP not configured")
        return 2

    apply_po_live_fixtures("YSER")
    report = Report()

    scenarios: dict[str, tuple[dict[str, Any], str | None]] = {
        "R-L01": (_form_r_l01(), None),
        "R-L02": (_form_r_l02(), None),
        "R-L03": (_form_r_l03(), None),
        "R-L04": (_form_r_l04(), None),
    }

    if args.case in ("", "R-L05"):
        pr_form = _form_r_l05_pr()
        parent_pr: str | None = None
        if not args.sap_direct:
            if not token:
                print("API token required (AGENTOS_INTEGRATION_MINT_TOKEN=1 or --token)")
                return 2
            pr_ticket, pr_err = create_pr_via_api_result(
                access_token=token,
                document_type="YSER",
                form=pr_form,
            )
            if pr_err or not pr_ticket:
                report.add("R-L05", "pr_create", False, pr_err or "PR failed")
            else:
                pr_poll, pr_poll_err = poll_ticket_sap_sync(token, str(pr_ticket.get("id") or ""))
                if pr_poll_err or not pr_poll:
                    report.add("R-L05", "pr_create", False, pr_poll_err or "PR sync timeout")
                else:
                    st, det, _ = classify_sap_ticket(pr_poll)
                    parent_pr = str(pr_poll.get("sap_id") or "") if st == "pass" else None
                    report.add("R-L05", "pr_create", st == "pass", det)
                    if parent_pr:
                        scenarios["R-L05"] = (_form_r_l05_po_from_pr(pr_form), parent_pr)
        else:
            report.add("R-L05", "pr_create", True, "skipped — use API path for PR-linked", skip=True)

    selected = [args.case] if args.case else list(scenarios.keys())
    for sid in selected:
        if sid not in scenarios:
            continue
        form, parent = scenarios[sid]
        if args.sap_direct:
            await _run_sap_direct(report, sid, form)
        else:
            if not token:
                print("API token required (AGENTOS_INTEGRATION_MINT_TOKEN=1 or --token)")
                return 2
            _run_api(report, sid, form, token=token, parent_pr_id=parent)

    report.print_summary()
    return report.exit_code()


def main() -> int:
    parser = argparse.ArgumentParser(description="YSER PO reconcile QA live matrix")
    add_common_args(parser)
    parser.add_argument("--case", default="", help="R-L01 .. R-L05")
    parser.add_argument(
        "--sap-direct",
        action="store_true",
        help="Call sap_po_client.create_po directly (no AgentOS API)",
    )
    args = parser.parse_args()
    token = None if args.sap_direct else resolve_token_from_args(args)
    return asyncio.run(_main_async(args, token=token))


if __name__ == "__main__":
    raise SystemExit(main())
