#!/usr/bin/env python3
"""Live: PR-linked PO with every *allowed* field changed vs SAP PR baseline; verify on SAP GET.

Runs simple + multi-cc + two-material for YUNB and YAST against existing QAS PRs.
Resolves parent ticket UUID from AgentOS API (not hardcoded map only).

Usage (agentos-backend/, API + SAP in .env):

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_from_pr_field_matrix.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_from_pr_field_matrix.py --case yunb-simple
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.sap_odata_utils import odata_entity_properties, odata_norm, odata_results_list, odata_text
from app.procurement.sap_po_client import get_po, sap_po_configured
from app.procurement.sap_po_payload import PO_ACCT_NAV, parse_po_items_from_read
from app.procurement.sap_pr_items import SapPrItemLine, fetch_pr_items

from scripts.integration.po_live_common import apply_po_live_fixtures, lookup_parent_pr_id_by_sap
import httpx

from scripts.integration.procurement_api_common import (
    api_base,
    classify_sap_ticket,
    create_po_via_api_result,
    load_env,
    resolve_token_from_args,
)


def _poll_ticket_sap(
    token: str, ticket_id: str, *, timeout_s: float = 180.0
) -> tuple[dict[str, Any] | None, str]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        with httpx.Client(timeout=60.0) as client:
            r = client.get(
                f"{api_base()}/api/procurement/tickets/{ticket_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code >= 400:
            return None, f"GET HTTP {r.status_code}"
        last = r.json()
        sap_id = str(last.get("sap_id") or "").strip()
        sync = last.get("sap_sync") if isinstance(last.get("sap_sync"), dict) else {}
        pending = bool(sync.get("sync_pending"))
        err = str(sync.get("last_error") or "").strip()
        attempts = int(sync.get("attempt_count") or 0)
        if sap_id and not sap_id.startswith("#") and not pending:
            return last, ""
        if err and attempts >= 1 and not sap_id:
            return last, err
        if err and not pending:
            return last, err
        time.sleep(2.5)
    return last, "timeout waiting for sap_id"


async def _bootstrap_fresh_prs(token: str) -> dict[str, str]:
    """Create open PRs on QAS for each matrix shape; return case_id → SAP PR number."""
    from scripts.integration.integration_reference_defaults import apply_pr_integration_fixtures
    from scripts.integration.run_pr_create_yast import YAST_SHAPES
    from scripts.integration.run_pr_create_yunb import YUNB_SHAPES
    from scripts.integration.procurement_api_common import create_pr_via_api_result

    delivery = os.environ.get("SAP_PO_DELIVERY_DATE", "2026-08-15")
    out: dict[str, str] = {}
    shapes = [
        ("yunb-simple", "YUNB", YUNB_SHAPES, apply_pr_integration_fixtures, "simple"),
        ("yunb-multi-cc", "YUNB", YUNB_SHAPES, apply_pr_integration_fixtures, "multi-cc"),
        ("yunb-two-material", "YUNB", YUNB_SHAPES, apply_pr_integration_fixtures, "two-material"),
        ("yast-simple", "YAST", YAST_SHAPES, apply_pr_integration_fixtures, "simple"),
        ("yast-multi-cc", "YAST", YAST_SHAPES, apply_pr_integration_fixtures, "multi-cc"),
        ("yast-two-material", "YAST", YAST_SHAPES, apply_pr_integration_fixtures, "two-material"),
    ]
    for case_id, doc_type, shape_map, apply_fix, shape in shapes:
        apply_fix()
        form = shape_map[shape](header_note=f"bootstrap for {case_id}")
        for line in form.get("lines") or []:
            if isinstance(line, dict):
                line["delivery_date"] = delivery
        ticket, err = create_pr_via_api_result(
            access_token=token, document_type=doc_type, form=form
        )
        if err or not ticket:
            raise RuntimeError(f"bootstrap PR {case_id}: {err or 'no ticket'}")
        tid = str(ticket.get("id") or "")
        polled, poll_err = _poll_ticket_sap(token, tid, timeout_s=240.0)
        if poll_err or not polled:
            raise RuntimeError(f"bootstrap PR {case_id}: {poll_err or 'poll failed'}")
        sap_id = str(polled.get("sap_id") or "").strip()
        if not sap_id:
            raise RuntimeError(f"bootstrap PR {case_id}: no sap_id")
        out[case_id] = sap_id
        print(f"  bootstrap {case_id} -> PR {sap_id}")
    return out

CASES: dict[str, dict[str, str]] = {
    "yunb-simple": {"doc_type": "YUNB", "pr": "1040000063", "label": "1 mat 1 CC"},
    "yunb-multi-cc": {"doc_type": "YUNB", "pr": "1040000064", "label": "1 mat 2 CC"},
    "yunb-two-material": {"doc_type": "YUNB", "pr": "1040000065", "label": "2 mat 2 CC each"},
    "yast-simple": {"doc_type": "YAST", "pr": "1030000102", "label": "1 mat 1 asset"},
    "yast-multi-cc": {"doc_type": "YAST", "pr": "1030000103", "label": "1 mat 2 asset splits"},
    "yast-two-material": {"doc_type": "YAST", "pr": "1030000104", "label": "2 mat 2 splits each"},
}

# Deliberately wrong CC — enrichment must replace with PR CC before SAP create.
DECOY_CC = "WRONGCC00000"

DEFAULT_VENDOR = "1000000002"
ALT_VENDOR = "1000000003"
CHANGED_PRICE = "99.99"
CHANGED_TAX = "XE"
QTY_BUMP = Decimal("2")


@dataclass
class ExpectedLine:
    pr_item: str
    order_qty: str
    net_price: str
    pr_ccs: list[str]
    pr_assets: list[str]
    yast: bool


@dataclass
class CaseExpect:
    vendor: str
    tax_code: str
    header_note_substr: str
    lines: list[ExpectedLine] = field(default_factory=list)


def _dec(s: str) -> Decimal | None:
    t = (s or "").strip().replace(",", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _qty_close(a: str, b: str, *, tol: Decimal = Decimal("0.01")) -> bool:
    da, db = _dec(a), _dec(b)
    if da is None or db is None:
        return (a or "").strip() == (b or "").strip()
    return abs(da - db) <= tol


def _bump_qty(qty: str) -> str:
    """API validation requires positive whole-number qty strings."""
    d = _dec(qty) or Decimal("1")
    out = int((d + QTY_BUMP).to_integral_value())
    return str(max(out, 1))


def _alloc_rows_from_pr(item: SapPrItemLine, *, doc_type: str) -> list[dict[str, str]]:
    dt = doc_type.upper()
    pr_cc_rows = [a for a in item.acct_rows if a.cost_center]
    use_decoy_cc = dt == "YUNB" and len(pr_cc_rows) <= 1
    rows: list[dict[str, str]] = []
    for a in item.acct_rows:
        if dt == "YAST":
            row: dict[str, str] = {}
            if a.master_asset:
                row["master_asset"] = a.master_asset
            elif a.cost_center:
                row["master_asset"] = a.cost_center
            row["qty"] = _bump_qty(a.quantity or item.qty)
            rows.append(row)
        elif a.cost_center:
            rows.append(
                {
                    "cost_center": DECOY_CC if use_decoy_cc else a.cost_center,
                    "qty": _bump_qty(a.quantity or item.qty),
                }
            )
    if not rows:
        if dt == "YAST":
            rows = [{"master_asset": item.master_asset or "", "qty": _bump_qty(item.qty)}]
        else:
            rows = [
                {
                    "cost_center": DECOY_CC if use_decoy_cc else "",
                    "qty": _bump_qty(item.qty),
                }
            ]
    return rows


def _form_from_pr_with_deltas(
    *,
    doc_type: str,
    pr_number: str,
    items: list[SapPrItemLine],
    vendor: str,
) -> tuple[dict[str, Any], CaseExpect]:
    dt = doc_type.upper()
    pur_org = items[0].pur_org or os.environ.get("SAP_PUR_ORG", "1MGH")
    pur_group = items[0].pur_group or "A0B"
    note = f"field-matrix PR {pr_number} all-editable-changed"
    lines: list[dict[str, Any]] = []
    exp_lines: list[ExpectedLine] = []

    for item in items:
        allocs = _alloc_rows_from_pr(item, doc_type=dt)
        pr_ccs = [a.cost_center for a in item.acct_rows if a.cost_center]
        pr_assets = [
            a.master_asset or a.cost_center
            for a in item.acct_rows
            if (a.master_asset or a.cost_center)
        ]
        if not pr_assets and item.master_asset:
            pr_assets = [item.master_asset]

        lines.append(
            {
                "material": item.material,
                "short_text": item.short_text,
                "unit_price": CHANGED_PRICE,
                "net_price": CHANGED_PRICE,
                "order_unit": item.unit or "KG",
                "purchase_requisition_item": item.item_no,
                "item_category": item.item_cat,
                "account_assignment_cat": item.acct_cat,
                "delivery_date": os.environ.get("SAP_PO_DELIVERY_DATE", "2026-09-01"),
                "allocations": allocs,
            }
        )
        order_qty = str(sum(_dec(a.get("qty") or "0") or Decimal(0) for a in allocs))
        exp_lines.append(
            ExpectedLine(
                pr_item=item.item_no,
                order_qty=order_qty,
                net_price=CHANGED_PRICE,
                pr_ccs=pr_ccs,
                pr_assets=pr_assets,
                yast=dt == "YAST",
            )
        )

    header: dict[str, Any] = {
        "purchasing_org": pur_org,
        "purchasing_group": pur_group,
        "plant": items[0].plant if items else "",
        "storage_location": items[0].sloc if items else "",
        "material_group": items[0].material_group if items else "",
        "vendor": vendor,
        "header_note": note,
        "tax_code": CHANGED_TAX,
    }
    form = {"header": header, "lines": lines}
    expect = CaseExpect(
        vendor=vendor,
        tax_code=CHANGED_TAX,
        header_note_substr=pr_number,
        lines=exp_lines,
    )
    return form, expect


def _po_header_supplier(body: dict[str, Any]) -> str:
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return ""
    props = odata_entity_properties(root)
    return odata_text(props.get("Supplier"))


def _po_item_fields(body: dict[str, Any]) -> dict[str, dict[str, str]]:
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
        props = odata_entity_properties(entry)
        item_no = odata_text(props.get("PurchaseOrderItem"))
        if not item_no:
            continue
        out[item_no] = {
            "order_quantity": odata_text(props.get("OrderQuantity")),
            "net_price": odata_text(props.get("NetPriceAmount")),
            "tax_code": odata_text(props.get("TaxCode")),
            "pr_item": odata_text(props.get("PurchaseRequisitionItem")),
        }
    return out


async def _verify_po_on_sap(
    *,
    po_number: str,
    expect: CaseExpect,
    ticket_form: dict[str, Any] | None,
) -> list[str]:
    failures: list[str] = []
    body, err = await get_po(po_number=po_number, ticket_id="field-matrix-verify")
    if err or not body:
        return [f"SAP GET PO failed: {err or 'empty'}"]

    supplier = _po_header_supplier(body)
    if supplier and supplier != expect.vendor:
        failures.append(f"Supplier: got {supplier!r} expected {expect.vendor!r}")

    items_by_no = _po_item_fields(body)
    _, snapshots = parse_po_items_from_read(body)

    po_item_list = sorted(items_by_no.keys(), key=lambda x: int(x or "0"))
    if len(po_item_list) != len(expect.lines):
        failures.append(
            f"item count: PO has {len(po_item_list)} lines, expected {len(expect.lines)}"
        )

    for idx, exp in enumerate(expect.lines):
        item_no = po_item_list[idx] if idx < len(po_item_list) else ""
        row = items_by_no.get(item_no, {})
        if not _qty_close(row.get("order_quantity", ""), exp.order_qty):
            failures.append(
                f"item {item_no} OrderQuantity: got {row.get('order_quantity')!r} "
                f"expected {exp.order_qty!r}"
            )
        if row.get("net_price") and not _qty_close(row.get("net_price", ""), exp.net_price):
            failures.append(
                f"item {item_no} NetPriceAmount: got {row.get('net_price')!r} "
                f"expected {exp.net_price!r}"
            )
        if expect.tax_code and row.get("tax_code") and row.get("tax_code") != expect.tax_code:
            failures.append(
                f"item {item_no} TaxCode: got {row.get('tax_code')!r} expected {expect.tax_code!r}"
            )

        snap = snapshots[idx] if idx < len(snapshots) else None
        if snap and not exp.yast:
            for cc in exp.pr_ccs:
                if cc not in snap.cost_centers:
                    failures.append(
                        f"item {item_no} CostCenter {cc!r} missing on PO (got {snap.cost_centers})"
                    )
            if DECOY_CC in snap.cost_centers:
                failures.append(f"item {item_no} decoy CC {DECOY_CC!r} must not appear on PO")
        if snap and exp.yast and exp.pr_assets:
            # YAST PO acct may expose asset on MasterFixedAsset — check ticket form if SAP expand thin
            pass

    if ticket_form and isinstance(ticket_form.get("lines"), list):
        for bi, line in enumerate(ticket_form["lines"]):
            if not isinstance(line, dict):
                continue
            allocs = line.get("allocations")
            if not isinstance(allocs, list) or bi >= len(expect.lines):
                continue
            exp = expect.lines[bi]
            if exp.yast:
                for ai, a in enumerate(allocs):
                    if not isinstance(a, dict):
                        continue
                    asset = str(a.get("master_asset") or "").strip()
                    if exp.pr_assets and asset and asset not in exp.pr_assets:
                        failures.append(
                            f"ticket line {bi} alloc {ai} asset {asset!r} not in PR {exp.pr_assets!r}"
                        )
            else:
                for ai, a in enumerate(allocs):
                    if not isinstance(a, dict):
                        continue
                    cc = str(a.get("cost_center") or "").strip()
                    if cc == DECOY_CC:
                        failures.append(f"ticket line {bi} still has decoy CC (enrich failed)")
                    elif exp.pr_ccs and cc and cc not in exp.pr_ccs:
                        failures.append(
                            f"ticket line {bi} alloc {ai} CC {cc!r} not in PR {exp.pr_ccs!r}"
                        )

    return failures


async def run_case(
    case_id: str,
    meta: dict[str, str],
    *,
    token: str,
    vendor: str,
) -> dict[str, Any]:
    pr = meta["pr"]
    doc_type = meta["doc_type"]
    items, pr_err = await fetch_pr_items(pr_number=pr, ticket_id=f"matrix-{case_id}")
    if pr_err or not items:
        return {
            "case": case_id,
            "pr": pr,
            "status": "SKIP",
            "error": pr_err or "no PR items",
        }

    parent_id = lookup_parent_pr_id_by_sap(access_token=token, sap_pr_number=pr)
    if not parent_id:
        return {
            "case": case_id,
            "pr": pr,
            "status": "SKIP",
            "error": f"no AgentOS parent ticket for PR {pr} (create PR in app first)",
        }

    form, expect = _form_from_pr_with_deltas(
        doc_type=doc_type, pr_number=pr, items=items, vendor=vendor
    )
    ticket, api_err = create_po_via_api_result(
        access_token=token,
        document_type=doc_type,
        form=form,
        parent_pr_id=parent_id,
    )
    if api_err or not ticket:
        return {
            "case": case_id,
            "pr": pr,
            "status": "FAIL",
            "phase": "create",
            "error": api_err or "no ticket",
        }

    tid = str(ticket.get("id") or "")
    poll_err = ""
    if tid:
        polled, poll_err = _poll_ticket_sap(token, tid)
        if polled is not None:
            ticket = polled

    sap_id = str((ticket or {}).get("sap_id") or "").strip()
    _st, detail, _ = classify_sap_ticket(ticket or {})
    if poll_err and not sap_id:
        detail = poll_err or detail
    if not sap_id or sap_id.startswith("#"):
        return {
            "case": case_id,
            "pr": pr,
            "status": "FAIL",
            "phase": "create",
            "error": detail or "no numeric sap_id",
            "ticket_id": ticket.get("id"),
        }

    ticket_form = ticket.get("form") if isinstance(ticket.get("form"), dict) else None
    verify_errs = await _verify_po_on_sap(
        po_number=sap_id, expect=expect, ticket_form=ticket_form
    )
    status = "PASS" if not verify_errs else "FAIL"
    return {
        "case": case_id,
        "pr": pr,
        "doc_type": doc_type,
        "label": meta.get("label"),
        "status": status,
        "po_id": sap_id,
        "ticket_id": ticket.get("id"),
        "verify_errors": verify_errs,
        "changed": {
            "vendor": vendor,
            "price": CHANGED_PRICE,
            "tax_code": CHANGED_TAX,
            "qty_bump": str(QTY_BUMP),
            "decoy_cc": DECOY_CC,
        },
    }


async def main_async(args: argparse.Namespace, *, token: str) -> int:
    if not sap_po_configured():
        print("SAP PO not configured", file=sys.stderr)
        return 1
    vendor = (os.environ.get("SAP_VENDOR") or DEFAULT_VENDOR).strip()
    alt = (os.environ.get("SAP_VENDOR_ALT") or ALT_VENDOR).strip()
    if alt == vendor:
        alt = DEFAULT_VENDOR if vendor != ALT_VENDOR else ALT_VENDOR
    use_vendor = alt if args.alt_vendor else vendor

    ids = list(CASES.keys()) if args.case == "all" else [args.case]
    cases = dict(CASES)
    if args.bootstrap_pr:
        print("Bootstrapping fresh PRs on QAS…")
        fresh = await _bootstrap_fresh_prs(token)
        for cid, pr in fresh.items():
            if cid in cases:
                cases[cid] = {**cases[cid], "pr": pr}
    for cid in ids:
        apply_po_live_fixtures(cases[cid]["doc_type"])
    print(f"API: {api_base()}")
    print(f"Vendor override: {use_vendor!r}  Tax: {CHANGED_TAX}  Price: {CHANGED_PRICE}")
    print(f"Qty bump: +{QTY_BUMP} per allocation; decoy CC: {DECOY_CC}\n")

    results: list[dict[str, Any]] = []
    for cid in ids:
        print(f"--- {cid} PR {cases[cid]['pr']} ---")
        row = await run_case(cid, cases[cid], token=token, vendor=use_vendor)
        results.append(row)
        st = row.get("status")
        if st == "PASS":
            print(f"  PASS  PO {row.get('po_id')}")
        elif st == "SKIP":
            print(f"  SKIP  {row.get('error')}")
        else:
            print(f"  FAIL  {row.get('error') or row.get('verify_errors')}")

    out_path = Path(args.json_out)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(1 for r in results if r.get("status") == "PASS")
    print(f"\n{passed}/{len(results)} PASS — wrote {out_path}")
    return 0 if passed == len(results) else 2


def main() -> int:
    p = argparse.ArgumentParser(description="PR-linked PO: change all allowed fields, verify SAP")
    p.add_argument("--case", choices=[*CASES.keys(), "all"], default="all")
    p.add_argument("--json-out", default="/tmp/po_from_pr_field_matrix.json")
    p.add_argument(
        "--alt-vendor",
        action="store_true",
        help="Use SAP_VENDOR_ALT instead of SAP_VENDOR",
    )
    p.add_argument(
        "--bootstrap-pr",
        action="store_true",
        help="Create fresh PRs on QAS first (use when catalog PRs are already converted)",
    )
    from scripts.integration.procurement_api_common import add_common_args

    add_common_args(p)
    args = p.parse_args()
    load_env()
    token = resolve_token_from_args(args)
    return asyncio.run(main_async(args, token=token))


if __name__ == "__main__":
    raise SystemExit(main())
