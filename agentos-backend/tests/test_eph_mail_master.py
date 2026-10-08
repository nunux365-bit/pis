"""ePharma master: KAM + exclusion (no network)."""

from __future__ import annotations

from app.email_automation.pipeline import eph_mail_master as m


def test_exclusion_yes():
    assert m.eph_exclusion_skips([{m.EPH_COL_AUTO_EMAILER_EXCLUSION: "Yes"}]) is True
    assert m.eph_exclusion_skips([{m.EPH_COL_AUTO_EMAILER_EXCLUSION: "Y e s"}]) is True


def test_exclusion_no():
    assert m.eph_exclusion_skips([{m.EPH_COL_AUTO_EMAILER_EXCLUSION: "No"}]) is False


def test_kam_block():
    rows = [
        {
            m.EPH_COL_KAM: "Aryan",
            m.EPH_COL_KAM_CONTACT_NUMBER: "95 82 72 09 08",
        }
    ]
    html = m.eph_kam_contacts_block_html(rows)
    assert "Aryan" in html
    assert "9582720908" in html
    assert "below-given" in html


def test_alternate_header_keys():
    rows = [
        {
            "kam": "X",
            "KAM  CONTACT  NUMBER": "1",
            "auto  emailer  exclusion": "No",
        }
    ]
    assert m.eph_exclusion_skips(rows) is False
    assert "X" in m.eph_kam_contacts_block_html(rows)
