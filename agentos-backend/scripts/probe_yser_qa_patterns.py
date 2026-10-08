#!/usr/bin/env python3
"""Probe QA YSER PR/PO GET shapes — services per item, CC patterns, hydrate mapping."""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import load_env

load_env()

import httpx
from app.config.settings import settings
from app.procurement.sap_odata_utils import odata_entity_properties, odata_results_list, odata_text
from app.procurement.sap_po_client import (
    PO_HEADER_COLLECTION,
    _base_root,
    _po_csrf_root,
    get_po,
)
from app.procurement.sap_po_z_payload import form_from_z_po_read, parse_z_po_items_from_read
from app.procurement.sap_pr_client import (
    _credentials_or_error,
    _httpx_timeout,
    _request_with_csrf_retry,
    sap_json_headers,
)
from app.procurement.sap_pr_z_client import get_yser_pr
from app.procurement.sap_pr_z_payload import (
    cluster_yser_pr_service_entries,
    form_from_z_pr_read,
    z_pr_collection_url,
    z_pr_service_root_url,
)

KNOWN_PRS = [
    "1010000452",
    "1010000453",
    "1010000454",
    "1010000455",
    "1010000456",
    "1010000457",
    "1010000458",
    "1010000461",
    "1010000462",
    "1010000463",
    "1010000464",
    "1010000465",
    "1010000468",
]
KNOWN_POS = ["4030011016", "4030011017", "4030011027"]


@dataclass
class DocShape:
    doc_id: str
    kind: str
    n_items: int = 0
    n_services: int = 0
    n_clusters: int = 0
    n_ui_lines: int = 0
    services_per_item: list[int] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    err: str = ""


def _svc_rows(svcs: list) -> list[dict]:
    rows = []
    for entry in svcs:
        sp = odata_entity_properties(entry)
        rows.append(
            {
                "service": odata_text(sp.get("Service"))[-10:],
                "text": odata_text(sp.get("ShortText"))[:40],
                "qty": odata_text(sp.get("Quantity")),
                "gross": odata_text(sp.get("GrossPrice"))[:12],
                "cc": odata_text(sp.get("CostCenter")),
                "distr": odata_text(sp.get("DistrQuantity")),
                "acct": odata_text(sp.get("PRAcctAssgmtNumber")),
            }
        )
    return rows


def analyze_pr_body(pr_no: str, body: dict) -> DocShape:
    shape = DocShape(pr_no, "PR")
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    form = form_from_z_pr_read(body, document_type="YSER")
    shape.n_ui_lines = len(form.get("lines") or [])
    all_clusters = 0
    for entry in odata_results_list(root.get("to_Items")):
        props = odata_entity_properties(entry)
        if odata_text(props.get("IsDeleted")) == "X":
            continue
        shape.n_items += 1
        svcs = odata_results_list(props.get("to_Services") or entry.get("to_Services"))
        shape.n_services += len(svcs)
        shape.services_per_item.append(len(svcs))
        clusters = cluster_yser_pr_service_entries(svcs)
        all_clusters += len(clusters)
        rows = _svc_rows(svcs)
        ccs = [r["cc"] for r in rows]
        services = [r["service"] for r in rows]
        texts = [r["text"] for r in rows]
        shape.items.append(
            {
                "PRItem": odata_text(props.get("PRItem")),
                "MatGrp": odata_text(props.get("MaterialGroup"))[:12],
                "ItemCC": odata_text(props.get("CostCenter")),
                "n_svc": len(svcs),
                "n_clust": len(clusters),
                "rows": rows,
            }
        )
        if len(svcs) == 1:
            shape.patterns.append("1svc_per_item")
        elif len(set(ccs)) == len(ccs) and len(set(services)) == 1 and len(set(texts)) <= 1:
            shape.patterns.append("multi_cc_split")
        elif len(set(services)) < len(services):
            shape.patterns.append("multi_svc_dup_code")
        elif len(set(ccs)) < len(ccs):
            shape.patterns.append("multi_svc_same_cc")
        else:
            shape.patterns.append("multi_svc_diff_code")
    shape.n_clusters = all_clusters
    return shape


def analyze_po_body(po_no: str, body: dict) -> DocShape:
    shape = DocShape(po_no, "PO")
    form = form_from_z_po_read(body)
    shape.n_ui_lines = len(form.get("lines") or [])
    _, snaps = parse_z_po_items_from_read(body)
    for snap in snaps:
        if snap.is_deleted:
            continue
        shape.n_items += 1
        n = len(snap.services)
        shape.n_services += n
        shape.services_per_item.append(n)
        rows = []
        ccs = []
        services = []
        for svc in snap.services:
            services.append((svc.service or "")[-10:])
            for seg in svc.acct_segments:
                ccs.append(seg.cost_center)
                rows.append(
                    {
                        "service": (svc.service or "")[-10:],
                        "text": (svc.short_text or "")[:40],
                        "cc": seg.cost_center,
                        "qty": seg.quantity,
                    }
                )
        shape.items.append(
            {
                "POItem": snap.item_number,
                "PRLink": snap.purchase_requisition_item,
                "n_svc": n,
                "rows": rows,
            }
        )
        if n == 1:
            shape.patterns.append("1svc_per_item")
        elif len(set(ccs)) == len(ccs) and len(set(services)) == 1:
            shape.patterns.append("multi_cc_split")
        elif len(set(services)) < len(services):
            shape.patterns.append("multi_svc_dup_code")
        else:
            shape.patterns.append("multi_svc_diff_code")
    return shape


