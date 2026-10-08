"""Period-aware ``_resolve_site_async`` (duplicate display names / alias targets)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.agents.o2c_ohc.mis_drafts import _resolve_site_async


def _mapping_rows(dicts: list[dict]) -> MagicMock:
    """Async execute result: .mappings().all() -> RowMapping-like dict rows (``dict(row)`` in app code)."""
    out = MagicMock()
    out.mappings.return_value.all.return_value = [dict(d) for d in dicts]
    return out


@pytest.mark.asyncio
async def test_resolve_display_name_unique_billable_winner() -> None:
    bc = UUID("11111111-1111-1111-1111-111111111111")
    w = UUID("22222222-2222-2222-2222-222222222222")
    l = UUID("33333333-3333-3333-3333-333333333333")
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _mapping_rows([]),
            _mapping_rows(
                [
                    {
                        "service_site_id": w,
                        "billing_client_id": bc,
                        "display_name": "Plant A",
                        "canonical_name": "Plant A",
                        "created_at": datetime(2024, 1, 1),
                    },
                    {
                        "service_site_id": l,
                        "billing_client_id": bc,
                        "display_name": "Plant A",
                        "canonical_name": "Plant A",
                        "created_at": datetime(2024, 6, 1),
                    },
                ]
            ),
        ]
    )

    async def pick(_session, _b, s, *, period_start, period_end, allow_expired_contract_terms=False):
        return {"contract_terms_version_id": "tv"} if s == str(w) else None

    with patch("app.agents.o2c_ohc.mis_drafts._pick_terms_version_async", side_effect=pick):
        row = await _resolve_site_async(
            session, "Plant A", period_start=date(2025, 4, 1), period_end=date(2025, 4, 30)
        )
    assert row is not None
    assert row["service_site_id"] == w


@pytest.mark.asyncio
async def test_resolve_display_name_multi_billable_picks_newest_site() -> None:
    """Duplicate plants each billable for the period → prefer newest ``service_site`` row."""
    bc = UUID("11111111-1111-1111-1111-111111111111")
    older = UUID("22222222-2222-2222-2222-222222222222")
    newer = UUID("33333333-3333-3333-3333-333333333333")
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _mapping_rows([]),
            _mapping_rows(
                [
                    {
                        "service_site_id": older,
                        "billing_client_id": bc,
                        "display_name": "Plant A",
                        "canonical_name": "Plant A",
                        "created_at": datetime(2024, 1, 1),
                    },
                    {
                        "service_site_id": newer,
                        "billing_client_id": bc,
                        "display_name": "Plant A",
                        "canonical_name": "Plant A",
                        "created_at": datetime(2025, 1, 1),
                    },
                ]
            ),
        ]
    )

    async def pick(_session, _b, _s, *, period_start, period_end, allow_expired_contract_terms=False):
        return {"contract_terms_version_id": "tv"}

    with patch("app.agents.o2c_ohc.mis_drafts._pick_terms_version_async", side_effect=pick):
        row = await _resolve_site_async(
            session, "Plant A", period_start=date(2025, 4, 1), period_end=date(2025, 4, 30)
        )
    assert row is not None
    assert row["service_site_id"] == newer
