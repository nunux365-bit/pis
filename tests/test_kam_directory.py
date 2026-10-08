"""KAM directory loader — sheet parsing, join, and defensive fallbacks.

Pure unit tests: the Google Sheets reader is monkeypatched, so no network / SA
credentials are required.
"""

from __future__ import annotations

import pytest

from app.email_automation import kam_directory
from app.email_automation.sheets_sa import SheetTable


def _patch_tables(monkeypatch, tables: dict[str, SheetTable]) -> None:
    """Route ``read_table`` by tab name; raise for any tab not provided.

    Pins the configured tab names to the canonical test keys so the test is
    independent of the live ``settings.kam_directory_*_tab`` defaults.
    """

    monkeypatch.setattr(kam_directory.settings, "kam_directory_mapping_tab", "KAM Client Mapping")
    monkeypatch.setattr(kam_directory.settings, "kam_directory_emails_tab", "KAM_EmailIDs")

    def fake_read_table(sheet_id, tab, **kwargs):
        if tab in tables:
            return tables[tab]
        raise KeyError(f"missing tab {tab}")

    monkeypatch.setattr(kam_directory.sheets_sa, "read_table", fake_read_table)
    kam_directory.clear_cache()


def test_joins_mapping_with_emails(monkeypatch):
    _patch_tables(
        monkeypatch,
        {
            "KAM Client Mapping": SheetTable(
                headers=("Name", "HANA Code"),
                rows=(("Asha Rao", "H100"), ("Asha Rao", "H101"), ("Vik Shah", "H200")),
            ),
            "KAM_EmailIDs": SheetTable(
                headers=("KAM", "Email"),
                rows=(("Asha Rao", "asha.rao@1mg.com"), ("Vik Shah", "vik.shah@1mg.com")),
            ),
        },
    )

    d = kam_directory.load_kam_directory()

    assert not d.is_empty
    asha = d.kam_for_hana("H100")
    assert asha is not None and asha.email == "asha.rao@1mg.com"
    # HANA is matched case-insensitively / upper-cased.
    assert d.kam_for_hana("h101") is asha
    assert d.is_kam_email("ASHA.RAO@1mg.com") is True
    assert d.is_kam_email("invoices@1mg.com") is False
    assert sorted(d.hanas_for_key("asha.rao@1mg.com")) == ["H100", "H101"]


def test_real_sheet_shape(monkeypatch):
    """Mirrors the live sheet: 'Mail IDs' header, trailing commas, 'KAM: X' separator rows."""

    monkeypatch.setattr(kam_directory.settings, "kam_directory_mapping_tab", "KAM Client Mapping")
    monkeypatch.setattr(kam_directory.settings, "kam_directory_emails_tab", "KAM EmailIDs")

    def fake_read_table(sheet_id, tab, **kwargs):
        if tab == "KAM Client Mapping":
            return SheetTable(
                headers=("KAM Name", "Client Name", "Hana Code"),
                rows=(
                    ("KAM: Adarsh",),                              # separator row → skipped
                    ("Adarsh", "KAM Total", ""),                   # subtotal row → no HANA
                    ("Adarsh", "Cinedigm India None", "1000001498"),
                    ("Adarsh", "NELCO LIMITED None", "8117"),
                ),
            )
        if tab == "KAM EmailIDs":
            return SheetTable(
                headers=("KAM Name", "Mail IDs"),
                rows=(("Adarsh", "adarsh.singh1@1mg.com,"),),      # trailing comma
            )
        raise KeyError(tab)

    monkeypatch.setattr(kam_directory.sheets_sa, "read_table", fake_read_table)
    kam_directory.clear_cache()

    d = kam_directory.load_kam_directory()
    # Only the real KAM exists — no "KAM: Adarsh" separator junk.
    assert {k.name for k in d.kams_by_key.values()} == {"Adarsh"}
    adarsh = d.kam_for_hana("1000001498")
    assert adarsh is not None and adarsh.email == "adarsh.singh1@1mg.com"  # comma stripped
    assert d.is_kam_email("adarsh.singh1@1mg.com") is True
    assert sorted(d.hanas_for_key("adarsh.singh1@1mg.com")) == ["1000001498", "8117"]


def test_multiple_emails_in_one_cell(monkeypatch):
    _patch_tables(
        monkeypatch,
        {
            "KAM Client Mapping": SheetTable(
                headers=("Name", "HANA"), rows=(("Asha Rao", "H100"),),
            ),
            "KAM_EmailIDs": SheetTable(
                headers=("Name", "Mail IDs"),
                rows=(("Asha Rao", "asha.rao@1mg.com, asha.r@1mg.com"),),
            ),
        },
    )
    d = kam_directory.load_kam_directory()
    kam = d.kam_for_hana("H100")
    assert kam is not None and kam.email == "asha.rao@1mg.com"  # first = primary
    # Both addresses are recognised as the KAM's own.
    assert d.is_kam_email("asha.rao@1mg.com") is True
    assert d.is_kam_email("asha.r@1mg.com") is True


def test_multiple_codes_in_one_cell(monkeypatch):
    _patch_tables(
        monkeypatch,
        {
            "KAM Client Mapping": SheetTable(
                headers=("Name", "Business Keys"),
                rows=(("Asha Rao", "H100, H101 ; H102"),),
            ),
            "KAM_EmailIDs": SheetTable(
                headers=("Name", "Email"),
                rows=(("Asha Rao", "asha.rao@1mg.com"),),
            ),
        },
    )

    d = kam_directory.load_kam_directory()
    assert sorted(d.hanas_for_key("asha.rao@1mg.com")) == ["H100", "H101", "H102"]


def test_missing_emails_tab_degrades_gracefully(monkeypatch):
    # Only the mapping tab is available; the KAM still resolves (no email).
    _patch_tables(
        monkeypatch,
        {
            "KAM Client Mapping": SheetTable(
                headers=("Name", "HANA"),
                rows=(("Asha Rao", "H100"),),
            ),
        },
    )

    d = kam_directory.load_kam_directory()
    kam = d.kam_for_hana("H100")
    assert kam is not None and kam.name == "Asha Rao" and kam.email == ""
    # No emails on file → key falls back to the normalized name.
    assert kam.key == "asharao"
    assert d.is_kam_email("asha.rao@1mg.com") is False


def test_unknown_headers_yield_empty(monkeypatch):
    _patch_tables(
        monkeypatch,
        {
            "KAM Client Mapping": SheetTable(
                headers=("ColA", "ColB"),
                rows=(("x", "y"),),
            ),
        },
    )
    assert kam_directory.load_kam_directory().is_empty


def test_malformed_sheet_id_returns_empty(monkeypatch):
    monkeypatch.setattr(kam_directory.settings, "kam_directory_sheet_id", "bad id!")
    kam_directory.clear_cache()
    assert kam_directory.load_kam_directory().is_empty
