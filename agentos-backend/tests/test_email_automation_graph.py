"""Tests for the unified email-automation LangGraph and the per-variant pipeline.

Coverage:

* ``build_email_automation_graph``                    — node + edge shape, kill switch.
* ``workflow_catalog.list_workflow_catalog``          — both entry keys + node descriptions.
* ``workflow_runner.canonical_workflow_key``          — alias normalization.
* ``pipeline.build_variant_plans``                    — full per-variant pipeline
  (sheets → filter → projection → aggregate → lookup → render → resolve) on
  synthetic Excel that mirrors the real production sheets.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.agents.email_automation import (
    build_email_automation_graph,
    run_dispatch_async,
)
from app.config.settings import settings
from app.email_automation.pipeline import build_variant_plans
from app.email_automation.workflow_packs import REGISTRY
from app.services.workflow_catalog import list_workflow_catalog
from app.services.workflow_runner import (
    CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY,
    CANONICAL_EMAIL_AUTOMATION_SCAN_KEY,
    canonical_workflow_key,
)


# ---------------------------------------------------------------------------
# Synthetic Excel — mirrors the columns + header row offsets of the real file
# ---------------------------------------------------------------------------


def _write_invoice_h_psp(ws) -> None:
    """``Invoice details-H(all)&T(Psp)`` — header at row 2 (0-indexed)."""

    ws.append([])
    ws.append([])
    ws.append(
        [
            "Co", "Code", "HANA Code", "Name of the party", "Brand Name",
            "Business Owner", "KAM 2", "RPT", "KAM",
            "Invoice Date", "Invoice No:-", "Credit Days",
            "Original Amount from Aug'24", "Net Amount Pending",
            "Write off Amount", "Ap Adjustment/CN", "CW Recipts",
            "PLA", "Old Receipts", "Receipts-Mar", "26AS",
            "TDS/Adjustment", "SD Amount", "Final Amount Pending",
            "PGB/CPGB Amount/Rent", "Location", "Days", "Due Days",
            "Ageing type", "Ageing type-Audit", "Remarks", "Consider",
            "Mapping type", "Entry code", "Mapping status",
            "Business transfer Invoice", "Business Unit", "Retail",
            "Segment", "Action", "PLA Remarks", "Program Details-PSP",
            "Buisness Unit Old( 27th may'24)", "Check", "PO Number", "Period",
        ]
    )

    # Row 1: ePharma SUN PHARMA (matches all 5 ePharma filters → keep)
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "1000000001", "SUN PHARMACEUTICAL INDUSTRIES LTD",
            "Cetaphil", "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 1, 15), "HH2506026266", 75,
            500000, 250000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 250000,
            0, "Mumbai", 90, 60,
            "1-3 Months", None, "Invoice", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )
    # Row 2: ePharma SUN PHARMA but Remarks=TDS → ePharma drops it
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "1000000001", "SUN PHARMACEUTICAL INDUSTRIES LTD",
            "Cetaphil", "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 1, 22), "HH2506026355", 75,
            100000, 100000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 100000,
            0, "Mumbai", 60, 30,
            "0-1 Months", None, "TDS", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )
    # Row 3: CHW row (Business Unit = Corporate Wellness, Remarks=Invoice) →
    # ePharma drops; CHW H&T keeps.
    ws.append(
        [
            "TATA 1MGH", "DMPL999", "1000022222", "ACME CORP CORPORATE",
            None, "Owner", "K2", None, "K1",
            datetime(2025, 12, 1), "HH2511099999", 30,
            80000, 80000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 80000,
            0, "Bangalore", 120, 90,
            "3-6 Months", None, "Invoice", "Positive",
            None, None, None, None, "Corporate Wellness", None,
            "Lab- Affiliate Channel", None, None, None, None, None, None, None,
        ]
    )
    # Row 4: ePharma but Final Amount Pending = 0 → ePharma drops.
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "1000000003", "ZERO BALANCE LTD",
            None, "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 2, 1), "HH99999", 75,
            10, 10,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            0, "Mumbai", 30, 0,
            "0-1 Months", None, "Invoice", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )
    # Former T(labs) row — now on the merged H&T tab (same HANA as row 3).
    ws.append(
        [
            "TATA 1MGH", "LAB0090", "1000022222", "ACME CORP CORPORATE",
            None, "Owner", "K2", None, "K1",
            datetime(2026, 1, 5), "TM-LAB-0001", 30,
            45000, 45000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 45000,
            0, "Bangalore", 60, 30,
            "1-3 Months", None, "Invoice", "Positive",
            None, None, None, None, "Corporate Wellness", None,
            "Lab- Affiliate Channel", None, None, None, None, None, None, None,
        ]
    )
    # e-Diagnostic-Aggregator row → diagnostics_aggregator variant keeps it
    # (Business Unit + Remarks allowlist + Final Amount Pending ≥ 1).
    ws.append(
        [
            "TATA 1MGH", "OTI-360", "2000000360", "DIAG CLINIC E2E",
            None, "Owner", "K2", None, "K1",
            datetime(2026, 4, 13), "TM2606000053", 30,
            80500, 80500,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 80500,
            0, "Delhi", 90, 60,
            "1-3 Months", None, "Invoice", "Positive",
            None, None, None, None, "e-Diagnostic-Aggregator", None,
            "Diagnostics", None, None, None, None, None, None, None,
        ]
    )


def _write_invoice_t_labs(ws) -> None:
    """``Invoice details-T(labs)`` — header at row 3."""

    ws.append([])
    ws.append([])
    ws.append([])
    ws.append(
        [
            "Customer Code", "HANA Code", "Name-HANA", "Customer Name",
            "KAM", "RPT", "Business Owner", "Business Unit", "Segment",
            "Credit Days", "Type", "Doc. No.", "Posting Date", "Invoice Number",
            "Original Amount", "Pending Amount", "Receipts", "TDS Mapped",
            "Net amount Pending", "Remarks", "Knock off Status", "Due Days",
            "Ageing", "Remarks",
        ]
    )
    # Row 1: CHW labs party — passes all CHW T(labs) filters. The trailing
    # "Positive" populates the *second* ``Remarks`` column (Consider).
    ws.append(
        [
            "1MGLABD0090", "1000022222", "ACME CORP CORPORATE", "ACME CORP CORPORATE",
            None, None, "Alok Bharti", "Corporate Wellness", "Lab- Affiliate Channel",
            30, "IN", "211000049", datetime(2026, 1, 5), "TM-LAB-0001",
            45000, 45000, 0, 0,
            45000, "Invoice", "Open", 60,
            "1-3 Months", "Positive",
        ]
    )
    # Row 2: CHW labs but Remarks="Adjustment" → drop on Remarks filter.
    ws.append(
        [
            "1MGLABD0091", "1000022222", "ACME CORP CORPORATE", "ACME CORP CORPORATE",
            None, None, "Alok Bharti", "Corporate Wellness", "Lab- Affiliate Channel",
            30, "JE", "211000050", datetime(2026, 1, 10), "TM-LAB-0002",
            10000, 5000, 5000, 0,
            5000, "Adjustment", "Open", 30,
            "0-1 Months", "Positive",
        ]
    )
    # Row 3: CHW labs but BU != Corporate Wellness → drop on BU filter.
    ws.append(
        [
            "1MGLABD0092", "1000033333", "OTHER LTD", "OTHER LTD",
            None, None, "Alok Bharti", "Others", "Lab- Affiliate Channel",
            30, "IN", "211000051", datetime(2026, 1, 12), "TM-LAB-0003",
            5000, 5000, 0, 0,
            5000, "Invoice", "Open", 30,
            "0-1 Months", "Positive",
        ]
    )
    # Row 4: CHW labs, Remarks="Invoice", BU=Corporate Wellness, but the
    # second Remarks / Consider column is blank → drop on Consider filter.
    ws.append(
        [
            "1MGLABD0093", "1000044444", "BLANK CONSIDER LTD", "BLANK CONSIDER LTD",
            None, None, "Alok Bharti", "Corporate Wellness", "Lab- Affiliate Channel",
            30, "IN", "211000052", datetime(2026, 1, 15), "TM-LAB-0004",
            7500, 7500, 0, 0,
            7500, "Invoice", "Open", 30,
            "0-1 Months", None,
        ]
    )


_DMPL_SAP_HEADERS: tuple[str, ...] = (
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


def _dmpl_sap_row(
    *,
    customer: str,
    amount: Decimal | int | float,
    inv_no: str,
    inv_date: datetime,
    net_due: datetime,
    party_name: str,
) -> list[object | None]:
    """One SAP-shaped ``Invoice wise DMPL`` data row for CHW tests."""

    row: list[object | None] = [None] * len(_DMPL_SAP_HEADERS)
    idx = {name: i for i, name in enumerate(_DMPL_SAP_HEADERS)}
    row[idx["Company Code"]] = "1MGH"
    row[idx["Customer"]] = customer
    row[idx["A"]] = customer
    row[idx["Company Code Currency Key"]] = "INR"
    row[idx["Company Code Currency Value"]] = amount
    row[idx["Document Number"]] = "8006485806"
    row[idx["Document Header Text"]] = inv_no
    row[idx["Document Date"]] = inv_date
    row[idx["Net Due Date"]] = net_due
    row[idx["Customer Account: Name 1"]] = party_name
    return row


def _write_invoice_dmpl(ws) -> None:
    """``Invoice wise DMPL`` — SAP invoice-line export (Jun-2026+)."""

    ws.append([None] * 6 + [0])  # title row (production workbooks)
    ws.append(list(_DMPL_SAP_HEADERS))
    past_due = datetime(2026, 1, 15)
    future_due = datetime(2026, 6, 25)
    ws.append(
        _dmpl_sap_row(
            customer="DMPL4892",
            amount=38604,
            inv_no="B020822600000755",
            inv_date=datetime(2026, 1, 22),
            net_due=past_due,
            party_name="TATA CONSULTANCY SERVICES LTD",
        )
    )
    # Not Due–only DMPL party — still mails under Jun-2026+ rules when C ≥ 0.
    ws.append(
        _dmpl_sap_row(
            customer="DMPL4899",
            amount=10000,
            inv_no="B020822600000999",
            inv_date=datetime(2026, 2, 1),
            net_due=future_due,
            party_name="OTHER PARTY",
        )
    )


def _write_receivable_as_on(ws) -> None:
    """``Receivable as on <date>`` — header at row 3 (0-indexed)."""

    for _ in range(3):
        ws.append([])
    ws.append(
        [
            "Entity Name", "Type", "Code", "Name of the party", "KAM", "RPT",
            "Business Unit", "Segment", "Name of Business owner", "Credit Period",
            "Check", "Receivables", "Not Due", "0-1 Months", "1-3 Months",
            "3-6 Months", "6-9 Months", "9-12 Months", "+1Yrs",
            "Subsquent Receipt", "Due TDS", "Net Due", "Not Due TDS",
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "2000000360", "DIAG CLINIC E2E", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Ashyin Thakral/Prateek verma", 30, 0,
            45500, 0, 0, 0, 45500, 0, 0, 0, 0, 0, 45500, 0,
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "1000001169", "VISIT HEALTH E2E", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Ashyin Thakral/Prateek verma", 30, 0,
            12000, 0, 0, 12000, 0, 0, 0, 0, 0, 0, 12000, 0,
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "1000001458", "LIFE CARE CLINIC", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Dr Prashant Nag", 30, 0,
            320, 0, 0, 320, 0, 0, 0, 0, 0, 0, 320, 0,
        ]
    )


def _write_receivable_as_on(ws) -> None:
    """``Receivable as on <date>`` — header at row 3 (0-indexed)."""

    for _ in range(3):
        ws.append([])
    ws.append(
        [
            "Entity Name", "Type", "Code", "Name of the party", "KAM", "RPT",
            "Business Unit", "Segment", "Name of Business owner", "Credit Period",
            "Check", "Receivables", "Not Due", "0-1 Months", "1-3 Months",
            "3-6 Months", "6-9 Months", "9-12 Months", "+1Yrs",
            "Subsquent Receipt", "Due TDS", "Net Due", "Not Due TDS",
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "2000000360", "DIAG CLINIC E2E", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Ashyin Thakral/Prateek verma", 30, 0,
            45500, 0, 0, 0, 45500, 0, 0, 0, 0, 0, 45500, 0,
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "1000001169", "VISIT HEALTH E2E", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Ashyin Thakral/Prateek verma", 30, 0,
            12000, 0, 0, 12000, 0, 0, 0, 0, 0, 0, 12000, 0,
        ]
    )
    ws.append(
        [
            "1MGT", "OTI", "1000001458", "LIFE CARE CLINIC", None, "Non RPT",
            "e-Diagnostic-Aggregator", "Aggregator",
            "Dr Prashant Nag", 30, 0,
            320, 0, 0, 320, 0, 0, 0, 0, 0, 0, 320, 0,
        ]
    )


def _write_party_wise(ws) -> None:
    """``Party wise Ageing-H&T`` — header at row 4. Used for unaccounted lookup."""

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
    # SUN PHARMA — has 50,000 unaccounted revenue.
    ws.append(
        [
            "1MGH", "1000000001", "SUN PHARMACEUTICAL INDUSTRIES LTD", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            50000, 0, "",
        ]
    )
    # ACME CHW — no unaccounted revenue (invoice-level rows live on H&T only).
    ws.append(
        [
            "1MGH", "1000022222", "ACME CORP CORPORATE", "K2", "", "Corporate Wellness",
            "Lab- Affiliate Channel", "Owner", 30,
            0, 0, 45000, 80000, 0, 0, 0, 125000,
            0, 0, "",
        ]
    )
    # Diagnostics aggregator party — scoped unaccounted on aggregator BU only.
    ws.append(
        [
            "1MGT", "2000000360", "DIAG CLINIC E2E", "", "", "e-Diagnostic-Aggregator",
            "Aggregator", "Ashyin Thakral/Prateek verma", 30,
            0, 0, 45500, 0, 0, 0, 0, 45500,
            5000, 0, "",
        ]
    )
    # Diagnostics aggregator party — scoped unaccounted on aggregator BU only.
    ws.append(
        [
            "2000000360", "DIAG CLINIC E2E", "", "", "e-Diagnostic-Aggregator",
            "Aggregator", "Ashyin Thakral/Prateek verma", 30,
            0, 0, 45500, 0, 0, 0, 0, 45500,
            5000, 0, "",
        ]
    )


def _make_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)

    ws3 = wb.create_sheet("Invoice wise DMPL")
    _write_invoice_dmpl(ws3)

    ws4 = wb.create_sheet("Party wise Ageing-H&T")
    _write_party_wise(ws4)

    ws5 = wb.create_sheet("Receivable as on 27.05.2026")
    _write_receivable_as_on(ws5)

    wb.save(str(path))


def test_diagnostics_aggregator_receivable_filter_scopes_bus_owner_and_net_due() -> None:
    from app.email_automation.engine import dsl as _dsl
    from app.email_automation.engine import normalizer as _norm
    from app.email_automation.workflow_packs.payment_reminder import (
        _DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER,
        _TYPES_RECEIVABLE_AS_ON,
    )

    def _typed(keys: dict[str, object]) -> dict[str, object]:
        return {
            k: _norm.normalize_value(v, _TYPES_RECEIVABLE_AS_ON.get(k, "raw"))
            for k, v in keys.items()
        }

    ok = {
        "code#0": "1000001169",
        "business unit#0": "e-Diagnostic-Aggregator",
        "name of business owner#0": "Ashyin Thakral/Prateek verma",
        "net due#0": Decimal("973001.37"),
    }
    assert _dsl.evaluate(_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER, _typed(ok))

    pharma_agg = {
        **ok,
        "code#0": "8000000036",
        "business unit#0": "e-Pharmacy (Platform Aggregator)",
    }
    assert _dsl.evaluate(_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER, _typed(pharma_agg))

    wrong_owner = {**ok, "name of business owner#0": "Dr Prashant Nag"}
    assert not _dsl.evaluate(_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER, _typed(wrong_owner))

    sub_rupee = {**ok, "net due#0": Decimal("0.5")}
    assert not _dsl.evaluate(_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER, _typed(sub_rupee))

    chw_bu = {**ok, "business unit#0": "Corporate Wellness"}
    assert not _dsl.evaluate(_DIAGNOSTICS_AGGREGATOR_RECEIVABLE_FILTER, _typed(chw_bu))


def test_chw_invoice_filter_accepts_positive_consider_on_merged_ht() -> None:
    """Merged ``Invoice details-H&T`` CHW gate: ``Consider = Positive`` + ``Ageing type``."""

    from app.email_automation.engine import dsl as _dsl
    from app.email_automation.engine import normalizer as _norm
    from app.email_automation.workflow_packs.payment_reminder import (
        _CHW_INVOICE_FILTER,
        _TYPES_INVOICE_H_PSP,
    )

    def _typed(keys: dict[str, object]) -> dict[str, object]:
        return {
            k: _norm.normalize_value(v, _TYPES_INVOICE_H_PSP.get(k, "raw"))
            for k, v in keys.items()
        }

    base = {
        "hana code#0": "H1",
        "remarks#0": "Invoice",
        "business unit#0": "Corporate Wellness",
        "ageing type#0": "1-3 Months",
        "net amount pending#0": Decimal("100"),
        "final amount pending#0": Decimal("100"),
    }
    positive = {**base, "consider#0": "  Positive  "}
    assert _dsl.evaluate(_CHW_INVOICE_FILTER, _typed(positive))

    neither = {**base, "consider#0": ""}
    assert not _dsl.evaluate(_CHW_INVOICE_FILTER, _typed(neither))


# ---------------------------------------------------------------------------
# Graph compilation + structure
# ---------------------------------------------------------------------------


def test_unified_graph_compiles_with_expected_nodes_and_edges() -> None:
    g = build_email_automation_graph()
    meta = g.get_graph()
    node_ids = {n for n in meta.nodes if n not in {"__start__", "__end__"}}
    assert node_ids == {
        "mode_router",
        "ingest_messages",
        "collections_intelligence",
        "kam_reply_intelligence",
        "classify_and_process",
        "reclaim_stuck",
        "send_batch",
    }

    edges = {(e.source, e.target) for e in meta.edges}
    assert ("__start__", "mode_router") in edges
    # Conditional fan-out from the router into both branches.
    assert ("mode_router", "ingest_messages") in edges
    assert ("mode_router", "reclaim_stuck") in edges
    # Scan branch: ingest → collections intelligence → KAM intelligence → classify.
    assert ("ingest_messages", "collections_intelligence") in edges
    assert ("collections_intelligence", "kam_reply_intelligence") in edges
    assert ("kam_reply_intelligence", "classify_and_process") in edges
    assert ("classify_and_process", "__end__") in edges
    # Dispatch branch.
    assert ("reclaim_stuck", "send_batch") in edges
    assert ("send_batch", "__end__") in edges


def test_workflow_catalog_exposes_unified_graph_under_both_keys() -> None:
    catalog = list_workflow_catalog()
    by_key = {entry["key"]: entry for entry in catalog}
    assert CANONICAL_EMAIL_AUTOMATION_SCAN_KEY in by_key
    assert CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY in by_key

    scan = by_key[CANONICAL_EMAIL_AUTOMATION_SCAN_KEY]
    dispatch = by_key[CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY]

    # Both entries point at the SAME compiled graph — they expose identical
    # node/edge lists. The runner picks the branch via ``mode``.
    assert {n["id"] for n in scan["nodes"]} == {n["id"] for n in dispatch["nodes"]}
    assert scan["entry_branch"] == "scan"
    assert dispatch["entry_branch"] == "dispatch"

    # Every node has a non-default description.
    for node in scan["nodes"]:
        assert node["description"] != "Workflow node."


def test_canonical_workflow_key_normalizes_email_automation_aliases() -> None:
    assert (
        canonical_workflow_key("email_automation.scan")
        == CANONICAL_EMAIL_AUTOMATION_SCAN_KEY
    )
    assert (
        canonical_workflow_key("Email.Automation.Dispatch")
        == CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY
    )
    assert canonical_workflow_key("unknown_workflow") == "unknown_workflow"


# ---------------------------------------------------------------------------
# Kill switch on dispatch branch
# ---------------------------------------------------------------------------


def test_dispatch_run_respects_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import settings

    monkeypatch.setattr(settings, "email_automation_enabled", False, raising=False)
    out = asyncio.run(run_dispatch_async(limit=10))
    assert out["disabled"] is True
    assert out["sent_ids"] == []
    assert out["failed"] == []
    assert out["reclaim"] == {"requeued": 0, "completed": 0}


# ---------------------------------------------------------------------------
# End-to-end per-variant plan building (synthetic Excel)
# ---------------------------------------------------------------------------


_JUN_REF = date(2026, 6, 10)


def _run_variant(
    variant_name: str,
    xlsx: Path,
    tracker: list[dict[str, object]],
    *,
    reference_date: date | None = _JUN_REF,
):
    from app.email_automation.pipeline.process import (
        _set_projection_reference_date_for_tests,
    )

    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    variant = pack.variant(variant_name)
    _set_projection_reference_date_for_tests(reference_date)
    try:
        return asyncio.run(
            build_variant_plans(
                pack=pack,
                variant=variant,
                xlsx_path=xlsx,
                tracker_rows=tracker,
                period="2026-W16",
            )
        )
    finally:
        _set_projection_reference_date_for_tests(None)


def test_epharma_variant_keeps_sun_pharma_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ePharma's 5 SOP filters keep one row (SUN PHARMA), drop the rest:
    the TDS row, the Corporate Wellness row, the zero-balance row.

    Forces ``test_mode=False`` because the assertions below verify the
    production behavior of the new ``static_to`` contract: the AR-desk
    address must appear in ``resolved_to``. In test mode the resolver
    strips it (by design) so the audit trail is unambiguous.
    """

    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    tracker = [
        {
            "KAM": "Vikas",
            "BP Code": "1000000001",
            "Billed to Entity Name": "SUN PHARMACEUTICAL INDUSTRIES LTD",
            "KAM Email": "vikas.singh2@1mg.com",
            "KAM Contact Number": "8168480913",
            "Auto Emailer Exclusion": "No",
            "Email 1": "ar.team@sunpharma.com",
            "Email 2": "treasury@sunpharma.com",
        }
    ]

    plans = _run_variant("epharma", xlsx, tracker)
    assert len(plans) == 1
    plan = plans[0]
    assert plan["business_key"] == "1000000001"
    assert plan["row_count"] == 1
    assert "ar.team@sunpharma.com" in plan["resolved_to"]
    assert "vikas.singh2@1mg.com" in plan["resolved_cc"]
    # Static AR-desk address is in TO (action-owner), not CC. Per the
    # static_to contract, it's appended to every production email so the
    # collections desk has visibility on every reminder.
    assert "tata1mg.invoices@1mg.com" in plan["resolved_to"]
    assert "tata1mg.invoices@1mg.com" not in plan["resolved_cc"]
    # Banner is NOT triggered because the customer has real recipients.
    assert plan.get("client_recipient_missing") is False


