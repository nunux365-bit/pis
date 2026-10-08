#!/usr/bin/env python3
"""Smoke: list SAP outbound fields injected on the live create path (not from UI form).

Runs the same path as ``service.create_ticket``: normalize → defaults → reference UoM → build_*_payload.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.db.session import AsyncSessionLocal
from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.sap_defaults import DEFAULT_ORDER_UNIT, apply_procurement_defaults
from app.procurement.sap_po_payload import PO_DEFAULT_CURRENCY, build_po_payload
from app.procurement.sap_pr_payload import build_pr_payload
from app.procurement.sap_order_unit import apply_line_order_units_from_reference
from scripts.integration.sap_po_doc_fixtures import apply_doc_master_to_po_form

TICKET_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Keys we always send but the user does not type (product / SAP API rules — review with SAP).
KNOWN_BACKEND_RULES: dict[str, str] = {
    "SourceDetermination": "PR header — always False",
    "DocumentCurrency": f"PO header — always {PO_DEFAULT_CURRENCY!r}",
    "ProductType": "PR item — 1 material / 2 service (from doc type)",
    "PurchaseRequisitionType": "PR item — mirrors workflow type",
    "PurchaseOrderType": "PO header — mirrors workflow type",
    "AccountAssignmentCategory": "YUNB→K, YAST→A (cleared on form before build)",
    "PurchaseOrderItemCategory": "PO item — 0 material / 9 service",
    "PurchasingDocumentItemCategory": "PR item — 0 or 9 (YSER)",
    "CompanyCode": "Header company_code = purchasing_org (defaults)",
    "FixedSupplier": "PR — copied from header vendor when set",
    "MultipleAcctAssgmtDistribution": "1 when multi acct rows",
    "ScheduleLine": "PO schedule line number 0001",
    "ScheduleLineOrderQuantity": "PO schedule — mirrors order qty",
    "OrderPriceUnit": "YAST PO — mirrors purchase unit",
}

# Keys that may surprise SAP if not previously agreed.
REVIEW_DEFAULTS: dict[str, str] = {
    f"BaseUnit|{DEFAULT_ORDER_UNIT}": f"PR — order_unit blank → {DEFAULT_ORDER_UNIT!r}",
    f"PurchaseOrderQuantityUnit|{DEFAULT_ORDER_UNIT}": f"PO — order_unit blank → {DEFAULT_ORDER_UNIT!r}",
    "NetPriceAmount|0.00": "PO — empty net price → 0.00",
    "PurchaseRequisitionItemText|AO": "PR line 1 — full-UUID AO{32HEX} suffix when ticket_id set",
}


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


async def _prepare_form(
    form: dict[str, Any], *, document_type: str, kind: str
) -> dict[str, Any]:
    norm = normalize_form(document_type, copy.deepcopy(form))
    apply_procurement_defaults(norm, document_type=document_type, kind=kind)
    async with AsyncSessionLocal() as session:
        await apply_line_order_units_from_reference(
            session, form=norm, document_type=document_type, ticket_kind=kind
        )
    return norm


def _doc_master_ui_form(document_type: str, *, kind: str) -> dict[str, Any]:
    dt = document_type.upper()
    blk = default_empty_block(dt)
    if dt == "YUNB":
        blk.update(
            material="4100000012",
            short_text=f"{dt} defaults smoke",
            delivery_date="2026-08-15",
            unit_price="100.00",
            allocations=[{"cost_center": "LCO91001A0", "qty": "5"}],
        )
        if kind == "PO":
            blk["net_price"] = "100.00"
        header = {
            "purchasing_org": "1LFS",
            "purchasing_group": "A0B",
            "plant": "L001",
            "storage_location": "1001",
            "material_group": "S002-0001",
            "header_note": f"{dt} outbound defaults smoke",
        }
        if kind == "PO":
            header["vendor"] = "1000000002"
            header["tax_code"] = "XE"
    else:  # YAST
        blk.update(
            material="4100000018",
            short_text=f"{dt} defaults smoke",
            delivery_date="2026-08-15",
            unit_price="1500.00",
            asset="7100001182",
            purchasing_info_record="5300000009",
            allocations=[{"asset": "7100001182", "qty": "5"}],
        )
        if kind == "PO":
            blk["net_price"] = "1500.00"
        header = {
            "purchasing_org": "1LFS",
            "purchasing_group": "A0B",
            "plant": "L001",
            "storage_location": "1003",
            "material_group": "M003-0043",
            "header_note": f"{dt} outbound defaults smoke",
        }
        if kind == "PO":
            header["vendor"] = "1000000002"
            header["tax_code"] = "XE"
    return {"header": header, "lines": [blk]}


def _form_only_keys(form: dict[str, Any]) -> set[str]:
    flat = _flatten(form)
    keys: set[str] = set()
    for path in flat:
        leaf = path.split(".")[-1]
        keys.add(leaf)
        keys.add(path)
    return keys


def _audit_payload(
    *,
    label: str,
    payload: dict[str, Any],
    form: dict[str, Any],
) -> list[str]:
    flat = _flatten(payload)
    form_vals = {str(v).strip() for v in _flatten(form).values() if v not in (None, "", [])}
    findings: list[str] = []

    for path, val in sorted(flat.items()):
        if val in (None, "", []):
            continue
        leaf = path.rsplit(".", 1)[-1]
        sval = str(val)

        if leaf in KNOWN_BACKEND_RULES:
            findings.append(f"  [rule] {path} = {sval!r} — {KNOWN_BACKEND_RULES[leaf]}")
            continue

        for key, note in REVIEW_DEFAULTS.items():
            if "|" in key:
                k, want = key.split("|", 1)
                if leaf == k and want in sval:
                    findings.append(f"  [review] {path} = {sval!r} — {note}")
                    break
            elif key in sval and leaf == "PurReqnDescription":
                findings.append(f"  [review] {path} = {sval!r} — {note}")
                break
        else:
            if sval in form_vals or any(sval in fv for fv in form_vals if len(fv) > 3):
                continue
            if leaf in (
                "PurchaseRequisition",
                "PurchaseOrder",
                "PurchaseRequisitionItem",
                "PurchaseOrderItem",
                "PurchaseReqnItem",
                "AccountAssignmentNumber",
                "ScheduleLine",
            ):
                continue
            if leaf == "GLAccount" or leaf == "ControllingArea":
                findings.append(f"  [unexpected] {path} = {sval!r} — GL/CO should not default")
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    print("Form header (not sent on OData create unless mapped):")
    hdr = form.get("header") if isinstance(form.get("header"), dict) else {}
    print(f"  purchasing_doc_type={hdr.get('purchasing_doc_type')!r}")
    print(f"  company_code={hdr.get('company_code')!r}")
    print(f"  payment_terms={hdr.get('payment_terms')!r} (PO create omits; update uses 0001 if empty)")
    for line in form.get("lines") or []:
        if isinstance(line, dict):
            print(f"  line order_unit={line.get('order_unit')!r}")
    print("\nInjected / derived on SAP POST:")
    for line in findings:
        print(line)
    if not findings:
        print("  (none flagged)")
    return findings


async def main() -> int:
    cases: list[tuple[str, str, str, dict[str, Any]]] = []
    for dt in ("YUNB", "YAST"):
        for kind in ("PR", "PO"):
            ui = _doc_master_ui_form(dt, kind=kind)
            if kind == "PO":
                ui = apply_doc_master_to_po_form(
                    normalize_form(dt, ui), document_type=dt
                )
            cases.append((f"{kind} {dt}", dt, kind, ui))

    any_unexpected = False
    for label, dt, kind, ui in cases:
        form = await _prepare_form(ui, document_type=dt, kind=kind)
        if kind == "PR":
            payload = build_pr_payload(
                form=form, document_type=dt, pr_number="", ticket_id=TICKET_ID
            )
        else:
            payload = build_po_payload(
                form=form,
                document_type=dt,
                po_number="",
                ticket_id=TICKET_ID,
                kind="PO",
            )
        findings = _audit_payload(label=label, payload=payload, form=form)
        if any("[unexpected]" in f for f in findings):
            any_unexpected = True
        print("\nPayload snippet (header + first item keys):")
        if kind == "PR":
            items = payload.get("to_PurchaseReqnItem") or []
        else:
            items = payload.get("to_PurchaseOrderItem") or []
        snippet = {k: payload.get(k) for k in payload if k.startswith("to_") is False}
        if items:
            snippet["first_item"] = items[0]
        print(json.dumps(snippet, indent=2, default=str)[:4000])

    print("\n" + "=" * 72)
    print("SUMMARY — not sent on SAP create (form-only)")
    print("  company_code=org, payment_terms cleared; purchasing_doc_type not auto-filled")
    print("SUMMARY — agreed SAP rules on create")
    print("  Acct cat K/A, schedule line delivery, no GL/CO on YAST acct rows")
    print("SUMMARY — review with SAP if not yet signed off")
    print(f"  PO DocumentCurrency={PO_DEFAULT_CURRENCY}")
    print("  PO TaxJurisdiction only when header.tax_jurisdiction is set")
    print(f"  Blank order_unit → {DEFAULT_ORDER_UNIT} (if reference DB has no UoM)")
    print("  PR SourceDetermination=False, PR/PO description [AO:uuid] tag on create")
    print("  PO PaymentTerms only when header.payment_terms is set")
    return 1 if any_unexpected else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
