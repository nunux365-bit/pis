"""SAP PR payload mapping and live client (httpx mocked) — API_PURCHASEREQ_PROCESS_SRV."""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.procurement import sap_pr_client, sap_pr_payload, sap_sync
from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults


def _sample_yser_pr_form() -> dict:
    blk = default_empty_block("YSER")
    blk["service"] = "SVC001"
    blk["short_text"] = "Consulting"
    blk["delivery_date"] = "2026-05-15"
    blk["unit_price"] = "50"
    blk["valuation_price"] = "50"
    blk["allocations"] = [{"cost_center": "HBM11001A0", "qty": "2"}]
    form = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "S0Z",
                "plant": "H002",
                "storage_location": "1001",
                "service_group": "S089-0001",
                "header_note": "Test note",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    return form


def test_order_unit_from_reference_extra() -> None:
    assert sap_pr_payload.order_unit_from_reference_extra({"base_unit": "EA"}) == "EA"
    assert sap_pr_payload.order_unit_from_reference_extra({"Order Unit": "KG"}) == "KG"
    assert sap_pr_payload.order_unit_from_reference_extra(
        {"Base Unit of Measure": "EA", "Service Short Text": "x"}
    ) == "EA"
    assert sap_pr_payload.order_unit_from_reference_extra({}) == ""


def test_build_z_yser_payload_matches_service_pr_doc() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["order_unit"] = "EA"
    payload = build_z_yser_pr_payload(form=form, document_type="YSER", ticket_id="t-1")
    assert payload["DocumentType"] == "YSER"
    assert payload["PRNumber"] == ""
    items = payload["to_Items"]
    assert len(items) == 1
    row = items[0]
    assert row["PRItem"] == "00010"
    assert row["PurOrg"] == "1MGH"
    assert row["ItemCat"] == "D"
    assert row["ActAssignmentCat"] == "K"
    assert row["Material"] == ""
    assert row["MaterialGroup"] == "S089-0001"
    assert row["Quantity"] == "1.000"
    assert payload["PurReqnDescription"] == form["header"]["header_note"]
    assert row["HeaderNote"] == form["header"]["header_note"]
    assert row["Extsourcesystem"] == sap_pr_payload.sap_ticket_ext_system_marker("t-1")
    assert row["ShortText"] == "Consulting"
    assert "AO" not in row["ShortText"]
    services = row["to_Services"]
    assert len(services) == 1
    assert services[0]["Service"] == "SVC001"
    assert "ServiceNumber" not in services[0]
    assert services[0]["UOM"] == "EA"
    assert services[0]["CostCenter"] == form["lines"][0]["allocations"][0]["cost_center"]
    assert services[0]["DistrQuantity"] == "2.000"
    assert services[0]["Distrib"] == "1"
    assert services[0]["GrossPrice"] == "50.0"
    assert "ValuationPrice" not in services[0]
    assert row["ValuationPrice"] == ""
    assert row["DeliveryDate"] == "20260515"


def test_update_payload_sends_valuation_gross_and_distr() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["unit_price"] = "80"
    form["lines"][0]["valuation_price"] = "400"
    form["lines"][0]["allocations"] = [
        {"cost_center": "HBM11001A0", "qty": "3"},
        {"cost_center": "HCO91001A0", "qty": "2"},
    ]
    inner = build_z_yser_pr_payload(
        form=form,
        document_type="YSER",
        pr_number="1010000316",
        for_update=True,
    )
    item = inner["to_Items"][0]
    assert item["ValuationPrice"] == "400"
    assert item["Quantity"] == "1.000"
    services = item["to_Services"]
    assert services[0]["GrossPrice"] == "400"
    assert services[0]["DistrQuantity"] == "3.000"
    assert services[1]["DistrQuantity"] == "2.000"
    assert services[1]["GrossPrice"] == "400"


def test_create_payload_keeps_item_valuation_empty() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    inner = build_z_yser_pr_payload(
        form=_sample_yser_pr_form(), document_type="YSER", ticket_id="t"
    )
    assert inner["to_Items"][0]["ValuationPrice"] == ""


def test_form_from_z_pr_read_prefers_item_valuation_when_positive() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "380.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "GrossPrice": "150.0000",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "4.000",
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    line = form["lines"][0]
    assert line["valuation_price"] == "380"
    assert line["unit_price"] == "95"


def test_form_from_z_pr_read_uses_service_gross_and_distr() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "0.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "ShortText": "svc text",
                                    "GrossPrice": "150.0000",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "3.000",
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    line = form["lines"][0]
    assert line["short_text"] == "svc text"
    assert line["unit_price"] == "50"
    assert line["valuation_price"] == "150"
    assert line["allocations"][0]["qty"] == "3"


def test_update_payload_keeps_all_form_services() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    blk2 = default_empty_block("YSER")
    blk2["service"] = "000000010000000007"
    blk2["short_text"] = "Second service"
    blk2["allocations"] = [{"cost_center": "HBM11001A0", "qty": "1"}]
    form["lines"].append(blk2)
    inner = build_z_yser_pr_payload(
        form=form,
        document_type="YSER",
        pr_number="1010000316",
        for_update=True,
    )
    assert len(inner["to_Items"]) == 2
    codes = {item["to_Services"][0]["Service"] for item in inner["to_Items"]}
    assert str(form["lines"][0].get("service") or "").strip() in codes
    assert "000000010000000007" in codes


def test_finalize_z_yser_update_post_body_flat() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload, finalize_z_yser_update_post_body

    form = _sample_yser_pr_form()
    inner = build_z_yser_pr_payload(
        form=form,
        document_type="YSER",
        pr_number="1010000316",
        ticket_id="t-upd",
        for_update=True,
    )
    assert inner["PurReqnDescription"] == form["header"]["header_note"]
    post = finalize_z_yser_update_post_body(inner)
    assert "d" not in post
    assert post["PRNumber"] == "1010000316"
    assert post["PurReqnDescription"] == form["header"]["header_note"]
    item = post["to_Items"][0]
    assert item["PRNumber"] == "1010000316"
    assert item["HeaderNote"]
    svc0 = post["to_Items"][0]["to_Services"][0]
    assert svc0["GrossPrice"] == "50"
    assert svc0["DistrQuantity"] == "2.000"
    assert svc0["PRAcctAssgmtNumber"] == "01"
    assert svc0["PRNumber"] == "1010000316"


def test_build_z_yser_item_delete_post_body() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_item_delete_post_body

    post = build_z_yser_item_delete_post_body(
        pr_number="1010000572",
        item_numbers=["10", "00010"],
    )
    assert post["PRNumber"] == "1010000572"
    assert post["DocumentType"] == "YSER"
    assert len(post["to_Items"]) == 2
    for item in post["to_Items"]:
        assert item["PRItem"] == "00010"
        assert item["PRNumber"] == "1010000572"
        assert item["IsDeleted"] == "X"
        assert "to_Services" not in item


def test_build_z_yser_trace_on_first_ext_source_system() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    form = _sample_yser_pr_form()
    payload = build_z_yser_pr_payload(form=form, document_type="YSER", ticket_id=tid)
    assert payload["PurReqnDescription"] == form["header"]["header_note"]
    row = payload["to_Items"][0]
    assert row["Extsourcesystem"] == sap_pr_payload.sap_ticket_ext_system_marker(tid)
    assert sap_pr_payload.pr_item_trace_marker(tid) not in row["ShortText"]
    assert "[AO:" not in payload["PurReqnDescription"]
    assert len(payload["PurReqnDescription"]) <= sap_pr_payload.SAP_TEXT_FIELD_MAX_LEN


def test_build_z_yser_payload_multi_cc_inline_rows() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "3"},
    ]
    services = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"][0]["to_Services"]
    assert len(services) == 2
    assert services[0]["Service"] == "SVC001"
    assert services[0]["CostCenter"] == "CC1"
    assert services[1]["CostCenter"] == "CC2"
    assert services[0]["GrossPrice"] == "50.0"
    assert services[1]["GrossPrice"] == "50.0"
    assert services[0]["Quantity"] == "4.000"
    assert services[0]["DistrQuantity"] == "1.000"
    assert services[1]["DistrQuantity"] == "3.000"
    assert float(services[0]["DistrPercentage"]) == pytest.approx(25.0)
    assert float(services[1]["DistrPercentage"]) == pytest.approx(75.0)


def test_gross_price_on_every_cc_row_per_service() -> None:
    """``GrossPrice`` is per UI service line — repeated on every CC split row (key is ``GrossPrice``)."""
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "2"},
    ]
    form["lines"].append(
        {
            **form["lines"][0],
            "service": "SVC002",
            "unit_price": "75",
            "valuation_price": "75",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )
    items_create = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"]
    items_update = build_z_yser_pr_payload(
        form=form, document_type="YSER", pr_number="1010000999", for_update=True
    )["to_Items"]
    assert len(items_create) == 1
    svc1_create = items_create[0]["to_Services"]
    svc2_create = [s for s in svc1_create if s["Service"] == "SVC002"]
    assert len(svc2_create) == 1
    assert len(items_update) == 2
    svc1_update = items_update[0]["to_Services"]
    svc2_update = items_update[1]["to_Services"]
    assert svc1_create[0]["GrossPrice"] == "50.0"
    assert svc1_create[1]["GrossPrice"] == "50.0"
    assert svc2_create[0]["GrossPrice"] == "75.0"
    assert svc1_update[0]["GrossPrice"] == "50"
    assert svc1_update[1]["GrossPrice"] == "50"
    assert svc2_update[0]["GrossPrice"] == "75"


def test_build_z_yser_update_payload_absolute_distr_multi_cc() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "1"},
    ]
    services = build_z_yser_pr_payload(
        form=form, document_type="YSER", pr_number="1010000999", for_update=True
    )["to_Items"][0]["to_Services"]
    assert services[0]["DistrQuantity"] == "1.000"
    assert services[1]["DistrQuantity"] == "1.000"
    assert services[0]["Quantity"] == "2.000"
    assert services[1]["Quantity"] == "2.000"
    assert services[0]["PRAcctAssgmtNumber"] == "01"
    assert services[1]["PRAcctAssgmtNumber"] == "02"


