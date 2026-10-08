#!/usr/bin/env python3
"""Live review: per-line tax — backward compat + API → DB → SAP round trip.

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_tax_line_review_live.py

Covers:
  - Old-style header-only tax (legacy DB shape)
  - New per-line tax (different codes per item)
  - PR line tax stored in DB (not sent to SAP PR API)
  - PO create → SAP poll → GET hydrate → update tax → optional delete line
  - All PO types: YUNB, YAST, YSER
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.po_live_common import (
    apply_po_live_fixtures,
    default_delivery_date,
    simple_po_form,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    classify_sap_ticket,
    create_po_via_api_result,
    create_pr_via_api_result,
    get_ticket_via_api,
    load_env,
    patch_resync_via_api_result,
    poll_ticket_sap_sync,
    resolve_token_from_args,
)
from scripts.integration.run_pr_create_yunb import simple_yunb_form

PO_DOCUMENT_TYPES: tuple[str, ...] = ("YUNB", "YAST", "YSER")


@dataclass
class Row:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        st = "SKIP" if skip else ("PASS" if ok else "FAIL")
        self.rows.append(Row(name, st, detail[:500]))
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[st]
        print(f"  [{mark} {st}] {name}" + (f" — {detail[:200]}" if detail else ""))

    def exit_code(self) -> int:
        return 1 if any(r.status == "FAIL" for r in self.rows) else 0


def _sap_po_item_taxes(po_number: str, *, document_type: str = "") -> dict[str, str] | None:
    import concurrent.futures

    from app.procurement.sap_po_client import get_po

    dt = (document_type or "").upper()

    async def _run() -> tuple[dict[str, Any] | None, str | None]:
        return await get_po(
            po_number=po_number,
            ticket_id="tax-review",
            document_type=dt,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        body, err = pool.submit(lambda: __import__("asyncio").run(_run())).result()
    if err or not body:
        return None
    from app.procurement.sap_odata_utils import odata_entity_properties, odata_results_list, odata_text

    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return None
    out: dict[str, str] = {}
    for entry in odata_results_list(root.get("to_PurchaseOrderItem")):
        props = odata_entity_properties(entry)
        item_no = odata_text(props.get("PurchaseOrderItem"))
        if item_no:
            out[item_no.lstrip("0") or item_no] = odata_text(props.get("TaxCode"))
    return out


def _db_form(ticket_id: str) -> dict[str, Any] | None:
    from sqlalchemy import create_engine, text

    from app.config.settings import settings

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT form FROM procurement_tickets WHERE id = :id"),
                {"id": ticket_id},
            ).fetchone()
    finally:
        engine.dispose()
    if not row or row[0] is None:
        return None
    form = row[0]
    if isinstance(form, dict):
        return copy.deepcopy(form)
    if isinstance(form, str):
        return json.loads(form)
    return None


def _poll_ok(access_token: str, ticket_id: str, timeout_s: float = 240) -> tuple[bool, str]:
    ticket, err = poll_ticket_sap_sync(access_token, ticket_id, timeout_s=timeout_s)
    if ticket and not err:
        sap_id = str(ticket.get("sap_id") or "").strip()
        return bool(sap_id and not sap_id.startswith("#")), sap_id or "ok"
    return False, err or "poll failed"


def _line_taxes(form: dict[str, Any]) -> list[str]:
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    out: list[str] = []
    for row in lines:
        if isinstance(row, dict):
            out.append(str(row.get("tax_code") or "").strip())
    return out


def _align_yser_env_plant_sloc() -> None:
    """YSER plant (H006) must not use YUNB composite sloc (H001|…)."""
    import os

    plant = (os.environ.get("SAP_PLANT") or "").strip()
    sloc = (os.environ.get("SAP_SLOC") or "").strip()
    if plant and "|" in sloc and not sloc.startswith(f"{plant}|"):
        os.environ["SAP_SLOC"] = (os.environ.get("SAP_YSER_SLOC") or f"{plant}|2038").strip()


def _align_yser_cost_centers_from_db() -> None:
    import os

    import psycopg2

    from app.config.settings import settings
    from scripts.integration.integration_reference_defaults import (
        _cost_centers_for_po,
        _sloc_for_sap,
    )

    plant = (os.environ.get("SAP_PLANT") or "").strip()
    sloc = (os.environ.get("SAP_SLOC") or "").strip()
    org = (os.environ.get("SAP_PUR_ORG") or "").strip()
    if not plant or not sloc or not org:
        return
    bare = _sloc_for_sap(sloc, plant=plant)
    conn = psycopg2.connect(settings.database_url_sync)
    try:
        cur = conn.cursor()
        ccs = _cost_centers_for_po(cur, org=org, storage_location=bare, limit=2)
        if ccs:
            os.environ["SAP_COST_CENTER"] = ccs[0]
        if len(ccs) > 1:
            os.environ["SAP_COST_CENTER_2"] = ccs[1]
    finally:
        conn.close()


def _fix_yser_form_lines(form: dict[str, Any]) -> None:
    import os

    cc1 = (os.environ.get("SAP_COST_CENTER") or "").strip()
    cc2 = (os.environ.get("SAP_COST_CENTER_2") or cc1).strip()
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        allocs = line.get("allocations")
        if not isinstance(allocs, list):
            continue
        cc = cc2 if idx == 1 and cc2 else cc1
        for alloc in allocs:
            if isinstance(alloc, dict) and cc:
                alloc["cost_center"] = cc


def _fix_yser_form_header(form: dict[str, Any]) -> dict[str, Any]:
    import os

    _align_yser_env_plant_sloc()
    _align_yser_cost_centers_from_db()
    hdr = form.setdefault("header", {})
    plant = (os.environ.get("SAP_PLANT") or "").strip()
    sloc = (os.environ.get("SAP_SLOC") or "").strip()
    if plant:
        hdr["plant"] = plant
    if sloc:
        hdr["storage_location"] = sloc
    _fix_yser_form_lines(form)
    return form


def _po_header_note(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:6]}"[:12]


def _append_second_po_line(lines: list[dict[str, Any]], document_type: str, tax: str) -> bool:
    import os

    dt = document_type.upper()
    if not lines or not isinstance(lines[0], dict):
        return False
    if dt in ("YUNB", "YAST"):
        mat2 = os.environ.get("SAP_MATERIAL_2", "").strip()
        if not mat2:
            return False
        l2 = copy.deepcopy(lines[0])
        l2["material"] = mat2
        l2["short_text"] = f"Line 2 {mat2}"[:40]
        l2["tax_code"] = tax
        l2.setdefault("delivery_date", default_delivery_date())
        l2.setdefault("net_price", l2.get("unit_price", "10"))
        if dt == "YAST":
            asset2 = os.environ.get("SAP_ASSET_2", "").strip()
            asset = asset2 or str((l2.get("allocations") or [{}])[0].get("asset") or "").strip()
            if asset:
                l2["asset"] = asset
            l2["allocations"] = [{"asset": asset, "qty": "1"}]
        else:
            alloc0 = (l2.get("allocations") or [{}])[0]
            l2["allocations"] = [{"cost_center": alloc0.get("cost_center", ""), "qty": "1"}]
        lines.append(l2)
        return True
    if dt == "YSER":
        svc2 = os.environ.get("SAP_SERVICE_2", "").strip()
        if not svc2:
            return False
        l0 = lines[0]
        cc = str((l0.get("allocations") or [{}])[0].get("cost_center") or "").strip()
        if not cc:
            cc = os.environ.get("SAP_COST_CENTER", "").strip()
        lines.append(
            {
                "service": svc2,
                "short_text": "YSER tax line 2",
                "delivery_date": l0.get("delivery_date") or default_delivery_date(),
                "unit_price": os.environ.get("SAP_UNIT_PRICE_2", "50"),
                "net_price": os.environ.get("SAP_UNIT_PRICE_2", "50"),
                "tax_code": tax,
                "allocations": [{"cost_center": cc, "qty": "1"}],
            }
        )
        return True
    return False


def _old_header_only_po_form(document_type: str) -> dict[str, Any]:
    """Simulate pre-migration ticket: tax on header only, lines without tax_code."""
    dt = document_type.upper()
    form = simple_po_form(dt, header_note=_po_header_note("o"))
    hdr = form.setdefault("header", {})
    hdr["tax_code"] = "FA"
    for line in form.get("lines") or []:
        if isinstance(line, dict):
            line.pop("tax_code", None)
    if dt == "YSER":
        _fix_yser_form_header(form)
    return form


def _new_per_line_po_form(document_type: str) -> dict[str, Any]:
    dt = document_type.upper()
    form = simple_po_form(dt, header_note=_po_header_note("n"))
    hdr = form.setdefault("header", {})
    hdr["tax_code"] = ""
    lines = [row for row in (form.get("lines") or []) if isinstance(row, dict)]
    taxes = ["FA", "WA"]
    if lines:
        lines[0]["tax_code"] = taxes[0]
        lines[0].setdefault("delivery_date", default_delivery_date())
        lines[0].setdefault("net_price", lines[0].get("unit_price", "10"))
    if len(lines) == 1:
        _append_second_po_line(lines, dt, taxes[1])
    elif len(lines) >= 2:
        lines[1]["tax_code"] = taxes[1]
    form["lines"] = lines
    if dt == "YSER":
        _fix_yser_form_header(form)
    return form


def _old_header_only_pr_form() -> dict[str, Any]:
    """Legacy PR: tax on header only."""
    form = simple_yunb_form(header_note=f"tax-pr-old-{uuid.uuid4().hex[:8]}")
    hdr = form.setdefault("header", {})
    hdr["tax_code"] = "FB"
    for line in form.get("lines") or []:
        if isinstance(line, dict):
            line.pop("tax_code", None)
    return form


def _pr_with_line_tax_form() -> dict[str, Any]:
    form = simple_yunb_form(header_note=f"tax-pr-{uuid.uuid4().hex[:8]}")
    hdr = form.setdefault("header", {})
    hdr["tax_code"] = "FB"
    for line in form.get("lines") or []:
        if isinstance(line, dict):
            line["tax_code"] = "FB"
    return form


def _po_payload_items(document_type: str, form: dict[str, Any]) -> list[dict[str, Any]]:
    dt = document_type.upper()
    if dt == "YSER":
        from app.procurement.sap_po_z_payload import build_z_yser_po_inner_payload

        inner = build_z_yser_po_inner_payload(form=form, ticket_id="tax-inprocess")
        items = inner.get("to_PurchaseOrderItem") or []
        return [row for row in items if isinstance(row, dict)]
    from app.procurement.sap_po_payload import build_po_payload

    payload = build_po_payload(form=form, document_type=dt)
    items = payload.get("to_PurchaseOrderItem") or []
    return [row for row in items if isinstance(row, dict)]


def run_inprocess_checks(rep: Report) -> None:
    from app.procurement.field_schema import normalize_form
    from app.procurement.line_tax_code import line_tax_code
    from app.procurement.sap_pr_payload import build_pr_payload

    pr_norm = normalize_form("YUNB", _pr_with_line_tax_form())
    pr_item = build_pr_payload(form=pr_norm, document_type="YUNB")["to_PurchaseReqnItem"][0]
    rep.add(
        "inprocess: PR API omits TaxCode",
        "TaxCode" not in pr_item,
        f"keys sample={sorted(pr_item.keys())[:8]}",
    )

    for dt in PO_DOCUMENT_TYPES:
        old = _old_header_only_po_form(dt)
        norm = normalize_form(dt, old)
        rep.add(
            f"inprocess {dt}: normalize promotes header tax to lines",
            norm["lines"][0].get("tax_code") == "FA",
            f"line tax={norm['lines'][0].get('tax_code')!r}",
        )
        items = _po_payload_items(dt, norm)
        row = items[0] if items else {}
        rep.add(
            f"inprocess {dt}: old header-only → SAP item TaxCode",
            row.get("TaxCode") == "FA",
            f"TaxCode={row.get('TaxCode')!r}",
        )
        new = normalize_form(dt, _new_per_line_po_form(dt))
        if len(new["lines"]) >= 2:
            items2 = _po_payload_items(dt, new)
            ok = (
                line_tax_code(new["lines"][0], new["header"]) == "FA"
                and items2[0].get("TaxCode") == "FA"
                and items2[1].get("TaxCode") == "WA"
            )
            rep.add(
                f"inprocess {dt}: per-line tax on PO items",
                ok,
                f"SAP taxes={[i.get('TaxCode') for i in items2]}",
            )
        else:
            rep.add(
                f"inprocess {dt}: per-line tax on PO items",
                True,
                "single-line only",
                skip=True,
            )


def run_po_tax_roundtrip(rep: Report, token: str, document_type: str) -> None:
    dt = document_type.upper()
    apply_po_live_fixtures(dt)
    if dt == "YSER":
        _align_yser_env_plant_sloc()
        _align_yser_cost_centers_from_db()

    old_po_form = _old_header_only_po_form(dt)
    po_old, po_old_err = create_po_via_api_result(
        access_token=token, document_type=dt, form=old_po_form
    )
    if po_old_err or not po_old:
        rep.add(f"{dt} PO create (old header tax)", False, po_old_err or "no ticket")
    else:
        po_old_id = str(po_old.get("id") or "")
        ok_o, det_o = _poll_ok(token, po_old_id)
        rep.add(f"{dt} PO old-style → SAP", ok_o, det_o)
        got_o = get_ticket_via_api(access_token=token, ticket_id=po_old_id)
        taxes_o = _line_taxes(got_o.get("form") or {})
        rep.add(
            f"{dt} GET PO hydrate (old create) line tax",
            "FA" in taxes_o,
            f"taxes={taxes_o} sap_id={got_o.get('sap_id')!r}",
        )
        sap_id_o = str(got_o.get("sap_id") or "").strip()
        if sap_id_o and not sap_id_o.startswith("#"):
            sap_taxes = _sap_po_item_taxes(sap_id_o, document_type=dt)
            rep.add(
                f"{dt} SAP PO item TaxCode (old-style create)",
                bool(sap_taxes) and "FA" in sap_taxes.values(),
                f"sap_item_taxes={sap_taxes}",
            )
        form_up = copy.deepcopy(got_o.get("form") or {})
        lines_up = form_up.get("lines") if isinstance(form_up.get("lines"), list) else []
        if lines_up and isinstance(lines_up[0], dict):
            lines_up[0]["tax_code"] = "WA"
        patched, perr = patch_resync_via_api_result(
            access_token=token,
            ticket_id=po_old_id,
            form=form_up,
            version=int(got_o.get("version") or 1),
        )
        if perr:
            rep.add(f"{dt} API PO update tax (old ticket)", False, perr)
        else:
            ok_p, det_p = _poll_ok(token, po_old_id)
            rep.add(f"{dt} API PO update tax → SAP", ok_p, det_p)
            got_p = get_ticket_via_api(access_token=token, ticket_id=po_old_id)
            rep.add(
                f"{dt} GET after PO tax update",
                _line_taxes(got_p.get("form") or {}).count("WA") >= 1,
                f"taxes={_line_taxes(got_p.get('form') or {})}",
            )

    new_po_form = _new_per_line_po_form(dt)
    if len(new_po_form.get("lines") or []) < 2:
        rep.add(
            f"{dt} API PO create (per-line tax)",
            False,
            "need 2 lines — set SAP_MATERIAL_2 / SAP_SERVICE_2",
            skip=True,
        )
    else:
        po_new, po_new_err = create_po_via_api_result(
            access_token=token, document_type=dt, form=new_po_form
        )
        if po_new_err or not po_new:
            rep.add(f"{dt} API PO create (per-line tax)", False, po_new_err or "no ticket")
        else:
            po_new_id = str(po_new.get("id") or "")
            ok_n, det_n = _poll_ok(token, po_new_id)
            rep.add(f"{dt} API PO per-line → SAP", ok_n, det_n)
            got_n = get_ticket_via_api(access_token=token, ticket_id=po_new_id)
            taxes_n = _line_taxes(got_n.get("form") or {})
            rep.add(
                f"{dt} GET PO hydrate per-line taxes",
                "FA" in taxes_n and "WA" in taxes_n,
                f"taxes={taxes_n}",
            )
            sap_id_n = str(got_n.get("sap_id") or "").strip()
            if sap_id_n and not sap_id_n.startswith("#"):
                sap_taxes_n = _sap_po_item_taxes(sap_id_n, document_type=dt)
                rep.add(
                    f"{dt} SAP PO per-item TaxCode",
                    sap_taxes_n is not None
                    and "FA" in (sap_taxes_n or {}).values()
                    and "WA" in (sap_taxes_n or {}).values(),
                    f"sap_item_taxes={sap_taxes_n}",
                )
            form_del = copy.deepcopy(got_n.get("form") or {})
            lines_del = form_del.get("lines") if isinstance(form_del.get("lines"), list) else []
            if len(lines_del) > 1:
                form_del["lines"] = [lines_del[0]]
                pdel, derr = patch_resync_via_api_result(
                    access_token=token,
                    ticket_id=po_new_id,
                    form=form_del,
                    version=int(got_n.get("version") or 1),
                )
                if derr:
                    rep.add(f"{dt} API PO delete line", False, derr)
                else:
                    ok_d, det_d = _poll_ok(token, po_new_id)
                    rep.add(f"{dt} API PO delete line → SAP", ok_d, det_d)
                    got_d = get_ticket_via_api(access_token=token, ticket_id=po_new_id)
                    rep.add(
                        f"{dt} GET after delete (1 line)",
                        len(_line_taxes(got_d.get("form") or {})) == 1,
                        f"taxes={_line_taxes(got_d.get('form') or {})}",
                    )


def run_api_roundtrip(rep: Report, token: str, *, po_types: tuple[str, ...]) -> None:

    # --- Old-style PR (header tax only) ---
    old_pr_form = _old_header_only_pr_form()
    old_pr_ticket, old_pr_err = create_pr_via_api_result(
        access_token=token, document_type="YUNB", form=old_pr_form
    )
    if old_pr_err or not old_pr_ticket:
        rep.add("API PR create (old header tax)", False, old_pr_err or "no ticket")
    else:
        old_pr_id = str(old_pr_ticket.get("id") or "")
        ok_opr, det_opr = _poll_ok(token, old_pr_id, 180)
        rep.add("API PR old-style → SAP", ok_opr, det_opr)
        got_opr = get_ticket_via_api(access_token=token, ticket_id=old_pr_id)
        taxes_opr = _line_taxes(got_opr.get("form") or {})
        rep.add(
            "GET PR hydrate (old create) line tax",
            "FB" in taxes_opr,
            f"taxes={taxes_opr} sap_id={got_opr.get('sap_id')!r}",
        )
        form_pr_up = copy.deepcopy(got_opr.get("form") or {})
        lines_pr_up = form_pr_up.get("lines") if isinstance(form_pr_up.get("lines"), list) else []
        if lines_pr_up and isinstance(lines_pr_up[0], dict):
            lines_pr_up[0]["tax_code"] = "FA"
        patched_pr, pr_perr = patch_resync_via_api_result(
            access_token=token,
            ticket_id=old_pr_id,
            form=form_pr_up,
            version=int(got_opr.get("version") or 1),
        )
        if pr_perr:
            rep.add("API PR update tax (old ticket)", False, pr_perr)
        else:
            ok_pr_u, det_pr_u = _poll_ok(token, old_pr_id, 180)
            rep.add("API PR update tax → SAP", ok_pr_u, det_pr_u)
            got_pr_u = get_ticket_via_api(access_token=token, ticket_id=old_pr_id)
            rep.add(
                "GET after PR tax update",
                _line_taxes(got_pr_u.get("form") or {}).count("FA") >= 1,
                f"taxes={_line_taxes(got_pr_u.get('form') or {})}",
            )

    # --- PR with line tax (DB only on SAP PR) ---
    pr_id = ""
    db_pr: dict[str, Any] | None = None
    pr_form = _pr_with_line_tax_form()
    pr_ticket, pr_err = create_pr_via_api_result(
        access_token=token, document_type="YUNB", form=pr_form
    )
    if pr_err or not pr_ticket:
        rep.add("API PR create", False, pr_err or "no ticket")
    else:
        pr_id = str(pr_ticket.get("id") or "")
        ok_pr, det_pr = _poll_ok(token, pr_id, 180)
        rep.add("API PR create → SAP", ok_pr, det_pr)
        db_pr = _db_form(pr_id)
        rep.add(
            "DB PR line tax persisted",
            bool(db_pr) and "FB" in _line_taxes(db_pr or {}),
            f"line taxes={_line_taxes(db_pr or {})}",
        )
        got_pr = get_ticket_via_api(access_token=token, ticket_id=pr_id)
        rep.add(
            "GET PR hydrate line tax",
            "FB" in _line_taxes(got_pr.get("form") or {}),
            f"form_source={got_pr.get('form_source')!r} taxes={_line_taxes(got_pr.get('form') or {})}",
        )

    for dt in po_types:
        print(f"\n--- {dt} PO tax round-trip ---\n")
        run_po_tax_roundtrip(rep, token, dt)

    # --- PO from PR prefill ---
    if pr_id and db_pr:
        from scripts.integration.procurement_api_common import get_prefill_po_via_api_result

        pfill, pfill_err = get_prefill_po_via_api_result(
            access_token=token, pr_ticket_id=pr_id
        )
        if pfill_err:
            rep.add("PO prefill from PR", False, pfill_err)
        else:
            pf_form = pfill.get("form") if isinstance(pfill, dict) else {}
            rep.add(
                "PO prefill carries PR line tax",
                "FB" in _line_taxes(pf_form if isinstance(pf_form, dict) else {}),
                f"taxes={_line_taxes(pf_form if isinstance(pf_form, dict) else {})}",
            )


def main() -> int:
    p = argparse.ArgumentParser(description="Per-line tax live review")
    add_common_args(p)
    p.add_argument("--inprocess-only", action="store_true", help="Skip API/SAP live calls")
    p.add_argument(
        "--po-type",
        action="append",
        choices=list(PO_DOCUMENT_TYPES),
        dest="po_types",
        help="Limit live PO tests (repeatable; default: all PO types)",
    )
    args = p.parse_args()
    po_types = tuple(args.po_types) if args.po_types else PO_DOCUMENT_TYPES
    load_env()
    rep = Report()
    applied_defaults = apply_integration_reference_defaults()

    print("\n=== Per-line tax review ===\n")
    print("Backward compat:")
    print("  - line_tax_code(block, header): line wins, header fallback")
    print("  - normalize_form promotes header tax onto lines")
    print("  - PO SAP outbound: TaxCode per item (YUNB/YAST OData, YSER Z API)")
    print("  - PR SAP: no TaxCode (line tax is AgentOS-only until PO)\n")

    run_inprocess_checks(rep)

    if not args.inprocess_only:
        try:
            import httpx

            httpx.get(f"{api_base()}/health", timeout=5.0)
        except Exception as e:
            rep.add("API reachable", False, str(e), skip=True)
            print(f"\nAPI not reachable ({api_base()}); skipping live API tests.")
        else:
            os.environ.setdefault("AGENTOS_INTEGRATION_MINT_TOKEN", "1")
            print(integration_defaults_summary(applied_defaults))
            token = resolve_token_from_args(args)
            print(f"\nLive API round-trip ({api_base()}):\n")
            run_api_roundtrip(rep, token, po_types=po_types)

    print("\n" + "=" * 72)
    n_fail = sum(1 for r in rep.rows if r.status == "FAIL")
    n_pass = sum(1 for r in rep.rows if r.status == "PASS")
    print(f"SUMMARY: PASS={n_pass} FAIL={n_fail}")
    print("=" * 72)
    return rep.exit_code()


if __name__ == "__main__":
    import os

    raise SystemExit(main())
