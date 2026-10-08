"""``calc_notes_amount`` enrichment from ``calc_notes`` terminal ``final_amount=``."""

from __future__ import annotations

from decimal import Decimal

from app.agents.o2c_ohc.mis_drafts import (
    _apply_calc_notes_amount_to_summary_rows,
    _parse_terminal_final_amount_from_calc_notes,
)


def test_parse_last_final_amount_with_commas() -> None:
    text = "a final_amount=1,000.5 x final_amount: 2000"
    assert _parse_terminal_final_amount_from_calc_notes(text) == Decimal("2000")


def test_apply_calc_notes_amount_fallback_to_final_amount() -> None:
    sj = {
        "summary_rows": [
            {"final_amount": 42.0, "calc_notes": "no marker here"},
        ]
    }
    _apply_calc_notes_amount_to_summary_rows(sj)
    assert sj["summary_rows"][0]["calc_notes_amount"] == 42.0


def test_apply_calc_notes_amount_from_notes() -> None:
    sj = {
        "summary_rows": [
            {
                "final_amount": 99.0,
                "calc_notes": "line_subtotal=50+49 | final_amount=101.25",
            },
        ]
    }
    _apply_calc_notes_amount_to_summary_rows(sj)
    assert sj["summary_rows"][0]["calc_notes_amount"] == 101.25
    assert sj["summary_rows"][0]["final_amount"] == 101.25


def test_apply_calc_notes_amount_prefers_llm_key() -> None:
    """When the model emits ``calc_notes_amount``, do not override from ``calc_notes``."""
    sj = {
        "summary_rows": [
            {
                "final_amount": 99.0,
                "calc_notes_amount": 77.5,
                "calc_notes": "final_amount=101.25",
            },
        ]
    }
    _apply_calc_notes_amount_to_summary_rows(sj)
    assert sj["summary_rows"][0]["calc_notes_amount"] == 77.5
    assert sj["summary_rows"][0]["final_amount"] == 77.5
