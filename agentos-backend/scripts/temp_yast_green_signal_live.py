#!/usr/bin/env python3
"""YAST PR/PO green-signal matrix — no GL/CO on outbound acct (SAP derives). Temp."""

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
    SAP_PO_DOC_YUNB_MATERIAL,
    SAP_PO_DOC_YUNB_UNIT,
    SAP_PO_DOC_YUNB_PRICE,
    apply_po_doc_master_env,
)
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_pr_client import create_pr, get_pr, update_pr, _credentials_or_error, _httpx_timeout, sap_service_root_url, sap_json_headers
from app.procurement.sap_pr_payload import build_pr_payload
from app.procurement.sap_po_client import _sap_create_po, get_po, sap_po_configured
from app.procurement.sap_po_payload import build_po_payload, PO_ACCT_CREATE_NAV
from app.procurement.sap_ticket_form_read import form_from_pr_sap_read, form_from_po_sap_read
from app.config.settings import settings
import httpx

load_env()
apply_po_doc_master_env()


def _yast_header() -> dict[str, str]:
    return {
        "purchasing_org": "1LFS",
        "purchasing_group": "A0B",
        "plant": "L001",
        "storage_location": "1003",
        "material_group": SAP_PO_DOC_YAST_MATERIAL_GROUP,
        "vendor": "1000000002",
        "header_note": "YAST green signal",
    }


def _yast_line(
    *,
    material: str = SAP_PO_DOC_YAST_MATERIAL,
    short_text: str,
    qty: str = "5",
    unit: str = SAP_PO_DOC_YAST_UNIT,
    price: str = SAP_PO_DOC_YAST_PRICE,
    delivery: str = "2026-08-15",
    allocs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "material": material,
        "short_text": short_text,
        "asset": SAP_PO_DOC_YAST_ASSET,
        "delivery_date": delivery,
        "unit_price": price,
        "valuation_price": price,
        "net_price": price,
        "order_unit": unit,
        "purchasing_info_record": SAP_PO_DOC_YAST_INFO_RECORD,
        "allocations": allocs or [{"cost_center": "", "qty": qty}],
    }


def _prep_pr_form(form: dict[str, Any]) -> dict[str, Any]:
    f = normalize_form("YAST", form)
    apply_procurement_defaults(f, document_type="YAST", kind="PR")
    return f


def _prep_po_form(form: dict[str, Any]) -> dict[str, Any]:
    f = normalize_form("YAST", form)
    apply_procurement_defaults(f, document_type="YAST", kind="PO")
    return f


def _sent_gl(acct_rows: list[dict]) -> bool:
    for r in acct_rows:
        if r.get("GLAccount") or r.get("ControllingArea"):
            return True
    return False


async def _pr_acct_raw(pr: str) -> list[dict]:
    base, u, p = _credentials_or_error()[0]
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(u, p),
    ) as c:
        t = await c.get(sap_service_root_url(base), headers=sap_json_headers(csrf_token="fetch"))
        csrf = t.headers.get("x-csrf-token", "")
        url = (
            f"{base}/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/"
            f"A_PurchaseRequisitionItem(PurchaseRequisition='{pr}',PurchaseRequisitionItem='10')"
            f"/to_PurchaseReqnAcctAssgmt?$format=json"
        )
        r = await c.get(url, headers=sap_json_headers(csrf_token=csrf))
        out = []
        for row in r.json().get("d", {}).get("results", []):
            out.append(
                {
                    k: row.get(k)
                    for k in (
                        "Quantity",
                        "CostCenter",
                        "MasterFixedAsset",
                        "GLAccount",
                        "ControllingArea",
                    )
                }
            )
        return out


async def run_pr_case(name: str, form: dict[str, Any], *, resubmit_qty: str | None = None) -> dict[str, Any]:
    f = _prep_pr_form(form)
    payload = build_pr_payload(form=f, document_type="YAST", ticket_id=str(uuid.uuid4()))
    acct_sent = payload["to_PurchaseReqnItem"][0].get("to_PurchaseReqnAcctAssgmt", [])
    out: dict[str, Any] = {
        "case": name,
        "sent_gl": _sent_gl(acct_sent),
        "sent_acct": acct_sent,
    }
    pr, err = await create_pr(ticket_id=f"gs-{name}", form=f, document_type="YAST")
    out["create"] = "201" if pr and not err else "fail"
    out["id"] = pr
    out["error"] = (err or "")[:300]
    if not pr or err:
        return out
    out["sap_acct"] = await _pr_acct_raw(pr)
    body, _ = await get_pr(pr_number=pr, ticket_id="t", document_type="YAST")
    ln = (form_from_pr_sap_read(body or {}, document_type="YAST").get("lines") or [{}])[0]
    out["read"] = {
        "acct_cat": ln.get("account_assignment_cat"),
        "asset": ln.get("asset"),
        "allocs": ln.get("allocations"),
    }
    if resubmit_qty:
        f2 = _prep_pr_form(form)
        f2["lines"][0]["allocations"] = [{"cost_center": "", "qty": resubmit_qty}]
        _, uerr = await update_pr(sap_id=pr, ticket_id=f"gs-{name}-u", form=f2, document_type="YAST")
        out["resubmit"] = "ok" if not uerr else "fail"
        out["resubmit_error"] = (uerr or "")[:300]
        if not uerr:
            out["sap_acct_after"] = await _pr_acct_raw(pr)
            body2, _ = await get_pr(pr_number=pr, ticket_id="t", document_type="YAST")
            ln2 = (form_from_pr_sap_read(body2 or {}, document_type="YAST").get("lines") or [{}])[0]
            out["read_after"] = {"allocs": ln2.get("allocations")}
    return out


