"""Excel reader — header detection, NBSP normalization, duplicate headers."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.email_automation.engine.excel_reader import (
    HeaderDetectionConfig,
    detect_header_row,
    read_sheet,
)


HEADER_HINTS = (
    "hana code",
    "name-hana",
    "business unit",
    "invoice no",
    "invoice date",
    "net amount pending",
    "remarks",
    "0-1 months",
)


def _write_xlsx(tmp_path: Path, rows_per_sheet: dict[str, list[list[object]]]) -> Path:
    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)
    for sheet_name, rows in rows_per_sheet.items():
        ws = wb.create_sheet(title=sheet_name)
        for r in rows:
            ws.append(r)
    path = tmp_path / "test.xlsx"
    wb.save(path)
    return path


def test_detect_header_row_prefers_hint_matches_over_title_row():
    cfg = HeaderDetectionConfig(
        hints=frozenset({"hana code", "business unit", "invoice no"})
    )
    rows = [
        ["Receivable Report — As of 31-Mar-2026", None, None],  # title row (score ~1)
        ["HANA Code", "Business Unit", "Invoice No"],           # header row (score 6)
        ["H001", "e-Pharmacy", "INV-1"],
    ]
    assert detect_header_row(rows, cfg) == 1


def test_detect_header_row_defaults_to_zero_when_no_matches():
    cfg = HeaderDetectionConfig(hints=frozenset({"utterly_unknown"}))
    rows = [["A", "B"], ["x", "y"]]
    assert detect_header_row(rows, cfg) == 0


def test_read_sheet_normalizes_nbsp_and_whitespace_in_headers(tmp_path):
    path = _write_xlsx(
        tmp_path,
        {
            "Sheet1": [
                ["Receivable Report", None, None, None],  # title row
                ["HANA\u00a0Code", " Business  Unit ", "Invoice No", "Net Amount Pending"],
                ["H001", "e-Pharmacy", "INV-1", 1234.56],
            ]
        },
    )
    data = read_sheet(path, "Sheet1", header_hints=HEADER_HINTS)
    assert data.header_row_index == 1
    assert "hana code#0" in data.canonical_keys
    assert "business unit#0" in data.canonical_keys
    assert "net amount pending#0" in data.canonical_keys
    assert len(data.rows) == 1
    assert data.rows[0]["hana code#0"] == "H001"
    assert data.rows[0]["net amount pending#0"] == 1234.56


def test_read_sheet_makes_duplicate_headers_addressable(tmp_path):
    path = _write_xlsx(
        tmp_path,
        {
            "T(labs)": [
                ["HANA Code", "Remarks", "Remarks", "Net Amount Pending"],
                ["H001", "Invoice", "Positive", 1000],
                ["H002", "Invoice", "Negative", 2000],
            ]
        },
    )
    data = read_sheet(path, "T(labs)", header_hints=HEADER_HINTS + ("remarks",))
    assert "remarks#0" in data.canonical_keys
    assert "remarks#1" in data.canonical_keys
    assert data.rows[0]["remarks#0"] == "Invoice"
    assert data.rows[0]["remarks#1"] == "Positive"
    assert data.rows[1]["remarks#1"] == "Negative"


def test_read_sheet_skips_fully_blank_rows(tmp_path):
    path = _write_xlsx(
        tmp_path,
        {
            "S": [
                ["HANA Code", "Remarks"],
                ["H001", "x"],
                [None, None],
                ["", ""],
                ["H002", "y"],
            ]
        },
    )
    data = read_sheet(path, "S", header_hints=HEADER_HINTS)
    assert len(data.rows) == 2


def test_read_sheet_raises_on_missing_sheet(tmp_path):
    path = _write_xlsx(tmp_path, {"Only": [["HANA Code"], ["H1"]]})
    with pytest.raises(KeyError):
        read_sheet(path, "NotThere", header_hints=HEADER_HINTS)


def test_read_sheet_casefold_fallback_matches_sheet_name(tmp_path):
    path = _write_xlsx(tmp_path, {" T(labs) ": [["HANA Code"], ["H1"]]})
    # Note trailing/leading whitespace in sheet name — casefold fallback finds it.
    data = read_sheet(path, "t(labs)", header_hints=HEADER_HINTS)
    assert data.rows[0]["hana code#0"] == "H1"


def test_read_sheet_matches_when_spaces_and_case_differ_from_config(tmp_path):
    """Finance tabs often add spaces around ``&`` and vary title-case."""
    physical = "Invoice details-H(All) & T(PSP)"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Remarks", "Consider", "Ageing type", "Final Amount Pending"],
                ["H1", "e-Pharmacy", "Invoice", "Positive", "0-1 Months", 100],
            ],
        },
    )
    data = read_sheet(
        path,
        "Invoice details-H(all)&T(Psp)",
        header_hints=HEADER_HINTS + ("consider", "ageing type", "final amount"),
    )
    assert data.sheet_name == physical
    assert data.rows[0]["hana code#0"] == "H1"


def test_read_sheet_matches_tab_without_brackets_to_pack_with_parentheses(tmp_path):
    """Loose fingerprint ignores ``()`` so finance can omit parentheses around All / Psp."""
    physical = "Invoice details-H All & T PSP"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Remarks"],
                ["H1", "e-Pharmacy", "Invoice"],
            ],
        },
    )
    data = read_sheet(
        path,
        "Invoice details-H(all)&T(Psp)",
        header_hints=HEADER_HINTS,
    )
    assert data.sheet_name == physical


def test_read_sheet_loose_match_treats_hyphen_as_optional(tmp_path):
    """Loose key drops hyphens so a space- or word-style tab matches the pack hyphen form."""
    physical = "Invoice details H All & T PSP"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Remarks"],
                ["H1", "e-Pharmacy", "Invoice"],
            ],
        },
    )
    data = read_sheet(
        path,
        "Invoice details-H(all)&T(Psp)",
        header_hints=HEADER_HINTS,
    )
    assert data.sheet_name == physical


def test_read_sheet_loose_ambiguous_picks_closest_strict_match(tmp_path):
    """When two tabs share a loose fingerprint, prefer the stricter string match."""

    path = _write_xlsx(
        tmp_path,
        {
            "ReAB": [["HANA Code"], ["H1"]],
            "Re-A-B": [["HANA Code"], ["H2"]],
        },
    )
    data = read_sheet(path, "Re-A-B", header_hints=HEADER_HINTS)
    assert data.sheet_name == "Re-A-B"
    assert data.rows[0]["hana code#0"] == "H2"


def test_read_sheet_matches_en_dash_instead_of_hyphen_in_tab_name(tmp_path):
    """Excel sometimes stores en dash (U+2013) where the pack uses hyphen-minus."""
    physical = "Invoice details\u2013H(All) & T(PSP)"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Remarks"],
                ["H1", "e-Pharmacy", "Invoice"],
            ],
        },
    )
    data = read_sheet(
        path,
        "Invoice details-H(all)&T(Psp)",
        header_hints=HEADER_HINTS,
    )
    assert data.sheet_name == physical
    assert data.rows[0]["hana code#0"] == "H1"


def test_read_sheet_matches_truncated_31_char_tab_as_prefix_of_config(tmp_path):
    """Excel caps sheet names at 31 chars; trailing NBSPs pad length without changing the fingerprint."""
    # Norm key is strict prefix of pack key ``invoicedetails-h(all)&t(psp)``; tab is exactly 31 chars.
    physical = "Invoice details-H(all) & T(Ps\u00a0\u00a0"
    assert len(physical) == 31
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Remarks"],
                ["H1", "e-Pharmacy", "Invoice"],
            ],
        },
    )
    data = read_sheet(
        path,
        "Invoice details-H(all)&T(Psp)",
        header_hints=HEADER_HINTS,
    )
    assert data.sheet_name == physical


def test_read_sheet_receivable_as_on_resolves_dated_tab_name(tmp_path):
    physical = "Receivable as on 27.05.2026"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                [],
                [],
                [],
                [
                    "Entity Name", "Type", "Code", "Name of the party",
                    "Business Unit", "Name of Business owner", "Net Due",
                ],
                ["1MGT", "OTI", "H001", "Party A", "e-Diagnostic-Aggregator",
                 "Ashyin Thakral/Prateek verma", 1000],
            ],
        },
    )
    data = read_sheet(
        path,
        "Receivable as on",
        header_hints=(
            "entity name", "type", "code", "name of the party",
            "business unit", "name of business owner", "net due",
        ),
    )
    assert data.sheet_name == physical
    assert data.rows[0]["code#0"] == "H001"


def test_read_sheet_party_wise_ageing_matches_without_h_and_t_suffix(tmp_path):
    """Workbook tab ``Party wise Ageing`` resolves when pack asks for ``…-H&T``."""
    physical = "Party wise Ageing"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["Code", "Business Unit", "Unaccounted receipts"],
                ["H1", "e-Pharmacy", 50],
            ],
        },
    )
    data = read_sheet(
        path,
        "Party wise Ageing-H&T",
        header_hints=("code", "business unit", "unaccounted"),
    )
    assert data.sheet_name == physical
    assert data.rows[0]["code#0"] == "H1"


def test_read_sheet_legacy_invoice_tabs_resolve_to_merged_h_and_t(tmp_path):
    """Jun-2026 format: legacy H(all)&T(Psp) / T(labs) names → ``Invoice details-H&T``."""
    physical = "Invoice details-H&T"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Ageing type", "Final Amount Pending"],
                ["H1", "e-Pharmacy", "0-1 Months", 100],
            ],
        },
    )
    for configured in (
        "Invoice details-H(all)&T(Psp)",
        "Invoice details-T(labs)",
    ):
        data = read_sheet(
            path,
            configured,
            header_hints=("hana code", "business unit", "ageing type"),
        )
        assert data.sheet_name == physical
        assert data.rows[0]["hana code#0"] == "H1"


def test_read_sheet_partywise_ageing_h_and_t_spacing_alias(tmp_path):
    physical = "Partywise Ageing H & T"
    path = _write_xlsx(
        tmp_path,
        {
            physical: [
                ["HANA Code", "Business Unit", "Unaccounted receipts"],
                ["H1", "e-Pharmacy", 50],
            ],
        },
    )
    data = read_sheet(
        path,
        "Party wise Ageing-H&T",
        header_hints=("hana code", "business unit", "unaccounted"),
    )
    assert data.sheet_name == physical
    assert data.rows[0]["hana code#0"] == "H1"
