"""PO form enrichment from SAP PR items."""

from __future__ import annotations

import pytest

from app.procurement.po_sap_enrichment import (
    _enrich_po_form_with_items,
    _resolve_yser_po_line_pr_item,
    build_po_prefill_form_from_pr,
    match_po_line_to_pr_item,
    po_allocations_match_sap_pr,
    sap_pr_items_to_po_form,
)
from app.procurement import sap_po_payload
from app.procurement.sap_po_payload import (
    build_po_payload,
    iter_po_acct_post_bodies,
    po_line_needs_acct_post_create,
)
from app.procurement.sap_pr_items import SapPrAcctRow, SapPrItemLine


def _sap_line(
    *,
    item_no: str,
    material: str = "4200000027",
    unit: str = "KG",
    cc: str = "HCO91001H0",
    pur_org: str = "1MGH",
) -> SapPrItemLine:
    return SapPrItemLine(
        item_no=item_no,
        material=material,
        plant="H001",
        sloc="3021",
        material_group="SD05-0001",
        qty="1.000",
        unit=unit,
        price="10.00",
        pur_group="A0B",
        pur_org=pur_org,
        item_cat="0",
        acct_cat="K",
        short_text="line",
        acct_rows=[SapPrAcctRow(seq="1", cost_center=cc, quantity="1.000", master_asset="")],
    )


