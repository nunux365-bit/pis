#!/usr/bin/env python3
"""Temporary live review: YAST + attachments API → SAP → hydrate. Delete after run."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.sap_po_doc_fixtures import (  # noqa: E402
    apply_po_doc_master_env,
    apply_yast_pr_doc_env,
)
from scripts.integration.procurement_api_common import (  # noqa: E402
    api_base,
    create_po_via_api,
    create_pr_via_api,
    get_ticket_via_api,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_access_token,
)
from scripts.integration.run_pr_create_yast import yast_multi_asset_form  # noqa: E402
from scripts.integration.integration_reference_defaults import (  # noqa: E402
    apply_integration_reference_defaults,
    apply_pr_integration_fixtures,
    load_po_db_context,
)
from scripts.integration.po_live_common import build_po_matrix_cases  # noqa: E402

from app.procurement.field_schema import (  # noqa: E402
    allocation_field_specs,
    block_field_specs,
    normalize_form,
    validate_form,
)
from app.procurement.sap_defaults import apply_procurement_defaults  # noqa: E402
from app.procurement.sap_po_client import get_po  # noqa: E402
from app.procurement.sap_pr_client import get_pr  # noqa: E402
from app.procurement.sap_ticket_form_read import (  # noqa: E402
    form_from_po_sap_read,
    form_from_pr_sap_read,
)
from app.procurement.sap_yast_acct import asset_codes_equal  # noqa: E402
import httpx  # noqa: E402

results: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
    status = "SKIP" if skip else ("PASS" if ok else "FAIL")
    results.append((name, status, detail[:400]))
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[status]
    print(f"\n[{mark} {status}] {name}")
    if detail:
        print(f"    {detail[:500]}")


def main() -> int:
    # Catalogue + SAP QAS master from remote/local ``pr_po_reference_values``.
    # Doc-master env (1LFS / 4100000018 / 7100001182) fails on current QAS.
    apply_integration_reference_defaults()
    apply_pr_integration_fixtures("YAST")
    # Distinct 1MGH assets from remote asset master (public Anln1).
    os.environ["SAP_ASSET"] = "001000000000"
    os.environ["SAP_ASSET_2"] = "001000000001"
    os.environ.setdefault("AGENTOS_API_BASE_URL", "http://127.0.0.1:8000")
    print("API:", api_base())
    print(
        "ORG",
        os.environ.get("SAP_PUR_ORG"),
        "PLANT",
        os.environ.get("SAP_PLANT"),
        "MAT",
        os.environ.get("SAP_MATERIAL"),
        "MG",
        os.environ.get("SAP_MATERIAL_GROUP"),
        "A1",
        os.environ.get("SAP_ASSET"),
        "A2",
        os.environ.get("SAP_ASSET_2"),
    )

    token = resolve_access_token(email=None, password=None, token=None)
    a1, a2 = os.environ["SAP_ASSET"], os.environ["SAP_ASSET_2"]

    alloc_keys = [f.key for f in allocation_field_specs("YAST")]
    asset_block = next(f for f in block_field_specs("YAST") if f.key == "asset")
    rec("schema YAST allocation = asset+qty", alloc_keys == ["asset", "qty"], f"alloc={alloc_keys}")
    rec(
        "schema YAST line asset hidden/not required",
        asset_block.ui_visible is False and asset_block.required_pr is False,
        f"ui_visible={asset_block.ui_visible} required_pr={asset_block.required_pr}",
    )

    dup_form = yast_multi_asset_form(header_note="dup-test")
    dup_form["lines"][0]["allocations"] = [
        {"asset": a1, "qty": "1"},
        {"asset": a1.lstrip("0") or a1, "qty": "1"},
    ]
    dup_form = normalize_form("YAST", dup_form)
    apply_procurement_defaults(dup_form, document_type="YAST", kind="PR")
    errs = validate_form(kind="PR", document_type="YAST", form=dup_form)
    rec(
        "validate_form rejects duplicate asset",
        any("duplicate asset" in e for e in errs),
        "; ".join(errs)[:200],
    )
    try:
        create_pr_via_api(access_token=token, document_type="YAST", form=dup_form)
        rec("API create rejects duplicate asset", False, "create unexpectedly succeeded")
    except SystemExit as e:
        msg = str(e)
        rec(
            "API create rejects duplicate asset",
            "400" in msg or "duplicate" in msg.lower(),
            msg[:250],
        )

    form = yast_multi_asset_form(header_note=f"review-yast-multi {int(time.time())}")
    try:
        ticket = create_pr_via_api(access_token=token, document_type="YAST", form=form)
    except SystemExit as e:
        rec("PR multi-asset create API→SAP", False, str(e)[:300])
        _summary()
        return 1
    tid = str(ticket["id"])
    polled, perr = poll_ticket_sap_sync(token, tid, timeout_s=180)
    sap_pr = str((polled or {}).get("sap_id") or "").strip()
    ok_create = bool(sap_pr) and not sap_pr.startswith("#") and not perr
    rec("PR multi-asset create API→SAP", ok_create, f"ticket={tid} sap={sap_pr!r} err={perr!r}")
    if not ok_create:
        _summary()
        return 1

    loop = asyncio.new_event_loop()

    async def hydrate_pr(pr: str, seed: dict) -> dict:
        body, err = await get_pr(pr_number=pr, ticket_id=f"{tid}-hyd", document_type="YAST")
        if err or not body:
            raise RuntimeError(err or "get_pr empty")
        return form_from_pr_sap_read(body, document_type="YAST", seed_form=seed)

    hyd = loop.run_until_complete(hydrate_pr(sap_pr, form))
    allocs = (hyd.get("lines") or [{}])[0].get("allocations") or []
    assets = [str(a.get("asset") or "") for a in allocs]
    has_a1 = any(asset_codes_equal(a, a1) for a in assets)
    has_a2 = any(asset_codes_equal(a, a2) for a in assets)
    no_cc = all(not str(a.get("cost_center") or "").strip() for a in allocs if isinstance(a, dict))
    rec(
        "PR hydrate multi-asset (2 MFA, no CC)",
        has_a1 and has_a2 and len(allocs) >= 2 and no_cc,
        f"assets={assets} qtys={[a.get('qty') for a in allocs]}",
    )

    upd = copy.deepcopy(hyd)
    for a in upd["lines"][0]["allocations"]:
        if asset_codes_equal(str(a.get("asset")), a1):
            a["qty"] = "1"
        elif asset_codes_equal(str(a.get("asset")), a2):
            a["qty"] = "2"
        else:
            a["qty"] = str(int(float(str(a.get("qty") or "1").replace(",", "."))))
    ver = int((polled or ticket).get("version") or 1)
    patched, perr2 = patch_resync_via_api_result(
        access_token=token, ticket_id=tid, form=upd, version=ver
    )
    polled2, perr2b = poll_ticket_sap_sync(token, tid, timeout_s=180)
    hyd2 = loop.run_until_complete(hydrate_pr(sap_pr, upd))
    allocs2 = (hyd2.get("lines") or [{}])[0].get("allocations") or []
    q1 = next(
        (
            str(int(float(str(a.get("qty")).replace(",", "."))))
            for a in allocs2
            if asset_codes_equal(str(a.get("asset")), a1)
        ),
        None,
    )
    q2 = next(
        (
            str(int(float(str(a.get("qty")).replace(",", "."))))
            for a in allocs2
            if asset_codes_equal(str(a.get("asset")), a2)
        ),
        None,
    )
    rec(
        "PR redistribute qtys within total",
        q1 == "1" and q2 == "2",
        f"q1={q1} q2={q2} patch_err={perr2!r} poll_err={perr2b!r}",
    )

    upd2 = copy.deepcopy(hyd2)
    for a in upd2["lines"][0]["allocations"]:
        if asset_codes_equal(str(a.get("asset")), a1):
            a["qty"] = "2"
        elif asset_codes_equal(str(a.get("asset")), a2):
            a["qty"] = "3"
        else:
            a["qty"] = str(int(float(str(a.get("qty") or "1").replace(",", "."))))
    ver2 = int((polled2 or patched or {}).get("version") or ver + 1)
    patched3, perr3 = patch_resync_via_api_result(
        access_token=token, ticket_id=tid, form=upd2, version=ver2
    )
    polled3, perr3b = poll_ticket_sap_sync(token, tid, timeout_s=180)
    hyd3 = loop.run_until_complete(hydrate_pr(sap_pr, upd2))
    allocs3 = (hyd3.get("lines") or [{}])[0].get("allocations") or []
    q1b = next(
        (
            str(int(float(str(a.get("qty")).replace(",", "."))))
            for a in allocs3
            if asset_codes_equal(str(a.get("asset")), a1)
        ),
        None,
    )
    q2b = next(
        (
            str(int(float(str(a.get("qty")).replace(",", "."))))
            for a in allocs3
            if asset_codes_equal(str(a.get("asset")), a2)
        ),
        None,
    )
    body, _ = loop.run_until_complete(
        get_pr(pr_number=sap_pr, ticket_id=f"{tid}-rq", document_type="YAST")
    )
    items = ((body or {}).get("d") or body or {}).get("to_PurchaseReqnItem") or {}
    results_items = items.get("results") if isinstance(items, dict) else items
    total_rq = (
        str(results_items[0].get("RequestedQuantity") or "")
        if isinstance(results_items, list) and results_items
        else "?"
    )
    rec(
        "PR increase total qty MFA $batch",
        q1b == "2" and q2b == "3",
        f"q1={q1b} q2={q2b} RQ={total_rq!r} patch_err={perr3!r} poll_err={perr3b!r}",
    )

    ctx = load_po_db_context(apply_env=True)
    cases = build_po_matrix_cases(ctx)
    spec = cases["yast_multi_cc"]
    po_form = copy.deepcopy(spec["form"])
    po_form["lines"][0]["allocations"] = [
        {"asset": a1, "qty": "2"},
        {"asset": a2, "qty": "1"},
    ]
    po_form["lines"][0]["asset"] = a1
    po_form = normalize_form("YAST", po_form)
    apply_procurement_defaults(po_form, document_type="YAST", kind="PO")
    po_form["header"]["header_note"] = f"yp{int(time.time()) % 10_000_000_000:010d}"[:12]

    try:
        po_ticket = create_po_via_api(access_token=token, document_type="YAST", form=po_form)
    except SystemExit as e:
        rec("PO multi-asset create API→SAP", False, str(e)[:300])
        po_ticket = None
        po_tid = "n/a"
        sap_po = "n/a"
        ok_po = False
    if po_ticket:
        po_tid = str(po_ticket["id"])
        po_polled, po_perr = poll_ticket_sap_sync(token, po_tid, timeout_s=240)
        sap_po = str((po_polled or {}).get("sap_id") or "").strip()
        ok_po = bool(sap_po) and not sap_po.startswith("#") and not po_perr
        rec("PO multi-asset create API→SAP", ok_po, f"ticket={po_tid} sap={sap_po!r} err={po_perr!r}")
    else:
        po_polled, po_perr = None, None

    if ok_po:

        async def hydrate_po(po: str, seed: dict) -> dict:
            body, err = await get_po(
                po_number=po, ticket_id=f"{po_tid}-hyd", document_type="YAST"
            )
            if err or not body:
                raise RuntimeError(err or "get_po empty")
            return form_from_po_sap_read(body, document_type="YAST", seed_form=seed)

        po_hyd = loop.run_until_complete(hydrate_po(sap_po, po_form))
        po_allocs = (po_hyd.get("lines") or [{}])[0].get("allocations") or []
        po_assets = [str(a.get("asset") or "") for a in po_allocs]
        rec(
            "PO hydrate multi-asset",
            any(asset_codes_equal(a, a1) for a in po_assets)
            and any(asset_codes_equal(a, a2) for a in po_assets),
            f"assets={po_assets} qtys={[a.get('qty') for a in po_allocs]}",
        )

        po_upd = copy.deepcopy(po_hyd)
        for a in po_upd["lines"][0]["allocations"]:
            if asset_codes_equal(str(a.get("asset")), a1):
                a["qty"] = "3"
            elif asset_codes_equal(str(a.get("asset")), a2):
                a["qty"] = "2"
            else:
                a["qty"] = str(int(float(str(a.get("qty") or "1").replace(",", "."))))
        po_upd["lines"][0]["qty"] = "5"
        po_ver = int((po_polled or po_ticket).get("version") or 1)
        po_patched, po_perr2 = patch_resync_via_api_result(
            access_token=token, ticket_id=po_tid, form=po_upd, version=po_ver
        )
        po_polled2, po_perr2b = poll_ticket_sap_sync(token, po_tid, timeout_s=240)
        po_hyd2 = loop.run_until_complete(hydrate_po(sap_po, po_upd))
        po_allocs2 = (po_hyd2.get("lines") or [{}])[0].get("allocations") or []
        pq1 = next(
            (
                str(int(float(str(a.get("qty")).replace(",", "."))))
                for a in po_allocs2
                if asset_codes_equal(str(a.get("asset")), a1)
            ),
            None,
        )
        pq2 = next(
            (
                str(int(float(str(a.get("qty")).replace(",", "."))))
                for a in po_allocs2
                if asset_codes_equal(str(a.get("asset")), a2)
            ),
            None,
        )
        sync_po = str((((po_polled2 or {}).get("sap_sync") or {}).get("last_error") or ""))
        rec(
            "PO increase total via schedule-line $batch",
            pq1 == "3" and pq2 == "2",
            f"q1={pq1} q2={pq2} patch_err={po_perr2!r} poll_err={po_perr2b!r} sync={sync_po[:180]!r}",
        )
    else:
        rec("PO hydrate multi-asset", False, "skipped", skip=True)
        rec("PO increase total via schedule-line $batch", False, "skipped", skip=True)
        po_tid = "n/a"
        sap_po = "n/a"

    fix_dir = Path("tests/fixtures/sap_attachment_formats")
    attach_cases = [
        ("test_random.pdf", "application/pdf"),
        ("test_random.jpg", "image/jpeg"),
        ("test_random.jpeg", "image/jpeg"),
        ("test_random.png", "image/png"),
        ("test_random.xls", "application/vnd.ms-excel"),
        (
            "test_random.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ]
    tcur = get_ticket_via_api(access_token=token, ticket_id=tid)
    ver_a = int(tcur.get("version") or 1)
    for fname, mime in attach_cases:
        path = fix_dir / fname
        if not path.exists():
            rec(f"attach {fname}", False, "fixture missing", skip=True)
            continue
        data = path.read_bytes()
        url = f"{api_base()}/api/procurement/tickets/{tid}"
        files = [("files", (fname, data, mime))]
        form_data = {"version": str(ver_a), "payload": json.dumps({"resync_sap": True})}
        with httpx.Client(timeout=300) as c:
            r = c.patch(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data=form_data,
                files=files,
            )
        if r.status_code >= 400:
            rec(f"attach+SAP {fname}", False, f"HTTP {r.status_code}: {r.text[:220]}")
            continue
        body_j = r.json()
        ver_a = int(body_j.get("version") or ver_a + 1)
        polled_a, _ = poll_ticket_sap_sync(token, tid, timeout_s=90)
        sync_a = (polled_a or {}).get("sap_sync") or {}
        err_a = str(sync_a.get("last_error") or "")
        atts = (polled_a or body_j).get("attachments") or []
        names = [
            str(a.get("filename") or a.get("file_name") or a.get("name") or "")
            for a in atts
            if isinstance(a, dict)
        ]
        listed = any(fname.lower() in n.lower() for n in names)
        fail_msg = err_a and any(
            x in err_a.lower()
            for x in ("attach", "mime", "content", "file type", "not allowed")
        )
        rec(
            f"attach+SAP {fname}",
            listed or (r.status_code < 400 and not fail_msg),
            f"listed={listed} http={r.status_code} names_sample={names[-4:]} err={err_a[:140]!r}",
        )
        if polled_a:
            ver_a = int(polled_a.get("version") or ver_a)

    url = f"{api_base()}/api/procurement/tickets/{tid}"
    form_data = {"version": str(ver_a), "payload": json.dumps({"resync_sap": False})}
    with httpx.Client(timeout=60) as c:
        r = c.patch(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data=form_data,
            files=[("files", ("evil.exe.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))],
        )
    rec(
        "reject dangerous double-ext attach",
        r.status_code >= 400,
        f"HTTP {r.status_code}: {r.text[:180]}",
    )

    for path in ["/procurement/pr/new", "/procurement/po/new"]:
        with httpx.Client(timeout=30, follow_redirects=True) as c:
            r = c.get(f"http://127.0.0.1:3000{path}")
        rec(f"UI page {path}", r.status_code == 200, f"HTTP {r.status_code}")

    print(f"\nDocs: PR={sap_pr} PO={sap_po} tickets PR={tid} PO={po_tid}")
    return _summary()


def _summary() -> int:
    print("\n===== SUMMARY =====")
    fails = 0
    for n, s, d in results:
        print(f"{s:4} {n}" + (f" — {d}" if d and s != "PASS" else ""))
        if s == "FAIL":
            fails += 1
    print(
        f"\n{sum(1 for _, s, _ in results if s == 'PASS')} PASS / "
        f"{fails} FAIL / {sum(1 for _, s, _ in results if s == 'SKIP')} SKIP"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
