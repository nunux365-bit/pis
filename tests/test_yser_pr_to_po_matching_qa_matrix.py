"""QA matrix: YSER PR→PO item matching after multi-service subline fan-out.

Scenario IDs mirror SAP sign-off patterns (multi-CC, multi-service, partial PO).
Offline only — exercises ``fetch_pr_items`` shape + enrich + allocation validation.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.procurement.po_sap_enrichment import (
    _enrich_po_form_with_items,
    _resolve_yser_po_line_pr_item,
    match_po_line_to_pr_item,
    po_allocations_match_sap_pr,
    sap_pr_items_to_po_form,
)
from app.procurement.sap_pr_items import SapPrAcctRow, SapPrItemLine, _parse_z_pr_item_row


def _base_item(**overrides: Any) -> SapPrItemLine:
    data: dict[str, Any] = dict(
        item_no="10",
        sap_pr_item="10",
        material="",
        plant="H001",
        sloc="3021",
        material_group="S001-0001",
        qty="1.000",
        unit="EA",
        price="10.00",
        pur_group="A0B",
        pur_org="1MGH",
        item_cat="9",
        acct_cat="K",
        short_text="svc",
        service_performer="10000000006",
        acct_rows=[
            SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1.000", master_asset="")
        ],
    )
    data.update(overrides)
    return SapPrItemLine(**data)


def _line(
    *,
    service: str,
    pri: str = "10",
    cc: str = "HCO91001H0",
    qty: str = "1",
) -> dict[str, Any]:
    return {
        "service": service,
        "purchase_requisition_item": pri,
        "allocations": [{"cost_center": cc, "qty": qty}],
    }


def _po4030011266_shape() -> tuple[list[SapPrItemLine], list[dict[str, Any]]]:
    """Reported QA: one PR item, two subline services, different cost centres."""
    sap_items = [
        _base_item(
            item_no="10",
            sap_pr_item="10",
            service_performer="10000000006",
            acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1.000", master_asset="")
            ],
        ),
        _base_item(
            item_no="20",
            sap_pr_item="10",
            service_performer="10000000007",
            acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001A0", quantity="1.000", master_asset="")
            ],
        ),
    ]
    form_lines = [
        _line(service="10000000006", pri="10", cc="HCO91001H0"),
        _line(service="10000000007", pri="10", cc="HCO91001A0"),
    ]
    return sap_items, form_lines


class TestPo4030011266Regression:
    """PO-4030011266 — linked PR→PO blocked on multi-service / multi-CC same PR item."""

    def test_qa_p01_validation_allows_conversion(self) -> None:
        sap_items, lines = _po4030011266_shape()
        form = {"lines": lines}
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_p02_resolve_distinct_service_rows(self) -> None:
        sap_items, lines = _po4030011266_shape()
        a = _resolve_yser_po_line_pr_item(lines[0], sap_items=sap_items)
        b = _resolve_yser_po_line_pr_item(lines[1], sap_items=sap_items)
        assert a and b and a.service_performer != b.service_performer
        assert a.sap_pr_item == b.sap_pr_item == "10"

    def test_qa_p03_enrich_keeps_per_service_cc(self) -> None:
        sap_items, lines = _po4030011266_shape()
        form = {"header": {"vendor": "1000000002"}, "lines": copy.deepcopy(lines)}
        assert _enrich_po_form_with_items(form, document_type="YSER", sap_items=sap_items) is None
        assert form["lines"][0]["allocations"][0]["cost_center"] == "HCO91001H0"
        assert form["lines"][1]["allocations"][0]["cost_center"] == "HCO91001A0"
        assert form["lines"][0]["purchase_requisition_item"] == "10"
        assert form["lines"][1]["purchase_requisition_item"] == "10"

    def test_qa_p04_prefill_stamps_real_pr_item_not_synthetic_line(self) -> None:
        sap_items, _ = _po4030011266_shape()
        form = sap_pr_items_to_po_form(sap_items, document_type="YSER")
        assert len(form["lines"]) == 2
        assert form["lines"][0]["purchase_requisition_item"] == "10"
        assert form["lines"][1]["purchase_requisition_item"] == "10"


class TestYserPrToPoMatchingEdgeCases:
    """SAP-shaped edge cases for linked PO allocation validation."""

    def test_qa_m01_two_real_pr_items_different_services(self) -> None:
        sap_items = [
            _base_item(item_no="10", sap_pr_item="10", service_performer="10000000006", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1", master_asset="")
            ]),
            _base_item(item_no="20", sap_pr_item="20", service_performer="10000000007", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001A0", quantity="1", master_asset="")
            ]),
        ]
        form = {
            "lines": [
                _line(service="10000000006", pri="10", cc="HCO91001H0"),
                _line(service="10000000007", pri="20", cc="HCO91001A0"),
            ]
        }
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_m02_leading_zero_pr_item_ref(self) -> None:
        sap_items, lines = _po4030011266_shape()
        lines = copy.deepcopy(lines)
        lines[0]["purchase_requisition_item"] = "00010"
        lines[1]["purchase_requisition_item"] = "00010"
        form = {"lines": lines}
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_m03_single_merged_pr_item_multi_cc_legacy_shape(self) -> None:
        sap_items = [
            _base_item(
                item_no="10",
                sap_pr_item="10",
                service_performer="10000000006",
                acct_rows=[
                    SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1", master_asset=""),
                    SapPrAcctRow(seq="2", cost_center="HCO91001A0", quantity="1", master_asset=""),
                ],
            )
        ]
        form = {
            "lines": [
                _line(service="10000000006", pri="10", cc="HCO91001H0"),
                _line(service="10000000007", pri="10", cc="HCO91001A0"),
            ]
        }
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_m04_partial_po_one_of_two_services(self) -> None:
        sap_items, lines = _po4030011266_shape()
        form = {"lines": [lines[0]]}
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_m05_three_services_one_pr_item(self) -> None:
        sap_items = [
            _base_item(item_no="10", sap_pr_item="10", service_performer="SVC-A", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="CC-A", quantity="1", master_asset="")
            ]),
            _base_item(item_no="20", sap_pr_item="10", service_performer="SVC-B", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="CC-B", quantity="1", master_asset="")
            ]),
            _base_item(item_no="30", sap_pr_item="10", service_performer="SVC-C", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="CC-C", quantity="1", master_asset="")
            ]),
        ]
        form = {
            "lines": [
                _line(service="SVC-A", pri="10", cc="CC-A"),
                _line(service="SVC-B", pri="10", cc="CC-B"),
                _line(service="SVC-C", pri="10", cc="CC-C"),
            ]
        }
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None

    def test_qa_m06_rejects_wrong_cc_on_correct_service(self) -> None:
        sap_items, lines = _po4030011266_shape()
        bad = copy.deepcopy(lines)
        bad[0]["allocations"] = [{"cost_center": "HCO91001A0", "qty": "1"}]
        form = {"lines": bad}
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items)

    def test_qa_m07_rejects_unknown_cc(self) -> None:
        sap_items, lines = _po4030011266_shape()
        bad = copy.deepcopy(lines)
        bad[1]["allocations"] = [{"cost_center": "WRONG", "qty": "1"}]
        form = {"lines": bad}
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items)

    def test_qa_m08_rejects_cc_from_sibling_service(self) -> None:
        sap_items = [
            _base_item(item_no="10", sap_pr_item="10", service_performer="10000000006", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1", master_asset="")
            ]),
            _base_item(item_no="20", sap_pr_item="20", service_performer="10000000007", acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001A0", quantity="1", master_asset="")
            ]),
        ]
        form = {
            "lines": [
                _line(service="10000000007", pri="20", cc="HCO91001H0"),
            ]
        }
        assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items)

    def test_qa_m09_match_by_service_when_pri_missing(self) -> None:
        sap_items, lines = _po4030011266_shape()
        row = {"service": "10000000007", "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}]}
        hit = match_po_line_to_pr_item(
            row=row, document_type="YSER", sap_items=sap_items, used_item_nos=set()
        )
        assert hit is not None
        assert hit.service_performer == "10000000007"
        assert hit.sap_pr_item == "10"

    def test_qa_m10_match_disambiguates_same_pri_by_cc_when_service_missing(self) -> None:
        sap_items, _ = _po4030011266_shape()
        row = {
            "purchase_requisition_item": "10",
            "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
        }
        hit = _resolve_yser_po_line_pr_item(row, sap_items=sap_items)
        assert hit is not None
        assert hit.service_performer == "10000000007"


class TestFetchPrItemsParser:
    """``fetch_pr_items`` Z parser must retain real SAP PR item on fan-out."""

    def test_qa_f01_parse_z_pr_item_row_sets_sap_pr_item(self) -> None:
        raw = {
            "PRItem": "10",
            "Plant": "H001",
            "SLoc": "3021",
            "MaterialGroup": "S001-0001",
            "Quantity": "2.000",
            "ValuationPrice": "10.00",
            "PurGroup": "A0B",
            "PurOrg": "1MGH",
            "ItemCat": "9",
            "ActAssignmentCat": "K",
            "ShortText": "multi svc",
            "to_Services": [
                {
                    "Service": "10000000006",
                    "CostCenter": "HCO91001H0",
                    "DistrQuantity": "1.000",
                    "ShortText": "svc a",
                    "PurDocItemExternalReference": "REF-A",
                },
                {
                    "Service": "10000000007",
                    "CostCenter": "HCO91001A0",
                    "DistrQuantity": "1.000",
                    "ShortText": "svc b",
                    "PurDocItemExternalReference": "REF-B",
                },
            ],
        }
        parsed = _parse_z_pr_item_row(raw, ui_line_start=10)
        assert len(parsed) == 2
        assert parsed[0].sap_pr_item == "10"
        assert parsed[1].sap_pr_item == "10"
        assert parsed[0].item_no == "10"
        assert parsed[1].item_no == "20"
        from app.procurement.sap_pr_z_payload import normalize_service_performer_code

        assert parsed[0].service_performer == normalize_service_performer_code("10000000006")
        assert parsed[1].service_performer == normalize_service_performer_code("10000000007")
        assert parsed[0].acct_rows[0].cost_center == "HCO91001H0"
        assert parsed[1].acct_rows[0].cost_center == "HCO91001A0"

    def test_qa_f02_parse_single_service_multi_cc_keeps_one_line(self) -> None:
        raw = {
            "PRItem": "10",
            "Plant": "H001",
            "MaterialGroup": "S001-0001",
            "Quantity": "2.000",
            "to_Services": [
                {
                    "Service": "10000000006",
                    "CostCenter": "HCO91001H0",
                    "DistrQuantity": "1.000",
                    "PurDocItemExternalReference": "REF-SPLIT",
                },
                {
                    "Service": "10000000006",
                    "CostCenter": "HCO91001A0",
                    "DistrQuantity": "1.000",
                    "PurDocItemExternalReference": "REF-SPLIT",
                },
            ],
        }
        parsed = _parse_z_pr_item_row(raw)
        assert len(parsed) == 1
        assert parsed[0].sap_pr_item == "10"
        assert len(parsed[0].acct_rows) == 2
        ccs = {r.cost_center for r in parsed[0].acct_rows}
        assert ccs == {"HCO91001H0", "HCO91001A0"}
