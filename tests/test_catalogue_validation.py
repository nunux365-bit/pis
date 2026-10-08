"""Catalogue-scoped validation and reference filters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.procurement.catalogue_validation import validate_form_against_catalogue
from app.procurement.field_schema import normalize_form
from app.procurement.reference_query import (
    storage_location_matches_plant,
    workflow_material_product_types,
)
from app.procurement.tax_code_allowlist import tax_code_allowlist_codes

YCAP_MATERIAL = "5000000000"
YUNB_MATERIAL = "4000000002"
YCAP_EXTRA = {"material_type": "YCAP", "material_group": "C035-0001"}
YUNB_EXTRA = {"material_type": "YUNB", "material_group": "C016-0001"}


def test_workflow_material_product_types() -> None:
    assert workflow_material_product_types("YUNB") == ("YUNB",)
    assert workflow_material_product_types("YAST") == ("YUNB", "YCAP")
    assert workflow_material_product_types("YSER") is None


def test_storage_location_matches_plant() -> None:
    assert storage_location_matches_plant(storage_location="H001|3021", plant="H001")
    assert not storage_location_matches_plant(storage_location="0003|0001", plant="H001")
    assert not storage_location_matches_plant(storage_location="3021", plant="H001")


def _mock_material_catalogue_session(extra_by_code: dict[str, dict | None]) -> AsyncMock:
    session = AsyncMock()

    async def _execute(stmt):  # noqa: ANN001 — SQLAlchemy statement
        result = MagicMock()
        compiled = stmt.compile(dialect=postgresql.dialect())
        domain = compiled.params.get("domain_1")
        code = compiled.params.get("code_1")
        if domain == "material" and code:
            result.scalar_one_or_none.return_value = extra_by_code.get(code)
        else:
            result.scalar_one_or_none.return_value = None
        return result

    session.execute = _execute
    return session


def _material_line_form(*, document_type: str, material: str) -> dict:
    allowed_tax = sorted(tax_code_allowlist_codes())[0]
    blk: dict = {
        "material": material,
        "short_text": "Test material line",
        "delivery_date": "2026-04-20",
        "unit_price": "100",
        "valuation_price": "100",
        "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
    }
    if document_type == "YAST":
        blk["asset"] = "10000001"
    return normalize_form(
        document_type,
        {
            "header": {
                "tax_code": allowed_tax,
                "material_group": YCAP_EXTRA["material_group"]
                if material == YCAP_MATERIAL
                else YUNB_EXTRA["material_group"],
            },
            "lines": [blk],
        },
    )


@pytest.mark.asyncio
async def test_validate_yast_accepts_ycap_material() -> None:
    form = _material_line_form(document_type="YAST", material=YCAP_MATERIAL)
    errs = await validate_form_against_catalogue(
        session=_mock_material_catalogue_session({YCAP_MATERIAL: YCAP_EXTRA}),
        kind="PR",
        document_type="YAST",
        form=form,
    )
    assert not any("SAP type" in e for e in errs)


@pytest.mark.asyncio
async def test_validate_yast_accepts_yunb_material() -> None:
    form = _material_line_form(document_type="YAST", material=YUNB_MATERIAL)
    errs = await validate_form_against_catalogue(
        session=_mock_material_catalogue_session({YUNB_MATERIAL: YUNB_EXTRA}),
        kind="PR",
        document_type="YAST",
        form=form,
    )
    assert not any("SAP type" in e for e in errs)


@pytest.mark.asyncio
async def test_validate_yunb_rejects_ycap_material() -> None:
    form = _material_line_form(document_type="YUNB", material=YCAP_MATERIAL)
    errs = await validate_form_against_catalogue(
        session=_mock_material_catalogue_session({YCAP_MATERIAL: YCAP_EXTRA}),
        kind="PR",
        document_type="YUNB",
        form=form,
    )
    assert any(
        "SAP type YCAP" in e and "use a YUNB material" in e for e in errs
    ), errs


def _cc_form(*, document_type: str, plant: str, sloc: str, org: str = "1MGH") -> dict:
    allowed_tax = sorted(tax_code_allowlist_codes())[0]
    return normalize_form(
        document_type,
        {
            "header": {
                "tax_code": allowed_tax,
                "purchasing_org": org,
                "plant": plant,
                "storage_location": sloc,
                "material_group": YUNB_EXTRA["material_group"] if document_type == "YUNB" else "",
                "service_group": "S089-0001" if document_type == "YSER" else "",
            },
            "lines": [
                {
                    "material": YUNB_MATERIAL if document_type == "YUNB" else "",
                    "service": "000000001000000000" if document_type == "YSER" else "",
                    "short_text": "CC plant scope line",
                    "delivery_date": "2026-04-20",
                    "unit_price": "100",
                    "valuation_price": "100",
                    "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_validate_yunb_cost_center_uses_plant_not_selected_sloc() -> None:
    """CC valid for another sloc under the same plant must pass when plant is set."""
    form = _cc_form(document_type="YUNB", plant="H001", sloc="H001|3021")

    with (
        patch(
            "app.procurement.catalogue_validation._cost_center_matches_org",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.procurement.catalogue_validation._cost_center_matches_plant",
            new_callable=AsyncMock,
            return_value=True,
        ) as plant_match,
        patch(
            "app.procurement.catalogue_validation._cost_center_matches_business_area",
            new_callable=AsyncMock,
            return_value=False,
        ) as sloc_match,
    ):
        errs = await validate_form_against_catalogue(
            session=_mock_material_catalogue_session({YUNB_MATERIAL: YUNB_EXTRA}),
            kind="PR",
            document_type="YUNB",
            form=form,
        )

    plant_match.assert_awaited()
    sloc_match.assert_not_awaited()
    assert not any("storage location" in e.lower() for e in errs)
    assert not any("not valid for plant" in e for e in errs)


@pytest.mark.asyncio
async def test_validate_yser_rejects_cost_center_outside_plant() -> None:
    form = _cc_form(document_type="YSER", plant="H001", sloc="H001|3497")

    with (
        patch(
            "app.procurement.catalogue_validation._cost_center_matches_org",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.procurement.catalogue_validation._cost_center_matches_plant",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        errs = await validate_form_against_catalogue(
            session=AsyncMock(),
            kind="PO",
            document_type="YSER",
            form=form,
        )

    assert any("not valid for plant H001" in e for e in errs), errs


@pytest.mark.asyncio
async def test_validate_yast_skips_cost_center_plant_scope() -> None:
    form = _material_line_form(document_type="YAST", material=YUNB_MATERIAL)
    form["header"]["purchasing_org"] = "1MGH"
    form["header"]["plant"] = "H001"
    form["lines"][0]["allocations"] = [{"cost_center": "HBM11001A0", "qty": "1"}]

    with patch(
        "app.procurement.catalogue_validation._cost_center_matches_plant",
        new_callable=AsyncMock,
        return_value=False,
    ) as plant_match:
        errs = await validate_form_against_catalogue(
            session=_mock_material_catalogue_session({YUNB_MATERIAL: YUNB_EXTRA}),
            kind="PR",
            document_type="YAST",
            form=form,
        )

    plant_match.assert_not_awaited()
    assert not any("cost centre" in e.lower() for e in errs)


@pytest.mark.asyncio
async def test_cost_center_json_filters_compile() -> None:
    from app.procurement.catalogue_validation import (
        _cost_center_matches_business_area,
        _cost_center_matches_org,
    )

    captured: list[str] = []

    async def _execute(stmt):  # noqa: ANN001 — SQLAlchemy statement
        captured.append(str(stmt.compile(dialect=postgresql.dialect())))
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session = AsyncMock()
    session.execute = _execute
    await _cost_center_matches_business_area(session, cc="HBM11001A0", business_area="3021")
    await _cost_center_matches_org(session, cc="HBM11001A0", org="1MGH")
    assert captured
    for sql in captured:
        assert "->>" in sql
