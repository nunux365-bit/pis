"""Shared helpers for repeated live PO API runs (AgentOS → SAP OData)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from scripts.integration.integration_reference_defaults import PoDbContext
from scripts.integration.procurement_api_common import api_base

# Known QAS PR numbers → AgentOS ticket UUID (refresh via --list-parent-prs).
DEFAULT_PARENT_PR_BY_SAP: dict[str, str] = {
    "1040000063": "891b7ce3-9711-46b5-ac2e-d7f283d39f3f",
    "1040000064": "9550af07-30db-4a9f-880e-b61fc33f13c3",
    "1040000065": "893a577c-01a0-483c-ab2f-f30941d26638",
}


def default_delivery_date() -> str:
    # QAS enforces next workday on schedule-line delivery; keep safely in the future.
    # 2026-08-15 is Saturday on QAS; schedule line needs next workday (2026-08-17).
    return (os.environ.get("SAP_PO_DELIVERY_DATE") or "2026-08-17").strip()


def distinct_integration_material(*, exclude: str = "") -> str:
    """Pick a catalogue material from env that differs from ``exclude``."""
    ex = (exclude or "").strip()
    for key in ("SAP_MATERIAL_2", "SAP_MATERIAL"):
        candidate = (os.environ.get(key) or "").strip()
        if candidate and candidate != ex:
            return candidate
    return ""


def distinct_integration_service(*, exclude: str = "") -> str:
    """Pick a catalogue service from env that differs from ``exclude``."""
    ex = (exclude or "").strip()
    for key in ("SAP_SERVICE_2", "SAP_SERVICE"):
        candidate = (os.environ.get(key) or "").strip()
        if candidate and candidate != ex:
            return candidate
    return ""


def apply_po_live_fixtures(document_type: str) -> dict[str, str]:
    """Env overrides for live PO runs — DB catalogue by default; doc master opt-in."""
    from scripts.integration.integration_reference_defaults import (
        apply_integration_reference_defaults,
        load_po_db_context,
    )
    from scripts.integration.sap_po_doc_fixtures import (
        apply_po_doc_master_env,
        po_use_doc_master,
    )

    if po_use_doc_master():
        return apply_po_doc_master_env()
    dt = (document_type or "").upper()
    if dt == "YSER":
        return load_po_db_context(apply_env=False).yser_context().force_apply_to_environ()
    return apply_integration_reference_defaults()


def po_line(
    *,
    material: str,
    short_text: str,
    allocations: list[dict[str, str]],
    delivery_date: str | None = None,
    unit_price: str | None = None,
) -> dict[str, Any]:
    return {
        "material": material,
        "short_text": short_text,
        "delivery_date": delivery_date or default_delivery_date(),
        "unit_price": unit_price or os.environ.get("SAP_UNIT_PRICE", "10"),
        "allocations": allocations,
    }


def simple_po_form(
    document_type: str,
    *,
    header_note: str = "",
    delivery_date: str | None = None,
) -> dict[str, Any]:
    """Minimal 1-line PO form — passes API validation (incl. delivery_date)."""
    dt = document_type.upper()
    material = os.environ.get("SAP_MATERIAL", "4200000027")
    cc = os.environ.get("SAP_COST_CENTER", "HCO91001H0")
    unit = os.environ.get("SAP_ORDER_UNIT", "KG")
    line: dict[str, Any] = po_line(
        material=material,
        short_text=f"{dt} PO live integration",
        allocations=[{"cost_center": cc, "qty": "1"}],
        delivery_date=delivery_date,
        unit_price=os.environ.get("SAP_UNIT_PRICE", "10"),
    )
    line["order_unit"] = unit
    line["net_price"] = line.get("unit_price", "10")
    if dt == "YSER":
        line = {
            "service": os.environ.get("SAP_SERVICE", "10000000006"),
            "short_text": f"{dt} PO live integration",
            "delivery_date": delivery_date or default_delivery_date(),
            "unit_price": os.environ.get("SAP_UNIT_PRICE", "10"),
            "allocations": [{"cost_center": cc, "qty": "1"}],
        }
    header: dict[str, Any] = {
        "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
        "purchasing_group": os.environ.get("SAP_PUR_GROUP", "A0B"),
        "plant": os.environ.get("SAP_PLANT", "H002" if dt == "YSER" else "H001"),
        "storage_location": os.environ.get("SAP_SLOC", "1001" if dt == "YSER" else "3021"),
        "material_group": os.environ.get("SAP_MATERIAL_GROUP", "SD05-0001"),
        "service_group": os.environ.get("SAP_SERVICE_GROUP", "S001-0001"),
        "vendor": (os.environ.get("SAP_VENDOR") or "").strip(),
        "header_note": header_note or f"PO {dt}"[:12],
    }
    tax = os.environ.get("SAP_TAX_CODE", "XE").strip()
    if tax:
        header["tax_code"] = tax
    pay = (os.environ.get("SAP_PAYMENT_TERMS") or "").strip()
    if pay:
        header["payment_terms"] = pay
    if dt == "YAST":
        asset = os.environ.get("SAP_ASSET", "").strip()
        if asset:
            line["asset"] = asset
        info_rec = os.environ.get("SAP_PO_INFO_RECORD", "").strip()
        if info_rec:
            line["purchasing_info_record"] = info_rec
        gl = os.environ.get("SAP_PO_GL_ACCOUNT", "").strip()
        co = os.environ.get("SAP_PO_CONTROLLING_AREA", "").strip()
        if gl:
            header["gl_account"] = gl
        if co:
            header["controlling_area"] = co
    form: dict[str, Any] = {
        "header": header,
        "lines": [line],
    }
    from scripts.integration.sap_po_doc_fixtures import (
        apply_doc_master_to_po_form,
        po_use_doc_master,
    )

    if po_use_doc_master():
        form = apply_doc_master_to_po_form(form, dt)
    return form


def _po_header(ctx: PoDbContext, document_type: str, **overrides: Any) -> dict[str, Any]:
    dt = document_type.upper()
    header: dict[str, Any] = {
        "purchasing_org": ctx.purchasing_org,
        "purchasing_group": ctx.purchasing_group,
        "plant": ctx.plant,
        "storage_location": ctx.storage_location,
        "vendor": ctx.vendor,
        "header_note": f"{dt} PO ok"[:12],
    }
    if dt in ("YUNB", "YAST") and ctx.material_group:
        header["material_group"] = ctx.material_group
    if dt == "YSER" and ctx.service_group:
        header["service_group"] = ctx.service_group
    if ctx.payment_terms:
        header["payment_terms"] = ctx.payment_terms
    if dt == "YSER":
        tax = (ctx.tax_code or os.environ.get("SAP_TAX_CODE") or "").strip()
    else:
        tax = (os.environ.get("SAP_TAX_CODE") or "").strip()
    if tax:
        header["tax_code"] = tax
    header.update(overrides)
    return header


def _po_line(
    ctx: PoDbContext,
    *,
    short_text: str,
    material: str = "",
    service: str = "",
    unit: str | None = None,
    price: str | None = None,
    delivery: str | None = None,
    allocs: list[dict[str, str]] | None = None,
    asset: str = "",
    info_rec: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "short_text": short_text,
        "delivery_date": delivery or default_delivery_date(),
        "unit_price": price or ctx.unit_price,
        "net_price": price or ctx.unit_price,
        "order_unit": unit or ctx.order_unit,
        "allocations": allocs or [{"cost_center": ctx.cost_center, "qty": "5"}],
    }
    if material:
        row["material"] = material
    if service:
        row["service"] = service
    if asset:
        row["asset"] = asset
    if info_rec:
        row["purchasing_info_record"] = info_rec
    return row


def build_po_matrix_cases(ctx: PoDbContext) -> dict[str, dict[str, Any]]:
    """Phase-2 standalone PO matrix — catalogue master + UI-aligned derivation."""
    if not ctx.vendor:
        raise SystemExit(
            "No company-scoped vendor in pr_po_reference_values — sync vendor master or set SAP_VENDOR"
        )
    if not ctx.material:
        raise SystemExit("No material in pr_po_reference_values — import/sync material master")

    yunb_hdr = _po_header(ctx, "YUNB")
    yast_hdr = _po_header(ctx, "YAST")
    ys = ctx.yser_context()
    yser_hdr = _po_header(ys, "YSER")

    asset = ctx.asset
    asset_2 = ctx.asset_2 or os.environ.get("SAP_ASSET_2", "").strip() or asset
    info_rec = ctx.info_record

    return {
        "yunb_simple": {
            "document_type": "YUNB",
            "form": {
                "header": yunb_hdr,
                "lines": [
                    _po_line(ctx, material=ctx.material, short_text="YUNB simple"),
                ],
            },
        },
        "yunb_multi_cc": {
            "document_type": "YUNB",
            "form": {
                "header": yunb_hdr,
                "lines": [
                    _po_line(
                        ctx,
                        material=ctx.material,
                        short_text="YUNB multi-CC",
                        allocs=[
                            {"cost_center": ctx.cost_center, "qty": "2"},
                            {"cost_center": ctx.cost_center_2, "qty": "3"},
                        ],
                    ),
                ],
            },
        },
        "yunb_two_material": {
            "document_type": "YUNB",
            "form": {
                "header": yunb_hdr,
                "lines": [
                    _po_line(
                        ctx,
                        material=ctx.material,
                        short_text="YUNB line A",
                        delivery="2026-08-01",
                    ),
                    _po_line(
                        ctx,
                        material=ctx.material_2,
                        short_text="YUNB line B",
                        delivery="2026-09-01",
                    ),
                ],
            },
        },
        "yast_simple": {
            "document_type": "YAST",
            "form": {
                "header": yast_hdr,
                "lines": [
                    _po_line(
                        ctx,
                        material=ctx.material,
                        short_text="YAST simple",
                        asset=asset,
                        info_rec=info_rec,
                        allocs=[{"asset": asset, "qty": "5"}],
                    ),
                ],
            },
        },
        "yast_multi_cc": {
            "document_type": "YAST",
            "form": {
                "header": yast_hdr,
                "lines": [
                    _po_line(
                        ctx,
                        material=ctx.material,
                        short_text="YAST multi-asset",
                        asset=asset,
                        info_rec=info_rec,
                        allocs=[
                            {"asset": asset, "qty": "2"},
                            {"asset": asset_2, "qty": "3"},
                        ],
                    ),
                ],
            },
        },
        "yast_two_material": {
            "document_type": "YAST",
            "form": {
                "header": yast_hdr,
                "lines": [
                    _po_line(
                        ctx,
                        material=ctx.material,
                        short_text="YAST line A",
                        price="100.00",
                        asset=asset,
                        info_rec=info_rec,
                        allocs=[{"asset": asset, "qty": "5"}],
                    ),
                    _po_line(
                        ctx,
                        material=ctx.material_2,
                        short_text="YAST line B",
                        price="100.00",
                        delivery="2026-09-15",
                        asset=asset_2,
                        info_rec=info_rec,
                        allocs=[{"asset": asset_2, "qty": "3"}],
                    ),
                ],
            },
        },
        "yser_simple": {
            "document_type": "YSER",
            "form": {
                "header": yser_hdr,
                "lines": [
                    _po_line(
                        ys,
                        service=ys.service,
                        short_text="YSER simple",
                        allocs=[{"cost_center": ys.cost_center, "qty": "2"}],
                    ),
                ],
            },
        },
        "yser_multi_cc": {
            "document_type": "YSER",
            "form": {
                "header": yser_hdr,
                "lines": [
                    _po_line(
                        ys,
                        service=ys.service,
                        short_text="YSER multi-CC",
                        allocs=[
                            {"cost_center": ys.cost_center_2, "qty": "1"},
                            {"cost_center": ys.cost_center, "qty": "1"},
                        ],
                    ),
                ],
            },
        },
        "yser_two_service": {
            "document_type": "YSER",
            "form": {
                "header": yser_hdr,
                "lines": [
                    _po_line(
                        ys,
                        service=ys.service,
                        short_text="YSER svc A",
                        allocs=[
                            {"cost_center": ys.cost_center_2, "qty": "1"},
                            {"cost_center": ys.cost_center, "qty": "1"},
                        ],
                    ),
                    _po_line(
                        ys,
                        service=ys.service_2,
                        short_text="YSER svc B",
                        price="50.00",
                        allocs=[{"cost_center": ys.cost_center_2, "qty": "2"}],
                    ),
                ],
            },
        },
    }


def resolve_parent_pr_id(
    *,
    parent_pr_id: str | None,
    parent_pr_sap: str | None,
    access_token: str | None = None,
) -> str | None:
    if parent_pr_id and parent_pr_id.strip():
        return parent_pr_id.strip()
    sap = (parent_pr_sap or os.environ.get("SAP_PARENT_PR") or "").strip()
    if not sap:
        return None
    if sap in DEFAULT_PARENT_PR_BY_SAP:
        return DEFAULT_PARENT_PR_BY_SAP[sap]
    if access_token:
        found = lookup_parent_pr_id_by_sap(access_token=access_token, sap_pr_number=sap)
        if found:
            return found
    raise SystemExit(
        f"Unknown parent PR {sap!r}. Pass --parent-pr-id or run with --list-parent-prs."
    )


def lookup_parent_pr_id_by_sap(*, access_token: str, sap_pr_number: str) -> str | None:
    url = f"{api_base()}/api/procurement/tickets/parent-prs"
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code >= 400:
        return None
    data = resp.json()
    if not isinstance(data, list):
        return None
    sap = sap_pr_number.strip()
    for row in data:
        if isinstance(row, dict) and str(row.get("sap_id") or "").strip() == sap:
            tid = str(row.get("id") or "").strip()
            if tid:
                return tid
    return None


def list_parent_prs(*, access_token: str, document_type: str | None = None) -> None:
    url = f"{api_base()}/api/procurement/tickets/parent-prs"
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code >= 400:
        raise SystemExit(f"parent-prs failed HTTP {resp.status_code}: {resp.text[:500]}")
    rows = resp.json()
    if not isinstance(rows, list):
        raise SystemExit("parent-prs returned non-list")
    dt_filter = (document_type or "").upper()
    print(f"{'sap_id':<14} {'doc_type':<6} {'ticket_id'}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if dt_filter and str(row.get("document_type") or "").upper() != dt_filter:
            continue
        sap_id = str(row.get("sap_id") or "")
        if not sap_id:
            continue
        print(f"{sap_id:<14} {str(row.get('document_type') or ''):<6} {row.get('id')}")


def print_po_live_result(ticket: dict[str, Any], *, label: str) -> int:
    """Print ticket + SAP error block for Basis calls; return exit code."""
    sap_id = ticket.get("sap_id")
    sync = ticket.get("sap_sync") if isinstance(ticket.get("sap_sync"), dict) else {}
    last_error = str(sync.get("last_error") or "").strip()
    attempts = sync.get("attempt_count")
    create_submitted = sync.get("create_submitted")

    print(f"\n{'=' * 60}")
    print(label)
    print(f"{'=' * 60}")
    print(f"ticket_id:        {ticket.get('id')}")
    print(f"document_type:    {ticket.get('document_type')}")
    print(f"parent_pr_id:     {ticket.get('parent_pr_id')}")
    print(f"sap_id:           {sap_id!r}")
    print(f"sync attempts:    {attempts}")
    print(f"create_submitted: {create_submitted}")

    if last_error:
        print("\n--- SAP error (copy for Basis) ---")
        print(last_error)
        print("--- end SAP error ---\n")

    if sap_id and str(sap_id).strip() and not str(sap_id).strip().startswith("#"):
        print("RESULT: OK — numeric PO in SAP")
        return 0
    print("RESULT: FAIL — no numeric sap_id")
    return 1


def print_run_command_hint(*, document_type: str, parent_pr_sap: str | None) -> None:
    base = (
        "cd agentos-backend && source .venv/bin/activate && "
        "AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py"
    )
    parts = [base, f"--doc-type {document_type.upper()}"]
    if parent_pr_sap:
        parts.append(f"--parent-pr-sap {parent_pr_sap}")
    print("Re-run:\n  " + " ".join(parts) + "\n")
