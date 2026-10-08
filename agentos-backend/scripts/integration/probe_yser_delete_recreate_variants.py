#!/usr/bin/env python3
"""Extended probes: recreate strategies + IsDeleted on to_Services rows."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from app.config.settings import settings
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_pr_client import (
    _credentials_or_error,
    _httpx_timeout,
    _request_with_csrf_retry,
    create_pr,
    get_pr,
    sap_post_create_headers,
    sap_service_root_url,
)
from app.procurement.sap_pr_z_client import delete_yser_pr
from app.procurement.sap_pr_z_payload import (
    build_z_yser_item_delete_post_body,
    build_z_yser_pr_payload,
    count_z_pr_sap_items,
    finalize_z_yser_update_post_body,
    form_from_z_pr_read,
    normalize_service_performer_code,
    verify_yser_read_matches_form,
    z_pr_collection_url,
)
from app.procurement.sap_ticket_form_read import load_form_from_sap
from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults
from scripts.integration.procurement_api_common import load_env
from scripts.integration.probe_yser_delete_recreate_live import multi_cc_form
from scripts.integration.run_pr_create_yser import complex_yser_form


async def z_post(tid: str, inner: dict) -> tuple[bool, str]:
    creds, err = _credentials_or_error()
    if err:
        return False, err
    base, user, password = creds
    post = finalize_z_yser_update_post_body(inner)
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=tid,
            method="POST",
            url=z_pr_collection_url(base),
            json_body=post,
            headers_builder=sap_post_create_headers,
            csrf_service_root=sap_service_root_url(base),
        )
    if resp.status_code >= 400:
        try:
            e = resp.json().get("error", {})
        except Exception:
            e = resp.text[:500]
        return False, f"HTTP {resp.status_code}: {str(e)[:350]}"
    return True, ""


async def read_state(pr: str, target: dict, tid: str) -> dict:
    body, _ = await get_pr(pr_number=pr, ticket_id=tid, document_type="YSER")
    if not body:
        return {"error": "no body"}
    ui = form_from_z_pr_read(body, document_type="YSER", seed_form=target)
    return {
        "ui_lines": len(ui.get("lines") or []),
        "sap_items": count_z_pr_sap_items(body),
        "verify_mm": verify_yser_read_matches_form(body, form=target)[:3],
    }


async def main() -> int:
    load_env()
    apply_integration_reference_defaults()
    out: list[dict] = []
    tid = str(uuid.uuid4())

    # --- Grouped service: after item delete, try recreate variants ---
    form = normalize_form("YSER", complex_yser_form(header_note=f"var-{uuid.uuid4().hex[:6]}"))
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    pr, err = await create_pr(ticket_id=tid, form=form, document_type="YSER")
    if not pr:
        print("create failed", err)
        return 1
    hydrated, _, _ = await load_form_from_sap(
        kind="PR", document_type="YSER", sap_id=pr, ticket_id=tid, seed_form=form
    )
    item_no = str((hydrated or {}).get("lines", [{}])[0].get("sap_pr_item") or "10")
    target = copy.deepcopy(form)
    target["lines"] = [copy.deepcopy(form["lines"][0])]
    svc_b = normalize_service_performer_code(form["lines"][1]["service"])

    ok_del, err_del = await z_post(
        tid + "-del",
        build_z_yser_item_delete_post_body(pr_number=pr, item_numbers=[item_no]),
    )
    out.append({"case": "grouped_delete_item", "post_ok": ok_del, "err": err_del})

    variants = []
    upd_same = copy.deepcopy(target)
    for line in upd_same["lines"]:
        line["sap_pr_item"] = item_no
        line["purchase_requisition_item"] = item_no
    variants.append(
        (
            "recreate_same_item_for_update",
            build_z_yser_pr_payload(
                form=upd_same,
                document_type="YSER",
                pr_number=pr,
                for_update=True,
                sap_item_count=1,
            ),
        )
    )
    upd_new = copy.deepcopy(target)
    for line in upd_new["lines"]:
        line.pop("sap_pr_item", None)
        line["purchase_requisition_item"] = "20"
    variants.append(
        (
            "recreate_new_item_20",
            build_z_yser_pr_payload(
                form=upd_new,
                document_type="YSER",
                pr_number=pr,
                for_update=True,
                sap_item_count=0,
            ),
        )
    )
    upd_create = copy.deepcopy(target)
    for line in upd_create["lines"]:
        line["sap_pr_item"] = item_no
        line["purchase_requisition_item"] = item_no
    inner_c = build_z_yser_pr_payload(
        form=upd_create, document_type="YSER", pr_number=pr, for_update=False
    )
    inner_c["PRNumber"] = pr
    variants.append(("recreate_create_shape", inner_c))

    for name, inner in variants:
        ok, perr = await z_post(tid + name, inner)
        state = await read_state(pr, target, tid + name)
        out.append({"case": f"grouped_{name}", "post_ok": ok, "post_err": perr[:200], **state})

    # --- Service IsDeleted on to_Services (no item delete) ---
    pr2, _ = await create_pr(ticket_id=tid + "2", form=form, document_type="YSER")
    one_line = copy.deepcopy(form)
    one_line["lines"] = [one_line["lines"][0]]
    full = build_z_yser_pr_payload(
        form=form,
        document_type="YSER",
        pr_number=pr2 or "",
        for_update=True,
        sap_item_count=1,
    )
    svc_rows = full["to_Items"][0]["to_Services"]
    new_rows = []
    for s in svc_rows:
        code = normalize_service_performer_code(s.get("Service", ""))
        if code == svc_b:
            new_rows.append({**s, "IsDeleted": "X"})
        else:
            new_rows.append(s)
    full["to_Items"][0]["to_Services"] = new_rows
    ok_svc, err_svc = await z_post(tid + "svcdel", full)
    state_svc = await read_state(pr2 or "", one_line, tid + "svcdel")
    out.append(
        {
            "case": "service_row_isdeleted",
            "post_ok": ok_svc,
            "post_err": err_svc[:200],
            **state_svc,
        }
    )

    # --- Multi-CC: item delete + recreate 1 CC ---
    mcc = normalize_form("YSER", multi_cc_form(header_note=f"cc-{uuid.uuid4().hex[:6]}"))
    apply_procurement_defaults(mcc, document_type="YSER", kind="PR")
    pr3, _ = await create_pr(ticket_id=tid + "3", form=mcc, document_type="YSER")
    if pr3:
        h3, _, _ = await load_form_from_sap(
            kind="PR", document_type="YSER", sap_id=pr3, ticket_id=tid + "3", seed_form=mcc
        )
        ino = str((h3 or {}).get("lines", [{}])[0].get("sap_pr_item") or "10")
        tgt = copy.deepcopy(mcc)
        tgt["lines"][0]["allocations"] = [copy.deepcopy(mcc["lines"][0]["allocations"][0])]
        tgt["lines"][0]["valuation_price"] = "100"
        tgt["lines"][0]["unit_price"] = "100"
        await z_post(
            tid + "3del",
            build_z_yser_item_delete_post_body(pr_number=pr3, item_numbers=[ino]),
        )
        upd3 = copy.deepcopy(tgt)
        upd3["lines"][0]["sap_pr_item"] = ino
        upd3["lines"][0]["purchase_requisition_item"] = ino
        inner3 = build_z_yser_pr_payload(
            form=upd3,
            document_type="YSER",
            pr_number=pr3,
            for_update=True,
            sap_item_count=1,
        )
        ok3, err3 = await z_post(tid + "3rec", inner3)
        state3 = await read_state(pr3, tgt, tid + "3rec")
        out.append(
            {
                "case": "multi_cc_delete_recreate",
                "post_ok": ok3,
                "post_err": err3[:200],
                **state3,
            }
        )
        await delete_yser_pr(sap_id=pr3, ticket_id=tid + "3")

    await delete_yser_pr(sap_id=pr, ticket_id=tid)
    if pr2:
        await delete_yser_pr(sap_id=pr2, ticket_id=tid + "2")

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