def test_epharma_total_outstanding_subtracts_unaccounted_revenue(tmp_path: Path) -> None:
    """SUN PHARMA: net pending INR 2,50,000; unaccounted INR 50,000 (looked up
    from Party wise Ageing-H&T). Subject + body should show:

      net_pending_total = INR 2,50,000.00
      unaccounted       = INR 50,000.00
      total_outstanding = INR 2,00,000.00 (per template PDF)
    """

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    plans = _run_variant("epharma", xlsx, [])
    assert len(plans) == 1
    plan = plans[0]
    body = plan["rendered_body_html"]

    assert "50,000.00" in body, "unaccounted revenue must show 50,000"
    assert "2,00,000.00" in body, "total outstanding (net - unacc) must show 2,00,000"
    # Wording per ePharma SOP (Auto emailer doc.docx.pdf, Step 6).
    assert "HANA Code" in body
    assert "Name of the party" in body
    assert "Brand Name" in body
    assert "Invoice Date" in body
    assert "Final Amount Pending" in body
    assert "Status" in body
    assert "Amount Pending:" in body
    assert "Due this month:" in body
    assert "Overdue Amount Pending:" in body
    assert "Unaccounted Revenue:" in body
    assert "Current Total Overdue:" in body
    assert "compliance concern at our end" in body
    assert "Dear Partner" in body
    assert "online marketing services provided by Tata 1mg" in body
    assert "payment advice/UTR" in body
    assert "Thank you for your continued support" in body
    assert "Invoicing &amp; Collections Team" in body
    subject = plan["rendered_subject"]
    assert subject == (
        "Payment Reminder \u2013 Overdue Invoices | Tata1mg | 1000000001"
    )


