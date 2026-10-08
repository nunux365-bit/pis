#!/usr/bin/env python3
"""PO CREATE vs GET investigation on QA — compare payload sent vs SAP response."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import load_env
from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults

load_env()
apply_integration_reference_defaults()

import httpx
from app.config.settings import settings
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_odata_utils import odata_entity_properties, odata_results_list, odata_text
from app.procurement.sap_po_client import (
    _base_root,
    _credentials_or_error,
    _httpx_timeout,
    _po_csrf_root,
    _request_with_csrf_retry,
    create_po,
    get_po,
    sap_json_headers,
)
from app.procurement.sap_po_z_payload import (
    PO_SERVICE_ACCT_NAV,
    PO_SERVICES_NAV,
    build_z_yser_po_inner_payload,
    form_from_z_po_read,
    parse_z_po_items_from_read,
    z_po_entity_url,
)

ACCT_KEYS = (
    "AccountAssignmentNumber",
    "CostCenter",
    "Quantity",
    "PurgDocNetAmount",
    "IsDeleted",
)
SVC_KEYS = (
    "Service",
    "ServiceEntrySheetItemDesc",
    "ConfirmedQuantity",
    "NetPriceAmount",
    "NetAmount",
    "QuantityUnit",
    "MultipleAcctAssgmtDistribution",
)
ITEM_KEYS = (
    "PurchaseOrderItem",
    "MaterialGroup",
    "MultipleAcctAssgmtDistribution",
    "NetPriceAmount",
    "PurchaseRequisitionItem",
)


def _pick(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: str(d.get(k, "")).strip()[:40] for k in keys if d.get(k) not in (None, "")}


def summarize_po_create(inner: dict) -> list[dict]:
    out = []
    for item in inner.get("to_PurchaseOrderItem") or []:
        svcs_out = []
        for svc in item.get(PO_SERVICES_NAV) or []:
            accts = []
            for a in svc.get(PO_SERVICE_ACCT_NAV) or []:
                accts.append(_pick(a, ACCT_KEYS))
            svcs_out.append({**_pick(svc, SVC_KEYS), "accts": accts})
        out.append({**_pick(item, ITEM_KEYS), "n_services": len(svcs_out), "services": svcs_out})
    return out


def summarize_po_get_body(body: dict, *, label: str = "resolved") -> list[dict]:
    out = []
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
        props = odata_entity_properties(entry)
        svcs_out = []
        for svc_entry in odata_results_list(entry.get(PO_SERVICES_NAV) or props.get(PO_SERVICES_NAV)):
            sp = odata_entity_properties(svc_entry)
            accts = []
            for a in odata_results_list(
                svc_entry.get(PO_SERVICE_ACCT_NAV) or sp.get(PO_SERVICE_ACCT_NAV)
            ):
                accts.append(_pick(odata_entity_properties(a), ACCT_KEYS))
            svcs_out.append({**_pick(sp, SVC_KEYS), "accts": accts})
        out.append({**_pick(props, ITEM_KEYS), "n_services": len(svcs_out), "services": svcs_out})
    return out


def summarize_po_parse(body: dict) -> dict:
    form = form_from_z_po_read(body)
    _, snaps = parse_z_po_items_from_read(body)
    return {
        "ui_lines": len(form.get("lines") or []),
        "items": [
            {
                "item": s.item_number,
                "n_services": len(s.services),
                "services": [
                    {
                        "service": (svc.service or "")[-10:],
                        "text": (svc.short_text or "")[:30],
                        "net": (svc.net_price_amount or "")[:10],
                        "segs": [(a.cost_center, a.quantity, a.seq) for a in svc.acct_segments],
                    }
                    for svc in s.services
                ],
            }
            for s in snaps
            if not s.is_deleted
        ],
    }


def diff_create_get(sent: list, got: list) -> list[str]:
    notes: list[str] = []
    if len(sent) != len(got):
        notes.append(f"item count {len(sent)} -> {len(got)}")
    for i, (s_item, g_item) in enumerate(zip(sent, got)):
        if s_item.get("n_services") != g_item.get("n_services"):
            notes.append(
                f"item[{i}] services {s_item.get('n_services')} -> {g_item.get('n_services')}"
            )
        for j, (s_svc, g_svc) in enumerate(zip(s_item.get("services") or [], g_item.get("services") or [])):
            s_accts = s_svc.get("accts") or []
            g_accts = g_svc.get("accts") or []
            if len(s_accts) != len(g_accts):
                notes.append(f"item[{i}] svc[{j}] acct count {len(s_accts)} -> {len(g_accts)}")
            for k, (sa, ga) in enumerate(zip(s_accts, g_accts)):
                if sa.get("CostCenter") != ga.get("CostCenter"):
                    notes.append(
                        f"item[{i}] svc[{j}] acct[{k}] CC {sa.get('CostCenter')!r} -> {ga.get('CostCenter')!r}"
                    )
                if sa.get("Quantity") != ga.get("Quantity"):
                    notes.append(
                        f"item[{i}] svc[{j}] acct[{k}] qty {sa.get('Quantity')!r} -> {ga.get('Quantity')!r}"
                    )
            if s_svc.get("Service")[-10:] != g_svc.get("Service")[-10:]:
                notes.append(f"item[{i}] svc[{j}] service code changed")
    return notes


async def fetch_po_raw_expand(
    client: httpx.AsyncClient, *, base: str, po_number: str
) -> dict | None:
    """Direct OData GET with $expand — bypass AgentOS deferred resolver."""
    csrf_root = _po_csrf_root(base, document_type="YSER")
    expand = f"to_PurchaseOrderItem/to_Services/to_AccountAssignment"
    url = f"{z_po_entity_url(base, po_number)}?$expand={expand}"
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id="po-raw",
        method="GET",
        url=url,
        json_body=None,
        headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
        csrf_service_root=csrf_root,
    )
    if resp.status_code >= 400:
        return None
    return resp.json()


def _yser_form(lines: list[dict], note: str) -> dict:
    sg = os.environ.get("SAP_SERVICE_GROUP", "").strip()
    vendor = os.environ.get("SAP_VENDOR", "1000000002").strip()
    form = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
                "purchasing_group": os.environ.get("SAP_PUR_GROUP", "A0B"),
                "plant": os.environ.get("SAP_PLANT", "H002"),
                "storage_location": os.environ.get("SAP_SLOC", "1001"),
                "service_group": sg,
                "vendor": vendor,
                "header_note": note,
                "tax_code": "XE",
            },
            "lines": lines,
        },
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PO")
    return form


async def run_scenario(name: str, form: dict, client, base: str) -> dict[str, Any]:
    sent = summarize_po_create(build_z_yser_po_inner_payload(form=form, ticket_id="probe"))
    po, err = await create_po(ticket_id=f"po-{uuid.uuid4().hex[:8]}", form=form, document_type="YSER")
    result: dict[str, Any] = {"name": name, "po": po, "create_err": err, "sent": sent}
    if not po:
        return result

    resolved_body, gerr = await get_po(po_number=po, ticket_id="probe", document_type="YSER")
    raw_body = await fetch_po_raw_expand(client, base=base, po_number=po)
    got_resolved = summarize_po_get_body(resolved_body or {}, label="resolved")
    got_raw = summarize_po_get_body(raw_body or {}, label="raw") if raw_body else []
    hydrate = summarize_po_parse(resolved_body or {})

    result.update(
        {
            "get_err": gerr,
            "got_resolved": got_resolved,
            "got_raw": got_raw,
            "raw_matches_resolved": got_raw == got_resolved,
            "diff_sent_vs_get": diff_create_get(sent, got_resolved),
            "hydrate": hydrate,
        }
    )
    return result


async def main() -> None:
    creds, err = _credentials_or_error()
    if err:
        print(err)
        return

    base, user, password = creds
    svc = os.environ.get("SAP_SERVICE", "").strip()
    svc2 = os.environ.get("SAP_SERVICE_2", svc).strip()
    cc = os.environ.get("SAP_COST_CENTER", "").strip()
    cc2 = os.environ.get("SAP_COST_CENTER_2", cc).strip()
    line = {
        "delivery_date": "2026-09-01",
        "order_unit": "EA",
    }

    scenarios = [
        (
            "GROUPED_2_services_1CC_each",
            _yser_form(
                [
                    {
                        **line,
                        "service": svc,
                        "short_text": "PO-SVC-A",
                        "unit_price": "100",
                        "net_price": "100",
                        "allocations": [{"cost_center": cc, "qty": "1"}],
                    },
                    {
                        **line,
                        "service": svc2,
                        "short_text": "PO-SVC-B",
                        "unit_price": "80",
                        "net_price": "80",
                        "allocations": [{"cost_center": cc2, "qty": "1"}],
                    },
                ],
                f"po-grp-{uuid.uuid4().hex[:6]}",
            ),
        ),
        (
            "MULTI_CC_1_service",
            _yser_form(
                [
                    {
                        **line,
                        "service": svc,
                        "short_text": "PO-MULTICC",
                        "unit_price": "50",
                        "net_price": "200",
                        "allocations": [
                            {"cost_center": cc, "qty": "1"},
                            {"cost_center": cc2, "qty": "3"},
                        ],
                    },
                ],
                f"po-cc-{uuid.uuid4().hex[:6]}",
            ),
        ),
        (
            "DUPLICATE_same_service",
            _yser_form(
                [
                    {
                        **line,
                        "service": svc,
                        "short_text": "PO-DUP-A",
                        "unit_price": "50",
                        "net_price": "50",
                        "allocations": [{"cost_center": cc, "qty": "1"}],
                    },
                    {
                        **line,
                        "service": svc,
                        "short_text": "PO-DUP-B",
                        "unit_price": "60",
                        "net_price": "60",
                        "allocations": [{"cost_center": cc, "qty": "1"}],
                    },
                ],
                f"po-dup-{uuid.uuid4().hex[:6]}",
            ),
        ),
    ]

    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        results = []
        for name, form in scenarios:
            print(f"\nRunning {name}...")
            results.append(await run_scenario(name, form, client, base))

        # Existing POs from prior probes
        for po in ["4030011016", "4030011027", "4030011028", "4030011029"]:
            resolved, gerr = await get_po(po_number=po, ticket_id="probe", document_type="YSER")
            raw = await fetch_po_raw_expand(client, base=base, po_number=po)
            results.append(
                {
                    "name": f"EXISTING_{po}",
                    "po": po,
                    "create_err": None,
                    "sent": None,
                    "get_err": gerr,
                    "got_resolved": summarize_po_get_body(resolved or {}),
                    "got_raw": summarize_po_get_body(raw or {}) if raw else [],
                    "raw_matches_resolved": summarize_po_get_body(resolved or {})
                    == summarize_po_get_body(raw or {}),
                    "diff_sent_vs_get": [],
                    "hydrate": summarize_po_parse(resolved or {}),
                }
            )

    print("\n" + "=" * 72)
    print("PO CREATE vs GET INVESTIGATION SUMMARY")
    print("=" * 72)
    for r in results:
        print(f"\n--- {r['name']} PO={r.get('po')} ---")
        if r.get("create_err"):
            print(f"  CREATE FAILED: {r['create_err']}")
            continue
        if r.get("get_err"):
            print(f"  GET FAILED: {r['get_err']}")
            continue
        if r.get("sent") is not None:
            print("  CREATE sent:")
            print(json.dumps(r["sent"], indent=4))
        print("  GET resolved:")
        print(json.dumps(r["got_resolved"], indent=4))
        if not r.get("raw_matches_resolved", True):
            print("  !! RAW $expand differs from resolved GET:")
            print(json.dumps(r["got_raw"], indent=4))
        else:
            print("  Raw $expand matches AgentOS resolved GET: YES")
        if r.get("diff_sent_vs_get"):
            print("  CREATE vs GET diffs:")
            for d in r["diff_sent_vs_get"]:
                print(f"    - {d}")
        else:
            print("  CREATE vs GET structure: MATCH" if r.get("sent") else "  (GET only)")
        print("  Hydrate:")
        print(json.dumps(r["hydrate"], indent=4))


if __name__ == "__main__":
    asyncio.run(main())
