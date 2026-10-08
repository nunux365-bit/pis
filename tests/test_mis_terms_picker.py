"""MIS contract_terms_version picker (Phase 3 rules)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.o2c_ohc import mis_drafts
from app.agents.o2c_ohc.terms_picker import pick_from_billable_rows


def _row(ctv_id: str, status: str, *, line_count: int = 1, created_at: datetime | None = None) -> dict:
    return {
        "contract_terms_version_id": ctv_id,
        "billing_client_id": "bc",
        "status": status,
        "billing_profile": "generic",
        "line_count": line_count,
        "created_at": created_at or datetime(2026, 1, 1),
    }


def _session_with_rows(rows: list[dict]) -> MagicMock:
    m = MagicMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    m.execute = AsyncMock(return_value=result)
    return m


def test_pick_from_billable_rows_rules() -> None:
    assert pick_from_billable_rows([]) is None
    assert pick_from_billable_rows([_row("a", "approved")])["contract_terms_version_id"] == "a"
    assert pick_from_billable_rows([_row("p", "pending")])["contract_terms_version_id"] == "p"
    approved_pending = [_row("a", "approved"), _row("p", "pending")]
    assert pick_from_billable_rows(approved_pending)["contract_terms_version_id"] == "a"
    assert pick_from_billable_rows([_row("a", "approved"), _row("b", "approved")]) is None
    two_pending = [
        _row("p1", "pending", line_count=2, created_at=datetime(2026, 1, 1)),
        _row("p2", "pending", line_count=5, created_at=datetime(2026, 2, 1)),
    ]
    assert pick_from_billable_rows(two_pending)["contract_terms_version_id"] == "p2"
    tie = [
        _row("p1", "pending", line_count=3, created_at=datetime(2026, 1, 1)),
        _row("p2", "pending", line_count=3, created_at=datetime(2026, 6, 1)),
    ]
    assert pick_from_billable_rows(tie)["contract_terms_version_id"] == "p2"


def test_pick_pending_tie_breaks_aware_created_at() -> None:
    two_pending = [
        _row("p1", "pending", line_count=3, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        _row("p2", "pending", line_count=3, created_at=datetime(2026, 6, 1)),
    ]
    assert pick_from_billable_rows(two_pending)["contract_terms_version_id"] == "p2"


@pytest.mark.asyncio
async def test_pick_returns_none_when_multiple_approved() -> None:
    rows = [_row("a", "approved"), _row("b", "approved")]
    got = await mis_drafts._pick_terms_version_async(
        _session_with_rows(rows),
        "bc",
        "ss",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert got is None


@pytest.mark.asyncio
async def test_pick_returns_single_pending() -> None:
    row = _row("p", "pending")
    got = await mis_drafts._pick_terms_version_async(
        _session_with_rows([row]),
        "bc",
        "ss",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert got is not None
    assert got["contract_terms_version_id"] == "p"


@pytest.mark.asyncio
async def test_pick_approved_over_pending() -> None:
    rows = [_row("a", "approved"), _row("p", "pending")]
    got = await mis_drafts._pick_terms_version_async(
        _session_with_rows(rows),
        "bc",
        "ss",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert got["contract_terms_version_id"] == "a"
