"""Integration tests for the Jun-2026 receivables workbook format.

Validates tab resolution, all three PAYMENT_REMINDER_WEEKLY variants, and the
Data Viz dashboard parser against a synthetic workbook that mirrors production
tab titles and column shapes (``Invoice details-H&T``, ``Partywise Ageing H & T``,
``Receivable as on <date>``, etc.).

When ``RECEIVABLES_JUN_2026_FIXTURE`` points at a real finance file, an optional
slow test exercises the same assertions against production data.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.email_automation.engine import excel_reader as xr
from app.email_automation.engine import dsl
from app.email_automation.pipeline import build_variant_plans
from app.email_automation.workflow_packs import REGISTRY
from app.email_automation.workflow_packs.payment_reminder import (
    CHW_VARIANT,
    DIAGNOSTICS_AGGREGATOR_VARIANT,
    EPHARMA_VARIANT,
    PAYMENT_REMINDER_WEEKLY,
    _CHW_INVOICE_FILTER,
    _EPHARMA_FILTER,
    _INVOICE_H_PSP_HINTS,
    _PARTY_WISE_HINTS,
)
from app.services import receivable_dashboard as rd

# Optional path to the real Jun-2026 shared workbook (local QA only).
PRODUCTION_JUN_FIXTURE = Path(
    os.environ.get(
        "RECEIVABLES_JUN_2026_FIXTURE",
        "/Users/pankaj/Downloads/receivables-10.06.2026-Shared.xlsx",
    )
)


def _write_jun_invoice_h_t(ws) -> None:
    """Merged ``Invoice details-H&T`` — H&T header layout with ``Ageing type``."""

    ws.append([])
    ws.append([])
    ws.append(
        [
            "Co", "Code", "HANA Code", "Name of the party", "Brand Name",
            "Business Owner", "KAM 2", "RPT", "KAM",
            "Invoice Date", "Invoice No:-", "Credit Days",
            "Original Amount from Aug'24", "Net amount Pending",
            "Final Amount Pending", "Ageing type", "Remarks", "Consider",
            "Business Unit", "Segment",
        ]
    )
    # ePharma — passes all filters.
    ws.append(
        [
            "1MGH", "EPH001", "1000000001", "SUN PHARMA", "Brand A",
            "Owner", "K2", None, "K1",
            datetime(2026, 1, 10), "INV-EPH-001", 30,
            250000, 250000, 250000,
            "1-3 Months", "Invoice", "Positive",
            "e-Pharmacy", "Advertisement",
        ]
    )
    # CHW invoice row on merged H&T.
    ws.append(
        [
            "TATA 1MGH", "LAB0090", "1000022222", "ACME CORP", None,
            "Owner", "K2", None, "K1",
            datetime(2026, 1, 5), "TM-LAB-0001", 30,
            45000, 45000, 45000,
            "1-3 Months", "Invoice", "Positive",
            "Corporate Wellness", "Lab- Affiliate Channel",
        ]
    )
    # e-Diagnostic-Aggregator invoice row on merged H&T (diagnostics variant).
    ws.append(
        [
            "1MGT", "OTI-360", "2000000360", "DIAG CLINIC E2E", None,
            "Owner", "K2", None, "K1",
            datetime(2026, 4, 13), "TM2606000053", 30,
            80500, 80500, 80500,
            "1-3 Months", "Invoice", "Positive",
            "e-Diagnostic-Aggregator", "Aggregator",
        ]
    )


def _write_jun_partywise(ws) -> None:
    for _ in range(4):
        ws.append([])
    ws.append(
        [
            "Co", "HANA Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws.append(
        [
            "1MGH", "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            50000, 0, "",
        ]
    )
    # Party-only CHW (no invoice on H&T).
    ws.append(
        [
            "1MGH", "PARTYONLY99", "PARTY ONLY CORP", "", "", "Corporate Wellness",
            "OHC PHARMA", "Amit", 45,
            0, 0, 0, 50000, 0, 0, 0, 50000,
            0, 0, "",
        ]
    )


_JUN_DMPL_SAP_HEADERS: tuple[str, ...] = (
    "Company Code", "Fiscal Year", "Posting period", "Customer", "A",
    "Company Code Currency Key", "Company Code Currency Value",
    "Processed Database Rows", "Document Currency Key", "Document Currency Value",
    "Changed On", "Clearing Item", "Company Code", "Clearing Entry Date",
    "Document Number", "Document Header Text", "Document type", "Document Date",
    "Posting Key", "Posting Date", "Business place", "Entry Date", "Reference date",
    "Business Area", "G/L Account", "Specl G/L Assgt", "Group Chart of Accts",
    "Profit Center", "Reference", "Reference Key 1", "Ref.key (header) 1",
    "Reference Key 2", "Reference key 3", "Offsetting Account in General Ledger",
    "Place of Supply", "Cleared Item", "Search term", "Net Due Date",
    "Customer Account: Name 1",
)


def _write_jun_dmpl(ws) -> None:
    ws.append([None] * 6 + [0])
    ws.append(list(_JUN_DMPL_SAP_HEADERS))
    row: list[object | None] = [None] * len(_JUN_DMPL_SAP_HEADERS)
    idx = {name: i for i, name in enumerate(_JUN_DMPL_SAP_HEADERS)}
    row[idx["Company Code"]] = "1MGH"
    row[idx["Customer"]] = "DMPL4892"
    row[idx["A"]] = "DMPL4892"
    row[idx["Company Code Currency Key"]] = "INR"
    row[idx["Company Code Currency Value"]] = 38604
    row[idx["Document Number"]] = "8006485806"
    row[idx["Document Header Text"]] = "B020822600000755"
    row[idx["Document Date"]] = datetime(2026, 1, 22)
    row[idx["Net Due Date"]] = datetime(2026, 1, 15)
    row[idx["Customer Account: Name 1"]] = "TATA CONSULTANCY SERVICES LTD"
    ws.append(row)


def _write_jun_receivable_as_on(ws) -> None:
    for _ in range(3):
        ws.append([])
    ws.append(
        [
            "Entity Name", "Type", "Code", "Name of the party", "KAM", "RPT",
            "Business Unit", "Segment", "Name of Business owner", "Credit Period",
            "Check", "Receivables", "Not Due", "0-1 Months", "1-3 Months",
            "3-6 Months", "6-9 Months", "9-12 Months", "+1yrs", "Subsquent receipt",
            "Due TDS", "Net Due", "Not Due TDS",
        ]
    )
    ws.append(
        [
            "1MGT", "IN", "2000000360", "DIAG CLINIC E2E", None, None,
            "e-Diagnostic-Aggregator", "Aggregator",
            "Ashyin Thakral/Prateek verma", 30,
            1, 50000, 0, 45500, 0, 0, 0, 0, 0, 0, 0, 45500, 0,
        ]
    )
    # Row for dashboard parser smoke (ePharma BU).
    ws.append(
        [
            "1MGT", "IN", "DASH001", "Dashboard Party", None, None,
            "e-Pharmacy", "Advertisement", "Owner", 30,
            1, 100000, 0, 100000, 0, 0, 0, 0, 0, 0, 0, 100000, 0,
        ]
    )


def build_jun2026_workbook(path: Path) -> None:
    """Synthetic workbook with Jun-2026 physical tab titles."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_jun_invoice_h_t(ws)

    wb.create_sheet("Invoice wise DMPL")
    _write_jun_dmpl(wb["Invoice wise DMPL"])

    ws_pw = wb.create_sheet("Partywise Ageing H & T")
    _write_jun_partywise(ws_pw)

    ws_recv = wb.create_sheet("Receivable as on 10.06.2026")
    _write_jun_receivable_as_on(ws_recv)

    ws_u = wb.create_sheet("Partywise Unbilled")
    ws_u.append(["Business Unit", "Unbilled"])
    ws_u.append(["e-Pharmacy", 1000])

    ws_t = wb.create_sheet("TDS Ageing")
    ws_t.append(["Code", "Name of the party", "Business Unit", "0-1 Months", "1-3 Months"])
    ws_t.append(["DASH001", "Dashboard Party", "e-Pharmacy", 0, 0])

    wb.save(str(path))