def test_epharma_includes_not_due_rows_and_sends_heads_up(tmp_path: Path) -> None:
    """Not Due rows appear in the table with Status; not-due-only parties still send."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    # Replace SUN PHARMA row ageing to Not Due only (due within June 2026).
    ws.cell(row=4, column=10, value=datetime(2026, 6, 1))
    ws.cell(row=4, column=12, value=15)
    ws.cell(row=4, column=10, value=datetime(2026, 6, 1))
    ws.cell(row=4, column=12, value=15)
    ws.cell(row=4, column=29, value="Not Due")

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            250000, 0, 0, 0, 0, 0, 0, 250000,
            0, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun.get("skip_gate") is None
    body = sun["rendered_body_html"]
    assert "To be due this Month" not in body
    assert "Due this Month" in body
    assert "upcoming invoices listed above" in body
    assert "Overdue Amount Pending: \u20b90.00" in body
    assert "Current Total Overdue: \u20b90.00" in body
    assert "Amount Pending: \u20b92,50,000.00" in body
    assert "Due this month: \u20b92,50,000.00" in body


def test_epharma_not_due_only_sends_when_unaccounted_covers_total(
    tmp_path: Path,
) -> None:
    """Not Due–only: send when T ≥ ₹1 even if C < 0 (unaccounted > overdue exposure)."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws.cell(row=4, column=10, value=datetime(2026, 6, 1))
    ws.cell(row=4, column=12, value=15)
    ws.cell(row=4, column=29, value="Not Due")

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            250000, 0, 0, 0, 0, 0, 0, 250000,
            300000, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun.get("skip_gate") is None
    assert "Due this Month" in sun["rendered_body_html"]


