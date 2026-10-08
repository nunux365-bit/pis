"""``service_site`` dedupe at contract ingest (same plant, different ``site_key``)."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from app.agents.o2c_ohc.ingest_helpers import (
    _normalize_site_label_for_dedupe,
    _pick_existing_service_site_from_matches,
    _service_site_label_matches_from_rows,
)


def test_normalize_site_label_unifies_dashes_and_space() -> None:
    a = _normalize_site_label_for_dedupe("ASAL \u2013  Kurali")
    b = _normalize_site_label_for_dedupe("ASAL - Kurali")
    assert a == b == "asal - kurali"


def _row(
    sid: UUID,
    *,
    site_key: str | None = None,
    canonical: str = "",
    display: str | None = None,
    created_at=None,
) -> dict:
    return {
        "id": sid,
        "site_key": site_key,
        "canonical_name": canonical,
        "display_name": display or "",
        "created_at": created_at,
    }


def test_find_existing_matches_display_name_after_normalization() -> None:
    existing = UUID("11111111-1111-1111-1111-111111111111")
    rows = [_row(existing, canonical="Other canonical", display="ASAL \u2013 Kurali")]
    site = {
        "canonical_name": "Unused",
        "display_name": "ASAL - Kurali",
        "service_category": "ohc",
    }
    matches = _service_site_label_matches_from_rows(rows, site)
    assert len(matches) == 1
    assert _pick_existing_service_site_from_matches(matches, incoming_site_key=None, billable_site_ids=set()) == existing


def test_find_existing_matches_canonical_name() -> None:
    existing = UUID("33333333-3333-3333-3333-333333333333")
    rows = [_row(existing, site_key="ipd_chakan", canonical="IPD Chakan", display=None)]
    site = {"canonical_name": "IPD Chakan", "display_name": "", "service_category": "ohc"}
    matches = _service_site_label_matches_from_rows(rows, site)
    assert _pick_existing_service_site_from_matches(matches, incoming_site_key=None, billable_site_ids=set()) == existing


def test_find_existing_returns_none_when_no_match() -> None:
    rows = [_row(UUID("55555555-5555-5555-5555-555555555555"), canonical="A", display="B")]
    site = {"canonical_name": "C", "display_name": "D", "service_category": "ohc"}
    matches = _service_site_label_matches_from_rows(rows, site)
    assert matches == []


def test_find_existing_ambiguous_duplicate_labels_without_period_returns_none() -> None:
    a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    rows = [
        _row(a, site_key="plant_a_cd", canonical="", display="CD – Chakan"),
        _row(b, site_key="plant_b_cd", canonical="", display="CD – Chakan"),
    ]
    site = {
        "canonical_name": "CD Chakan",
        "display_name": "CD - Chakan",
        "service_category": "ohc",
        "site_key": "new_contract_key",
    }
    matches = _service_site_label_matches_from_rows(rows, site)
    assert len(matches) == 2
    assert (
        _pick_existing_service_site_from_matches(matches, incoming_site_key="new_contract_key", billable_site_ids=set())
        is None
    )


def test_find_existing_duplicate_labels_prefers_site_key_overlap() -> None:
    a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    rows = [
        _row(a, site_key="taco_other", canonical="", display="ASAL - Kurali"),
        _row(b, site_key="taco_asal_kurali", canonical="", display="ASAL - Kurali"),
    ]
    site = {
        "canonical_name": "ASAL Kurali",
        "display_name": "ASAL - Kurali",
        "service_category": "ohc",
        "site_key": "taco_asal_kurali",
    }
    matches = _service_site_label_matches_from_rows(rows, site)
    assert _pick_existing_service_site_from_matches(
        matches, incoming_site_key="taco_asal_kurali", billable_site_ids=set()
    ) == b


def test_find_existing_duplicate_labels_prefers_billable_in_period() -> None:
    winner = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    loser = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    rows = [
        _row(winner, site_key="x", canonical="", display="Same", created_at=datetime(2024, 1, 1)),
        _row(loser, site_key="y", canonical="", display="Same", created_at=datetime(2024, 6, 1)),
    ]
    site = {"canonical_name": "Same", "display_name": "Same", "service_category": "ohc", "site_key": "z"}
    matches = _service_site_label_matches_from_rows(rows, site)
    billable = {str(winner)}
    assert (
        _pick_existing_service_site_from_matches(
            matches, incoming_site_key="z", billable_site_ids=billable
        )
        == winner
    )
