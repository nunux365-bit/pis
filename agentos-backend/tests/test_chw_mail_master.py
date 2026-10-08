"""CHW Mail Master: exclusion + RPO contact block (no network)."""

from __future__ import annotations

from app.email_automation.pipeline import chw_mail_master as m


def test_exclusion_yes_triggers():
    rows = [
        {
            m.CHW_COL_AUTO_REMINDER_EXCLUSION: "Yes",
        }
    ]
    assert m.chw_exclusion_skips(rows) is True
    assert m.chw_exclusion_skips([{m.CHW_COL_AUTO_REMINDER_EXCLUSION: "yes"}]) is True
    assert m.chw_exclusion_skips([{m.CHW_COL_AUTO_REMINDER_EXCLUSION: " Yes "}]) is True
    assert m.chw_exclusion_skips([{m.CHW_COL_AUTO_REMINDER_EXCLUSION: "Y e s"}]) is True


def test_exclusion_no_does_not_trigger():
    rows = [{m.CHW_COL_AUTO_REMINDER_EXCLUSION: "No"}]
    assert m.chw_exclusion_skips(rows) is False
    assert m.chw_exclusion_skips([{m.CHW_COL_AUTO_REMINDER_EXCLUSION: ""}]) is False
    assert m.chw_exclusion_skips(()) is False


def test_exclusion_any_row_yes():
    rows = [
        {m.CHW_COL_AUTO_REMINDER_EXCLUSION: "No"},
        {m.CHW_COL_AUTO_REMINDER_EXCLUSION: "Yes"},
    ]
    assert m.chw_exclusion_skips(rows) is True


def test_rpo_dedupes_and_orders():
    rows = [
        {m.CHW_COL_RPO_SPOC: "Nishika", m.CHW_COL_RPO_TEAM_CONTACT: "9329512313"},
        {m.CHW_COL_RPO_SPOC: "Hemant", m.CHW_COL_RPO_TEAM_CONTACT: "9304800300"},
        {m.CHW_COL_RPO_SPOC: "Nishika", m.CHW_COL_RPO_TEAM_CONTACT: "9329512313"},
    ]
    html = m.chw_rpo_contacts_block_html(rows)
    assert "1/" in html and "2/" in html
    assert "Nishika" in html and "Hemant" in html
    assert "9329512313" in html and "9304800300" in html
    assert html.count("Nishika") == 1


def test_rpo_skips_incomplete_row():
    rows = [
        {m.CHW_COL_RPO_SPOC: "A", m.CHW_COL_RPO_TEAM_CONTACT: ""},
        {m.CHW_COL_RPO_SPOC: "", m.CHW_COL_RPO_TEAM_CONTACT: "1"},
    ]
    assert m.chw_rpo_contacts_block_html(rows) == ""


def test_columns_match_with_header_spacing_and_case():
    """Header keys: case/spacing/nbsp variants still resolve to the logical columns."""
    rows = [
        {
            "rpo  spoc": "Ann",
            "RPO  TEAM  CONTACT": "9 3 0 0",
            "auto  -  reminder  exclusion": "No",
        }
    ]
    assert m.chw_exclusion_skips(rows) is False
    html = m.chw_rpo_contacts_block_html(rows)
    assert "Ann" in html
    assert "9300" in html


def test_rpo_html_escapes():
    rows = [
        {m.CHW_COL_RPO_SPOC: "<b>x</b>", m.CHW_COL_RPO_TEAM_CONTACT: "1"},
    ]
    html = m.chw_rpo_contacts_block_html(rows)
    assert "<b>" not in html
    assert "&lt;" in html


def test_tracker_rows_for_key_matches_resolve():
    from app.email_automation.engine import resolver

    tr = [
        {"HANA Code": "8039", "k": 1},
        {"HANA Code": " 8039 ", "k": 2},
        {"HANA Code": "8040", "k": 3},
    ]
    m1 = resolver.tracker_rows_for_key(
        tr, business_key_parts=("8039",), key_columns=("HANA Code",)
    )
    assert len(m1) == 2
    m2 = resolver.tracker_rows_for_key(
        tr, business_key_parts=("8040",), key_columns=("HANA Code",)
    )
    assert len(m2) == 1