def test_epharma_not_due_only_sends_when_small_unaccounted(tmp_path: Path) -> None:
    """Not Due–only: send when T ≥ ₹1 even if any unaccounted makes C negative."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws.cell(row=4, column=10, value=datetime(2026, 6, 1))
    ws.cell(row=4, column=12, value=15)
    ws.cell(row=4, column=29, value="Not Due")

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            250000, 0, 0, 0, 0, 0, 0, 250000,
            50000, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun.get("skip_gate") is None


def test_epharma_mixed_still_sends_when_overdue_covered_by_unaccounted(
    tmp_path: Path,
) -> None:
    """Mixed table: send even when Current Total Overdue ≤ 0 if Not Due rows exist."""

    from datetime import datetime

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    # Second row: same HANA, Not Due upcoming invoice.
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "1000000001", "SUN PHARMACEUTICAL INDUSTRIES LTD",
            "Cetaphil Upcoming", "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 6, 5), "HH2606009999", 15,
            100000, 100000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 100000,
            0, "Mumbai", 30, 0,
            "Not Due", None, "Invoice", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    # Unaccounted exactly covers overdue (250k); Not Due row (100k) should still mail.
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            250000, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun.get("skip_gate") is None
    assert sun["row_count"] == 2
    body = sun["rendered_body_html"]
    assert "Due this Month" in body
    assert "Overdue" in body
    assert "upcoming invoices listed above" in body


def test_epharma_mixed_sends_when_current_overdue_negative(
    tmp_path: Path,
) -> None:
    """Mixed table: send when T ≥ ₹1 even if C < 0 (upcoming rows present)."""

    from datetime import datetime

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "1000000001", "SUN PHARMACEUTICAL INDUSTRIES LTD",
            "Cetaphil Upcoming", "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 6, 5), "HH2606009999", 15,
            100000, 100000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 100000,
            0, "Mumbai", 30, 0,
            "Not Due", None, "Invoice", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            400000, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun.get("skip_gate") is None
    assert "Due this Month" in sun["rendered_body_html"]
    assert "upcoming invoices listed above" in sun["rendered_body_html"]


def test_diagnostics_aggregator_variant_reads_invoice_ht_and_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "email_automation_test_mode", False)
    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    tracker = [
        {
            "Code": "2000000360",
            "Affiliate Name": "DIAG CLINIC E2E",
            "Mail ID 1": "finance@diagclinic.example",
            "Mail ID 2": "ops@diagclinic.example",
        },
    ]
    plans = _run_variant("diagnostics_aggregator", xlsx, tracker)
    by_key = {p["business_key"]: p for p in plans}

    # Only the e-Diagnostic-Aggregator row on Invoice details-H&T survives.
    assert set(by_key) == {"2000000360"}
    diag = by_key["2000000360"]
    assert diag["row_count"] == 1
    assert diag["source_sheets"] == ["Invoice details-H&T"]
    # Mail ID 1 → TO; Mail ID 2 → CC
    assert "finance@diagclinic.example" in diag["resolved_to"]
    assert "ops@diagclinic.example" not in diag["resolved_to"]
    assert "ops@diagclinic.example" in diag["resolved_cc"]
    # Static AR-desk address is in TO
    assert "himanshu.singh@1mg.com" in diag["resolved_to"]
    # Subject carries the party name (Name-HANA) BEFORE the brand, so the HANA
    # code stays the trailing token the Reply Tracker parsers rely on.
    assert diag["rendered_subject"].endswith(
        "| DIAG CLINIC E2E | Tata1mg | 2000000360"
    )
    body = diag["rendered_body_html"]
    assert "DIAG CLINIC E2E" in body
    assert ">Remarks<" in body
    assert "remain overdue in our records" in body


def test_chw_variant_merges_invoice_and_dmpl_sheets(tmp_path: Path) -> None:
    """CHW reads merged ``Invoice details-H&T`` + ``Invoice wise DMPL``.

    Synthetic data has:
      ACME CORP   (1000022222) → 2 invoice rows on H&T (former H&T + T(labs))
      DMPL4892    → 1 DMPL row only
    """

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    chw_tracker = [
        {
            "KAM Name": "Alok",
            "HANA Code": "1000022222",
            "TO 1 (Fill all details basis Col J & K)": "ap.team@acme.com",
            "TO 2": "treasury@acme.com",
            "CC 1": "alok.bharti@1mg.com",
            "CC3": "cw_payments@1mg.com",
        },
        # DMPL party not in tracker — expect routing to static (AR desk) +
        # ``client_not_in_tracker`` flag (no longer a blocking review reason).
    ]

    plans = _run_variant("chw", xlsx, chw_tracker)
    by_key = {p["business_key"]: p for p in plans}

    assert set(by_key) == {"1000022222", "DMPL4892", "DMPL4899"}

    acme = by_key["1000022222"]
    assert acme["row_count"] == 2
    assert acme["source_sheets"] == ["Invoice details-H&T"]
    assert acme["rendered_subject"] == (
        "Payment Reminder \u2013 Overdue Invoices | Tata1mg | 1000022222"
    )
    assert "ap.team@acme.com" in acme["resolved_to"]
    assert "treasury@acme.com" in acme["resolved_to"]
    assert "cw_payments@1mg.com" in acme["resolved_to"]
    assert "alok.bharti@1mg.com" in acme["resolved_cc"]
    # CHW's static AR-desk address is in TO (action-owner), never CC.
    assert "cw_payments@1mg.com" not in acme["resolved_cc"]
    assert "tata1mg.invoices@1mg.com" not in acme["resolved_cc"]

    dmpl = by_key["DMPL4892"]
    assert dmpl["row_count"] == 1
    assert dmpl["source_sheets"] == ["Invoice wise DMPL"]
    # New contract: customer not in tracker → no blocking review reason,
    # just the ``client_not_in_tracker`` flag + routed to static.
    reason_codes = {r.get("code") for r in dmpl["review_reasons"]}
    assert "tracker_not_found" not in reason_codes
    assert dmpl.get("client_not_in_tracker") is True
    # The body picked up the "customer not in tracker" banner.
    assert "not in the master tracker" in dmpl["rendered_body_html"]
    assert "DMPL4892" in dmpl["rendered_body_html"]


def test_chw_uses_invoice_sheet_not_partywise(tmp_path: Path) -> None:
    """CHW invoice table rows come from H&T / DMPL only — not partywise ageing."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    plans = _run_variant("chw", xlsx, [])
    acme = next(p for p in plans if p["business_key"] == "1000022222")
    assert acme["row_count"] == 2
    assert acme["source_sheets"] == ["Invoice details-H&T"]