def test_build_z_yser_payload_complex_grouped_pr_acct_per_service() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    base = _sample_yser_pr_form()["lines"][0]
    form = _sample_yser_pr_form()
    form["lines"] = [
        {
            **base,
            "service": "SVC001",
            "service_group": "S089-0001",
            "allocations": [
                {"cost_center": "CC1", "qty": "1"},
                {"cost_center": "CC2", "qty": "1"},
            ],
        },
        {
            **base,
            "service": "SVC002",
            "service_group": "S089-0001",
            "allocations": [
                {"cost_center": "CC3", "qty": "1"},
                {"cost_center": "CC4", "qty": "1"},
            ],
        },
        {
            **base,
            "service": "SVC001",
            "service_group": "SD05-0001",
            "allocations": [
                {"cost_center": "CC1", "qty": "1"},
                {"cost_center": "CC2", "qty": "1"},
            ],
        },
        {
            **base,
            "service": "SVC002",
            "service_group": "SD05-0001",
            "allocations": [
                {"cost_center": "CC3", "qty": "1"},
                {"cost_center": "CC4", "qty": "1"},
            ],
        },
    ]
    items = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"]
    assert len(items) == 2
    assert [s["PRAcctAssgmtNumber"] for s in items[0]["to_Services"]] == [
        "01",
        "02",
        "01",
        "02",
    ]
    assert [s["PRAcctAssgmtNumber"] for s in items[1]["to_Services"]] == [
        "01",
        "02",
        "01",
        "02",
    ]


def test_merge_yser_update_prefers_sap_allocation_qty() -> None:
    from app.procurement.sap_pr_z_payload import merge_yser_update_form_with_sap

    submitted = {
        "lines": [
            {
                "service": "10000000006",
                "unit_price": "160",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1"},
                    {"cost_center": "CC2", "qty": "2"},
                ],
            }
        ]
    }
    sap_form = {
        "lines": [
            {
                "service": "10000000006",
                "unit_price": "150",
                "allocations": [
                    {"cost_center": "CC1", "qty": "0.999", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "2.001", "pr_acct_assgmt_number": "02"},
                ],
            }
        ]
    }
    merged = merge_yser_update_form_with_sap(submitted, sap_form=sap_form)
    allocs = merged["lines"][0]["allocations"]
    assert allocs[0]["qty"] == "0.999"
    assert allocs[1]["qty"] == "2.001"
    assert allocs[0]["pr_acct_assgmt_number"] == "01"
    assert allocs[1]["pr_acct_assgmt_number"] == "02"
    assert merged["lines"][0]["unit_price"] == "160"


def test_merge_yser_update_preserves_sap_item_and_acct_serials() -> None:
    from app.procurement.sap_pr_z_payload import merge_yser_update_form_with_sap

    submitted = {
        "lines": [
            {
                "service": "10000000006",
                "service_group": "S089-0001",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1"},
                    {"cost_center": "CC2", "qty": "1"},
                ],
            }
        ]
    }
    sap_form = {
        "lines": [
            {
                "service": "10000000006",
                "service_group": "S089-0001",
                "purchase_requisition_item": "10",
                "sap_pr_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            }
        ]
    }
    merged = merge_yser_update_form_with_sap(submitted, sap_form=sap_form)
    line = merged["lines"][0]
    assert line["purchase_requisition_item"] == "10"
    assert line["sap_pr_item"] == "10"
    assert [a["pr_acct_assgmt_number"] for a in line["allocations"]] == ["01", "02"]


def test_build_z_yser_create_vs_update_pr_acct_numbering() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "1"},
    ]
    create_accts = [
        s["PRAcctAssgmtNumber"]
        for s in build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"][0][
            "to_Services"
        ]
    ]
    assert create_accts == ["01", "02"]

    hydrated = {
        **form,
        "lines": [
            {
                **form["lines"][0],
                "sap_pr_item": "10",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            }
        ],
    }
    update_accts = [
        s["PRAcctAssgmtNumber"]
        for s in build_z_yser_pr_payload(
            form=hydrated,
            document_type="YSER",
            pr_number="1010000999",
            for_update=True,
            sap_item_count=1,
        )["to_Items"][0]["to_Services"]
    ]
    assert update_accts == ["01", "02"]


def test_form_from_z_pr_read_nested_acct_assgmt_doc_shape() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "MaterialGroup": "S001-0001",
                        "to_Services": {
                            "results": [
                                {
                                    "ServiceNumber": "000000010000000006",
                                    "ShortText": "laptop Dell",
                                    "Quantity": "1.000",
                                    "UOM": "EA",
                                    "to_AcctAssgmt": {
                                        "results": [
                                            {
                                                "PRAcctAssgmtNumber": "01",
                                                "CostCenter": "HCO91001H0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert form["lines"][0]["service"] == "000000010000000006"
    assert form["lines"][0]["purchase_requisition_item"] == "10"
    assert form["lines"][0]["allocations"][0]["pr_acct_assgmt_number"] == "01"


def test_yser_pr_item_number_for_block_prefers_sap_pr_item() -> None:
    from app.procurement.sap_pr_z_payload import yser_pr_item_number_for_block

    assert (
        yser_pr_item_number_for_block(
            {"sap_pr_item": "20", "purchase_requisition_item": "10"},
            line_index=0,
        )
        == "20"
    )


def test_merge_yser_update_overlays_service_group() -> None:
    from app.procurement.line_catalog_group import line_catalog_group
    from app.procurement.sap_pr_z_payload import merge_yser_update_form_with_sap

    submitted = {
        "lines": [
            {
                "service": "10000000006",
                "service_group": "SNEW-0001",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ]
    }
    sap_form = {
        "lines": [
            {
                "service": "10000000006",
                "service_group": "SOLD-0001",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ]
    }
    merged = merge_yser_update_form_with_sap(submitted, sap_form=sap_form)
    assert merged["lines"][0]["service_group"] == "SNEW-0001"
    assert (
        line_catalog_group(merged["lines"][0], {}, "YSER") == "SNEW-0001"
    )


def test_build_z_yser_payload_two_service_absolute_distr() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"] = [
        {
            **form["lines"][0],
            "allocations": [
                {"cost_center": "CC1", "qty": "1"},
                {"cost_center": "CC2", "qty": "2"},
            ],
        },
        {
            **form["lines"][0],
            "service": "SVC002",
            "short_text": "Consulting",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        },
    ]
    items = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"]
    assert len(items) == 1
    assert items[0]["Quantity"] == "1.000"
    distrs = [float(s["DistrQuantity"]) for s in items[0]["to_Services"]]
    assert sum(distrs) == pytest.approx(4.0)
    assert distrs == pytest.approx([1.0, 2.0, 1.0])


def test_build_z_yser_create_payload_matches_update_distr_qty() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "1"},
    ]
    create_services = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"][0][
        "to_Services"
    ]
    update_services = build_z_yser_pr_payload(
        form=form, document_type="YSER", pr_number="1010000999", for_update=True
    )["to_Items"][0]["to_Services"]
    assert [s["DistrQuantity"] for s in create_services] == [
        s["DistrQuantity"] for s in update_services
    ]


def test_form_from_z_pr_read_restores_ui_qty_from_distr_shares() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "0.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "Quantity": "3.000",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "0.166",
                                },
                                {
                                    "Service": "000000010000000006",
                                    "Quantity": "3.000",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "0.334",
                                },
                                {
                                    "Service": "000000010000000007",
                                    "Quantity": "1.000",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "0.500",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["allocations"][0]["cost_center"] == "CC1"
    assert float(form["lines"][0]["allocations"][0]["qty"]) == pytest.approx(1, abs=0.01)
    assert float(form["lines"][0]["allocations"][1]["qty"]) == pytest.approx(2, abs=0.01)
    assert form["lines"][1]["allocations"] == [{"cost_center": "CC1", "qty": "1"}]


def test_build_z_yser_payload_groups_services_by_service_group() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    line2 = {**form["lines"][0], "service": "SVC002", "short_text": "Consulting"}
    form["lines"] = [form["lines"][0], line2]
    items = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"]
    assert len(items) == 1
    assert items[0]["PRItem"] == "00010"
    assert len(items[0]["to_Services"]) == 2
    svc_codes = {s["Service"] for s in items[0]["to_Services"]}
    assert svc_codes == {"SVC001", "SVC002"}
    assert items[0]["to_Services"][0]["PRAcctAssgmtNumber"] == "01"
    assert items[0]["to_Services"][1]["PRAcctAssgmtNumber"] == "01"


def test_build_z_yser_payload_legacy_one_item_per_line_on_update() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    line2 = {**form["lines"][0], "service": "SVC002", "short_text": "Consulting"}
    form["lines"] = [form["lines"][0], line2]
    items = build_z_yser_pr_payload(
        form=form,
        document_type="YSER",
        pr_number="1010000400",
        for_update=True,
        sap_item_count=2,
    )["to_Items"]
    assert len(items) == 2
    assert items[0]["PRItem"] == "00010"
    assert items[1]["PRItem"] == "00020"
    assert len(items[0]["to_Services"]) == 1
    assert len(items[1]["to_Services"]) == 1


def test_build_z_yser_payload_allows_duplicate_service_codes() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    line2 = {
        **form["lines"][0],
        "service": "SVC001",
        "short_text": "Duplicate service line",
        "allocations": [{"cost_center": "HBM11002A0", "qty": "1"}],
    }
    form["lines"] = [form["lines"][0], line2]
    services = build_z_yser_pr_payload(form=form, document_type="YSER")["to_Items"][0][
        "to_Services"
    ]
    assert len(services) == 2
    assert services[0]["Service"] == "SVC001"
    assert services[1]["Service"] == "SVC001"
    assert services[0]["ShortText"] != services[1]["ShortText"]


def test_form_from_z_pr_read_grouped_one_sap_item_two_ui_lines() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "MaterialGroup": "S089-0001",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "ShortText": "First",
                                    "GrossPrice": "50.0",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "SVC002",
                                    "ShortText": "Second",
                                    "GrossPrice": "75.0",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "1.000",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["purchase_requisition_item"] == "10"
    assert form["lines"][1]["purchase_requisition_item"] == "10"
    assert form["lines"][0]["sap_pr_item"] == "10"
    assert form["lines"][1]["sap_pr_item"] == "10"
    assert form["lines"][0]["service"] == "SVC001"
    assert form["lines"][1]["service"] == "SVC002"


def test_form_from_z_pr_read_duplicate_service_codes_distinct_ext_refs() -> None:
    """PR 1010000935: same service twice on one grouped item must stay separate lines."""
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "1350.0000",
                        "MaterialGroup": "S083-0001",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000001000000065",
                                    "ShortText": "Genset/DG Rent",
                                    "Quantity": "1.000",
                                    "GrossPrice": "100.0000",
                                    "CostCenter": "TBM13021A0",
                                    "DistrQuantity": "1.000",
                                    "PRAcctAssgmtNumber": "01",
                                    "PurDocItemExternalReference": "REF-001",
                                },
                                {
                                    "Service": "000000001000000454",
                                    "ShortText": "DATA LOGGER",
                                    "Quantity": "2.000",
                                    "GrossPrice": "200.0000",
                                    "CostCenter": "TBM13021A0",
                                    "DistrQuantity": "2.000",
                                    "PurDocItemExternalReference": "REF-002",
                                },
                                {
                                    "Service": "000000001000000065",
                                    "ShortText": "Genset/DG Rent",
                                    "Quantity": "1.000",
                                    "GrossPrice": "250.0000",
                                    "CostCenter": "TBM13021B0",
                                    "DistrQuantity": "1.000",
                                    "PRAcctAssgmtNumber": "02",
                                    "PurDocItemExternalReference": "REF-003",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 3
    genset_lines = [ln for ln in form["lines"] if ln["service"] == "000000001000000065"]
    assert len(genset_lines) == 2
    assert genset_lines[0]["allocations"] == [
        {"cost_center": "TBM13021A0", "qty": "1", "pr_acct_assgmt_number": "01"}
    ]
    assert genset_lines[0]["unit_price"] == "100"
    assert genset_lines[1]["allocations"] == [
        {"cost_center": "TBM13021B0", "qty": "1", "pr_acct_assgmt_number": "02"}
    ]
    assert genset_lines[1]["unit_price"] == "250"
    logger = next(ln for ln in form["lines"] if ln["service"] == "000000001000000454")
    assert logger["unit_price"] == "200"
    assert logger["allocations"][0]["qty"] == "2"


def test_form_from_z_pr_read_legacy_two_sap_items() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "ShortText": "First",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                }
                            ]
                        },
                    },
                    {
                        "PRItem": "00020",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC002",
                                    "ShortText": "Second",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "1.000",
                                }
                            ]
                        },
                    },
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["sap_pr_item"] == "10"
    assert form["lines"][1]["sap_pr_item"] == "20"


