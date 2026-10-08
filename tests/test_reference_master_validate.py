"""Reference master validation against live DB (skipped when empty)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import PrPoReferenceValue
from app.db.session import AsyncSessionLocal
from app.procurement.reference_master_validate import validate_reference_master


@pytest.mark.asyncio
async def test_validate_reference_master_live_db() -> None:
    async with AsyncSessionLocal() as session:
        n = await session.scalar(select(func.count()).select_from(PrPoReferenceValue))
        if not n:
            pytest.skip("pr_po_reference_values empty")
        report = await validate_reference_master(session)
    assert report.ok, [f"{i.level}:{i.check}:{i.message}" for i in report.issues]