async def list_recent_yser_prs(client, base: str, limit: int = 35) -> list[str]:
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id="list-pr",
        method="GET",
        url=f"{z_pr_collection_url(base)}?$orderby=PRNumber desc&$top={limit}&$select=PRNumber",
        json_body=None,
        headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
        csrf_service_root=z_pr_service_root_url(base),
    )
    if resp.status_code >= 400:
        return []
    root = resp.json().get("d") or resp.json()
    out = []
    for row in odata_results_list(root.get("results") if isinstance(root, dict) else root):
        if isinstance(row, dict):
            pr = odata_text(row.get("PRNumber"))
            if pr:
                out.append(pr.lstrip("0") or pr)
    return out


async def list_recent_yser_pos(client, base: str, limit: int = 25) -> list[str]:
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id="list-po",
        method="GET",
        url=(
            f"{_base_root(base)}{PO_HEADER_COLLECTION}"
            f"?$filter=PurchaseOrderType eq 'YSER'"
            f"&$orderby=PurchaseOrder desc&$top={limit}&$select=PurchaseOrder"
        ),
        json_body=None,
        headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
        csrf_service_root=_po_csrf_root(base, document_type="YSER"),
    )
    if resp.status_code >= 400:
        return []
    root = resp.json().get("d") or resp.json()
    return [
        odata_text(r.get("PurchaseOrder")).lstrip("0") or odata_text(r.get("PurchaseOrder"))
        for r in odata_results_list(root.get("results") if isinstance(root, dict) else root)
        if isinstance(r, dict) and odata_text(r.get("PurchaseOrder"))
    ]


async def main() -> None:
    creds, err = _credentials_or_error()
    if err:
        print(err)
        return
    base, user, password = creds
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        listed_prs = await list_recent_yser_prs(client, base)
        listed_pos = await list_recent_yser_pos(client, base)

    pr_ids = list(dict.fromkeys(listed_prs + KNOWN_PRS))[:32]
    po_ids = list(dict.fromkeys(listed_pos + KNOWN_POS))[:18]
    print(f"QA sample: {len(pr_ids)} PRs (SAP listed {len(listed_prs)}), {len(po_ids)} POs\n")

    shapes: list[DocShape] = []
    for pr in pr_ids:
        body, gerr = await get_yser_pr(pr_number=pr, ticket_id="pat")
        shapes.append(analyze_pr_body(pr, body) if body else DocShape(pr, "PR", err=gerr or "empty"))
    for po in po_ids:
        body, gerr = await get_po(po_number=po, ticket_id="pat", document_type="YSER")
        shapes.append(analyze_po_body(po, body) if body else DocShape(po, "PO", err=gerr or "empty"))

    pr_ok = [s for s in shapes if s.kind == "PR" and not s.err]
    po_ok = [s for s in shapes if s.kind == "PO" and not s.err]

    print("=== PR: services-per-item layout ===")
    for k, v in Counter(tuple(s.services_per_item) for s in pr_ok).most_common(15):
        print(f"  layout {k}: {v} docs")

    print("\n=== PR: clustering impact ===")
    changed = [s for s in pr_ok if s.n_services != s.n_ui_lines]
    print(f"  Clustering changes line count: {len(changed)}/{len(pr_ok)} docs")
    for s in changed:
        print(f"    {s.doc_id}: svc_rows={s.n_services} -> ui_lines={s.n_ui_lines} layout={s.services_per_item}")

    print("\n=== PR: multi-service-per-item (grouped OPS style) ===")
    grouped = [s for s in pr_ok if any(x > 1 for x in s.services_per_item)]
    print(f"  Count: {len(grouped)}")
    for s in grouped:
        print(f"    {s.doc_id}: layout={s.services_per_item} svc={s.n_services} ui={s.n_ui_lines} {s.patterns}")

    print("\n=== KEY PRs (raw service rows) ===")
    for doc in [
        "1010000461",
        "1010000462",
        "1010000463",
        "1010000464",
        "1010000465",
        "1010000468",
        "1010000452",
        "1010000456",
    ]:
        s = next((x for x in pr_ok if x.doc_id == doc), None)
        if not s:
            continue
        print(f"\nPR {doc}: items={s.n_items} svc_rows={s.n_services} clusters={s.n_clusters} ui={s.n_ui_lines}")
        for it in s.items:
            print(f"  Item {it['PRItem']} grp={it['MatGrp']} itemCC={it['ItemCC']} svc={it['n_svc']} clust={it['n_clust']}")
            for r in it["rows"]:
                print(
                    f"    ..{r['service']} text={r['text']!r} cc={r['cc']} "
                    f"distr={r['distr']} acct={r['acct']} qty={r['qty']}"
                )

    print("\n=== PO samples ===")
    for s in po_ok[:15]:
        print(f"  PO {s.doc_id}: layout={s.services_per_item} svc={s.n_services} ui={s.n_ui_lines} {s.patterns}")

    total_svc = sum(s.n_services for s in pr_ok)
    total_ui = sum(s.n_ui_lines for s in pr_ok)
    print(f"\nAGGREGATE {len(pr_ok)} PRs: sap_service_rows={total_svc} hydrated_ui_lines={total_ui}")


if __name__ == "__main__":
    asyncio.run(main())