def test_form_from_z_pr_read_uses_header_purreqndescription() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "PurReqnDescription": "Note with [AO:ABCDEF0123456789]",
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "IsDeleted": "",
                        "ShortText": "line",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "1.000",
                                    "GrossPrice": "100.0",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert form["header"]["header_note"] == "Note with"


def test_form_from_z_pr_read_skips_deleted_items() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "IsDeleted": "X",
                        "ShortText": "gone",
                        "to_Services": {"results": []},
                    },
                    {
                        "PRItem": "00020",
                        "IsDeleted": "",
                        "ShortText": "active",
                        "ValuationPrice": "0.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "ShortText": "active svc",
                                    "GrossPrice": "      250.0000",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "1.000",
                                }
                            ]
                        },
                    },
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 1
    assert form["lines"][0]["short_text"] == "active svc"
    assert form["lines"][0]["unit_price"] == "250"
    assert form["lines"][0]["valuation_price"] == "250"
    assert form["lines"][0]["gross_price"] == "250"


def test_count_z_pr_sap_items_ignores_deleted_item_stubs() -> None:
    from app.procurement.sap_pr_z_payload import count_z_pr_sap_items

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {"PRItem": "00010", "IsDeleted": "X", "to_Services": {"results": []}},
                    {"PRItem": "00020", "IsDeleted": "", "to_Services": {"results": []}},
                ]
            }
        }
    }
    assert count_z_pr_sap_items(body) == 1


def test_form_from_z_pr_read_splits_line_total_to_unit_for_multi_cc() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "IsDeleted": "",
                        "ValuationPrice": "      450.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "ShortText": "svc",
                                    "GrossPrice": "      450.0000",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "000000010000000006",
                                    "ShortText": "svc",
                                    "CostCenter": "HCO91001A0",
                                    "DistrQuantity": "2.000",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 1
    line = form["lines"][0]
    assert line["unit_price"] == "150"
    assert line["valuation_price"] == "450"
    assert line["gross_price"] == "450"
    assert len(line["allocations"]) == 2


def test_z_alloc_qty_to_form_scales_millis() -> None:
    from app.procurement.sap_pr_z_payload import _z_alloc_qty_to_form

    assert _z_alloc_qty_to_form("1000.000", service_qty="4000.000") == "1"
    assert _z_alloc_qty_to_form("0.500", service_qty="1.000") == "0.5"


def test_normalize_service_performer_code_qas_short() -> None:
    from app.procurement.sap_pr_z_payload import normalize_service_performer_code

    assert normalize_service_performer_code("10000000006") == "000000010000000006"
    assert normalize_service_performer_code("000000010000000007") == "000000010000000007"
    assert normalize_service_performer_code("SVC001") == "SVC001"


def test_pick_recovery_document_by_tag_requires_tag_in_text() -> None:
    from app.procurement.sap_pr_payload import pick_recovery_document_by_tag

    rows = [
        {
            "PurchaseRequisition": "1020000001",
            "PurReqnDescription": "[AO:BA5D86CB]",
        },
        {
            "PurchaseRequisition": "1010000999",
            "PurReqnDescription": "Fresh YSER [AO:BEE7EBA0]",
        },
    ]
    assert (
        pick_recovery_document_by_tag(
            rows,
            tags=["[AO:BEE7EBA0]"],
            number_key="PurchaseRequisition",
            text_keys=("PurReqnDescription",),
        )
        == "1010000999"
    )
    assert (
        pick_recovery_document_by_tag(
            rows,
            tags=["[AO:BEE7EBA0]"],
            number_key="PurchaseRequisition",
            text_keys=("PurReqnDescription",),
        )
        != "1020000001"
    )
    assert (
        pick_recovery_document_by_tag(
            rows,
            tags=["[AO:ZZZZZZZZ]"],
            number_key="PurchaseRequisition",
            text_keys=("PurReqnDescription",),
        )
        is None
    )


def test_pick_recovery_rejects_compact_substring_inside_another_full_tag() -> None:
    from app.procurement.sap_pr_payload import pick_recovery_document_by_tag

    full_other = "[AO:E5B66B62758A4D44B2CB69671599CE1A]"
    rows = [{"PRNumber": "1010000999", "HeaderNote": f"Note {full_other}"}]
    compact_same_prefix = "[AO:E5B66B62]"
    assert (
        pick_recovery_document_by_tag(
            rows,
            tags=[compact_same_prefix],
            number_key="PRNumber",
            text_keys=("HeaderNote",),
        )
        is None
    )
    assert (
        pick_recovery_document_by_tag(
            rows,
            tags=[full_other],
            number_key="PRNumber",
            text_keys=("HeaderNote",),
        )
        == "1010000999"
    )


def test_agentos_markers_in_text_extracts_complete_tokens() -> None:
    from app.procurement.sap_pr_payload import agentos_markers_in_text

    blob = "x [AO:abc] y [AO:E5B66B62758A4D44B2CB69671599CE1A] z"
    assert agentos_markers_in_text(blob) == [
        "[AO:ABC]",
        "[AO:E5B66B62758A4D44B2CB69671599CE1A]",
    ]


def _sample_yunb_pr_form(*, allocations: list[dict] | None = None) -> dict:
    blk = default_empty_block("YUNB")
    blk["material"] = "4200000027"
    blk["short_text"] = "Consumable"
    blk["delivery_date"] = "2026-07-01"
    blk["unit_price"] = "10"
    blk["valuation_price"] = "10"
    blk["allocations"] = allocations or [{"cost_center": "HCO91001H0", "qty": "1"}]
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "header_note": "YUNB test",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PR")
    return form


def test_yunb_account_assignment_category_always_k() -> None:
    form = _sample_yunb_pr_form()
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")["to_PurchaseReqnItem"][0]
    assert row["AccountAssignmentCategory"] == "K"


def test_yunb_pr_payload_per_line_material_group() -> None:
    form = _sample_yunb_pr_form()
    line_b = default_empty_block("YUNB")
    line_b.update(
        {
            "material_group": "MG-B",
            "material": "4200000028",
            "short_text": "Line B",
            "delivery_date": "2026-07-01",
            "unit_price": "5",
            "valuation_price": "5",
            "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
        }
    )
    form["lines"][0]["material_group"] = "MG-A"
    form["lines"].append(line_b)
    items = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")["to_PurchaseReqnItem"]
    assert items[0]["MaterialGroup"] == "MG-A"
    assert items[1]["MaterialGroup"] == "MG-B"