async def run_po_case(name: str, form: dict[str, Any]) -> dict[str, Any]:
    f = _prep_po_form(form)
    payload = build_po_payload(form=f, document_type="YAST", ticket_id=str(uuid.uuid4()))
    row = payload["to_PurchaseOrderItem"][0]
    acct_sent = row.get(PO_ACCT_CREATE_NAV, [])
    out: dict[str, Any] = {
        "case": name,
        "sent_gl": _sent_gl(acct_sent),
        "sent_acct": acct_sent,
    }
    po, err = await _sap_create_po(
        payload=payload, ticket_id=f"gs-po-{name}", form=f, document_type="YAST"
    )
    out["create"] = "201" if po and not err else "fail"
    out["id"] = po
    out["error"] = (err or "")[:300]
    if not po or err:
        return out
    body, _ = await get_po(po_number=po, ticket_id="t")
    ui = form_from_po_sap_read(body or {}, document_type="YAST")
    out["read_lines"] = [
        {
            "material": ln.get("material"),
            "asset": ln.get("asset"),
            "acct_cat": ln.get("account_assignment_cat"),
            "delivery": ln.get("delivery_date"),
            "allocs": ln.get("allocations"),
        }
        for ln in ui.get("lines") or []
    ]
    return out


async def main() -> None:
    if not sap_po_configured():
        print("SAP not configured")
        sys.exit(1)

    results: list[dict] = []

    results.append(
        await run_pr_case(
            "pr_simple",
            {"header": _yast_header(), "lines": [_yast_line(short_text="PR simple")]},
            resubmit_qty="7",
        )
    )
    results.append(
        await run_pr_case(
            "pr_multi_qty",
            {
                "header": _yast_header(),
                "lines": [
                    _yast_line(
                        short_text="PR multi-qty",
                        qty="5",
                        allocs=[
                            {"cost_center": "", "qty": "2"},
                            {"cost_center": "", "qty": "3"},
                        ],
                    )
                ],
            },
        )
    )
    results.append(
        await run_pr_case(
            "pr_two_material",
            {
                "header": _yast_header(),
                "lines": [
                    _yast_line(
                        material=SAP_PO_DOC_YUNB_MATERIAL,
                        short_text="PR line A",
                        unit=SAP_PO_DOC_YUNB_UNIT,
                        price=SAP_PO_DOC_YUNB_PRICE,
                        delivery="2026-08-01",
                        allocs=[{"cost_center": "", "qty": "5"}],
                    ),
                    _yast_line(
                        short_text="PR line B",
                        delivery="2026-09-01",
                        allocs=[{"cost_center": "", "qty": "3"}],
                    ),
                ],
            },
        )
    )

    po_header = {**_yast_header(), "tax_code": "XE", "header_note": "YAST PO green"}
    results.append(
        await run_po_case(
            "po_simple",
            {"header": po_header, "lines": [_yast_line(short_text="PO simple")]},
        )
    )
    results.append(
        await run_po_case(
            "po_multi_qty",
            {
                "header": po_header,
                "lines": [
                    _yast_line(
                        short_text="PO multi-qty",
                        allocs=[
                            {"cost_center": "", "qty": "2"},
                            {"cost_center": "", "qty": "3"},
                        ],
                    )
                ],
            },
        )
    )
    results.append(
        await run_po_case(
            "po_two_material",
            {
                "header": po_header,
                "lines": [
                    _yast_line(
                        material=SAP_PO_DOC_YUNB_MATERIAL,
                        short_text="PO line A",
                        unit=SAP_PO_DOC_YUNB_UNIT,
                        price=SAP_PO_DOC_YUNB_PRICE,
                        delivery="2026-08-01",
                    ),
                    _yast_line(short_text="PO line B", delivery="2026-09-01", allocs=[{"cost_center": "", "qty": "3"}]),
                ],
            },
        )
    )

    print(json.dumps(results, indent=2))
    fails = [r for r in results if r.get("create") != "201" or r.get("sent_gl")]
    print("\n=== SUMMARY ===")
    for r in results:
        flag = "PASS" if r.get("create") == "201" and not r.get("sent_gl") else "FAIL"
        extra = ""
        if r.get("resubmit"):
            extra = f" resubmit={r['resubmit']}"
        print(f"{flag} {r['case']:18} {r.get('id','')} sent_gl={r.get('sent_gl')}{extra}")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