def test_chw_email_body_uses_pdf_verbatim_columns(tmp_path: Path) -> None:
    """CHW email body uses the six template PDF columns (chw_email.pdf) — no Brand Name."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    plans = _run_variant("chw", xlsx, [])
    acme = next(p for p in plans if p["business_key"] == "1000022222")
    body = acme["rendered_body_html"]

    assert "HANA Code" in body
    assert "Name-HANA" in body
    assert "Invoice Date" in body
    assert "Invoice Number" in body
    assert "Net Amount Pending" in body
    assert "Ageing" in body
    assert "Invoice Amount" not in body
    # Brand Name column is ePharma-only.
    assert "Brand Name" not in body
    # Wording per CHW template PDF (chw_email.pdf).
    assert "Greetings from Tata 1mg" in body
    assert "We'd like to draw your attention" in body
    assert "cw_payments@1mg.com" in body
    assert "compliance concern" in body
    assert "Amount Pending:" in body
    assert "Due this month:" in body
    assert "Overdue Amount Pending:" in body
    assert "Unaccounted Revenue:" in body
    assert "Current Total Overdue:" in body
    assert "Sum of Net Amount Pending:" not in body


def test_potential_overpayment_surfaces_when_unaccounted_exceeds_pending(
    tmp_path: Path,
) -> None:
    """If unaccounted_revenue > sum(net_pending) → total_outstanding ≤ 0 →
    plan must carry the ``potential_overpayment`` review reason so the
    pipeline auto-skips the send (per SOP §"Outstanding is zero or
    negative"). With no HITL, auto-skip is the only safe answer."""

    wb = Workbook()
    # ePharma source — one Sun Pharma invoice for ₹1,000.
    ws = wb.active
    ws.title = "Invoice details-H(all)&T(Psp)"
    _write_invoice_h_psp(ws)
    # Replace the Sun Pharma invoice amount to be tiny.
    # (We just keep _write_invoice_h_psp's row 1; SUN PHARMA Final Amount = 250,000.)

    # Lookup with massive unaccounted revenue > 250,000.
    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            500000, 0, "",  # huge unaccounted → total goes negative
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert (sun.get("skip_gate") or {}).get("code") == "potential_overpayment"
    assert sun["review_reasons"] == []


def test_insufficient_outstanding_surfaces_when_total_below_one_rupee(
    tmp_path: Path,
) -> None:
    """0 < total_outstanding < ₹1 after unaccounted → ``insufficient_outstanding``
    (materiality; same auto-skip path as overpayment)."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H(all)&T(Psp)"
    _write_invoice_h_psp(ws)

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            249999.2, 0, "",  # net 250,000 - 249,999.2 = 0.8 INR outstanding
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert (sun.get("skip_gate") or {}).get("code") == "insufficient_outstanding"
    assert sun["review_reasons"] == []


def test_chw_dmpl_dual_totals_split_not_due_and_overdue(tmp_path: Path) -> None:
    """DMPL SAP rows: Amount Pending includes Not Due; Overdue excludes it."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    from openpyxl import load_workbook

    wb = load_workbook(str(xlsx))
    ws = wb["Invoice wise DMPL"]
    ws.append(
        _dmpl_sap_row(
            customer="DMPL777",
            amount=50000,
            inv_no="B020822600000777",
            inv_date=datetime(2026, 1, 22),
            net_due=datetime(2026, 1, 10),
            party_name="CANARY TCS LTD",
        )
    )
    ws.append(
        _dmpl_sap_row(
            customer="DMPL777",
            amount=100000,
            inv_no="B020822600000778",
            inv_date=datetime(2026, 6, 1),
            net_due=datetime(2026, 6, 25),
            party_name="CANARY TCS LTD",
        )
    )
    wb.save(str(xlsx))

    plans = _run_variant("chw", xlsx, [])
    plan = next(p for p in plans if p["business_key"] == "DMPL777")
    assert Decimal(plan["totals"]["_net_pending"]) == Decimal("150000")
    assert Decimal(plan["totals"]["_overdue_pending"]) == Decimal("50000")
    body = plan["rendered_body_html"]
    assert "Due this Month" in body
    assert "upcoming invoices listed above" in body


def test_chw_dmpl_ageing_from_net_due_date(tmp_path: Path) -> None:
    """SAP DMPL ageing is Overdue vs Due this Month from Net Due Date."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    plans = _run_variant("chw", xlsx, [])
    overdue = next(p for p in plans if p["business_key"] == "DMPL4892")
    not_due = next(p for p in plans if p["business_key"] == "DMPL4899")
    assert "Overdue" in overdue["rendered_body_html"]
    assert "Due this Month" in not_due["rendered_body_html"]


def test_chw_includes_not_due_ht_rows(tmp_path: Path) -> None:
    """H&T Not Due rows appear in CHW table and trigger upcoming clause."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws.append(
        [
            "TATA 1MGH", "LAB0090", "1000022222", "ACME CORP CORPORATE",
            None, "Owner", "K2", None, "K1",
            datetime(2026, 6, 5), "TM-LAB-UPCOMING", 10,
            5000, 5000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 5000,
            0, "Bangalore", 30, 0,
            "Not Due", None, "Invoice", "Positive",
            None, None, None, None, "Corporate Wellness", None,
            "Lab- Affiliate Channel", None, None, None, None, None, None, None,
        ]
    )
    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000022222", "ACME CORP", "K2", "", "Corporate Wellness",
            "Lab- Affiliate Channel", "Owner", 30,
            0, 0, 45000, 0, 0, 0, 0, 45000,
            0, 0, "",
        ]
    )
    ws3 = wb.create_sheet("Invoice wise DMPL")
    _write_invoice_dmpl(ws3)

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("chw", xlsx, [])
    acme = next(p for p in plans if p["business_key"] == "1000022222")
    assert acme.get("skip_gate") is None
    body = acme["rendered_body_html"]
    assert "Due this Month" in body
    assert "upcoming invoices listed above" in body
    assert "Amount Pending:" in body
    assert "Due this month:" in body
    assert "Overdue Amount Pending:" in body