def test_yunb_pr_payload_sends_unit_price_not_valuation_total() -> None:
    """SAP ``PurchaseRequisitionPrice`` is per unit (BAPRE); UI valuation is line total."""
    form = _sample_yunb_pr_form(
        allocations=[{"cost_center": "HCO91001H0", "qty": "20"}]
    )
    form["lines"][0]["unit_price"] = "100"
    form["lines"][0]["valuation_price"] = "2000"
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")["to_PurchaseReqnItem"][0]
    assert row["PurchaseRequisitionPrice"] == "100"
    assert row["RequestedQuantity"] == "20.000"


def test_yunb_pr_payload_multi_cc_sends_unit_price_when_valuation_differs() -> None:
    form = _sample_yunb_pr_form(
        allocations=[
            {"cost_center": "HCO91001H0", "qty": "2"},
            {"cost_center": "HCO91001A0", "qty": "1"},
        ]
    )
    form["lines"][0]["unit_price"] = "10"
    form["lines"][0]["valuation_price"] = "30"
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")["to_PurchaseReqnItem"][0]
    assert row["PurchaseRequisitionPrice"] == "10"
    assert row["RequestedQuantity"] == "3.000"


def test_yunb_multi_cc_sets_k_and_qty_distribution() -> None:
    form = _sample_yunb_pr_form(
        allocations=[
            {"cost_center": "HCO91001H0", "qty": "2"},
            {"cost_center": "HCO91001A0", "qty": "1"},
        ]
    )
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")["to_PurchaseReqnItem"][0]
    assert row["AccountAssignmentCategory"] == "K"
    assert row["MultipleAcctAssgmtDistribution"] == "1"
    assert len(row["to_PurchaseReqnAcctAssgmt"]) == 2


