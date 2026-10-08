#!/usr/bin/env python3
"""YUNB/YAST PO complex create + GET probes. Temp — not for commit."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import load_env
from scripts.integration.sap_po_doc_fixtures import (
    SAP_PO_DOC_YAST_ASSET,
    SAP_PO_DOC_YAST_INFO_RECORD,
    SAP_PO_DOC_YAST_MATERIAL,
    SAP_PO_DOC_YAST_MATERIAL_GROUP,
    SAP_PO_DOC_YAST_PRICE,
    SAP_PO_DOC_YAST_UNIT,
    SAP_PO_DOC_YUNB_COST_CENTER,
    SAP_PO_DOC_YUNB_MATERIAL,
    SAP_PO_DOC_YUNB_PRICE,
    SAP_PO_DOC_YUNB_UNIT,
    apply_po_doc_master_env,
)
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import (
    _credentials_or_error,
    _httpx_timeout,
    _sap_create_po,
    sap_po_configured,
)
from app.procurement.sap_po_payload import (
    PO_ACCT_NAV,
    PO_SCHEDULE_NAV,
    build_po_payload,
    iter_po_acct_post_bodies,
    po_entity_url,
    po_read_full_url,
    sap_po_service_root_url,
)
from app.procurement.sap_pr_client import sap_json_headers
from app.procurement.sap_ticket_form_read import form_from_po_sap_read
from app.config.settings import settings
from app.procurement.sap_odata_utils import odata_deferred_uri, odata_results_list
import httpx

load_env()
apply_po_doc_master_env()


def _header() -> dict[str, Any]:
    return {
        "purchasing_org": "1LFS",
        "purchasing_group": "A0B",
        "plant": "L001",
        "storage_location": "1001",
        "material_group": "S002-0001",
        "vendor": "1000000002",
        "tax_code": "XE",
        "header_note": "AgentOS complex PO test",
        "gl_account": "29000001",
        "controlling_area": "T1MG",
    }


def _line(
    *,
    material: str,
    short_text: str,
    qty: str = "5",
    price: str = SAP_PO_DOC_YUNB_PRICE,
    unit: str = SAP_PO_DOC_YUNB_UNIT,
    delivery: str = "2026-08-17",
    allocs: list[dict[str, str]] | None = None,
    asset: str = "",
    info_rec: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "material": material,
        "short_text": short_text,
        "delivery_date": delivery,
        "unit_price": price,
        "net_price": price,
        "order_unit": unit,
        "allocations": allocs or [{"cost_center": SAP_PO_DOC_YUNB_COST_CENTER, "qty": qty}],
    }
    if asset:
        row["asset"] = asset
    if info_rec:
        row["purchasing_info_record"] = info_rec
    return row


CASES: dict[str, dict[str, Any]] = {
    "yunb_simple_delivery": {
        "document_type": "YUNB",
        "form": {
            "header": _header(),
            "lines": [_line(material=SAP_PO_DOC_YUNB_MATERIAL, short_text="YUNB simple")],
        },
    },
    "yunb_multi_cc": {
        "document_type": "YUNB",
        "form": {
            "header": _header(),
            "lines": [
                _line(
                    material=SAP_PO_DOC_YUNB_MATERIAL,
                    short_text="YUNB multi-CC",
                    allocs=[
                        {"cost_center": "LCO91001A0", "qty": "2"},
                        {"cost_center": "LCO91001H0", "qty": "3"},
                    ],
                )
            ],
        },
    },
    "yunb_two_material": {
        "document_type": "YUNB",
        "form": {
            "header": _header(),
            "lines": [
                _line(
                    material=SAP_PO_DOC_YUNB_MATERIAL,
                    short_text="YUNB line 10",
                    delivery="2026-08-01",
                ),
                _line(
                    material=SAP_PO_DOC_YAST_MATERIAL,
                    short_text="YUNB line 20",
                    unit=SAP_PO_DOC_YAST_UNIT,
                    price=SAP_PO_DOC_YAST_PRICE,
                    delivery="2026-09-01",
                ),
            ],
        },
    },
    "yast_simple": {
        "document_type": "YAST",
        "form": {
            "header": {**_header(), "material_group": SAP_PO_DOC_YAST_MATERIAL_GROUP, "storage_location": "1003"},
            "lines": [
                _line(
                    material=SAP_PO_DOC_YAST_MATERIAL,
                    short_text="YAST simple",
                    unit=SAP_PO_DOC_YAST_UNIT,
                    price=SAP_PO_DOC_YAST_PRICE,
                    asset=SAP_PO_DOC_YAST_ASSET,
                    info_rec=SAP_PO_DOC_YAST_INFO_RECORD,
                )
            ],
        },
    },
    "yast_multi_cc": {
        "document_type": "YAST",
        "form": {
            "header": {**_header(), "material_group": SAP_PO_DOC_YAST_MATERIAL_GROUP, "storage_location": "1003"},
            "lines": [
                _line(
                    material=SAP_PO_DOC_YAST_MATERIAL,
                    short_text="YAST multi-CC",
                    unit=SAP_PO_DOC_YAST_UNIT,
                    price=SAP_PO_DOC_YAST_PRICE,
                    asset=SAP_PO_DOC_YAST_ASSET,
                    info_rec=SAP_PO_DOC_YAST_INFO_RECORD,
                    allocs=[
                        {"cost_center": "HCO91001H0", "qty": "2"},
                        {"cost_center": "HBM11001A0", "qty": "1"},
                    ],
                )
            ],
        },
    },
    "yast_two_material": {
        "document_type": "YAST",
        "form": {
            "header": {**_header(), "material_group": SAP_PO_DOC_YAST_MATERIAL_GROUP, "storage_location": "1003"},
            "lines": [
                _line(
                    material=SAP_PO_DOC_YUNB_MATERIAL,
                    short_text="YAST line A",
                    unit=SAP_PO_DOC_YUNB_UNIT,
                    price="100.00",
                    asset=SAP_PO_DOC_YAST_ASSET,
                    info_rec=SAP_PO_DOC_YAST_INFO_RECORD,
                ),
                _line(
                    material=SAP_PO_DOC_YAST_MATERIAL,
                    short_text="YAST line B",
                    unit=SAP_PO_DOC_YAST_UNIT,
                    price=SAP_PO_DOC_YAST_PRICE,
                    delivery="2026-09-15",
                    asset=SAP_PO_DOC_YAST_ASSET,
                    info_rec=SAP_PO_DOC_YAST_INFO_RECORD,
                    allocs=[{"cost_center": "", "qty": "3"}],
                ),
            ],
        },
    },
}


async def _get_urls(po: str) -> dict[str, Any]:
    creds, err = _credentials_or_error()
    if err:
        return {"error": err}
    base, user, pw = creds
    urls = {
        "header_expand": po_read_full_url(base, po),
        "items_only": f"{po_entity_url(base, po)}/to_PurchaseOrderItem?$format=json",
        "item_10": (
            f"{base}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/"
            f"A_PurchaseOrderItem(PurchaseOrder='{po}',PurchaseOrderItem='10')"
            f"?$expand={PO_SCHEDULE_NAV},{PO_ACCT_NAV}&$format=json"
        ),
    }
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, pw),
    ) as client:
        root = sap_po_service_root_url(base)
        tok = await client.get(root, headers=sap_json_headers(csrf_token="fetch"))
        csrf = tok.headers.get("x-csrf-token", "")
        for name, url in urls.items():
            r = await client.get(url, headers=sap_json_headers(csrf_token=csrf))
            body: Any = None
            try:
                body = r.json()
            except Exception:
                body = r.text[:300]
            items_deferred = acct_inline = sched_inline = None
            if isinstance(body, dict):
                d = body.get("d", body)
                node = d.get("to_PurchaseOrderItem")
                if node is None and name == "item_10":
                    node = d
                if isinstance(node, dict):
                    items_deferred = bool(odata_deferred_uri(node))
                    results = odata_results_list(node)
                    if results:
                        it = results[0]
                        acct_inline = not odata_deferred_uri(it.get(PO_ACCT_NAV))
                        sched_inline = not odata_deferred_uri(it.get(PO_SCHEDULE_NAV))
                elif name == "item_10":
                    acct_inline = not odata_deferred_uri(d.get(PO_ACCT_NAV))
                    sched_inline = not odata_deferred_uri(d.get(PO_SCHEDULE_NAV))
            out[name] = {
                "status": r.status_code,
                "items_deferred": items_deferred,
                "acct_inline": acct_inline,
                "schedule_inline": sched_inline,
            }
    return out


async def run_case(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    dt = spec["document_type"]
    form = normalize_form(dt, spec["form"])
    apply_procurement_defaults(form, document_type=dt, kind="PO")
    try:
        payload = build_po_payload(form=form, document_type=dt, ticket_id=str(uuid.uuid4()))
    except Exception as e:
        return {"case": name, "create": "fail", "error": str(e)}

    acct_posts = iter_po_acct_post_bodies(form=form, document_type=dt, po_number="PREVIEW")
    po, err = await _sap_create_po(
        payload=payload,
        ticket_id=f"complex-{name}",
        form=form,
        document_type=dt,
    )
    result: dict[str, Any] = {
        "case": name,
        "document_type": dt,
        "lines": len(form.get("lines") or []),
        "acct_post_bodies": len(acct_posts),
        "create": "201" if po and not err else "fail",
        "po": po,
        "error": (err or "")[:500],
    }
    if po and not err:
        get_body, gerr = await _get_po_raw(po)
        result["get_error"] = gerr
        if get_body:
            ui_form = form_from_po_sap_read(get_body, document_type=dt)
            result["ui_lines"] = len(ui_form.get("lines") or [])
            if ui_form.get("lines"):
                ln = ui_form["lines"][0]
                result["ui_line1"] = {
                    "material": ln.get("material"),
                    "delivery_date": ln.get("delivery_date"),
                    "alloc_count": len(ln.get("allocations") or []),
                    "cc": [
                        (a.get("cost_center"), a.get("qty"))
                        for a in (ln.get("allocations") or [])
                        if isinstance(a, dict)
                    ],
                }
        result["get_probes"] = await _get_urls(po)
    return result


async def _get_po_raw(po: str) -> tuple[dict[str, Any] | None, str | None]:
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, pw = creds
    url = po_read_full_url(base, po)
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, pw),
    ) as client:
        root = sap_po_service_root_url(base)
        tok = await client.get(root, headers=sap_json_headers(csrf_token="fetch"))
        csrf = tok.headers.get("x-csrf-token", "")
        r = await client.get(url, headers=sap_json_headers(csrf_token=csrf))
        if r.status_code >= 400:
            return None, r.text[:400]
        return r.json(), None


async def main() -> None:
    if not sap_po_configured():
        print("SAP not configured")
        sys.exit(1)
    results = []
    for name, spec in CASES.items():
        print(f"Running {name}...", flush=True)
        results.append(await run_case(name, spec))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
