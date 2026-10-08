"""Order unit resolution from reference master on save."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_order_unit import apply_line_order_units_from_reference


@pytest.mark.asyncio
async def test_apply_line_order_units_from_reference_lookup() -> None:
    session = AsyncMock()

    async def fake_execute(stmt):
        result = MagicMock()
        result.all.return_value = [
            ("MAT1", "YUNB", "PR", {"base_unit": "EA"}),
            ("SVC1", "YSER", "PR", {"Base Unit of Measure": "NOS"}),
        ]
        return result

    session.execute = fake_execute

    yunb = normalize_form(
        "YUNB",
        {
            "header": {"purchasing_org": "1MGH"},
            "lines": [{**default_empty_block("YUNB"), "material": "MAT1"}],
        },
    )
    apply_procurement_defaults(yunb, document_type="YUNB", kind="PR")
    await apply_line_order_units_from_reference(session, form=yunb, document_type="YUNB")
    assert yunb["lines"][0]["order_unit"] == "EA"

    yser = normalize_form(
        "YSER",
        {
            "header": {"purchasing_org": "1MGH"},
            "lines": [{**default_empty_block("YSER"), "service": "SVC1"}],
        },
    )
    apply_procurement_defaults(yser, document_type="YSER", kind="PR")
    await apply_line_order_units_from_reference(session, form=yser, document_type="YSER")
    assert yser["lines"][0]["order_unit"] == "NOS"


@pytest.mark.asyncio
async def test_apply_line_order_units_prefers_workflow_specific_row() -> None:
    session = AsyncMock()

    async def fake_execute(stmt):
        result = MagicMock()
        result.all.return_value = [
            ("MAT1", "", "PR", {"base_unit": "QT"}),
            ("MAT1", "YUNB", "PR", {"base_unit": "EA"}),
        ]
        return result

    session.execute = fake_execute

    form = normalize_form(
        "YUNB",
        {
            "header": {"purchasing_org": "1MGH"},
            "lines": [{**default_empty_block("YUNB"), "material": "MAT1"}],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PR")
    await apply_line_order_units_from_reference(session, form=form, document_type="YUNB")
    assert form["lines"][0]["order_unit"] == "EA"


@pytest.mark.asyncio
async def test_apply_line_order_units_defaults_ea_without_catalog_code() -> None:
    session = AsyncMock()
    empty = MagicMock()
    empty.all.return_value = []
    session.execute = AsyncMock(return_value=empty)

    form = normalize_form(
        "YSER",
        {"header": {}, "lines": [default_empty_block("YSER")]},
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    await apply_line_order_units_from_reference(session, form=form, document_type="YSER")
    assert form["lines"][0]["order_unit"] == "EA"