def test_yast_account_assignment_category_asset_a() -> None:
    blk = default_empty_block("YAST")
    blk["material"] = "4200000027"
    blk["short_text"] = "Asset"
    blk["asset"] = "7100001182"
    blk["allocations"] = [{"asset": "7100001182", "qty": "5"}]
    form = normalize_form(
        "YAST",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "header_note": "YAST test",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YAST", kind="PR")
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YAST")["to_PurchaseReqnItem"][0]
    assert row["AccountAssignmentCategory"] == "A"
    acct = row["to_PurchaseReqnAcctAssgmt"]
    assert len(acct) == 1
    assert acct[0]["MasterFixedAsset"] == "7100001182"
    assert acct[0]["Quantity"] == "5.000"
    assert "CostCenter" not in acct[0] or not acct[0].get("CostCenter")
    assert "GLAccount" not in acct[0]
    assert "ControllingArea" not in acct[0]


def test_yast_pr_payload_multi_asset_allocations() -> None:
    blk = default_empty_block("YAST")
    blk["material"] = "4200000027"
    blk["short_text"] = "Multi asset"
    blk["allocations"] = [
        {"asset": "7100001182", "qty": "2"},
        {"asset": "003500008160", "qty": "3"},
    ]
    form = normalize_form(
        "YAST",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "header_note": "YAST multi",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YAST", kind="PR")
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YAST")["to_PurchaseReqnItem"][0]
    assert row["MultipleAcctAssgmtDistribution"] == "1"
    acct = row["to_PurchaseReqnAcctAssgmt"]
    assert len(acct) == 2
    assert acct[0]["MasterFixedAsset"] == "7100001182"
    assert acct[0]["Quantity"] == "2.000"
    assert acct[1]["MasterFixedAsset"] == "003500008160"
    assert acct[1]["Quantity"] == "3.000"
    assert "CostCenter" not in acct[0]
    assert "CostCenter" not in acct[1]


def test_yast_storage_location_composite_splits_for_sap() -> None:
    """UI stores plant|sloc; SAP expects sloc only."""
    blk = default_empty_block("YAST")
    blk["material"] = "4200000027"
    blk["asset"] = "7100001182"
    blk["allocations"] = [{"asset": "7100001182", "qty": "1"}]
    form = normalize_form(
        "YAST",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "H001|3021",
                "material_group": "SD05-0001",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YAST", kind="PR")
    row = sap_pr_payload.build_pr_payload(form=form, document_type="YAST")["to_PurchaseReqnItem"][0]
    assert row["StorageLocation"] == "3021"
    assert row["Plant"] == "H001"


def test_parse_pr_items_from_read_expanded() -> None:
    body = {
        "d": {
            "PurReqnDescription": "Note A",
            "to_PurchaseReqnItem": {
                "results": [
                    {
                        "PurchaseRequisitionItem": "10",
                        "Material": "MAT1",
                        "IsDeleted": "",
                        "RequestedQuantity": "2.000",
                        "to_PurchaseReqnAcctAssgmt": {
                            "results": [
                                {
                                    "PurchaseReqnAcctAssgmtNumber": "1",
                                    "CostCenter": "CC1",
                                    "Quantity": "2.000",
                                }
                            ]
                        },
                    },
                    {
                        "PurchaseRequisitionItem": "20",
                        "Material": "MAT2",
                        "IsDeleted": "X",
                    },
                ]
            },
        }
    }
    note, items = sap_pr_payload.parse_pr_items_from_read(body)
    assert note == "Note A"
    assert len(items) == 2
    assert items[0].item_number == "10" and not items[0].is_deleted
    assert items[0].cost_centers == ["CC1"]
    assert items[1].is_deleted


def _sample_yunb_complex_form() -> dict:
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "header_note": "Complex YUNB",
            },
            "lines": [
                {
                    "material": "4200000027",
                    "short_text": "Line A",
                    "delivery_date": "2026-07-01",
                    "unit_price": "10",
                    "valuation_price": "30",
                    "allocations": [
                        {"cost_center": "HCO91001H0", "qty": "2"},
                        {"cost_center": "HCO91001A0", "qty": "1"},
                    ],
                },
                {
                    "material": "4200000016",
                    "short_text": "Line B",
                    "delivery_date": "2026-07-10",
                    "unit_price": "25",
                    "valuation_price": "50",
                    "allocations": [
                        {"cost_center": "HCO91001H0", "qty": "3"},
                        {"cost_center": "HCO91001A0", "qty": "2"},
                    ],
                },
            ],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PR")
    return form


def test_iter_pr_form_lines_matches_existing_sap_items_by_material() -> None:
    form = _sample_yunb_complex_form()
    existing = [
        sap_pr_payload.SapPrItemSnapshot(
            item_number="10",
            is_deleted=False,
            material="4200000027",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
        sap_pr_payload.SapPrItemSnapshot(
            item_number="20",
            is_deleted=False,
            material="4200000016",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
    ]
    pairs = sap_pr_payload._iter_pr_form_lines(
        form, document_type="YUNB", existing_items=existing
    )
    assert [p[0] for p in pairs] == ["10", "20"]


def test_build_pr_resubmit_plan_complex_delete_second_line() -> None:
    form = _sample_yunb_complex_form()
    form["lines"] = [form["lines"][0]]
    existing = [
        sap_pr_payload.SapPrItemSnapshot(
            item_number="10",
            is_deleted=False,
            material="4200000027",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
        sap_pr_payload.SapPrItemSnapshot(
            item_number="20",
            is_deleted=False,
            material="4200000016",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
    ]
    plan = sap_pr_payload.build_pr_resubmit_plan(
        form=form,
        document_type="YUNB",
        pr_number="1040000999",
        existing_items=existing,
    )
    assert plan.items_to_mark_deleted == ["20"]
    assert plan.items_to_create == []
    assert plan.desired_item_numbers == ["10"]


def test_build_pr_resubmit_plan_complex_modify_reuses_item_numbers() -> None:
    form = _sample_yunb_complex_form()
    form["lines"][0]["unit_price"] = "199"
    form["lines"][0]["valuation_price"] = "199"
    existing = [
        sap_pr_payload.SapPrItemSnapshot(
            item_number="10",
            is_deleted=False,
            material="4200000027",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
        sap_pr_payload.SapPrItemSnapshot(
            item_number="20",
            is_deleted=False,
            material="4200000016",
            cost_centers=["HCO91001H0", "HCO91001A0"],
        ),
    ]
    plan = sap_pr_payload.build_pr_resubmit_plan(
        form=form,
        document_type="YUNB",
        pr_number="1040000999",
        existing_items=existing,
    )
    assert plan.items_to_mark_deleted == []
    assert plan.items_to_create == []
    assert sorted(plan.desired_item_numbers) == ["10", "20"]


def test_build_pr_resubmit_plan_deletes_extra_sap_item() -> None:
    form = _sample_yser_pr_form()
    existing = [
        sap_pr_payload.SapPrItemSnapshot(item_number="10", is_deleted=False),
        sap_pr_payload.SapPrItemSnapshot(item_number="20", is_deleted=False),
    ]
    plan = sap_pr_payload.build_pr_resubmit_plan(
        form=form,
        document_type="YSER",
        pr_number="1040000099",
        existing_items=existing,
    )
    assert plan.items_to_mark_deleted == ["20"]
    assert plan.desired_item_numbers == ["10"]


def test_build_pr_item_patches_include_acct_assignments() -> None:
    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "2"},
    ]
    patches = sap_pr_payload.build_pr_item_patches(
        form=form, document_type="YSER", pr_number="1040000099"
    )
    assert len(patches) == 1
    item_no, item_body, acct_patches, acct_creates, acct_deletes = patches[0]
    assert item_no == "10"
    assert "to_PurchaseReqnAcctAssgmt" not in item_body
    assert item_body["RequestedQuantity"] == "3.000"
    assert acct_patches == []
    assert acct_deletes == []
    assert len(acct_creates) == 2
    assert acct_creates[0]["CostCenter"] == "CC1"
    assert acct_creates[1]["CostCenter"] == "CC2"
    assert acct_creates[0]["PurchaseReqnAcctAssgmtNumber"] == "1"
    assert acct_creates[1]["PurchaseReqnAcctAssgmtNumber"] == "2"


def test_build_pr_item_patches_patch_existing_acct_and_post_new() -> None:
    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "2"},
    ]
    existing = [
        sap_pr_payload.SapPrItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[sap_pr_payload.SapAcctSegment(seq="1", cost_center="CC1")],
        )
    ]
    patches = sap_pr_payload.build_pr_item_patches(
        form=form,
        document_type="YSER",
        pr_number="1040000099",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert len(acct_patches) == 1 and acct_patches[0][0] == "1"
    assert acct_patches[0][1]["CostCenter"] == "CC1"
    assert len(acct_creates) == 1 and acct_creates[0]["CostCenter"] == "CC2"
    assert acct_creates[0]["PurchaseReqnAcctAssgmtNumber"] == "2"
    assert acct_deletes == []


def test_build_pr_item_patches_deletes_removed_cost_center() -> None:
    form = _sample_yser_pr_form()
    form["lines"][0]["allocations"] = [{"cost_center": "CC2", "qty": "2"}]
    existing = [
        sap_pr_payload.SapPrItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[
                sap_pr_payload.SapAcctSegment(seq="1", cost_center="CC1", quantity="1.000"),
                sap_pr_payload.SapAcctSegment(seq="2", cost_center="CC2", quantity="2.000"),
            ],
            cost_centers=["CC1", "CC2"],
        )
    ]
    patches = sap_pr_payload.build_pr_item_patches(
        form=form,
        document_type="YSER",
        pr_number="1040000099",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert acct_deletes == ["1"]
    # CC2 qty already matches SAP — no acct PATCH, only delete of removed CC1.
    assert acct_patches == []
    assert acct_creates == []


def test_build_pur_reqn_description_is_ops_note_only() -> None:
    desc = sap_pr_payload.build_pur_reqn_description("Operations note", ticket_id="abc-123")
    assert desc == "Operations note"
    assert "[AO:" not in desc


def test_build_pur_reqn_description_truncates_long_note() -> None:
    long_note = "A" * 80
    desc = sap_pr_payload.build_pur_reqn_description(
        long_note, ticket_id="e5b66b62-758a-4d44-b2cb-69671599ce1a"
    )
    assert len(desc) == sap_pr_payload.PUR_REQN_DESCRIPTION_MAX_LEN
    assert desc == "A" * sap_pr_payload.PUR_REQN_DESCRIPTION_MAX_LEN
    assert "[AO:" not in desc


def test_build_pr_item_text_includes_full_uuid_trace() -> None:
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    trace = sap_pr_payload.pr_item_trace_marker(tid)
    text = sap_pr_payload.build_pr_item_text_for_sap(
        "Consumable", ticket_id=tid, include_trace=True
    )
    assert text.endswith(trace)
    assert text.startswith("Consum")
    assert len(text) <= sap_pr_payload.SAP_TEXT_FIELD_MAX_LEN


def test_build_pr_payload_puts_ext_system_id_on_first_line_only() -> None:
    form = _sample_yunb_pr_form()
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    payload = sap_pr_payload.build_pr_payload(
        form=form, document_type="YUNB", ticket_id=tid
    )
    assert payload["PurReqnDescription"] == "YUNB test"
    items = payload["to_PurchaseReqnItem"]
    trace = sap_pr_payload.sap_ticket_ext_system_marker(tid)
    assert items[0]["PurReqnExternalSystemId"] == trace
    assert items[0]["PurchaseRequisitionItemText"] == "Consumable"


def test_sap_ticket_ref_tag_uses_full_uuid_hex() -> None:
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    assert sap_pr_payload.sap_ticket_ref_tag(tid) == "[AO:E5B66B62758A4D44B2CB69671599CE1A]"
    assert sap_pr_payload.compact_sap_ticket_ref_tag(tid) == "[AO:E5B66B62]"


def test_recovery_tags_include_external_field_markers() -> None:
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    tags = sap_pr_payload.recovery_tags_for_ticket(tid)
    assert sap_pr_payload.sap_ticket_ext_system_marker(tid) in tags
    assert sap_pr_payload.sap_ticket_correspnc_external_marker(tid) in tags


def test_recovery_tags_include_legacy_compact() -> None:
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    tags = sap_pr_payload.recovery_tags_for_ticket(tid)
    assert sap_pr_payload.sap_ticket_ref_tag(tid) in tags
    assert sap_pr_payload.compact_sap_ticket_ref_tag(tid) in tags
    assert sap_pr_payload.legacy_sap_ticket_ref_tag(tid) in tags


def test_build_sap_text_with_ticket_tag_prioritizes_full_tag() -> None:
    tid = "e5b66b62-758a-4d44-b2cb-69671599ce1a"
    text = sap_pr_payload.build_sap_text_with_ticket_tag("Hello", ticket_id=tid)
    assert sap_pr_payload.sap_ticket_ref_tag(tid) in text
    assert len(text) <= sap_pr_payload.SAP_TEXT_FIELD_MAX_LEN


def test_build_pr_payload_two_blocks_two_items() -> None:
    form = normalize_form(
        "YUNB",
        {
            "header": {"header_note": "two lines"},
            "lines": [
                {
                    **default_empty_block("YUNB"),
                    "material": "MAT001",
                    "allocations": [{"cost_center": "CC1", "qty": "1"}],
                },
                {
                    **default_empty_block("YUNB"),
                    "material": "MAT002",
                    "allocations": [{"cost_center": "CC2", "qty": "1"}],
                },
            ],
        },
    )
    items = sap_pr_payload.build_pr_payload(form=form, document_type="YUNB")[
        "to_PurchaseReqnItem"
    ]
    assert len(items) == 2
    assert items[0]["PurchaseRequisitionItem"] == "10"
    assert items[1]["PurchaseRequisitionItem"] == "20"
    tid = "abc-123"
    payload = sap_pr_payload.build_pr_payload(
        form=form, document_type="YUNB", ticket_id=tid
    )
    traced = payload["to_PurchaseReqnItem"]
    trace = sap_pr_payload.sap_ticket_ext_system_marker(tid)
    assert traced[0]["PurReqnExternalSystemId"] == trace
    assert "PurReqnExternalSystemId" not in traced[1]
    assert "AO" not in traced[0]["PurchaseRequisitionItemText"]


def test_parse_pr_number_odata_d_wrapper() -> None:
    body = {
        "d": {
            "__metadata": {
                "id": "https://host/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionHeader('1040000016')",
            },
            "PurchaseRequisition": "1040000016",
            "PurchaseRequisitionType": "YUNB",
        }
    }
    assert sap_pr_payload.parse_pr_number_from_response(body) == "1040000016"


def test_is_placeholder_pr_number() -> None:
    assert sap_pr_payload.is_placeholder_pr_number("#       1") is True
    assert sap_pr_payload.is_placeholder_pr_number("1030000082") is False


def test_sap_sync_create_pr_requires_sap_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sap_pr_client, "sap_pr_configured", lambda: False)
    sid, err = asyncio.run(
        sap_sync.create_pr(ticket_id="abc", form={"header": {}, "lines": []}, document_type="YSER")
    )
    assert sid is None
    assert err and "not configured" in err.lower()


def _csrf_fetch_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-csrf-token": "KAeuQxDW7unnrH7BTT-LXw=="},
        request=httpx.Request(
            "GET",
            "https://example.test/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/",
        ),
    )


@patch("app.procurement.sap_pr_z_client.httpx.AsyncClient")
def test_create_pr_live_parses_response(mock_client_cls: MagicMock) -> None:
    form = _sample_yser_pr_form()
    post_resp = httpx.Response(
        201,
        json={"d": {"PRNumber": "1010000186", "DocumentType": "YSER"}},
        request=httpx.Request("POST", "https://example.test/z/pr"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(return_value=_csrf_fetch_response())
    mock_client.post = AsyncMock(return_value=post_resp)
    mock_client.patch = AsyncMock()
    mock_client_cls.return_value = mock_client

    with patch.object(sap_pr_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_pr_client.settings, "procurement_sap_username", "user"):
            with patch.object(sap_pr_client.settings, "procurement_sap_password", "pass"):
                with patch.object(sap_pr_client.settings, "procurement_sap_verify_ssl", False):
                    sid, err = asyncio.run(
                        sap_pr_client.create_pr(ticket_id="t1", form=form, document_type="YSER")
                    )

    assert err is None
    assert sid == "1010000186"
    call = mock_client.post.call_args
    assert "Z_PURCHASE_REQUISITION_SRV/PRHeaderSet" in call[0][0]
    body = call[1]["json"]
    assert body["DocumentType"] == "YSER"
    assert "to_Items" in body


@patch("app.procurement.sap_pr_z_client.httpx.AsyncClient")
def test_update_pr_live_posts_z_header(mock_client_cls: MagicMock) -> None:
    form = _sample_yser_pr_form()
    ok = httpx.Response(204, request=httpx.Request("POST", "https://example.test/z/pr"))
    z_read_body = {
        "d": {
            "PRNumber": "1010000186",
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "PurOrg": "1MGH",
                        "PurGroup": "S0Z",
                        "Plant": "H002",
                        "MaterialGroup": "S089-0001",
                        "ShortText": "Consulting",
                        "DeliveryDate": "20260515",
                        "ValuationPrice": "100.00",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "ShortText": "Consulting",
                                    "UOM": "EA",
                                    "CostCenter": "HBM11001A0",
                                    "DistrQuantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(return_value=_csrf_fetch_response())
    mock_client.post = AsyncMock(return_value=ok)
    mock_client_cls.return_value = mock_client

    async def _get_yser_after_update(*, pr_number: str, ticket_id: str = "read"):
        return z_read_body, None

    with patch.object(sap_pr_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_pr_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_pr_client.settings, "procurement_sap_password", "p"):
                with patch(
                    "app.procurement.sap_pr_z_client.apply_yser_z_line_deletes",
                    AsyncMock(return_value=None),
                ):
                    with patch(
                        "app.procurement.sap_pr_client._yser_skip_z_update_after_deletes",
                        AsyncMock(return_value=(False, None)),
                    ):
                        with patch(
                            "app.procurement.sap_pr_z_client.get_yser_pr",
                            _get_yser_after_update,
                        ):
                            sid, err = asyncio.run(
                                sap_pr_client.update_pr(
                                    sap_id="1010000186",
                                    ticket_id="t2",
                                    form=form,
                                    document_type="YSER",
                                )
                            )

    assert err is None
    assert sid == "1010000186"
    assert mock_client.post.await_count >= 1
    urls = [c[0][0] for c in mock_client.post.call_args_list]
    assert any(u.rstrip("/").endswith("/PRHeaderSet") for u in urls)
    bodies = [c.kwargs.get("json") or {} for c in mock_client.post.call_args_list]
    assert bodies
    assert all("d" not in b for b in bodies)
    assert all(b.get("PRNumber") == "1010000186" for b in bodies)
    last = bodies[-1]
    assert isinstance(last.get("to_Items"), list)
    if last.get("PurReqnDescription"):
        assert last["PurReqnDescription"] == form["header"]["header_note"]
    headers = mock_client.post.call_args_list[-1].kwargs.get("headers") or {}
    assert "X-HTTP-Method" not in headers
    assert "PRNumber" not in headers


def test_pr_entity_urls() -> None:
    url = sap_pr_payload.pr_entity_url("https://10.1.98.18:44300", "1040000016")
    assert "A_PurchaseRequisitionHeader('1040000016')" in url
    item_url = sap_pr_payload.pr_item_entity_url(
        "https://host", pr_number="1040000016", item_number="10"
    )
    assert "A_PurchaseRequisitionItem" in item_url
    assert "PurchaseRequisitionItem='10'" in item_url


@patch("app.procurement.sap_pr_z_client.httpx.AsyncClient")
def test_create_pr_rejects_placeholder_pr_number(mock_client_cls: MagicMock) -> None:
    form = _sample_yser_pr_form()
    post_resp = httpx.Response(
        201,
        json={"d": {"PRNumber": "#       1"}},
        request=httpx.Request("POST", "https://example.test/z/pr"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=_csrf_fetch_response())
    mock_client.post = AsyncMock(return_value=post_resp)
    mock_client_cls.return_value = mock_client

    with patch.object(sap_pr_client.settings, "procurement_sap_base_url", "https://10.1.98.18:44300"):
        with patch.object(sap_pr_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_pr_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_pr_client.create_pr(ticket_id="t-ph", form=form, document_type="YSER")
                )

    assert sid is None
    assert err and "placeholder" in err.lower()


@patch("app.procurement.sap_pr_z_client.httpx.AsyncClient")
def test_create_pr_retries_once_on_csrf_403(mock_client_cls: MagicMock) -> None:
    form = _sample_yser_pr_form()
    csrf_ok = _csrf_fetch_response()
    forbidden = httpx.Response(
        403,
        json={"error": {"message": {"value": "CSRF token validation failed"}}},
        request=httpx.Request("POST", "https://example.test/z/pr"),
    )
    success = httpx.Response(
        201,
        json={"d": {"PRNumber": "1010000999"}},
        request=httpx.Request("POST", "https://example.test/z/pr"),
    )
    mock_client = MagicMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(return_value=csrf_ok)
    mock_client.post = AsyncMock(side_effect=[forbidden, success])
    mock_client.patch = AsyncMock()
    mock_client_cls.return_value = mock_client

    with patch.object(sap_pr_client.settings, "procurement_sap_base_url", "https://10.1.98.18:44300"):
        with patch.object(sap_pr_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_pr_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_pr_client.create_pr(ticket_id="t-csrf", form=form, document_type="YSER")
                )

    assert sid == "1010000999"
    assert err is None
    assert mock_client.get.await_count == 2
    assert mock_client.post.await_count == 2


def test_iter_pr_form_lines_yser_one_item_per_service() -> None:
    form = _sample_yser_pr_form()
    line2 = {**form["lines"][0], "service": "SVC002", "short_text": "Second"}
    form["lines"] = [form["lines"][0], line2]
    pairs = sap_pr_payload._iter_pr_form_lines(form, document_type="YSER")
    assert len(pairs) == 2
    assert pairs[0][0] == "10"
    assert pairs[1][0] == "20"


def test_build_z_yser_update_omits_removed_service_rows() -> None:
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload

    form = _sample_yser_pr_form()
    form["lines"] = [
        {
            **form["lines"][0],
            "allocations": [
                {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                {"cost_center": "CC2", "qty": "2", "pr_acct_assgmt_number": "02"},
            ],
        },
        {
            **form["lines"][0],
            "service": "SVC002",
            "short_text": "Second",
            "allocations": [{"cost_center": "CC3", "qty": "1", "pr_acct_assgmt_number": "03"}],
        },
    ]
    del_form = copy.deepcopy(form)
    del_form["lines"] = [del_form["lines"][0]]
    services = build_z_yser_pr_payload(
        form=del_form,
        document_type="YSER",
        pr_number="1010000400",
        for_update=True,
    )["to_Items"][0]["to_Services"]
    acct_nos = [s["PRAcctAssgmtNumber"] for s in services]
    services_codes = {s["Service"] for s in services}
    assert acct_nos == ["01", "02"]
    assert "SVC002" not in services_codes


def test_form_from_z_pr_read_preserves_pr_acct_assgmt_number() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "PRAcctAssgmtNumber": "01",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "000000010000000007",
                                    "PRAcctAssgmtNumber": "02",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "1.000",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert form["lines"][0]["allocations"][0]["pr_acct_assgmt_number"] == "01"
    assert form["lines"][1]["allocations"][0]["pr_acct_assgmt_number"] == "02"


def test_verify_yser_read_matches_form_two_items_same_acct_seq() -> None:
    from app.procurement.sap_pr_z_payload import verify_yser_read_matches_form

    form = _sample_yser_pr_form()
    form["lines"] = [
        {
            **form["lines"][0],
            "purchase_requisition_item": "10",
            "allocations": [
                {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                {"cost_center": "CC2", "qty": "2", "pr_acct_assgmt_number": "02"},
            ],
        },
        {
            **form["lines"][0],
            "service": "SVC002",
            "purchase_requisition_item": "20",
            "allocations": [{"cost_center": "CC3", "qty": "1", "pr_acct_assgmt_number": "01"}],
        },
    ]
    read_body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "PRAcctAssgmtNumber": "01",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "SVC001",
                                    "PRAcctAssgmtNumber": "02",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "2.000",
                                },
                            ]
                        },
                    },
                    {
                        "PRItem": "00020",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC002",
                                    "PRAcctAssgmtNumber": "01",
                                    "CostCenter": "CC3",
                                    "DistrQuantity": "1.000",
                                }
                            ]
                        },
                    },
                ]
            }
        }
    }
    assert verify_yser_read_matches_form(read_body, form=form) == []