def _run_variant(variant_name: str, xlsx: Path, tracker: list[dict[str, object]]):
    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    variant = pack.variant(variant_name)
    return asyncio.run(
        build_variant_plans(
            pack=pack,
            variant=variant,
            xlsx_path=xlsx,
            tracker_rows=tracker,
            period="2026-W24",
        )
    )


def test_jun2026_tab_resolution(tmp_path: Path) -> None:
    """Configured canonical names resolve to Jun physical tab titles."""

    xlsx = tmp_path / "jun2026.xlsx"
    build_jun2026_workbook(xlsx)

    inv = xr.read_sheet(
        xlsx, "Invoice details-H(all)&T(Psp)", header_hints=_INVOICE_H_PSP_HINTS
    )
    assert inv.sheet_name == "Invoice details-H&T"

    pw = xr.read_sheet(
        xlsx, "Party wise Ageing-H&T", header_hints=_PARTY_WISE_HINTS
    )
    assert pw.sheet_name == "Partywise Ageing H & T"


def test_jun2026_all_variants_and_dashboard(tmp_path: Path) -> None:
    """ePharma, CHW, diagnostics, and Data Viz all parse the Jun-shaped workbook."""

    xlsx = tmp_path / "jun2026.xlsx"
    build_jun2026_workbook(xlsx)

    eph = _run_variant("epharma", xlsx, [])
    assert len(eph) == 1
    assert eph[0]["business_key"] == "1000000001"
    assert eph[0]["source_sheets"] == ["Invoice details-H&T"]

    chw = _run_variant("chw", xlsx, [])
    chw_keys = {p["business_key"] for p in chw}
    assert chw_keys == {"1000022222", "DMPL4892"}
    assert "PARTYONLY99" not in chw_keys

    invoice_chw = next(p for p in chw if p["business_key"] == "1000022222")
    assert invoice_chw["source_sheets"] == ["Invoice details-H&T"]
    assert "TM-LAB-0001" in invoice_chw["rendered_body_html"]

    diag = _run_variant("diagnostics_aggregator", xlsx, [])
    assert len(diag) == 1
    assert diag[0]["business_key"] == "2000000360"
    assert diag[0]["source_sheets"] == ["Invoice details-H&T"]

    dash = rd.build_receivable_payload(xlsx, send_counts={})
    assert dash is not None
    meta = dash["meta"]
    assert meta["receivable_sheet"] == "Receivable as on 10.06.2026"
    assert meta["unbilled_sheet"] == "Partywise Unbilled"
    assert meta["receivable_layout"]["tds_sheet"] == "TDS Ageing"
    dq = meta["data_quality"]
    assert "tds_clip_count" in dq
    assert "stale_bucket_count" in dq