def test_match_by_material_and_partial_pr() -> None:
    sap_items = [_sap_line(item_no="10"), _sap_line(item_no="20", material="4200000016")]
    row = {"material": "4200000016", "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}]}
    used: set[str] = set()
    hit = match_po_line_to_pr_item(
        row=row, document_type="YUNB", sap_items=sap_items, used_item_nos=used
    )
    assert hit is not None
    assert hit.item_no == "20"


def test_enrich_does_not_overwrite_user_purchasing_org() -> None:
    form = {
        "header": {"purchasing_org": "1LFS", "company_code": "1LFS", "vendor": "1000000002"},
        "lines": [{"material": "4200000027", "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}]}],
    }
    sap_items = [_sap_line(item_no="10", pur_org="1MGH")]
    err = _enrich_po_form_with_items(form, document_type="YUNB", sap_items=sap_items)
    assert err is None
    assert form["header"]["purchasing_org"] == "1LFS"


def test_apply_sap_pr_item_defaults_sets_service_group_for_yser() -> None:
    from app.procurement.po_sap_enrichment import apply_sap_pr_item_defaults_to_header

    header: dict = {}
    item = _sap_line(item_no="10")
    item.material_group = "S001-0001"
    apply_sap_pr_item_defaults_to_header(header, [item], document_type="YSER")
    assert header["service_group"] == "S001-0001"


def test_defaults_sanitize_leaves_room_for_pr_delivery_enrichment() -> None:
    from app.procurement.field_schema import default_empty_block, normalize_form
    from app.procurement.po_sap_enrichment import _apply_sap_item_to_po_line
    from app.procurement.sap_defaults import apply_procurement_defaults

    blk = default_empty_block("YUNB")
    form = normalize_form(
        "YUNB",
        {
            "header": {"purchasing_org": "1MGH", "vendor": "1000000002"},
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PO")
    assert form["lines"][0]["delivery_date"] == ""

    sap_item = _sap_line(item_no="10")
    sap_item.delivery_date = "2026-09-15"
    _apply_sap_item_to_po_line(
        form["lines"][0], sap_item=sap_item, document_type="YUNB"
    )
    assert form["lines"][0]["delivery_date"] == "2026-09-15"


def test_enrich_po_form_stamps_line_material_group() -> None:
    form = {
        "header": {"purchasing_org": "1MGH"},
        "lines": [{"material": "4200000027", "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}]}],
    }
    item = _sap_line(item_no="10")
    item.material_group = "MG-LINE"
    err = _enrich_po_form_with_items(form, document_type="YUNB", sap_items=[item])
    assert err is None
    assert form["lines"][0]["material_group"] == "MG-LINE"
    assert form["header"]["material_group"] == "MG-LINE"


def test_apply_sap_pr_item_defaults_sets_vendor_from_fixed_supplier() -> None:
    from app.procurement.po_sap_enrichment import apply_sap_pr_item_defaults_to_header

    header: dict = {}
    item = _sap_line(item_no="10")
    item.fixed_supplier = "1000000002"
    apply_sap_pr_item_defaults_to_header(header, [item])
    assert header["vendor"] == "1000000002"


def test_sap_pr_items_to_po_form_clears_po_text_fields() -> None:
    form = sap_pr_items_to_po_form(
        [_sap_line(item_no="10", unit="KG")],
        document_type="YUNB",
        header_seed={
            "header": {
                "vendor": "1000000002",
                "po_remarks": "from pr",
                "po_deadlines": "from pr",
                "po_terms_of_delivery": "from pr",
            }
        },
    )
    assert form["header"]["po_remarks"] == ""
    assert form["header"]["po_deadlines"] == ""
    assert form["header"]["po_terms_of_delivery"] == ""


def test_sap_pr_items_to_po_form_sets_pr_item_and_unit() -> None:
    form = sap_pr_items_to_po_form(
        [_sap_line(item_no="10", unit="KG")],
        document_type="YUNB",
        header_seed={"header": {"vendor": "1000000002"}},
    )
    assert form["lines"][0]["purchase_requisition_item"] == "10"
    assert form["lines"][0]["order_unit"] == "KG"
    assert form["lines"][0]["unit_price"] == "10.00"
    assert form["lines"][0]["valuation_price"] == "10"


def test_enrich_fixes_cost_center_but_keeps_po_qty() -> None:
    form = {
        "header": {"vendor": "1000000002"},
        "lines": [
            {
                "material": "4200000027",
                "allocations": [{"cost_center": "WRONG", "qty": "99"}],
            }
        ],
    }
    sap_items = [
        _sap_line(item_no="10", cc="HCO91001H0"),
    ]
    err = _enrich_po_form_with_items(form, document_type="YUNB", sap_items=sap_items)
    assert err is None
    assert form["lines"][0]["allocations"] == [{"cost_center": "HCO91001H0", "qty": "99"}]


def _yser_sap_item(
    *,
    item_no: str = "10",
    services: list[tuple[str, str]],
) -> SapPrItemLine:
    """One Z PR item with merged acct rows across services."""
    acct_rows = [
        SapPrAcctRow(seq=str(i + 1), cost_center=cc, quantity="1.000", master_asset="")
        for i, (_svc, cc) in enumerate(services)
    ]
    return SapPrItemLine(
        item_no=item_no,
        sap_pr_item=item_no,
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
        short_text="services",
        service_performer=services[0][0] if services else "",
        acct_rows=acct_rows,
    )


def test_enrich_yser_multi_service_same_pr_item() -> None:
    sap_items = [
        _yser_sap_item(
            services=[
                ("10000000006", "HCO91001H0"),
                ("10000000007", "HCO91001A0"),
            ]
        )
    ]
    form = {
        "header": {"vendor": "1000000002"},
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "order_unit": "EA",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_requisition_item": "10",
                "order_unit": "EA",
                "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            },
        ],
    }
    err = _enrich_po_form_with_items(form, document_type="YSER", sap_items=sap_items)
    assert err is None
    assert form["lines"][0]["allocations"] == [{"cost_center": "HCO91001H0", "qty": "1"}]
    assert form["lines"][1]["allocations"] == [{"cost_center": "HCO91001A0", "qty": "1"}]
    assert form["lines"][0]["purchase_requisition_item"] == "10"
    assert form["lines"][1]["purchase_requisition_item"] == "10"


def test_po_allocations_match_yser_per_pr_item() -> None:
    sap_items = [
        _yser_sap_item(item_no="10", services=[("10000000006", "HCO91001H0")]),
        _yser_sap_item(item_no="20", services=[("10000000007", "HCO91001A0")]),
    ]
    form = {
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_requisition_item": "20",
                "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            },
        ],
    }
    assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None


def test_po_allocations_match_yser_rejects_cc_on_wrong_pr_item() -> None:
    sap_items = [
        _yser_sap_item(item_no="10", services=[("10000000006", "HCO91001H0")]),
        _yser_sap_item(item_no="20", services=[("10000000007", "HCO91001A0")]),
    ]
    form = {
        "lines": [
            {
                "service": "10000000007",
                "purchase_requisition_item": "20",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            }
        ],
    }
    assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items)


def test_po_allocations_match_yser_allows_multi_service_split() -> None:
    sap_items = [
        _yser_sap_item(
            services=[
                ("10000000006", "HCO91001H0"),
                ("10000000007", "HCO91001A0"),
            ]
        )
    ]
    form = {
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            },
        ],
    }
    assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None


def _yser_split_sap_items() -> list[SapPrItemLine]:
    """Shape returned by ``fetch_pr_items`` after Jul 2026 subline fan-out."""
    base = dict(
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
        short_text="services",
    )
    return [
        SapPrItemLine(
            **base,
            item_no="10",
            sap_pr_item="10",
            service_performer="10000000006",
            acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001H0", quantity="1.000", master_asset="")
            ],
        ),
        SapPrItemLine(
            **base,
            item_no="20",
            sap_pr_item="10",
            service_performer="10000000007",
            acct_rows=[
                SapPrAcctRow(seq="1", cost_center="HCO91001A0", quantity="1.000", master_asset="")
            ],
        ),
    ]


def test_po_allocations_match_yser_split_fetch_shape_same_pr_item() -> None:
    sap_items = _yser_split_sap_items()
    form = {
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            },
        ],
    }
    assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items) is None


