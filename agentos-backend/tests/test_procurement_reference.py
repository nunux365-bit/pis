"""Procurement reference helpers and field validation."""

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.models import PrPoReferenceValue
from app.procurement.field_schema import (
    default_empty_block,
    header_field_specs,
    normalize_form,
    schema_payload,
    validate_form,
)
from app.procurement.reference_query import (
    assert_searchable_domain,
    asset_public_code,
    clamp_search_limit,
    _material_type_workflow_predicate,
    _search_base_where,
    _storage_location_plant_predicate,
    reference_search_stmt,
    storage_location_sloc_code,
)
from app.procurement.sap_defaults import apply_procurement_defaults


def test_storage_location_sloc_code_parses_composite() -> None:
    assert storage_location_sloc_code("H001|3021") == "3021"
    assert storage_location_sloc_code("3021") == "3021"
    assert storage_location_sloc_code("H001|3021|extra") == "3021|extra"
    assert storage_location_sloc_code("") == ""
    assert storage_location_sloc_code("  H001|3203  ") == "3203"


def test_cost_center_search_facet_business_area_compiles() -> None:
    w = _search_base_where(
        domain="cost_center",
        q="",
        workflow_document_type="YUNB",
        ticket_kind="PR",
        company_code=None,
        cc_entity="1MGH",
        cc_business_area="3021",
    )
    sql = str(
        select(PrPoReferenceValue)
        .where(w)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    low = sql.lower()
    assert "where false" not in low
    assert "extra" in low
    assert "3021" in sql


def test_cost_center_search_plant_union_compiles_and_supersedes_single_ba() -> None:
    """Plant scopes CC via EXISTS on storage_location slocs; ignores legacy cc_business_area."""
    w = _search_base_where(
        domain="cost_center",
        q="HBM",
        workflow_document_type="YSER",
        ticket_kind="PO",
        company_code=None,
        cc_entity="1MGH",
        cc_business_area="3021",
        plant="H001",
    )
    sql = str(
        select(PrPoReferenceValue)
        .where(w)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    low = sql.lower()
    assert "where false" not in low
    assert "exists" in low
    assert "storage_location" in low
    assert "h001" in low
    # Single-sloc BA must not AND with plant-union (would incorrectly narrow eligibility).
    assert "'3021'" not in sql and '"3021"' not in sql


def test_yunb_block_short_text_hidden_from_form_ui() -> None:
    from app.procurement.field_schema import block_field_specs

    specs = block_field_specs("YUNB")
    short = next(f for f in specs if f.key == "short_text")
    assert short.ui_visible is False


def test_yunb_block_material_group_first() -> None:
    from app.procurement.field_schema import block_field_specs

    keys = [f.key for f in block_field_specs("YUNB")]
    assert keys.index("material_group") < keys.index("material")


def test_header_field_order_plant_after_purchasing_org() -> None:
    keys = [f.key for f in header_field_specs("YUNB")]
    assert keys.index("purchasing_org") < keys.index("plant")
    assert keys.index("plant") < keys.index("material_group")
    assert keys.index("material_group") < keys.index("purchasing_group")
    yser_keys = [f.key for f in header_field_specs("YSER")]
    assert yser_keys.index("plant") < yser_keys.index("service_group")


def test_clamp_search_limit() -> None:
    assert clamp_search_limit(None) == 25
    assert clamp_search_limit(25) == 25
    assert clamp_search_limit(999) == 100


def test_assert_searchable_domain() -> None:
    assert_searchable_domain("vendor")
    assert_searchable_domain("cost_center")
    assert_searchable_domain("asset")
    with pytest.raises(ValueError):
        assert_searchable_domain("plant")


def test_asset_public_code_strips_company_prefix() -> None:
    assert asset_public_code("1MGH|003500008160", {"asset_number": "003500008160"}) == "003500008160"
    assert asset_public_code("1MGH|6300000492", None) == "6300000492"


def test_yast_asset_field_is_search() -> None:
    from app.procurement.field_schema import allocation_field_specs, block_field_specs

    asset = next(f for f in block_field_specs("YAST") if f.key == "asset")
    assert asset.widget == "search"
    assert asset.reference_domain == "asset"
    assert asset.ui_visible is False
    alloc_asset = next(f for f in allocation_field_specs("YAST") if f.key == "asset")
    assert alloc_asset.widget == "search"
    assert alloc_asset.required_pr is True


def test_yast_normalize_promotes_line_asset_to_allocation() -> None:
    form = normalize_form(
        "YAST",
        {
            "header": {"material_group": "M020-0001"},
            "lines": [
                {
                    "material": "4000000002",
                    "asset": "003500008160",
                    "short_text": "Asset line",
                    "allocations": [{"qty": "1"}],
                }
            ],
        },
    )
    assert form["lines"][0]["allocations"] == [{"asset": "003500008160", "qty": "1"}]
    assert form["lines"][0]["asset"] == "003500008160"


def test_yast_multi_asset_allocations_preserved() -> None:
    form = normalize_form(
        "YAST",
        {
            "header": {"material_group": "M020-0001"},
            "lines": [
                {
                    "material": "4000000002",
                    "short_text": "Asset multi",
                    "allocations": [
                        {"asset": "003500008160", "qty": "2"},
                        {"asset": "7100001182", "qty": "1"},
                    ],
                }
            ],
        },
    )
    allocs = form["lines"][0]["allocations"]
    assert allocs[0] == {"asset": "003500008160", "qty": "2"}
    assert allocs[1] == {"asset": "7100001182", "qty": "1"}
    assert form["lines"][0]["asset"] == "003500008160"


def _filled_yast_pr_form(*, allocations: list[dict] | None = None) -> dict:
    blk = default_empty_block("YAST")
    blk["material"] = "4000000002"
    blk["material_group"] = "M020-0001"
    blk["short_text"] = "Asset line"
    blk["order_unit"] = "EA"
    blk["unit_price"] = "100"
    blk["valuation_price"] = "100"
    blk["delivery_date"] = "2026-04-20"
    blk["allocations"] = allocations or [
        {"asset": "003500008160", "qty": "1"},
        {"asset": "7100001182", "qty": "1"},
    ]
    n = normalize_form(
        "YAST",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "PROC01",
                "plant": "PL01",
                "storage_location": "PL01|SL01",
                "tax_code": "V18",
                "material_group": "M020-0001",
                "vendor": "",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(n, document_type="YAST", kind="PR")
    return n


def test_yast_rejects_duplicate_asset_in_block() -> None:
    form = _filled_yast_pr_form(
        allocations=[
            {"asset": "003500008160", "qty": "1"},
            {"asset": "3500008160", "qty": "1"},  # same as first, leading zeros differ
        ]
    )
    errs = validate_form(kind="PR", document_type="YAST", form=form)
    assert any("duplicate asset" in e for e in errs)


def test_yast_allows_same_asset_across_blocks() -> None:
    form = _filled_yast_pr_form(
        allocations=[{"asset": "003500008160", "qty": "1"}]
    )
    blk2 = default_empty_block("YAST")
    blk2["material"] = "4000000003"
    blk2["material_group"] = "M020-0001"
    blk2["short_text"] = "Second asset line"
    blk2["order_unit"] = "EA"
    blk2["unit_price"] = "50"
    blk2["valuation_price"] = "50"
    blk2["delivery_date"] = "2026-04-20"
    blk2["allocations"] = [{"asset": "003500008160", "qty": "2"}]
    form["lines"].append(blk2)
    form = normalize_form("YAST", form)
    apply_procurement_defaults(form, document_type="YAST", kind="PR")
    errs = validate_form(kind="PR", document_type="YAST", form=form)
    assert not any("duplicate asset" in e for e in errs)


def test_asset_search_requires_company_code() -> None:
    w = _search_base_where(
        domain="asset",
        q="rack",
        workflow_document_type="YAST",
        ticket_kind="PR",
        company_code=None,
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    assert "where false" in sql.lower()


def test_asset_search_scoped_by_company() -> None:
    w = _search_base_where(
        domain="asset",
        q="0035",
        workflow_document_type="YAST",
        ticket_kind="PR",
        company_code="1MGH",
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    low = sql.lower()
    assert "where false" not in low
    assert "extra" in low
    assert "ilike" in low


def test_asset_search_orders_by_created_on_desc() -> None:
    stmt = reference_search_stmt(
        domain="asset",
        q="",
        workflow_document_type="YAST",
        ticket_kind="PR",
        company_code="1MGH",
        limit=25,
        offset=0,
    )
    sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
    assert "sort_order" not in sql.split("order by", 1)[-1]
    assert " desc" in sql
    assert "pr_po_reference_values.code desc" in sql


def _filled_yunb_po_form(*, vendor: str) -> dict:
    blk = default_empty_block("YUNB")
    blk["material"] = "3000000004"
    blk["short_text"] = "Test line"
    blk["order_unit"] = "EA"
    blk["unit_price"] = "100"
    blk["valuation_price"] = "100"
    blk["delivery_date"] = "2026-04-20"
    blk["allocations"] = [{"cost_center": "HBM11001A0", "qty": "2"}]
    n = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "PROC01",
                "plant": "PL01",
                "storage_location": "PL01|SL01",
                "tax_code": "V18",
                "material_group": "MG003",
                "vendor": vendor,
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(n, document_type="YUNB", kind="PO")
    return n


def test_yunb_po_valid_with_vendor() -> None:
    form = _filled_yunb_po_form(vendor="1|1MGH")
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert errs == []


def test_allocation_qty_required_when_empty() -> None:
    form = _filled_yunb_po_form(vendor="1|1MGH")
    form["lines"][0]["allocations"] = [{"cost_center": "HBM11001A0", "qty": ""}]
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert any("Quantity" in e for e in errs)
    errs_po = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Quantity" in e for e in errs_po)


def test_yunb_pr_requires_unit_price_not_valuation_field() -> None:
    form = _filled_yunb_po_form(vendor="1|1MGH")
    form["lines"][0]["unit_price"] = ""
    form["lines"][0]["valuation_price"] = ""
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert any("Unit price" in e for e in errs)
    assert not any("Valuation" in e for e in errs)


def test_apply_defaults_sets_item_category_yser() -> None:
    blk = default_empty_block("YSER")
    blk["service"] = "SVC001"
    blk["short_text"] = "svc"
    blk["delivery_date"] = "2026-04-20"
    blk["unit_price"] = "50"
    blk["valuation_price"] = "50"
    blk["allocations"] = [{"cost_center": "HBM11001A0", "qty": "1"}]
    n = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "PROC01",
                "plant": "PL01",
                "storage_location": "PL01|SL01",
                "tax_code": "V18",
                "service_group": "S089-0001",
                "vendor": "",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(n, document_type="YSER", kind="PR")
    assert n["lines"][0]["item_category"] == "D"


def test_po_requires_vendor() -> None:
    form = _filled_yunb_po_form(vendor="")
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Vendor" in e for e in errs)


def test_pr_vendor_not_required() -> None:
    form = _filled_yunb_po_form(vendor="")
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert not any("Vendor" in e for e in errs)


def test_schema_payload_includes_sap_max_attempts() -> None:
    payload = schema_payload()
    assert "sap_max_attempts" in payload
    assert isinstance(payload["sap_max_attempts"], int)
    assert payload["sap_max_attempts"] >= 1
    assert "document_types" in payload
    assert len(payload["document_types"]) == 3
    dt0 = payload["document_types"][0]
    assert "block_fields" in dt0
    assert "allocation_fields" in dt0
    assert "po_header_fields" in dt0
    for dt in payload["document_types"]:
        pr_keys = [f["key"] for f in dt["header_fields"]]
        po_keys = [f["key"] for f in dt["po_header_fields"]]
        assert "requestor_email" not in pr_keys
        assert "requestor_email" in po_keys
        assert "header_note" in pr_keys
        assert "header_note" in po_keys
        pr_note = next(f for f in dt["header_fields"] if f["key"] == "header_note")
        po_note = next(f for f in dt["po_header_fields"] if f["key"] == "header_note")
        assert pr_note["max_length"] == 40
        assert po_note["max_length"] == 12
        for key in ("po_remarks", "po_deadlines", "po_terms_of_delivery"):
            assert key not in pr_keys
            assert key in po_keys
        block_keys = [f["key"] for f in dt["block_fields"]]
        assert "tax_code" in block_keys
        assert "tax_code" not in pr_keys
        assert "tax_code" not in po_keys


def test_header_note_max_length_validation() -> None:
    form = _filled_yunb_po_form(vendor="1000000002")
    form["header"]["header_note"] = "x" * 41
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert any("Header note" in e for e in errs)
    form["header"]["header_note"] = "x" * 40
    assert not any("Header note" in e for e in validate_form(kind="PR", document_type="YUNB", form=form))
    form["header"]["header_note"] = "x" * 13
    errs_po = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("12" in e for e in errs_po)
    form["header"]["header_note"] = "x" * 12
    assert not any("Header note" in e for e in validate_form(kind="PO", document_type="YUNB", form=form))


def test_po_text_field_validation() -> None:
    form = _filled_yunb_po_form(vendor="1000000002")
    form["header"]["po_remarks"] = "x" * 2501
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("PO remarks" in e for e in errs)
    form["header"]["po_remarks"] = "ok"
    assert not any("PO remarks" in e for e in validate_form(kind="PO", document_type="YUNB", form=form))


def test_pr_ignores_po_text_fields() -> None:
    form = _filled_yunb_po_form(vendor="1000000002")
    form["header"]["po_remarks"] = "x" * 3000
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert not any("PO remarks" in e for e in errs)


def test_po_requestor_email_validation() -> None:
    form = _filled_yunb_po_form(vendor="1000000002")
    assert not any(
        "Requestor email" in e for e in validate_form(kind="PO", document_type="YUNB", form=form)
    )
    form["header"]["requestor_email"] = "ops@1mg.com"
    assert not validate_form(kind="PO", document_type="YUNB", form=form)
    form["header"]["requestor_email"] = "not-an-email"
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Requestor email" in e for e in errs)
    form["header"]["requestor_email"] = "a" * 18 + "@example.com"
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert not any("Requestor email" in e for e in errs)
    form["header"]["requestor_email"] = "a" * 22 + "@example.com"
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Requestor email" in e for e in errs)


def test_pr_ignores_requestor_email_validation() -> None:
    form = _filled_yunb_po_form(vendor="1000000002")
    form["header"]["requestor_email"] = "not-an-email"
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert not any("Requestor email" in e for e in errs)


def test_service_search_short_code_within_service_group() -> None:
    """Digit / short_code matching applies inside the header service group filter."""
    w = _search_base_where(
        domain="service",
        q="10000000006",
        workflow_document_type="YSER",
        ticket_kind="PR",
        company_code=None,
        service_group="S013-0001",
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    low = sql.lower()
    assert "coalesce" in low
    assert "code" in low and "ilike" in low


def test_material_search_matches_description_extra() -> None:
    w = _search_base_where(
        domain="material",
        q="pantry consumable",
        workflow_document_type="YUNB",
        ticket_kind="PR",
        company_code=None,
        material_group="C016-0001",
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    low = sql.lower()
    assert "extra" in low
    assert "label" in low
    assert "ilike" in low


def _compiled_material_type_sql(workflow_document_type: str) -> str:
    w = _material_type_workflow_predicate(workflow_document_type)
    return str(
        select(PrPoReferenceValue)
        .where(w)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_material_type_predicate_yunb_includes_yunb_only() -> None:
    sql = _compiled_material_type_sql("YUNB")
    assert "IN ('YUNB')" in sql
    assert "YCAP" not in sql


def test_material_type_predicate_yast_includes_yunb_and_ycap() -> None:
    sql = _compiled_material_type_sql("YAST")
    assert "IN ('YUNB', 'YCAP')" in sql


def test_material_search_filters_yunb_product_type() -> None:
    w = _search_base_where(
        domain="material",
        q="pantry",
        workflow_document_type="YUNB",
        ticket_kind="PO",
        company_code=None,
        material_group="C016-0001",
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    low = sql.lower()
    assert "extra" in low
    assert "in (__" in low or "in (" in low


def test_material_search_filters_yast_product_type() -> None:
    w = _search_base_where(
        domain="material",
        q="split",
        workflow_document_type="YAST",
        ticket_kind="PR",
        company_code="1MGH",
        material_group="C035-0001",
    )
    sql = str(
        select(PrPoReferenceValue)
        .where(w)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "IN ('YUNB', 'YCAP')" in sql


def test_storage_location_search_scoped_to_plant() -> None:
    w = _storage_location_plant_predicate("H001")
    sql = str(
        select(PrPoReferenceValue)
        .where(w)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    low = sql.lower()
    assert "ilike" in low
    assert "h001|%" in low
    assert "->>" in sql
    assert "plant" in low


def test_json_text_extracts_compile_for_search_domains() -> None:
    """JSON-with-variant columns must use as_string(), not JSONB-only astext."""
    cases = [
        _search_base_where(
            domain="vendor",
            q="tata",
            workflow_document_type="YSER",
            ticket_kind="PO",
            company_code="1MGH",
        ),
        _search_base_where(
            domain="material",
            q="split",
            workflow_document_type="YUNB",
            ticket_kind="PR",
            company_code=None,
            material_group="C016-0001",
        ),
        _search_base_where(
            domain="asset",
            q="3500",
            workflow_document_type="YAST",
            ticket_kind="PR",
            company_code="1MGH",
        ),
        _search_base_where(
            domain="service",
            q="1000",
            workflow_document_type="YSER",
            ticket_kind="PR",
            company_code=None,
            service_group="C016-0001",
        ),
        _search_base_where(
            domain="storage_location",
            q="",
            workflow_document_type="YUNB",
            ticket_kind="PR",
            company_code=None,
            plant="H001",
        ),
    ]
    for w in cases:
        sql = str(
            select(PrPoReferenceValue)
            .where(w)
            .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "->>" in sql
        assert "astext" not in sql.lower()


def test_yunb_po_requires_tax_code() -> None:
    form = _filled_yunb_po_form(vendor="1|1MGH")
    form["header"]["tax_code"] = ""
    form["lines"][0]["tax_code"] = ""
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Tax code" in e for e in errs)


def test_yser_pr_accepts_decimal_allocation_qty() -> None:
    blk = default_empty_block("YSER")
    blk["service"] = "000000001000000000"
    blk["short_text"] = "svc"
    blk["delivery_date"] = "2026-04-20"
    blk["unit_price"] = "50"
    blk["valuation_price"] = "50"
    blk["allocations"] = [
        {"cost_center": "HBM11001A0", "qty": "0.999"},
        {"cost_center": "HBM11001A1", "qty": "2.001"},
    ]
    form = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "H001|3021",
                "service_group": "S089-0001",
            },
            "lines": [blk],
        },
    )
    errs = validate_form(kind="PR", document_type="YSER", form=form)
    assert errs == []


def test_yast_normalize_strips_cc_on_single_asset_allocation() -> None:
    form = normalize_form(
        "YAST",
        {
            "header": {"material_group": "M020-0001"},
            "lines": [
                {
                    "material": "4000000002",
                    "asset": "003500008160",
                    "short_text": "Asset line",
                    "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
                }
            ],
        },
    )
    assert form["lines"][0]["allocations"] == [{"asset": "003500008160", "qty": "1"}]
    assert "cost_center" not in form["lines"][0]["allocations"][0]


def test_yast_strips_cc_shaped_noise_to_single_line_asset() -> None:
    """YAST never used cost centres in QA — CC-shaped rows collapse to one asset split."""
    form = normalize_form(
        "YAST",
        {
            "header": {"material_group": "M020-0001"},
            "lines": [
                {
                    "material": "4000000002",
                    "asset": "003500008160",
                    "short_text": "Asset line",
                    "allocations": [
                        {"cost_center": "HBM11001A0", "qty": "2"},
                        {"cost_center": "HBM11001A1", "qty": "1"},
                    ],
                }
            ],
        },
    )
    allocs = form["lines"][0]["allocations"]
    assert allocs == [{"asset": "003500008160", "qty": "3"}]
    assert form["lines"][0]["asset"] == "003500008160"
    shell = _filled_yast_pr_form(allocations=[{"asset": "003500008160", "qty": "3"}])
    errs = validate_form(kind="PR", document_type="YAST", form=shell)
    assert not any("duplicate asset" in e for e in errs)



def test_plant_sloc_mismatch_rejected() -> None:
    form = _filled_yunb_po_form(vendor="1|1MGH")
    form["header"]["plant"] = "H001"
    form["header"]["storage_location"] = "0003|0001"
    errs = validate_form(kind="PO", document_type="YUNB", form=form)
    assert any("Storage location" in e for e in errs)


def test_vendor_search_short_query_compiles_to_empty() -> None:
    """Queries shorter than VENDOR_SEARCH_MIN_CHARS must not scan the vendor set."""
    w = _search_base_where(
        domain="vendor",
        q=" ",
        workflow_document_type="YSER",
        ticket_kind="PO",
        company_code=None,
    )
    sql = str(select(PrPoReferenceValue).where(w).compile(dialect=postgresql.dialect()))
    assert "WHERE false" in sql or "where false" in sql.lower()


@pytest.mark.asyncio
async def test_material_search_description_omits_product_type_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from app.procurement.reference_handlers import search_reference_values_payload

    row = MagicMock(spec=PrPoReferenceValue)
    row.code = "5000000000"
    row.label = "1.0 TR 3 STAR INVERTER SPLIT AC"
    row.document_type = ""
    row.applies_to_kind = ""
    row.extra = {"material_type": "YCAP"}

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[row]))))
    )

    monkeypatch.setattr(
        "app.procurement.reference_handlers.reference_search_count",
        AsyncMock(return_value=1),
    )
    payload = await search_reference_values_payload(
        session,
        domain="material",
        document_type="YAST",
        ticket_kind="PR",
        q="split",
        company_code="1MGH",
        limit=25,
        offset=0,
        material_group="C035-0001",
    )

    item = payload["items"][0]
    assert item["code"] == "5000000000"
    assert item["label"] == "1.0 TR 3 STAR INVERTER SPLIT AC [YCAP]"
    assert item["description"] == "1.0 TR 3 STAR INVERTER SPLIT AC"
