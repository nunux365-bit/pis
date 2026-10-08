"""Unit tests for compliance dashboard service (mocked DB)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.compliance_dashboard import (
    _base_where,
    _doctor_clause,
    _score_clauses,
    _search_clause,
    iso_to_ist_display,
    list_compliance_dashboard_runs,
)


def test_iso_to_ist_display_shifts_by_5h30m() -> None:
    assert iso_to_ist_display("2026-07-01T10:00:00+00:00") == "2026-07-01 15:30:00"


def test_iso_to_ist_display_rolls_to_next_ist_day() -> None:
    """19:30Z is already 01:00 the next morning in IST."""
    assert iso_to_ist_display("2026-07-22T19:30:00+00:00") == "2026-07-23 01:00:00"


def test_iso_to_ist_display_treats_naive_as_utc() -> None:
    assert iso_to_ist_display("2026-07-01T10:00:00") == "2026-07-01 15:30:00"


def test_iso_to_ist_display_handles_missing_and_bad_values() -> None:
    assert iso_to_ist_display(None) == ""
    assert iso_to_ist_display("") == ""
    assert iso_to_ist_display("garbage") == "garbage"


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def test_search_clause_includes_second_opinion_conversation_id() -> None:
    clause = _search_clause("724540")
    assert clause is not None
    compiled = str(clause)
    assert "mysql_second_opinion_conversation_id" in compiled or "input_data" in compiled


def test_doctor_clause_matches_name_and_slug() -> None:
    clause = _doctor_clause("sharma")
    assert clause is not None
    compiled = _sql(clause)
    assert "doctor_name" in compiled
    assert "doctor_slug" in compiled
    assert "%sharma%" in compiled


def test_doctor_clause_ignores_blank() -> None:
    assert _doctor_clause(None) is None
    assert _doctor_clause("   ") is None


def test_score_brackets_are_half_open_below_100() -> None:
    """Adjacent presets must not both claim a row sitting on the boundary."""
    lower = [_sql(c) for c in _score_clauses(0, 25)]
    upper = [_sql(c) for c in _score_clauses(25, 50)]
    assert any(">= 0" in c for c in lower)
    assert any("< 25" in c for c in lower)
    assert any(">= 25" in c for c in upper)
    assert any("< 50" in c for c in upper)


def test_top_score_bracket_is_closed_at_100() -> None:
    clauses = [_sql(c) for c in _score_clauses(75, 100)]
    assert any(">= 75" in c for c in clauses)
    assert any("<= 100" in c for c in clauses)


def test_base_where_combines_filters_conjunctively() -> None:
    parts = _base_where(
        since=None,
        until=None,
        status_filter="completed",
        search=None,
        grades=["A", "D+"],
        doctor="sharma",
        score_min=75,
        score_max=100,
        sheet_appended=True,
    )
    compiled = " AND ".join(_sql(p) for p in parts)
    assert "status = 'completed'" in compiled
    assert "grade IN ('A', 'D+')" in compiled
    assert "%sharma%" in compiled
    assert "composite_pct >= 75" in compiled
    assert "sheet_appended IS true" in compiled


def test_base_where_skips_unset_filters() -> None:
    assert _base_where(since=None, until=None, status_filter=None, search=None) == []
    parts = _base_where(
        since=None,
        until=None,
        status_filter=None,
        search=None,
        grades=[],
        doctor="",
        score_min=None,
        score_max=None,
        sheet_appended=None,
    )
    assert parts == []


def test_sheet_appended_false_is_not_treated_as_unset() -> None:
    parts = _base_where(
        since=None, until=None, status_filter=None, search=None, sheet_appended=False
    )
    assert len(parts) == 1
    assert "sheet_appended IS false" in _sql(parts[0])


@pytest.mark.asyncio
async def test_list_compliance_dashboard_runs_empty():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=exec_result)

    runs, total = await list_compliance_dashboard_runs(session, limit=25, offset=0)

    assert total == 0
    assert runs == []
    session.scalar.assert_awaited_once()
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_compliance_dashboard_runs_enriches_second_opinion_conversation_id():
    wr_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    row = MagicMock()
    row.workflow_run_id = wr_id
    row.composite_pct = None
    row.status = "completed"
    row.created_at = now
    row.doctor_slug = "smith"
    row.doctor_name = "Dr Smith"
    row.filename = "call_1.media"
    row.source_file_id = None
    row.source_type = "mysql"
    row.grade = None
    row.grade_label = None
    row.error_message = None
    row.sheet_appended = False

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=1)

    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [row]

    inp_result = MagicMock()
    inp_result.all.return_value = [(wr_id, {"mysql_second_opinion_conversation_id": 40999})]

    session.execute = AsyncMock(side_effect=[list_result, inp_result])

    runs, total = await list_compliance_dashboard_runs(session, limit=25, offset=0)

    assert total == 1
    assert len(runs) == 1
    assert runs[0]["mysql_second_opinion_conversation_id"] == 40999
    assert session.execute.await_count == 2