def test_verify_yser_read_matches_form_grouped_same_item() -> None:
    from app.procurement.sap_pr_z_payload import verify_yser_read_matches_form

    form = _sample_yser_pr_form()
    form["lines"] = [
        {
            **form["lines"][0],
            "service": "SVC001",
            "allocations": [
                {"cost_center": "CC1", "qty": "1"},
                {"cost_center": "CC2", "qty": "2"},
            ],
        },
        {
            **form["lines"][0],
            "service": "SVC002",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        },
    ]
    read_body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "SVC001",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "2.000",
                                },
                                {
                                    "Service": "SVC002",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    assert verify_yser_read_matches_form(read_body, form=form) == []


def test_verify_yser_read_matches_form_detects_missing_service() -> None:
    from app.procurement.sap_pr_z_payload import verify_yser_read_matches_form

    form = _sample_yser_pr_form()
    form["lines"] = [
        form["lines"][0],
        {**form["lines"][0], "service": "SVC002", "pr_acct_assgmt_number": "02"},
    ]
    read_body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "PRAcctAssgmtNumber": "01",
                                    "CostCenter": "HBM11001A0",
                                    "DistrQuantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    mm = verify_yser_read_matches_form(read_body, form=form)
    assert any("service lines" in m or "missing" in m for m in mm)


@pytest.mark.asyncio
async def test_paginate_recovery_scan_stops_after_bulk_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[int] = []
    bulk_rows = [
        {"PurchaseRequisition": f"104000{i}", "PurReqnExternalSystemId": "OTHER"}
        for i in range(150)
    ]

    async def fake_fetch(
        client: object, *, page: int, **kwargs: object
    ) -> list[dict[str, object]]:
        pages.append(page)
        return bulk_rows

    monkeypatch.setattr(sap_pr_client, "_fetch_recovery_results_page", fake_fetch)
    client = MagicMock()
    result = await sap_pr_client._paginate_recovery_scan(
        client,
        base="https://host",
        ticket_id="t-bulk",
        collection_url="https://host/items",
        number_key="PurchaseRequisition",
        field_key="PurReqnExternalSystemId",
        orderby="PurchaseRequisition desc",
    )
    assert result is None
    assert pages == [0]


@pytest.mark.asyncio
async def test_paginate_recovery_scan_stops_on_duplicate_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages: list[int] = []
    rows = [{"PurchaseRequisition": "1040003586", "PurReqnExternalSystemId": "OTHER"}] * 100

    async def fake_fetch(
        client: object, *, page: int, **kwargs: object
    ) -> list[dict[str, object]]:
        pages.append(page)
        return rows

    monkeypatch.setattr(sap_pr_client, "_fetch_recovery_results_page", fake_fetch)
    client = MagicMock()
    result = await sap_pr_client._paginate_recovery_scan(
        client,
        base="https://host",
        ticket_id="t-dup",
        collection_url="https://host/items",
        number_key="PurchaseRequisition",
        field_key="PurReqnExternalSystemId",
        orderby="PurchaseRequisition desc",
    )
    assert result is None
    assert pages == [0, 1]


@pytest.mark.asyncio
async def test_try_recover_pr_number_uses_single_item_collection_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_calls = 0
    paginate_calls = 0
    marker = sap_pr_payload.sap_ticket_ext_system_marker("ticket-abc")

    async def fake_fetch(client: object, **kwargs: object) -> list[dict[str, object]]:
        nonlocal fetch_calls
        fetch_calls += 1
        return [
            {
                "PurchaseRequisition": "1040009999",
                "PurReqnExternalSystemId": marker,
            }
        ]

    async def fake_paginate(client: object, **kwargs: object) -> str | None:
        nonlocal paginate_calls
        paginate_calls += 1
        return None

    monkeypatch.setattr(sap_pr_client, "_fetch_recovery_results_page", fake_fetch)
    monkeypatch.setattr(sap_pr_client, "_paginate_recovery_scan", fake_paginate)
    client = MagicMock()
    pr_no, err = await sap_pr_client._try_recover_pr_number(
        client, base="https://host", ticket_id="ticket-abc"
    )
    assert pr_no == "1040009999"
    assert err is None
    assert fetch_calls == 1
    assert paginate_calls == 0


@pytest.mark.asyncio
async def test_try_recover_pr_number_header_scan_only_after_item_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_calls = 0
    paginate_calls = 0

    async def fake_fetch(client: object, **kwargs: object) -> list[dict[str, object]]:
        nonlocal fetch_calls
        fetch_calls += 1
        return [
            {"PurchaseRequisition": "1040001000", "PurReqnExternalSystemId": "OTHER"}
        ]

    async def fake_paginate(client: object, **kwargs: object) -> str | None:
        nonlocal paginate_calls
        paginate_calls += 1
        return None

    monkeypatch.setattr(sap_pr_client, "_fetch_recovery_results_page", fake_fetch)
    monkeypatch.setattr(sap_pr_client, "_paginate_recovery_scan", fake_paginate)
    client = MagicMock()
    pr_no, err = await sap_pr_client._try_recover_pr_number(
        client, base="https://host", ticket_id="ticket-miss"
    )
    assert pr_no is None
    assert err is None
    assert fetch_calls == 1
    assert paginate_calls == 1


def test_build_yser_pr_update_posts_field_only_single_post() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = _sample_yser_pr_form()
    sap_form["lines"][0]["sap_pr_service_ref"] = "REF-001"
    sap_form["lines"][0]["purchase_requisition_item"] = "10"
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["short_text"] = "Updated consulting"
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000316",
        sap_item_count=1,
        ticket_id="t-upd",
    )
    assert len(posts) == 1
    assert posts[0]["to_Items"]
    assert not any(
        svc.get("IsDeleted") == "X"
        for item in posts[0]["to_Items"]
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict)
    )