def test_not_due_ht_dropped_when_due_after_month_end(tmp_path: Path) -> None:
    """H&T Not Due rows with due date after month-end are omitted from the email."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws.cell(row=4, column=10, value=datetime(2026, 6, 1))
    ws.cell(row=4, column=12, value=60)
    ws.cell(row=4, column=29, value="Not Due")

    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    for _ in range(4):
        ws2.append([])
    ws2.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws2.append(
        [
            "1000000001", "SUN PHARMA", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            250000, 0, 0, 0, 0, 0, 0, 250000,
            0, 0, "",
        ]
    )

    xlsx = tmp_path / "rec.xlsx"
    wb.save(str(xlsx))

    plans = _run_variant("epharma", xlsx, [])
    assert not any(p["business_key"] == "1000000001" for p in plans)


def test_not_due_dmpl_dropped_when_due_after_month_end(tmp_path: Path) -> None:
    """DMPL future-due rows beyond month-end are omitted; overdue rows stay."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    from openpyxl import load_workbook

    wb = load_workbook(str(xlsx))
    ws = wb["Invoice wise DMPL"]
    ws.append(
        _dmpl_sap_row(
            customer="DMPL888",
            amount=75000,
            inv_no="B020822600000888",
            inv_date=datetime(2026, 6, 1),
            net_due=datetime(2026, 8, 15),
            party_name="FAR FUTURE LTD",
        )
    )
    wb.save(str(xlsx))

    plans = _run_variant("chw", xlsx, [])
    assert not any(p["business_key"] == "DMPL888" for p in plans)
    assert any(p["business_key"] == "DMPL4892" for p in plans)


def test_overdue_ht_unaffected_by_not_due_month_sieve(tmp_path: Path) -> None:
    """Overdue H&T rows are never dropped by the Not Due month-end sieve."""

    xlsx = tmp_path / "rec.xlsx"
    _make_workbook(xlsx)

    plans = _run_variant("epharma", xlsx, [])
    sun = next(p for p in plans if p["business_key"] == "1000000001")
    assert sun["row_count"] >= 1
    assert "Overdue" in sun["rendered_body_html"]