def test_resolve_yser_po_line_pr_item_split_fetch_shape() -> None:
    sap_items = _yser_split_sap_items()
    line_a = {
        "service": "10000000006",
        "purchase_requisition_item": "10",
        "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
    }
    line_b = {
        "service": "10000000007",
        "purchase_requisition_item": "10",
        "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
    }
    hit_a = _resolve_yser_po_line_pr_item(line_a, sap_items=sap_items)
    hit_b = _resolve_yser_po_line_pr_item(line_b, sap_items=sap_items)
    assert hit_a is not None and hit_a.service_performer == "10000000006"
    assert hit_b is not None and hit_b.service_performer == "10000000007"
    assert hit_a.item_no != hit_b.item_no
    assert hit_a.sap_pr_item == hit_b.sap_pr_item == "10"


def test_enrich_yser_split_fetch_shape_same_pr_item() -> None:
    sap_items = _yser_split_sap_items()
    form = {
        "header": {"vendor": "1000000002"},
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "order_unit": "EA",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_requisition_item": "10",
                "order_unit": "EA",
                "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            },
        ],
    }
    err = _enrich_po_form_with_items(form, document_type="YSER", sap_items=sap_items)
    assert err is None
    assert form["lines"][0]["allocations"] == [{"cost_center": "HCO91001H0", "qty": "1"}]
    assert form["lines"][1]["allocations"] == [{"cost_center": "HCO91001A0", "qty": "1"}]
    assert form["lines"][0]["purchase_requisition_item"] == "10"
    assert form["lines"][1]["purchase_requisition_item"] == "10"


def test_po_allocations_match_yser_rejects_unknown_cc() -> None:
    sap_items = [_yser_sap_item(services=[("10000000006", "HCO91001H0")])]
    form = {
        "lines": [
            {
                "service": "10000000006",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "WRONG", "qty": "1"}],
            }
        ],
    }
    assert po_allocations_match_sap_pr(form, document_type="YSER", sap_items=sap_items)


def test_po_allocations_match_detects_user_edit() -> None:
    form = {
        "lines": [
            {
                "material": "4200000027",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "WRONG", "qty": "1"}],
            }
        ],
    }
    sap_items = [_sap_line(item_no="10", cc="HCO91001H0")]
    assert po_allocations_match_sap_pr(form, document_type="YUNB", sap_items=sap_items)


def test_build_po_payload_uses_purchase_requisition_item() -> None:
    form = sap_pr_items_to_po_form([_sap_line(item_no="10")], document_type="YUNB")
    form["header"]["vendor"] = "1000000002"
    form["header"]["purchasing_org"] = "1MGH"
    payload = build_po_payload(
        form=form,
        document_type="YUNB",
        parent_pr_number="1040000063",
    )
    row = payload["to_PurchaseOrderItem"][0]
    assert row["PurchaseRequisitionItem"] == "00010"


def test_multi_cc_deep_acct_on_create_payload() -> None:
    form = {
        "header": {"vendor": "1000000002", "purchasing_org": "1MGH", "plant": "H001"},
        "lines": [
            {
                "material": "4200000027",
                "delivery_date": "2026-08-15",
                "unit_price": "10",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1"},
                    {"cost_center": "CC2", "qty": "2"},
                ],
            }
        ],
    }
    assert not po_line_needs_acct_post_create(form["lines"][0])
    assert not iter_po_acct_post_bodies(form=form, document_type="YUNB", po_number="4500000123")
    payload = build_po_payload(form=form, document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    inline = row[sap_po_payload.PO_ACCT_CREATE_NAV]
    assert len(inline) == 2
    assert inline[0]["CostCenter"] == "CC1"
    assert row["MultipleAcctAssgmtDistribution"] == "1"
    assert row["AccountAssignmentCategory"] == "K"


def test_single_cc_deep_acct_on_create() -> None:
    form = {
        "header": {"vendor": "1000000002", "purchasing_org": "1MGH", "plant": "H001"},
        "lines": [
            {
                "material": "4200000027",
                "delivery_date": "2026-08-15",
                "unit_price": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    assert not po_line_needs_acct_post_create(form["lines"][0])
    payload = build_po_payload(form=form, document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    assert len(row[sap_po_payload.PO_ACCT_CREATE_NAV]) == 1


@pytest.mark.asyncio
async def test_build_po_prefill_db_fallback_promotes_header_group(monkeypatch) -> None:
    async def _fail_fetch(**_kwargs):
        return [], "sap unavailable"

    monkeypatch.setattr(
        "app.procurement.po_sap_enrichment.fetch_pr_items",
        _fail_fetch,
    )
    pr_form = {
        "header": {"material_group": "HDR-A", "vendor": "1000000002"},
        "lines": [
            {
                "material": "4200000027",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            }
        ],
    }
    form, err = await build_po_prefill_form_from_pr(
        pr_form,
        document_type="YUNB",
        pr_sap_id="1010000999",
        ticket_id="t1",
    )
    assert err == "sap unavailable"
    assert form["lines"][0]["material_group"] == "HDR-A"
    assert form["header"]["material_group"] == "HDR-A"