def test_build_yser_pr_update_posts_field_only_one_post_per_service() -> None:
    """Two changed services on one PRItem → one field-update POST each (not bundled)."""
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "short_text": "A",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "short_text": "B",
                "sap_pr_service_ref": "REF-B",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["short_text"] = "A upd"
    submitted["lines"][1]["short_text"] = "B upd"
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000998",
        sap_item_count=1,
        ticket_id="t-multi-field",
    )
    assert len(posts) == 2
    refs_per_post = [
        {
            s.get("PurDocItemExternalReference")
            for s in post["to_Items"][0]["to_Services"]
        }
        for post in posts
    ]
    assert refs_per_post == [{"REF-A"}, {"REF-B"}] or refs_per_post == [{"REF-B"}, {"REF-A"}]

    # qty "1" vs "1.000" alone must not invent a field update
    submitted2 = copy.deepcopy(sap_form)
    submitted2["lines"][0]["allocations"][0]["qty"] = "1.000"
    posts2 = build_yser_pr_update_posts(
        submitted=submitted2,
        sap_form=sap_form,
        pr_number="1010000998",
        sap_item_count=1,
        ticket_id="t-qty-norm",
    )
    assert posts2 == []


def test_defaults_preserve_hydrated_gross_price_for_field_delta() -> None:
    """PATCH apply_procurement_defaults must not turn unit-price mirror into false field updates.

    Hydrate stores line-total in ``gross_price``; defaults used to overwrite with ``unit_price``,
    making every sibling look changed and producing multi-item Z POSTs that SAP rejects.
    """
    from app.procurement.sap_defaults import apply_procurement_defaults
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "short_text": "A",
                "order_unit": "EA",
                "delivery_date": "2026-08-15",
                "unit_price": "200",
                "valuation_price": "400",
                "gross_price": "400",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "short_text": "B",
                "order_unit": "EA",
                "delivery_date": "2026-08-15",
                "unit_price": "200",
                "valuation_price": "400",
                "gross_price": "400",
                "sap_pr_service_ref": "REF-B",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["unit_price"] = "120"
    submitted["lines"][0]["valuation_price"] = "240"
    submitted["lines"][0]["short_text"] = "A upd"
    apply_procurement_defaults(submitted, document_type="YSER", kind="PR")
    assert submitted["lines"][0]["gross_price"] == "400"
    assert submitted["lines"][1]["gross_price"] == "400"
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000997",
        sap_item_count=1,
        ticket_id="t-defaults-gross",
    )
    assert len(posts) == 1
    refs = {
        s.get("PurDocItemExternalReference")
        for s in posts[0]["to_Items"][0]["to_Services"]
    }
    assert refs == {"REF-A"}


def test_build_yser_pr_update_posts_cc_delete() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-CC",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"}
    ]
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000400",
        sap_item_count=1,
        ticket_id="t-cc-del",
    )
    assert posts
    deleted = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict) and svc.get("IsAcctAssgmtDeleted") == "X"
    ]
    assert deleted
    assert any(svc.get("CostCenter") == "CC2" for svc in deleted)
    assert deleted[0].get("Distrib") == "1"
    kept = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict) and svc.get("IsAcctAssgmtDeleted") != "X"
    ]
    assert len(posts) == 1
    assert len(kept) == 1
    assert "Distrib" not in kept[0]


def test_build_yser_pr_update_posts_cc_delete_without_explicit_acct_numbers() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-CC",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1"},
                    {"cost_center": "CC2", "qty": "1"},
                ],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["allocations"] = [{"cost_center": "CC1", "qty": "1"}]
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000403",
        sap_item_count=1,
        ticket_id="t-cc-del-noacct",
    )
    deleted = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict) and svc.get("IsAcctAssgmtDeleted") == "X"
    ]
    assert deleted
    assert any(svc.get("CostCenter") == "CC2" for svc in deleted)
    assert deleted[0].get("PRAcctAssgmtNumber") == "02"


def test_build_yser_pr_update_posts_service_delete() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1", "pr_acct_assgmt_number": "01"},
                    {"cost_center": "CC2", "qty": "1", "pr_acct_assgmt_number": "02"},
                ],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-B",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = {"header": sap_form["header"], "lines": [sap_form["lines"][0]]}
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000401",
        sap_item_count=1,
        ticket_id="t-svc-del",
    )
    deleted = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict)
        and (svc.get("IsAcctAssgmtDeleted") == "X" or svc.get("IsDeleted") == "X")
    ]
    assert deleted
    assert any(svc.get("PurDocItemExternalReference") == "REF-B" for svc in deleted)
    # Single-CC delete with multi-CC sibling: CC-delete path, not service IsDeleted.
    assert all(svc.get("IsAcctAssgmtDeleted") == "X" for svc in deleted)
    assert all(not svc.get("IsDeleted") for svc in deleted)
    # Deleted-only: re-posting kept services corrupts later item IsDeleted on SAP.
    kept = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict)
        and svc.get("IsDeleted") != "X"
        and svc.get("IsAcctAssgmtDeleted") != "X"
    ]
    assert kept == []


def test_build_yser_pr_update_posts_service_delete_two_single_cc_siblings() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-B",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = {"header": sap_form["header"], "lines": [sap_form["lines"][0]]}
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000402",
        sap_item_count=1,
        ticket_id="t-svc-del-2single",
    )
    deleted = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict) and svc.get("IsDeleted") == "X"
    ]
    assert deleted
    assert all(not svc.get("IsAcctAssgmtDeleted") for svc in deleted)


def test_build_yser_pr_update_posts_header_note_only() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = _sample_yser_pr_form()
    sap_form["lines"][0]["sap_pr_service_ref"] = "REF-NOTE"
    sap_form["lines"][0]["purchase_requisition_item"] = "10"
    submitted = copy.deepcopy(sap_form)
    submitted["header"]["header_note"] = "Changed note"
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000316",
        sap_item_count=1,
        ticket_id="t-note",
    )
    assert len(posts) == 1
    assert posts[0].get("PurReqnDescription") == "Changed note"


def test_build_yser_pr_update_posts_header_note_clear() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = _sample_yser_pr_form()
    sap_form["header"]["header_note"] = "Old note"
    sap_form["lines"][0]["sap_pr_service_ref"] = "REF-NOTE"
    sap_form["lines"][0]["purchase_requisition_item"] = "10"
    submitted = copy.deepcopy(sap_form)
    submitted["header"]["header_note"] = ""
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000316",
        sap_item_count=1,
        ticket_id="t-note-clear",
    )
    assert len(posts) == 1
    assert posts[0].get("PurReqnDescription") == ""