def test_jun2026_filters_use_ageing_type_column(tmp_path: Path) -> None:
    """Merged H&T uses ``Ageing type`` (not legacy ``Ageing type 2``)."""

    xlsx = tmp_path / "jun2026.xlsx"
    build_jun2026_workbook(xlsx)
    sd = xr.read_sheet(xlsx, "Invoice details-H&T", header_hints=_INVOICE_H_PSP_HINTS)
    eph_rows = [r for r in sd.rows if dsl.evaluate(_EPHARMA_FILTER, r)]
    chw_rows = [r for r in sd.rows if dsl.evaluate(_CHW_INVOICE_FILTER, r)]
    assert len(eph_rows) == 1
    assert len(chw_rows) == 1


@pytest.mark.slow
@pytest.mark.skipif(
    not PRODUCTION_JUN_FIXTURE.exists(),
    reason="Set RECEIVABLES_JUN_2026_FIXTURE to the real Jun workbook path",
)
def test_jun2026_production_workbook_smoke() -> None:
    """Smoke test against the real Jun-2026 shared file when available locally."""

    path = PRODUCTION_JUN_FIXTURE

    inv = xr.read_sheet(path, "Invoice details-H&T", header_hints=_INVOICE_H_PSP_HINTS)
    assert len(inv.rows) > 1000

    pw = xr.read_sheet(path, "Partywise Ageing H & T", header_hints=_PARTY_WISE_HINTS)
    assert len(pw.rows) > 100

    async def _plans(variant):
        return await build_variant_plans(
            pack=PAYMENT_REMINDER_WEEKLY,
            variant=variant,
            xlsx_path=path,
            tracker_rows=[],
            period="2026-W24",
        )

    eph_n = len(asyncio.run(_plans(EPHARMA_VARIANT)))
    chw_n = len(asyncio.run(_plans(CHW_VARIANT)))
    diag_n = len(asyncio.run(_plans(DIAGNOSTICS_AGGREGATOR_VARIANT)))
    assert eph_n > 100
    assert chw_n > 100
    assert diag_n > 10

    dash = rd.build_receivable_payload(path, send_counts={})
    assert dash is not None
    assert dash["meta"]["receivable_sheet"].startswith("Receivable as on")

    # SAP-shaped DMPL on production workbook — CHW reads invoice lines.
    sd_dmpl = xr.read_sheet(
        path,
        "Invoice wise DMPL",
        header_hints=(
            "document header text", "company code currency value", "customer",
        ),
    )
    from app.email_automation.workflow_packs.payment_reminder import _CHW_DMPL_FILTER

    dmpl_pass = sum(1 for r in sd_dmpl.rows if dsl.evaluate(_CHW_DMPL_FILTER, r))
    assert dmpl_pass > 0

    dmpl_hanas = {
        str(r.get("customer#0")).strip()
        for r in sd_dmpl.rows
        if dsl.evaluate(_CHW_DMPL_FILTER, r)
    }
    # Party-only on partywise with no qualifying H&T or DMPL invoice lines → no plan.
    sd_inv = xr.read_sheet(path, "Invoice details-H&T", header_hints=_INVOICE_H_PSP_HINTS)
    inv_hanas = {
        str(r.get("hana code#0")).strip()
        for r in sd_inv.rows
        if dsl.evaluate(_CHW_INVOICE_FILTER, r)
    }
    party_only_hanas: set[str] = set()
    for r in pw.rows:
        h = str(r.get("hana code#0") or r.get("code#0") or "").strip()
        bu = str(r.get("business unit#0") or "").strip().casefold()
        if (
            h
            and bu == "corporate wellness"
            and h not in inv_hanas
            and h not in dmpl_hanas
        ):
            party_only_hanas.add(h)
    assert len(party_only_hanas) > 0
    chw_plan_keys = {p["business_key"] for p in asyncio.run(_plans(CHW_VARIANT))}
    assert party_only_hanas.isdisjoint(chw_plan_keys)
