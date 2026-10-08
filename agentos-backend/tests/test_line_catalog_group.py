"""Per-line catalogue group helpers."""

from __future__ import annotations

from app.procurement.line_catalog_group import (
    catalog_group_field,
    ensure_form_line_catalog_groups,
    line_catalog_group,
    sync_header_catalog_group,
)


def test_catalog_group_field() -> None:
    assert catalog_group_field("YSER") == "service_group"
    assert catalog_group_field("YUNB") == "material_group"


def test_line_catalog_group_prefers_line() -> None:
    block = {"material_group": "LINE-A"}
    header = {"material_group": "HDR-A"}
    assert line_catalog_group(block, header, "YUNB") == "LINE-A"


def test_line_catalog_group_falls_back_to_header() -> None:
    block: dict = {}
    header = {"material_group": "HDR-A"}
    assert line_catalog_group(block, header, "YUNB") == "HDR-A"


def test_validate_form_accepts_legacy_header_group_fallback() -> None:
    from app.procurement.field_schema import validate_form

    form = {
        "header": {"material_group": "M020-0001"},
        "lines": [
            {
                "material": "4000000002",
                "short_text": "line",
                "unit_price": "10",
                "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
            }
        ],
    }
    errs = validate_form(kind="PR", document_type="YUNB", form=form)
    assert not any("Material group" in e for e in errs)


def test_ensure_form_promotes_legacy_header_to_lines() -> None:
    form = {
        "header": {"material_group": "HDR-A"},
        "lines": [{"material": "M1"}, {"material": "M2"}],
    }
    ensure_form_line_catalog_groups(form, "YUNB")
    assert form["lines"][0]["material_group"] == "HDR-A"
    assert form["lines"][1]["material_group"] == "HDR-A"
    assert form["header"]["material_group"] == "HDR-A"


def test_sync_header_from_first_line() -> None:
    header: dict = {"material_group": ""}
    lines = [{"material_group": "LINE-A"}, {"material_group": "LINE-B"}]
    sync_header_catalog_group(header, lines, "YUNB")
    assert header["material_group"] == "LINE-A"
