"""SAP PO payload mapping and live client (httpx mocked) — API_PURCHASEORDER_PROCESS_SRV."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.procurement import sap_po_client, sap_po_payload, sap_sync
from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults


def _sample_yunb_po_form(*, vendor: str = "1000000002", parent_pr: str | None = None) -> dict:
    blk = default_empty_block("YUNB")
    blk["material"] = "4200000027"
    blk["short_text"] = "Consumable PO"
    blk["unit_price"] = "25"
    blk["net_price"] = "25"
    blk["allocations"] = [{"cost_center": "HCO91001H0", "qty": "2"}]
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "vendor": vendor,
                "header_note": "PO integration test",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PO")
    return form


def test_build_po_create_payload_schedule_line_delivery_odata() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["delivery_date"] = "2026-08-15"
    payload = sap_po_payload.build_po_payload(form=form, document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    sched = row.get(sap_po_payload.PO_SCHEDULE_NAV)
    assert isinstance(sched, list) and len(sched) == 1
    assert sched[0]["ScheduleLine"] == "0001"
    assert sched[0]["ScheduleLineDeliveryDate"] == "/Date(1786752000000)/"
    assert sched[0]["ScheduleLineOrderQuantity"] == "2.000"
    assert "DeliveryDate" not in row


def test_build_po_create_payload_yunb_standalone_doc_shape() -> None:
    form = _sample_yunb_po_form()
    payload = sap_po_payload.build_po_payload(
        form=form,
        document_type="YUNB",
        ticket_id="abc-123",
    )
    assert payload["PurchaseOrderType"] == "YUNB"
    assert payload["DocumentCurrency"] == "INR"
    assert "PaymentTerms" not in payload
    assert payload["Supplier"] == "1000000002"
    assert payload["CorrespncInternalReference"] == "PO integrati"
    assert (
        payload["CorrespncExternalReference"]
        == sap_po_payload.sap_ticket_correspnc_external_marker("abc-123")
    )
    items = payload["to_PurchaseOrderItem"]
    assert len(items) == 1
    row = items[0]
    item_text = row.get("PurchaseOrderItemText", "")
    assert isinstance(item_text, str) and item_text
    assert "AO" not in item_text
    assert "PurchaseRequisition" not in row
    assert row["NetPriceAmount"] == "25.00"
    assert row["PurchaseOrderItemCategory"] == "0"
    assert "OrderPriceUnit" not in row
    assert row.get("AccountAssignmentCategory") == "K"
    assert row.get("Plant") == "H001"
    assert row.get("MaterialGroup") == "SD05-0001"
    inline = row.get(sap_po_payload.PO_ACCT_CREATE_NAV)
    assert isinstance(inline, list) and len(inline) == 1
    assert inline[0]["CostCenter"] == "HCO91001H0"
    assert sap_po_payload.PO_ACCT_NAV not in row


def test_build_po_create_payload_omits_empty_header_note() -> None:
    form = _sample_yunb_po_form()
    form["header"]["header_note"] = ""
    payload = sap_po_payload.build_po_payload(form=form, document_type="YUNB", ticket_id="t1")
    assert "CorrespncInternalReference" not in payload


def test_build_po_header_patch_sets_correspnc_internal_reference() -> None:
    form = _sample_yunb_po_form()
    patch = sap_po_payload.build_po_header_patch(form=form, ticket_id="t1", document_type="YUNB")
    assert patch["CorrespncInternalReference"] == "PO integrati"


def test_build_po_header_patch_can_clear_header_note() -> None:
    form = _sample_yunb_po_form()
    form["header"]["header_note"] = ""
    patch = sap_po_payload.build_po_header_patch(form=form, ticket_id="t1", document_type="YUNB")
    assert patch["CorrespncInternalReference"] == ""


def test_build_po_header_patch_delta_omits_unchanged_sap_fields() -> None:
    form = _sample_yunb_po_form()
    existing = {
        "PurchasingGroup": "A0B",
        "SupplierRespSalesPersonName": "ops@1mg.com",
        "CorrespncInternalReference": "PO integrati",
    }
    patch = sap_po_payload.build_po_header_patch(
        form=form,
        ticket_id="t1",
        document_type="YUNB",
        creator_email="creator@1mg.com",
        existing_sap_header=existing,
    )
    assert patch == {}
    form["header"]["header_note"] = "Updated note"
    patch2 = sap_po_payload.build_po_header_patch(
        form=form,
        ticket_id="t1",
        document_type="YUNB",
        creator_email="creator@1mg.com",
        existing_sap_header=existing,
    )
    assert patch2 == {"CorrespncInternalReference": "Updated note"}


def test_build_po_create_payload_yunb_pr_linked_minimal_shape() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["purchase_requisition_item"] = "10"
    form["header"]["tax_code"] = "XE"
    form["header"]["tax_jurisdiction"] = "ABCDABCDAB"
    payload = sap_po_payload.build_po_payload(
        form=form,
        document_type="YUNB",
        parent_pr_number="1040000020",
    )
    assert "PaymentTerms" not in payload
    row = payload["to_PurchaseOrderItem"][0]
    assert row["PurchaseRequisition"] == "1040000020"
    assert row["PurchaseRequisitionItem"] == "00010"
    assert row["OrderQuantity"] == "2"
    assert row["Material"] == ""
    assert "Plant" not in row
    assert "StorageLocation" not in row
    assert "AccountAssignmentCategory" not in row
    assert sap_po_payload.PO_ACCT_CREATE_NAV not in row
    assert sap_po_payload.PO_SCHEDULE_NAV not in row
    form["lines"][0]["delivery_date"] = "2026-08-15"
    payload_sched = sap_po_payload.build_po_payload(
        form=form,
        document_type="YUNB",
        parent_pr_number="1040000020",
    )
    row_sched = payload_sched["to_PurchaseOrderItem"][0]
    assert sap_po_payload.PO_SCHEDULE_NAV in row_sched
    assert row["TaxCode"] == "XE"
    assert row["TaxJurisdiction"] == "ABCDABCDAB"


def test_po_create_tax_code_without_header_jurisdiction_omits_field() -> None:
    form = _sample_yunb_po_form()
    form["header"]["tax_code"] = "XE"
    form["header"]["tax_jurisdiction"] = ""
    payload = sap_po_payload.build_po_payload(form=form, document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    assert row.get("TaxCode") == "XE"
    assert "TaxJurisdiction" not in row


def test_po_create_pr_linked_tax_code_without_header_jurisdiction_omits_field() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["purchase_requisition_item"] = "10"
    form["header"]["tax_code"] = "XE"
    form["header"]["tax_jurisdiction"] = ""
    payload = sap_po_payload.build_po_payload(
        form=form,
        document_type="YUNB",
        parent_pr_number="1040000020",
    )
    row = payload["to_PurchaseOrderItem"][0]
    assert "TaxJurisdiction" not in row


def test_po_create_tax_jurisdiction_header_overrides_default() -> None:
    form = _sample_yunb_po_form()
    form["header"]["tax_code"] = "XE"
    form["header"]["tax_jurisdiction"] = "CUSTOMJUR01"
    payload = sap_po_payload.build_po_payload(form=form, document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    assert row.get("TaxJurisdiction") == "CUSTOMJUR01"


def test_apply_defaults_does_not_set_purchasing_doc_type() -> None:
    form = _sample_yunb_po_form()
    assert form["header"].get("purchasing_doc_type") == ""


def test_build_po_item_text_preserves_short_text() -> None:
    tid = "57585761-bb41-4369-83fe-49be9ceaf565"
    text = sap_po_payload.build_po_item_text_for_sap("PO line #1", ticket_id=tid)
    assert text == "PO line #1"
    assert "AO" not in text


def test_build_po_create_sets_correspnc_external_reference() -> None:
    tid = "57585761-bb41-4369-83fe-49be9ceaf565"
    form = _sample_yunb_po_form()
    payload = sap_po_payload.build_po_payload(
        form=form, document_type="YUNB", ticket_id=tid
    )
    assert payload["CorrespncExternalReference"] == "AO57585761"
    row = payload["to_PurchaseOrderItem"][0]
    assert "AO" not in row.get("PurchaseOrderItemText", "")


def test_build_po_update_payload_omits_payment_terms_when_unset() -> None:
    form = _sample_yunb_po_form()
    payload = sap_po_payload.build_po_payload(
        form=form,
        document_type="YUNB",
        po_number="4500000999",
        ticket_id="abc-123",
    )
    assert "PaymentTerms" not in payload
    row = payload["to_PurchaseOrderItem"][0]
    assert row["AccountAssignmentCategory"] == "K"
    assert row.get("OrderPriceUnit")


def test_build_po_create_payload_yast_inline_acct() -> None:
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M003-0043"
    form["lines"][0]["material"] = "4100000018"
    form["lines"][0]["asset"] = "7100001182"
    form["lines"][0]["purchasing_info_record"] = "5300000009"
    form["lines"][0]["allocations"] = [{"asset": "7100001182", "qty": "2"}]
    payload = sap_po_payload.build_po_payload(form=form, document_type="YAST")
    row = payload["to_PurchaseOrderItem"][0]
    assert row["AccountAssignmentCategory"] == "A"
    assert row["OrderPriceUnit"] == row["PurchaseOrderQuantityUnit"]
    inline = row.get(sap_po_payload.PO_ACCT_CREATE_NAV)
    assert isinstance(inline, list) and len(inline) == 1
    assert inline[0]["MasterFixedAsset"] == "7100001182"
    assert "GLAccount" not in inline[0]
    assert "ControllingArea" not in inline[0]
    form["lines"][0]["allocations"] = [
        {"asset": "7100001182", "qty": "2"},
        {"asset": "003500008160", "qty": "3"},
    ]
    payload_mc = sap_po_payload.build_po_payload(form=form, document_type="YAST")
    row_mc = payload_mc["to_PurchaseOrderItem"][0]
    inline_mc = row_mc[sap_po_payload.PO_ACCT_CREATE_NAV]
    assert len(inline_mc) == 2
    assert inline_mc[0]["MasterFixedAsset"] == "7100001182"
    assert inline_mc[1]["MasterFixedAsset"] == "003500008160"
    assert row_mc["MultipleAcctAssgmtDistribution"] == "1"


def test_build_po_payload_vendor_strips_company_suffix() -> None:
    form = _sample_yunb_po_form(vendor="1000000002|1MGH")
    payload = sap_po_payload.build_po_payload(form=form, document_type="YUNB")
    assert payload["Supplier"] == "1000000002"


def test_build_po_payload_maps_requestor_email_to_salesperson() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "requestor@example.com"
    payload = sap_po_payload.build_po_payload(
        form=form, document_type="YUNB", creator_email="creator@1mg.com"
    )
    assert payload["SupplierRespSalesPersonName"] == "requestor@example.com"
    assert payload["IncotermsLocation1"] == "creator@1mg.com"


def test_build_po_payload_maps_creator_email_to_incoterms_location1_yast() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "long.requestor.address@example.com"
    payload = sap_po_payload.build_po_payload(
        form=form,
        document_type="YAST",
        creator_email="long.creator.address@example.com",
    )
    assert payload["SupplierRespSalesPersonName"] == "long.requestor.address@example"
    assert payload["IncotermsLocation1"] == "long.creator.address@example.com"


def test_build_po_header_patch_maps_requestor_and_creator_email() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "ops@1mg.com"
    body = sap_po_payload.build_po_header_patch(
        form=form, ticket_id="t1", document_type="YUNB", creator_email="creator@1mg.com"
    )
    assert body["SupplierRespSalesPersonName"] == "ops@1mg.com"
    assert "IncotermsLocation1" not in body
    assert body["PurchasingGroup"] == "A0B"


def test_verify_po_read_checks_requestor_email_when_sap_has_value() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "ops@1mg.com"
    body = {
        "d": {
            "SupplierRespSalesPersonName": "other@1mg.com",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    mismatches = sap_po_payload.verify_po_read_against_form(
        body, form=form, document_type="YUNB", ticket_id="t1"
    )
    assert any("SupplierRespSalesPersonName" in m for m in mismatches)


def test_verify_po_read_skips_requestor_email_when_sap_empty() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "ops@1mg.com"
    body = {"d": {"to_PurchaseOrderItem": {"results": []}}}
    mismatches = sap_po_payload.verify_po_read_against_form(
        body, form=form, document_type="YUNB", ticket_id="t1"
    )
    assert not any("SupplierRespSalesPersonName" in m for m in mismatches)
    assert not any("IncotermsLocation1" in m for m in mismatches)


def test_verify_po_read_checks_incoterms_location1_when_sap_has_value() -> None:
    form = _sample_yunb_po_form()
    form["header"]["requestor_email"] = "ops@1mg.com"
    body = {
        "d": {
            "SupplierRespSalesPersonName": "ops@1mg.com",
            "IncotermsLocation1": "other@1mg.com",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    mismatches = sap_po_payload.verify_po_read_against_form(
        body,
        form=form,
        document_type="YUNB",
        ticket_id="t1",
        creator_email="creator@1mg.com",
    )
    assert any("IncotermsLocation1" in m for m in mismatches)


def test_build_po_payload_requires_vendor() -> None:
    form = _sample_yunb_po_form()
    form["header"].pop("vendor", None)
    with pytest.raises(ValueError, match="Vendor"):
        sap_po_payload.build_po_payload(form=form, document_type="YUNB")


def test_sap_sync_create_po_requires_sap_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sap_po_client, "sap_po_configured", lambda: False)
    sid, err = asyncio.run(
        sap_sync.create_po(
            ticket_id="t-po",
            form={"header": {"vendor": "1"}, "lines": [{}]},
            document_type="YUNB",
            parent_sap_id=None,
        )
    )
    assert sid is None
    assert err and "not configured" in err.lower()


def _csrf_fetch_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-csrf-token": "test-csrf=="},
        request=httpx.Request(
            "GET",
            "https://example.test/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/",
        ),
    )


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_create_po_live_parses_response(mock_client_cls: MagicMock) -> None:
    form = _sample_yunb_po_form()
    post_resp = httpx.Response(
        201,
        json={"d": {"PurchaseOrder": "4500000123", "PurchaseOrderType": "YUNB"}},
        request=httpx.Request("POST", "https://example.test/po"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(return_value=_csrf_fetch_response())
    mock_client.post = AsyncMock(return_value=post_resp)
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "user"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "pass"):
                with patch.object(sap_po_client.settings, "procurement_sap_verify_ssl", False):
                    sid, err = asyncio.run(
                        sap_po_client.create_po(
                            ticket_id="t1",
                            form=form,
                            document_type="YUNB",
                            parent_sap_id="1040000063",
                        )
                    )

    assert err is None
    assert sid == "4500000123"
    call = mock_client.post.call_args
    assert "API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder" in call[0][0]
    body = call[1]["json"]
    assert body["PurchaseOrderType"] == "YUNB"
    assert "to_PurchaseOrderItem" in body


def test_parse_po_items_from_read_skips_deleted_lines() -> None:
    body = {
        "d": {
            "CorrespncInternalReference": "Note [AO:ABC]",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "MAT1",
                        "PurchasingDocumentDeletionCode": "",
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "CC1",
                                    "Quantity": "2.000",
                                }
                            ]
                        },
                    },
                    {
                        "PurchaseOrderItem": "20",
                        "Material": "MAT2",
                        "PurchasingDocumentDeletionCode": "X",
                    },
                ]
            },
        }
    }
    header_ref, items = sap_po_payload.parse_po_items_from_read(body)
    assert "Note" in header_ref
    assert len(items) == 2
    active = [s for s in items if not s.is_deleted]
    assert len(active) == 1 and active[0].item_number == "10"
    assert active[0].acct_segments[0].cost_center == "CC1"


def test_build_po_resubmit_plan_deletes_extra_sap_item() -> None:
    form = _sample_yunb_po_form()
    existing = [
        sap_po_payload.SapPoItemSnapshot(item_number="10", is_deleted=False),
        sap_po_payload.SapPoItemSnapshot(item_number="20", is_deleted=False),
    ]
    plan = sap_po_payload.build_po_resubmit_plan(
        form=form,
        document_type="YUNB",
        po_number="4500000999",
        existing_items=existing,
    )
    assert plan.items_to_mark_deleted == ["20"]
    assert plan.desired_item_numbers == ["10"]


def test_build_po_item_patches_omits_tax_jurisdiction_without_header_value() -> None:
    form = _sample_yunb_po_form()
    form["header"]["tax_code"] = "XE"
    patches = sap_po_payload.build_po_item_patches(
        form=form, document_type="YUNB", po_number="4500001"
    )
    _, body, _, _, _ = patches[0]
    assert body.get("TaxCode") == "XE"
    assert "TaxJurisdiction" not in body
    patch_body = sap_po_payload.po_item_patch_body(body)
    assert "TaxJurisdiction" not in patch_body


def test_build_po_item_patches_multi_cc() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "2"},
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form, document_type="YUNB", po_number="4500001"
    )
    assert len(patches) == 1
    _, _, _, acct_creates, acct_deletes = patches[0]
    assert len(acct_creates) == 2
    assert acct_creates[0]["CostCenter"] == "CC1"
    assert acct_creates[1]["CostCenter"] == "CC2"
    assert acct_deletes == []


def test_build_po_item_patches_yast_multi_asset_qty_change() -> None:
    """YAST PO resubmit must patch asset rows (no CostCenter) — matched by MasterFixedAsset."""
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M003-0043"
    form["lines"][0]["material"] = "4100000018"
    form["lines"][0]["asset"] = "003500008160"
    form["lines"][0]["allocations"] = [
        {"asset": "003500008160", "qty": "4"},
        {"asset": "003500008161", "qty": "5"},
    ]
    existing = [
        sap_po_payload.SapPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[
                sap_po_payload.SapAcctSegment(
                    seq="1", cost_center="", quantity="2", master_asset="3500008160"
                ),
                sap_po_payload.SapAcctSegment(
                    seq="2", cost_center="", quantity="3", master_asset="3500008161"
                ),
            ],
        )
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YAST",
        po_number="4050003725",
        existing_items=existing,
    )
    assert len(patches) == 1
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert acct_creates == []
    assert acct_deletes == []
    assert len(acct_patches) == 2
    by_seq = {seq: body for seq, body in acct_patches}
    assert by_seq["1"] == {"Quantity": "4.000"}
    assert by_seq["2"] == {"Quantity": "5.000"}


def test_build_po_item_patches_yast_asset_swap_includes_mfa() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["asset"] = "003500008161"
    form["lines"][0]["allocations"] = [{"asset": "003500008161", "qty": "2"}]
    existing = [
        sap_po_payload.SapPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[
                sap_po_payload.SapAcctSegment(
                    seq="1", cost_center="", quantity="2", master_asset="3500008160"
                ),
            ],
        )
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YAST",
        po_number="4050003725",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    # Different asset → sequential match + MFA change, or create+delete.
    # With sequential fallback the existing seq is patched with new MFA.
    assert acct_deletes == []
    assert acct_creates == []
    assert len(acct_patches) == 1
    assert acct_patches[0][0] == "1"
    assert acct_patches[0][1]["MasterFixedAsset"] in ("003500008161", "3500008161")
    assert acct_patches[0][1]["Quantity"] == "2.000"


def test_build_po_item_patches_yast_skips_unchanged_qty() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["asset"] = "003500008160"
    form["lines"][0]["allocations"] = [{"asset": "003500008160", "qty": "2"}]
    existing = [
        sap_po_payload.SapPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[
                sap_po_payload.SapAcctSegment(
                    seq="1", cost_center="", quantity="2.000", master_asset="3500008160"
                ),
            ],
        )
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YAST",
        po_number="4050003725",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert acct_patches == []
    assert acct_creates == []
    assert acct_deletes == []


def test_parse_po_items_from_read_yast_master_asset() -> None:
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4000000002",
                        "to_AccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "",
                                    "Quantity": "2",
                                    "MasterFixedAsset": "3500008160",
                                },
                                {
                                    "AccountAssignmentNumber": "2",
                                    "Quantity": "1",
                                    "MasterFixedAsset": "3500008161",
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    _, items = sap_po_payload.parse_po_items_from_read(body)
    assert [s.master_asset for s in items[0].acct_segments] == [
        "3500008160",
        "3500008161",
    ]


def test_parse_po_items_from_read_uses_create_acct_nav() -> None:
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                        "to_AccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "LCO91001A0",
                                    "Quantity": "5",
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    _, items = sap_po_payload.parse_po_items_from_read(body)
    assert len(items) == 1
    assert items[0].acct_segments[0].cost_center == "LCO91001A0"


def test_build_po_item_patches_skips_acct_when_get_returns_no_segments() -> None:
    form = _sample_yunb_po_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "CC1", "qty": "1"},
        {"cost_center": "CC2", "qty": "2"},
    ]
    existing = [
        sap_po_payload.SapPoItemSnapshot(item_number="10", is_deleted=False),
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YUNB",
        po_number="4500001",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert acct_patches == []
    assert acct_creates == []
    assert acct_deletes == []


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_update_po_live_patches_header_items_and_acct(mock_client_cls: MagicMock) -> None:
    form = _sample_yunb_po_form()
    item_text = sap_po_payload.build_po_item_text_for_sap(
        form["lines"][0]["short_text"], ticket_id="57585761-bb41-4369-83fe-49be9ceaf565"
    )
    ok = httpx.Response(204, request=httpx.Request("PATCH", "https://example.test/po"))
    read_body = {
        "d": {
            "CorrespncInternalReference": "PO integrati",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4200000027",
                        "PurchaseOrderItemText": item_text,
                        "OrderQuantity": "2.000",
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "HCO91001H0",
                                    "Quantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    read_resp = httpx.Response(
        200,
        json=read_body,
        request=httpx.Request("GET", "https://example.test/po"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    get_calls = 0

    def _get_side_effect(url: str, **kwargs: object) -> httpx.Response:
        nonlocal get_calls
        if "$expand=to_PurchaseOrderItem" in url:
            get_calls += 1
            return read_resp
        return _csrf_fetch_response()

    mock_client.get = AsyncMock(side_effect=_get_side_effect)
    mock_client.patch = AsyncMock(return_value=ok)
    mock_client.post = AsyncMock()
    mock_client.delete = AsyncMock(return_value=httpx.Response(405, request=httpx.Request("DELETE", "x")))
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_po_client.update_po(
                        sap_id="4500000999",
                        ticket_id="57585761-bb41-4369-83fe-49be9ceaf565",
                        form=form,
                        document_type="YUNB",
                        parent_sap_id="1040000063",
                    )
                )

    assert err is None
    assert sid == "4500000999"
    assert get_calls == 2
    assert mock_client.patch.await_count >= 2
    urls = [c[0][0] for c in mock_client.patch.call_args_list]
    assert any("A_PurchaseOrder('4500000999')" in u for u in urls)


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_update_po_yast_multi_asset_qty_uses_batch(mock_client_cls: MagicMock) -> None:
    """Redistribute MFA qtys must $batch — sequential PATCH fails SAP sum validation."""
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M033-0001"
    form["header"]["header_note"] = "mfa"
    form["header"]["vendor"] = "1000006465"
    form["header"]["payment_terms"] = "YI09"
    form["header"]["purchasing_group"] = "A0B"
    form["header"]["plant"] = "H003"
    form["header"]["storage_location"] = "2503"
    form["lines"][0]["material"] = "4000001653"
    form["lines"][0]["order_unit"] = "EA"
    form["lines"][0]["unit_price"] = "100"
    form["lines"][0]["net_price"] = "100"
    form["lines"][0]["asset"] = "3500008000"
    form["lines"][0]["allocations"] = [
        {"asset": "3500008000", "qty": "1"},
        {"asset": "3500008001", "qty": "4"},
    ]
    form = normalize_form("YAST", form)
    apply_procurement_defaults(form, document_type="YAST", kind="PO")
    form["lines"][0]["account_assignment_cat"] = "A"

    item_text = sap_po_payload.build_po_item_text_for_sap(
        form["lines"][0]["short_text"] or "YAST", ticket_id="yast-mfa-batch"
    )
    read_body = {
        "d": {
            "PurchaseOrder": "4050003733",
            "PurchaseOrderType": "YAST",
            "Supplier": "1000006465",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "A0B",
            "PaymentTerms": "YI09",
            "CorrespncInternalReference": "mfa",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4000001653",
                        "Plant": "H003",
                        "StorageLocation": "2503",
                        "PurchaseOrderItemText": item_text,
                        "OrderQuantity": "5.000",
                        "PurchaseOrderQuantityUnit": "EA",
                        "NetPriceAmount": "100.00",
                        "MaterialGroup": "SD05-0001",
                        "AccountAssignmentCategory": "A",
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "MasterFixedAsset": "3500008000",
                                    "Quantity": "2.000",
                                },
                                {
                                    "AccountAssignmentNumber": "2",
                                    "MasterFixedAsset": "3500008001",
                                    "Quantity": "3.000",
                                },
                            ]
                        },
                    }
                ]
            },
        }
    }
    read_resp = httpx.Response(
        200, json=read_body, request=httpx.Request("GET", "https://example.test/po")
    )
    batch_resp = httpx.Response(
        202,
        text=(
            "--b\r\nContent-Type: multipart/mixed; boundary=c\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c--\r\n--b--\r\n"
        ),
        request=httpx.Request("POST", "https://example.test/$batch"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()

    def _get_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "$expand=to_PurchaseOrderItem" in url or "A_PurchaseOrder(" in url:
            return read_resp
        return _csrf_fetch_response()

    mock_client.get = AsyncMock(side_effect=_get_side_effect)
    mock_client.patch = AsyncMock(
        return_value=httpx.Response(204, request=httpx.Request("PATCH", "https://example.test/x"))
    )
    mock_client.post = AsyncMock(return_value=batch_resp)
    mock_client.delete = AsyncMock(
        return_value=httpx.Response(405, request=httpx.Request("DELETE", "x"))
    )
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_po_client.update_po(
                        sap_id="4050003733",
                        ticket_id="yast-mfa-batch",
                        form=form,
                        document_type="YAST",
                    )
                )

    assert err is None, err
    assert sid == "4050003733"
    assert mock_client.post.await_count >= 1
    batch_urls = [c.args[0] for c in mock_client.post.await_args_list if c.args]
    assert any("$batch" in str(u) for u in batch_urls)
    # Qty-only MFA redistribute must not PATCH the item (partner PI traps).
    item_patches = [
        c.args[0]
        for c in mock_client.patch.await_args_list
        if c.args and "A_PurchaseOrderItem" in str(c.args[0])
    ]
    assert item_patches == []


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_update_po_yast_qty_batch_then_item_text_patch(mock_client_cls: MagicMock) -> None:
    """Qty $batch must still follow up with item PATCH when short text also changed."""
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M033-0001"
    form["header"]["header_note"] = "mfa"
    form["header"]["vendor"] = "1000006465"
    form["header"]["payment_terms"] = "YI09"
    form["header"]["purchasing_group"] = "A0B"
    form["header"]["plant"] = "H003"
    form["header"]["storage_location"] = "2503"
    form["lines"][0]["material"] = "4000001653"
    form["lines"][0]["order_unit"] = "EA"
    form["lines"][0]["unit_price"] = "100"
    form["lines"][0]["net_price"] = "100"
    form["lines"][0]["short_text"] = "Updated YAST line text"
    form["lines"][0]["asset"] = "3500008000"
    form["lines"][0]["allocations"] = [
        {"asset": "3500008000", "qty": "1"},
        {"asset": "3500008001", "qty": "4"},
    ]
    form = normalize_form("YAST", form)
    apply_procurement_defaults(form, document_type="YAST", kind="PO")
    form["lines"][0]["account_assignment_cat"] = "A"

    old_text = sap_po_payload.build_po_item_text_for_sap(
        "Old YAST line text", ticket_id="yast-mfa-text"
    )
    new_text = sap_po_payload.build_po_item_text_for_sap(
        "Updated YAST line text", ticket_id="yast-mfa-text"
    )

    def _po_read(*, text: str, qty1: str, qty2: str) -> dict:
        return {
            "d": {
                "PurchaseOrder": "4050003733",
                "PurchaseOrderType": "YAST",
                "Supplier": "1000006465",
                "PurchasingOrganization": "1MGH",
                "PurchasingGroup": "A0B",
                "PaymentTerms": "YI09",
                "CorrespncInternalReference": "mfa",
                "to_PurchaseOrderItem": {
                    "results": [
                        {
                            "PurchaseOrderItem": "10",
                            "Material": "4000001653",
                            "Plant": "H003",
                            "StorageLocation": "2503",
                            "PurchaseOrderItemText": text,
                            "OrderQuantity": "5.000",
                            "PurchaseOrderQuantityUnit": "EA",
                            "NetPriceAmount": "100.00",
                            "MaterialGroup": "SD05-0001",
                            "AccountAssignmentCategory": "A",
                            "to_PurchaseOrderAccountAssignment": {
                                "results": [
                                    {
                                        "AccountAssignmentNumber": "1",
                                        "MasterFixedAsset": "3500008000",
                                        "Quantity": qty1,
                                    },
                                    {
                                        "AccountAssignmentNumber": "2",
                                        "MasterFixedAsset": "3500008001",
                                        "Quantity": qty2,
                                    },
                                ]
                            },
                        }
                    ]
                },
            }
        }

    state = {"patched_text": False}

    def _get_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "$expand=to_PurchaseOrderItem" in url or "A_PurchaseOrder(" in url:
            if state["patched_text"]:
                body = _po_read(text=new_text, qty1="1.000", qty2="4.000")
            else:
                body = _po_read(text=old_text, qty1="2.000", qty2="3.000")
            return httpx.Response(
                200, json=body, request=httpx.Request("GET", "https://example.test/po")
            )
        return _csrf_fetch_response()

    batch_resp = httpx.Response(
        202,
        text=(
            "--b\r\nContent-Type: multipart/mixed; boundary=c\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c--\r\n--b--\r\n"
        ),
        request=httpx.Request("POST", "https://example.test/$batch"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(side_effect=_get_side_effect)

    async def _patch_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "A_PurchaseOrderItem" in str(url):
            state["patched_text"] = True
        return httpx.Response(204, request=httpx.Request("PATCH", "https://example.test/x"))

    mock_client.patch = AsyncMock(side_effect=_patch_side_effect)
    mock_client.post = AsyncMock(return_value=batch_resp)
    mock_client.delete = AsyncMock(
        return_value=httpx.Response(405, request=httpx.Request("DELETE", "x"))
    )
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_po_client.update_po(
                        sap_id="4050003733",
                        ticket_id="yast-mfa-text",
                        form=form,
                        document_type="YAST",
                    )
                )

    assert err is None, err
    assert sid == "4050003733"
    assert any("$batch" in str(c.args[0]) for c in mock_client.post.await_args_list if c.args)
    item_patch_calls = [
        c
        for c in mock_client.patch.await_args_list
        if c.args and "A_PurchaseOrderItem" in str(c.args[0])
    ]
    assert len(item_patch_calls) == 1
    json_body = item_patch_calls[0].kwargs.get("json")
    assert json_body is not None
    assert "OrderQuantity" not in json_body
    assert json_body.get("PurchaseOrderItemText") == new_text


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_update_po_yast_qty_batch_then_price_patch(mock_client_cls: MagicMock) -> None:
    """Qty $batch + unit price change must follow up with item NetPriceAmount PATCH."""
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M033-0001"
    form["header"]["header_note"] = "mfa"
    form["header"]["vendor"] = "1000006465"
    form["header"]["payment_terms"] = "YI09"
    form["header"]["purchasing_group"] = "A0B"
    form["header"]["plant"] = "H003"
    form["header"]["storage_location"] = "2503"
    form["lines"][0]["material"] = "4000001653"
    form["lines"][0]["order_unit"] = "EA"
    form["lines"][0]["unit_price"] = "150"
    form["lines"][0]["net_price"] = "150"
    form["lines"][0]["short_text"] = "YAST price line"
    form["lines"][0]["asset"] = "3500008000"
    form["lines"][0]["allocations"] = [
        {"asset": "3500008000", "qty": "1"},
        {"asset": "3500008001", "qty": "4"},
    ]
    form = normalize_form("YAST", form)
    apply_procurement_defaults(form, document_type="YAST", kind="PO")
    form["lines"][0]["account_assignment_cat"] = "A"

    item_text = sap_po_payload.build_po_item_text_for_sap(
        "YAST price line", ticket_id="yast-mfa-price"
    )

    def _po_read(*, price: str, qty1: str, qty2: str) -> dict:
        return {
            "d": {
                "PurchaseOrder": "4050003733",
                "PurchaseOrderType": "YAST",
                "Supplier": "1000006465",
                "PurchasingOrganization": "1MGH",
                "PurchasingGroup": "A0B",
                "PaymentTerms": "YI09",
                "CorrespncInternalReference": "mfa",
                "to_PurchaseOrderItem": {
                    "results": [
                        {
                            "PurchaseOrderItem": "10",
                            "Material": "4000001653",
                            "Plant": "H003",
                            "StorageLocation": "2503",
                            "PurchaseOrderItemText": item_text,
                            "OrderQuantity": "5.000",
                            "PurchaseOrderQuantityUnit": "EA",
                            "NetPriceAmount": price,
                            "MaterialGroup": "SD05-0001",
                            "AccountAssignmentCategory": "A",
                            "to_PurchaseOrderAccountAssignment": {
                                "results": [
                                    {
                                        "AccountAssignmentNumber": "1",
                                        "MasterFixedAsset": "3500008000",
                                        "Quantity": qty1,
                                    },
                                    {
                                        "AccountAssignmentNumber": "2",
                                        "MasterFixedAsset": "3500008001",
                                        "Quantity": qty2,
                                    },
                                ]
                            },
                        }
                    ]
                },
            }
        }

    state = {"patched": False}

    def _get_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "$expand=to_PurchaseOrderItem" in url or "A_PurchaseOrder(" in url:
            if state["patched"]:
                body = _po_read(price="150.00", qty1="1.000", qty2="4.000")
            else:
                body = _po_read(price="100.00", qty1="2.000", qty2="3.000")
            return httpx.Response(
                200, json=body, request=httpx.Request("GET", "https://example.test/po")
            )
        return _csrf_fetch_response()

    batch_resp = httpx.Response(
        202,
        text=(
            "--b\r\nContent-Type: multipart/mixed; boundary=c\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
            "--c--\r\n--b--\r\n"
        ),
        request=httpx.Request("POST", "https://example.test/$batch"),
    )
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(side_effect=_get_side_effect)

    async def _patch_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "A_PurchaseOrderItem" in str(url):
            state["patched"] = True
        return httpx.Response(204, request=httpx.Request("PATCH", "https://example.test/x"))

    mock_client.patch = AsyncMock(side_effect=_patch_side_effect)
    mock_client.post = AsyncMock(return_value=batch_resp)
    mock_client.delete = AsyncMock(
        return_value=httpx.Response(405, request=httpx.Request("DELETE", "x"))
    )
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_po_client.update_po(
                        sap_id="4050003733",
                        ticket_id="yast-mfa-price",
                        form=form,
                        document_type="YAST",
                    )
                )

    assert err is None, err
    assert sid == "4050003733"
    item_patch_calls = [
        c
        for c in mock_client.patch.await_args_list
        if c.args and "A_PurchaseOrderItem" in str(c.args[0])
    ]
    assert len(item_patch_calls) == 1
    json_body = item_patch_calls[0].kwargs.get("json")
    assert json_body is not None
    assert "OrderQuantity" not in json_body
    assert json_body.get("NetPriceAmount") == "150.00"


def test_po_acct_assgmt_entity_url_uses_pur_ord_collection() -> None:
    url = sap_po_payload.po_acct_assgmt_entity_url(
        "https://10.1.98.18:44300",
        po_number="4080000001",
        item_number="10",
        acct_assgmt_number="1",
    )
    assert "A_PurOrdAccountAssignment" in url
    assert "A_PurchaseOrderAccountAssignment" not in url


def test_po_item_patch_body_keeps_tax_omits_pr_link() -> None:
    body = sap_po_payload.po_item_patch_body(
        {
            "PurchaseOrder": "4080000001",
            "PurchaseOrderItem": "10",
            "NetPriceAmount": "15.00",
            "TaxCode": "V0",
            "TaxJurisdiction": "ABCDABCDAB",
            "PurchaseRequisition": "1040000001",
            "PurchaseRequisitionItem": "00010",
        }
    )
    assert body == {
        "NetPriceAmount": "15.00",
        "TaxCode": "V0",
        "TaxJurisdiction": "ABCDABCDAB",
    }


def test_po_item_non_qty_detects_material_group_change() -> None:
    from app.procurement.sap_po_client import _po_item_non_qty_fields_changed

    snap = sap_po_payload.SapPoItemSnapshot(
        item_number="10",
        is_deleted=False,
        material="4000001653",
        item_text="same",
        plant="H003",
        storage_location="2503",
        net_price="100.00",
        tax_code="FA",
        material_group="M033-0001",
        order_unit="EA",
        order_quantity="5.000",
    )
    unchanged = {
        "Material": "4000001653",
        "PurchaseOrderItemText": "same",
        "Plant": "H003",
        "StorageLocation": "2503",
        "NetPriceAmount": "100.00",
        "TaxCode": "FA",
        "MaterialGroup": "M033-0001",
        "PurchaseOrderQuantityUnit": "EA",
        "OrderQuantity": "5.000",
        "PurchaseOrderItemCategory": "0",
        "AccountAssignmentCategory": "A",
        "ProductType": "1",
        "NetPriceQuantity": "1",
    }
    assert _po_item_non_qty_fields_changed(snap, unchanged) is False
    changed = {**unchanged, "MaterialGroup": "M040-0002"}
    assert _po_item_non_qty_fields_changed(snap, changed) is True


def test_po_item_non_qty_unknown_field_forces_patch() -> None:
    from app.procurement.sap_po_client import _po_item_non_qty_fields_changed

    snap = sap_po_payload.SapPoItemSnapshot(
        item_number="10",
        is_deleted=False,
        material="4000001653",
        material_group="M033-0001",
    )
    body = {
        "Material": "4000001653",
        "MaterialGroup": "M033-0001",
        "SomeNewSapField": "X",
    }
    assert _po_item_non_qty_fields_changed(snap, body) is True


@patch("app.procurement.sap_po_client.httpx.AsyncClient")
def test_update_po_yast_qty_batch_then_material_group_patch(
    mock_client_cls: MagicMock,
) -> None:
    """MaterialGroup change after MFA qty $batch must still follow up with item PATCH."""
    form = _sample_yunb_po_form()
    form["header"]["material_group"] = "M040-0002"
    form["header"]["header_note"] = "mfa"
    form["header"]["vendor"] = "1000006465"
    form["header"]["payment_terms"] = "YI09"
    form["header"]["purchasing_group"] = "A0B"
    form["header"]["plant"] = "H003"
    form["header"]["storage_location"] = "2503"
    form["lines"][0]["material"] = "4000001653"
    form["lines"][0]["material_group"] = "M040-0002"
    form["lines"][0]["order_unit"] = "EA"
    form["lines"][0]["unit_price"] = "100"
    form["lines"][0]["net_price"] = "100"
    form["lines"][0]["short_text"] = "YAST mg line"
    form["lines"][0]["asset"] = "3500008000"
    form["lines"][0]["allocations"] = [
        {"asset": "3500008000", "qty": "1"},
        {"asset": "3500008001", "qty": "4"},
    ]
    form = normalize_form("YAST", form)
    apply_procurement_defaults(form, document_type="YAST", kind="PO")
    form["lines"][0]["account_assignment_cat"] = "A"
    form["lines"][0]["material_group"] = "M040-0002"
    form["header"]["material_group"] = "M040-0002"

    item_text = sap_po_payload.build_po_item_text_for_sap(
        "YAST mg line", ticket_id="yast-mfa-mg"
    )

    def _po_read(*, mg: str, qty1: str, qty2: str) -> dict:
        return {
            "d": {
                "PurchaseOrder": "4050003733",
                "PurchaseOrderType": "YAST",
                "Supplier": "1000006465",
                "PurchasingOrganization": "1MGH",
                "PurchasingGroup": "A0B",
                "PaymentTerms": "YI09",
                "CorrespncInternalReference": "mfa",
                "to_PurchaseOrderItem": {
                    "results": [
                        {
                            "PurchaseOrderItem": "10",
                            "Material": "4000001653",
                            "Plant": "H003",
                            "StorageLocation": "2503",
                            "PurchaseOrderItemText": item_text,
                            "OrderQuantity": "5.000",
                            "PurchaseOrderQuantityUnit": "EA",
                            "NetPriceAmount": "100.00",
                            "MaterialGroup": mg,
                            "AccountAssignmentCategory": "A",
                            "to_PurchaseOrderAccountAssignment": {
                                "results": [
                                    {
                                        "AccountAssignmentNumber": "1",
                                        "MasterFixedAsset": "3500008000",
                                        "Quantity": qty1,
                                    },
                                    {
                                        "AccountAssignmentNumber": "2",
                                        "MasterFixedAsset": "3500008001",
                                        "Quantity": qty2,
                                    },
                                ]
                            },
                        }
                    ]
                },
            }
        }

    state = {"patched": False}

    def _get_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "$expand=to_PurchaseOrderItem" in url or "A_PurchaseOrder(" in url:
            if state["patched"]:
                body = _po_read(mg="M040-0002", qty1="1.000", qty2="4.000")
            else:
                body = _po_read(mg="SD05-0001", qty1="2.000", qty2="3.000")
            return httpx.Response(
                200, json=body, request=httpx.Request("GET", "https://example.test/po")
            )
        return _csrf_fetch_response()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.cookies = httpx.Cookies()
    mock_client.get = AsyncMock(side_effect=_get_side_effect)

    def _patch_side_effect(url: str, **kwargs: object) -> httpx.Response:
        if "A_PurchaseOrderItem" in str(url):
            state["patched"] = True
        return httpx.Response(204, request=httpx.Request("PATCH", "https://example.test/x"))

    mock_client.patch = AsyncMock(side_effect=_patch_side_effect)
    mock_client.post = AsyncMock(
        return_value=httpx.Response(
            202,
            text=(
                "--b\r\nContent-Type: multipart/mixed; boundary=c\r\n\r\n"
                "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
                "--c\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n"
                "--c--\r\n--b--\r\n"
            ),
            request=httpx.Request("POST", "https://example.test/$batch"),
        )
    )
    mock_client.delete = AsyncMock(
        return_value=httpx.Response(405, request=httpx.Request("DELETE", "x"))
    )
    mock_client_cls.return_value = mock_client

    with patch.object(sap_po_client.settings, "procurement_sap_base_url", "https://10.1.98.30:20400"):
        with patch.object(sap_po_client.settings, "procurement_sap_username", "u"):
            with patch.object(sap_po_client.settings, "procurement_sap_password", "p"):
                sid, err = asyncio.run(
                    sap_po_client.update_po(
                        sap_id="4050003733",
                        ticket_id="yast-mfa-mg",
                        form=form,
                        document_type="YAST",
                    )
                )

    assert err is None, err
    assert sid == "4050003733"
    item_patch_calls = [
        c
        for c in mock_client.patch.await_args_list
        if c.args and "A_PurchaseOrderItem" in str(c.args[0])
    ]
    assert len(item_patch_calls) == 1
    assert item_patch_calls[0].kwargs.get("json", {}).get("MaterialGroup") == "M040-0002"


def test_pr_item_rest_needs_patch_detects_material_group() -> None:
    from app.procurement.sap_pr_client import _pr_item_rest_needs_patch
    from app.procurement.sap_pr_payload import SapPrItemSnapshot

    snap = SapPrItemSnapshot(
        item_number="10",
        is_deleted=False,
        material="4000000002",
        item_text="note",
        plant="H001",
        storage_location="2501",
        unit_price="10.00",
        material_group="M033-0001",
        base_unit="EA",
        purchasing_group="A0D",
        purchasing_organization="1MGH",
        company_code="1MGH",
    )
    rest = {
        "Material": "4000000002",
        "PurchaseRequisitionItemText": "note",
        "Plant": "H001",
        "StorageLocation": "2501",
        "PurchaseRequisitionPrice": "10.00",
        "MaterialGroup": "M033-0001",
        "BaseUnit": "EA",
        "PurchasingGroup": "A0D",
        "PurchasingOrganization": "1MGH",
        "CompanyCode": "1MGH",
        "PurchaseRequisitionType": "YAST",
        "ProductType": "1",
    }
    assert _pr_item_rest_needs_patch(snap, rest) is False
    rest_g = {**rest, "MaterialGroup": "M040-0002"}
    assert _pr_item_rest_needs_patch(snap, rest_g) is True


def test_build_po_item_patches_skips_unchanged_acct_segment() -> None:
    form = _sample_yunb_po_form()
    existing = [
        sap_po_payload.SapPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            acct_segments=[
                sap_po_payload.SapAcctSegment(
                    seq="1", cost_center="HCO91001H0", quantity="2.000"
                )
            ],
        )
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YUNB",
        po_number="4500001",
        existing_items=existing,
    )
    _, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert acct_patches == []
    assert acct_creates == []
    assert acct_deletes == []


@pytest.mark.asyncio
async def test_sap_get_po_body_retries_resolve_mapping_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_pr_client import _SapRequestError

    po_json = {
        "d": {
            "PurchaseOrder": "4080000012",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                    }
                ]
            },
        }
    }
    resolve_calls = 0

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return po_json

    async def fake_csrf_request(
        client: object,
        *,
        base: str,
        ticket_id: str,
        method: str,
        url: str,
        json_body: object,
        headers_builder: object,
        csrf_service_root: str | None = None,
    ) -> FakeResp:
        assert method == "GET"
        return FakeResp()

    async def fake_resolve(
        client: object,
        *,
        base: str,
        body: dict,
        po_number: str,
        ticket_id: str,
        csrf_service_root: str | None = None,
    ) -> dict:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls < 2:
            raise _SapRequestError(
                "SAP HTTP 500: Invalid or no mapping to system data types found"
            )
        return body

    async def fake_z_header(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(sap_po_client, "_request_with_csrf_retry", fake_csrf_request)
    monkeypatch.setattr(sap_po_client, "resolve_po_read_body", fake_resolve)
    monkeypatch.setattr(sap_po_client, "_sap_fetch_z_po_header_root", fake_z_header)

    client = MagicMock()
    body, err = await sap_po_client._sap_get_po_body(
        client,
        base="https://host",
        po_number="4080000012",
        ticket_id="t-read",
        document_type="YUNB",
    )
    assert err is None
    assert body is not None
    assert resolve_calls == 2


@pytest.mark.asyncio
async def test_sap_get_po_body_does_not_retry_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_calls = 0

    class FakeResp:
        status_code = 401
        text = "Unauthorized"

        def json(self) -> dict:
            return {"error": {"message": {"value": "Unauthorized"}}}

    async def fake_csrf_request(
        client: object,
        *,
        base: str,
        ticket_id: str,
        method: str,
        url: str,
        json_body: object,
        headers_builder: object,
        csrf_service_root: str | None = None,
    ) -> FakeResp:
        nonlocal get_calls
        get_calls += 1
        return FakeResp()

    monkeypatch.setattr(sap_po_client, "_request_with_csrf_retry", fake_csrf_request)

    client = MagicMock()
    body, err = await sap_po_client._sap_get_po_body(
        client,
        base="https://host",
        po_number="4080000012",
        ticket_id="t-read",
        document_type="YUNB",
    )
    assert body is None
    assert err is not None
    assert get_calls == 1


@pytest.mark.asyncio
async def test_try_recover_po_yunb_skips_yser_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    yser_calls: list[int] = []

    async def fake_standard(
        client: object, *, base: str, ticket_id: str
    ) -> tuple[str | None, str | None]:
        return None, None

    async def fake_yser(client: object, *, base: str, ticket_id: str) -> str | None:
        yser_calls.append(1)
        return None

    monkeypatch.setattr(sap_po_client, "_try_recover_po_number", fake_standard)
    monkeypatch.setattr(sap_po_client, "_try_recover_yser_po_by_ext_system", fake_yser)
    monkeypatch.setattr(
        sap_po_client,
        "_credentials_or_error",
        lambda: (("https://host", "u", "p"), None),
    )

    recovered, err = await sap_po_client.try_recover_po(
        ticket_id="b9756726-aa27-411d-9430-7678da87ff4e",
        document_type="YUNB",
    )
    assert recovered is None
    assert err is None
    assert yser_calls == []


@pytest.mark.asyncio
async def test_try_recover_po_yser_runs_yser_scan_after_standard_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yser_calls: list[int] = []

    async def fake_standard(
        client: object, *, base: str, ticket_id: str
    ) -> tuple[str | None, str | None]:
        return None, None

    async def fake_yser(client: object, *, base: str, ticket_id: str) -> str | None:
        yser_calls.append(1)
        return "4080000999"

    monkeypatch.setattr(sap_po_client, "_try_recover_po_number", fake_standard)
    monkeypatch.setattr(sap_po_client, "_try_recover_yser_po_by_ext_system", fake_yser)
    monkeypatch.setattr(
        sap_po_client,
        "_credentials_or_error",
        lambda: (("https://host", "u", "p"), None),
    )

    recovered, err = await sap_po_client.try_recover_po(
        ticket_id="b9756726-aa27-411d-9430-7678da87ff4e",
        document_type="YSER",
    )
    assert recovered == "4080000999"
    assert err is None
    assert yser_calls == [1]


@pytest.mark.asyncio
async def test_try_recover_po_yser_skips_yser_scan_when_standard_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yser_calls: list[int] = []

    async def fake_standard(
        client: object, *, base: str, ticket_id: str
    ) -> tuple[str | None, str | None]:
        return "4080001000", None

    async def fake_yser(client: object, *, base: str, ticket_id: str) -> str | None:
        yser_calls.append(1)
        return None

    monkeypatch.setattr(sap_po_client, "_try_recover_po_number", fake_standard)
    monkeypatch.setattr(sap_po_client, "_try_recover_yser_po_by_ext_system", fake_yser)
    monkeypatch.setattr(
        sap_po_client,
        "_credentials_or_error",
        lambda: (("https://host", "u", "p"), None),
    )

    recovered, err = await sap_po_client.try_recover_po(
        ticket_id="b9756726-aa27-411d-9430-7678da87ff4e",
        document_type="YSER",
    )
    assert recovered == "4080001000"
    assert err is None
    assert yser_calls == []


def test_normalize_po_item_number_matches_padded_sap_values() -> None:
    assert sap_po_payload.normalize_po_item_number("00010") == "10"
    assert sap_po_payload.normalize_po_item_number("10") == "10"
    assert sap_po_payload.normalize_po_item_number("") == ""


def test_parse_po_items_from_read_normalizes_padded_item_numbers() -> None:
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "Material": "4100000012",
                    }
                ]
            }
        }
    }
    _, items = sap_po_payload.parse_po_items_from_read(body)
    assert len(items) == 1
    assert items[0].item_number == "10"


def test_build_po_item_patches_matches_padded_sap_snapshot() -> None:
    """SAP GET returns 00010; form iterates as 10 — must not treat as missing line."""
    form = _sample_yunb_po_form()
    existing = [
        sap_po_payload.SapPoItemSnapshot(
            item_number="00010",
            is_deleted=False,
            acct_segments=[
                sap_po_payload.SapAcctSegment(
                    seq="1", cost_center="HCO91001H0", quantity="2.000"
                )
            ],
        )
    ]
    patches = sap_po_payload.build_po_item_patches(
        form=form,
        document_type="YUNB",
        po_number="4500001",
        existing_items=existing,
    )
    assert len(patches) == 1
    item_no, _, acct_patches, acct_creates, acct_deletes = patches[0]
    assert item_no == "10"
    assert acct_patches == []
    assert acct_creates == []
    assert acct_deletes == []


def test_build_po_resubmit_plan_padded_sap_items_no_spurious_delete() -> None:
    """Before normalization, active {00010,00020} vs desired {10} deleted BOTH SAP lines."""
    form = _sample_yunb_po_form()
    existing = [
        sap_po_payload.SapPoItemSnapshot(item_number="00010", is_deleted=False),
        sap_po_payload.SapPoItemSnapshot(item_number="00020", is_deleted=False),
    ]
    plan = sap_po_payload.build_po_resubmit_plan(
        form=form,
        document_type="YUNB",
        po_number="4500000999",
        existing_items=existing,
    )
    assert plan.items_to_mark_deleted == ["20"]
    assert plan.desired_item_numbers == ["10"]
    assert plan.items_to_create == []


def test_verify_po_read_against_form_accepts_padded_sap_item_numbers() -> None:
    form = _sample_yunb_po_form()
    body = {
        "d": {
            "CorrespncInternalReference": "PO integrati",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "Material": "4200000027",
                        "OrderQuantity": "2.000",
                        "NetPriceAmount": "25.00",
                        "PurchaseOrderItemText": "Consumable PO",
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "HCO91001H0",
                                    "Quantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    mismatches = sap_po_payload.verify_po_read_against_form(
        body,
        form=form,
        document_type="YUNB",
        ticket_id="t1",
    )
    assert mismatches == []