def test_merge_yser_update_matches_duplicate_services_by_ext_ref() -> None:
    from app.procurement.sap_pr_z_payload import merge_yser_update_form_with_sap

    sap_form = {
        "header": {},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "pr_acct_assgmt_number": "01",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-B",
                "purchase_requisition_item": "10",
                "pr_acct_assgmt_number": "01",
                "unit_price": "250",
                "allocations": [{"cost_center": "CC2", "qty": "1"}],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["unit_price"] = "110"
    submitted["lines"][1]["unit_price"] = "260"
    merged = merge_yser_update_form_with_sap(submitted, sap_form=sap_form)
    assert merged["lines"][0]["sap_pr_service_ref"] == "REF-A"
    assert merged["lines"][0]["unit_price"] == "110"
    assert merged["lines"][1]["sap_pr_service_ref"] == "REF-B"
    assert merged["lines"][1]["unit_price"] == "260"


def test_form_from_z_pr_read_legacy_duplicate_service_different_gross() -> None:
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "SVC001",
                                    "ShortText": "Same",
                                    "Quantity": "1.000",
                                    "GrossPrice": "100.0000",
                                    "CostCenter": "CC1",
                                    "DistrQuantity": "1.000",
                                },
                                {
                                    "Service": "SVC001",
                                    "ShortText": "Same",
                                    "Quantity": "1.000",
                                    "GrossPrice": "250.0000",
                                    "CostCenter": "CC2",
                                    "DistrQuantity": "1.000",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["allocations"][0]["qty"] == "1"
    assert form["lines"][1]["allocations"][0]["qty"] == "1"


def test_form_from_z_pr_read_legacy_duplicate_service_partial_distr_0464() -> None:
    """QAS 1010000464: two SAP service rows, same code/CC, partial DistrPercentage shares."""
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "5000.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000001000000061",
                                    "ShortText": "Rent for premises",
                                    "Quantity": "5.000",
                                    "GrossPrice": "1000.0000",
                                    "CostCenter": "HBM11001A0",
                                    "DistrQuantity": "2.083",
                                    "DistrPercentage": "41.7",
                                    "PRAcctAssgmtNumber": "01",
                                },
                                {
                                    "Service": "000000001000000061",
                                    "ShortText": "Rent for premises",
                                    "Quantity": "5.000",
                                    "GrossPrice": "1000.0000",
                                    "CostCenter": "HBM11001A0",
                                    "DistrQuantity": "2.917",
                                    "DistrPercentage": "58.3",
                                    "PRAcctAssgmtNumber": "01",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["allocations"][0]["qty"] == "2.083"
    assert form["lines"][1]["allocations"][0]["qty"] == "2.917"
    assert form["lines"][0]["unit_price"] == "1000"
    assert form["lines"][1]["unit_price"] == "1000"
    assert float(form["lines"][0]["valuation_price"]) == pytest.approx(2083, rel=0.01)
    assert float(form["lines"][1]["valuation_price"]) == pytest.approx(2917, rel=0.01)


def test_form_from_z_pr_read_legacy_duplicate_service_partial_distr_0468() -> None:
    """QAS 1010000468: two SAP service rows, same code/CC, 50/50 partial shares."""
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body = {
        "d": {
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "ValuationPrice": "50.0000",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000001000000000",
                                    "ShortText": "R&M-3.0 Tr Cassette AC Non Inverter type",
                                    "Quantity": "1.000",
                                    "GrossPrice": "50.0000",
                                    "CostCenter": "HBM13021A0",
                                    "DistrQuantity": "0.500",
                                    "DistrPercentage": "50.0",
                                    "PRAcctAssgmtNumber": "01",
                                },
                                {
                                    "Service": "000000001000000000",
                                    "ShortText": "R&M-3.0 Tr Cassette AC Non Inverter type",
                                    "Quantity": "1.000",
                                    "GrossPrice": "50.0000",
                                    "CostCenter": "HBM13021A0",
                                    "DistrQuantity": "0.500",
                                    "DistrPercentage": "50.0",
                                    "PRAcctAssgmtNumber": "01",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_pr_read(body, document_type="YSER")
    assert len(form["lines"]) == 2
    assert form["lines"][0]["allocations"][0]["qty"] == "0.5"
    assert form["lines"][1]["allocations"][0]["qty"] == "0.5"
    assert form["lines"][0]["unit_price"] == "50"
    assert form["lines"][0]["valuation_price"] == "25"
    assert form["lines"][1]["valuation_price"] == "25"


def test_build_yser_pr_update_posts_add_service() -> None:
    from app.procurement.sap_pr_z_payload import normalize_service_performer_code
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"purchasing_org": "1MGH", "purchasing_group": "S0Z", "plant": "H002"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-OLD",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"].append(
        {
            "service": "SVC002",
            "service_group": "G1",
            "purchase_requisition_item": "10",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000402",
        sap_item_count=1,
        ticket_id="t-add-svc",
    )
    assert len(posts) >= 1
    new_svc_rows = [
        svc
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict)
        and normalize_service_performer_code(svc.get("Service") or "") == "SVC002"
        and svc.get("IsDeleted") != "X"
    ]
    assert new_svc_rows
    assert new_svc_rows[0].get("PurDocItemExternalReference")
    assert new_svc_rows[0].get("PRItem") == "00010"
    assert "ShortText" not in new_svc_rows[0]


def test_ensure_yser_pr_service_refs_unique_seq_on_update() -> None:
    from app.procurement.sap_pr_z_payload import _ensure_yser_pr_service_refs

    form = {
        "header": {},
        "lines": [
            {"service": "SVC001", "sap_pr_service_ref": "AOLINE-MARKER-001"},
            {"service": "SVC002"},
        ],
    }
    _ensure_yser_pr_service_refs(form, ticket_id="ticket-abc")
    assert form["lines"][1]["sap_pr_service_ref"].endswith("-002")
    assert form["lines"][1]["sap_pr_service_ref"] != form["lines"][0]["sap_pr_service_ref"]


def test_yser_pr_service_ext_ref_create_and_read() -> None:
    from app.procurement.sap_pr_z_payload import (
        _ensure_yser_pr_service_refs,
        build_z_yser_pr_payload,
        form_from_z_pr_read,
    )

    form = _sample_yser_pr_form()
    _ensure_yser_pr_service_refs(form, ticket_id="t-create")
    assert form["lines"][0].get("sap_pr_service_ref")
    inner = build_z_yser_pr_payload(form=form, document_type="YSER", ticket_id="t-create")
    svc = inner["to_Items"][0]["to_Services"][0]
    assert svc.get("PurDocItemExternalReference") == form["lines"][0]["sap_pr_service_ref"]

    read_body = {
        "d": {
            "PRNumber": "1010000999",
            "DocumentType": "YSER",
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": form["lines"][0]["service"],
                                    "PurDocItemExternalReference": form["lines"][0][
                                        "sap_pr_service_ref"
                                    ],
                                    "CostCenter": "HBM11001A0",
                                    "DistrQuantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    hydrated = form_from_z_pr_read(read_body, document_type="YSER", seed_form=form)
    assert hydrated["lines"][0].get("sap_pr_service_ref") == form["lines"][0]["sap_pr_service_ref"]


def test_validate_yser_form_service_refs_unique_detects_dup() -> None:
    from app.procurement.sap_pr_z_payload import validate_yser_form_service_refs_unique

    form = {
        "header": {},
        "lines": [
            {"service": "SVC001", "sap_pr_service_ref": "REF-A"},
            {"service": "SVC002", "sap_pr_service_ref": "REF-A"},
        ],
    }
    assert validate_yser_form_service_refs_unique(form) is not None


def test_build_yser_pr_update_posts_remediates_duplicate_ref_on_new_service() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {},
        "lines": [
            {
                "service": "SVC001",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = {
        "header": {},
        "lines": [
            {
                "service": "SVC001",
                "sap_pr_service_ref": "REF-A",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "sap_pr_service_ref": "REF-A",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000999",
        sap_item_count=1,
        ticket_id="t-dup",
    )
    assert posts
    refs = {
        svc.get("PurDocItemExternalReference")
        for post in posts
        for item in post.get("to_Items") or []
        for svc in item.get("to_Services") or []
        if isinstance(svc, dict) and svc.get("IsDeleted") != "X"
    }
    assert refs
    assert len(refs) == len(
        [r for r in refs if r]
    )
    assert "AOLINE-AOTDUP-001" in refs


def test_build_yser_pr_update_posts_new_legacy_item() -> None:
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_form = {
        "header": {"service_group": "G1"},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_pr_service_ref": "REF-10",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC003",
                "service_group": "G2",
                "sap_pr_service_ref": "REF-20",
                "purchase_requisition_item": "20",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"].append(
        {
            "service": "SVC002",
            "service_group": "G3",
            "short_text": "New legacy line",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )
    posts = build_yser_pr_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        pr_number="1010000400",
        sap_item_count=2,
        ticket_id="t-leg",
    )
    new_item_posts = [
        p
        for p in posts
        for item in p.get("to_Items") or []
        if item.get("PRItem") == "00030"
    ]
    assert new_item_posts


def test_normalize_form_preserves_sap_pr_service_ref() -> None:
    form = normalize_form(
        "YSER",
        {
            "header": {},
            "lines": [{"service": "SVC001", "sap_pr_service_ref": "REF-KEEP"}],
        },
    )
    assert form["lines"][0]["sap_pr_service_ref"] == "REF-KEEP"


def test_yser_resolved_form_item_numbers_requires_hydration_for_multi_item() -> None:
    from app.procurement.sap_pr_z_payload import yser_resolved_form_item_numbers

    form = {
        "header": {},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    sap_form = {
        "header": {},
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G2",
                "purchase_requisition_item": "20",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    keep, err = yser_resolved_form_item_numbers(form, sap_form=sap_form)
    assert keep == {"10"}
    assert err is None

    form_no_hydrate = {
        "header": {},
        "lines": [
            {
                "service": "SVC999",
                "service_group": "G9",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    keep2, err2 = yser_resolved_form_item_numbers(form_no_hydrate, sap_form=sap_form)
    assert keep2 == set()
    assert err2 is not None
    assert "hydrate" in err2.lower()

