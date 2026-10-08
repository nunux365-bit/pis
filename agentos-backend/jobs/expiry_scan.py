"""Expiry / shelf-life risk scan — inventory connector not assumed.

When `JOB_EXPIRY_SCAN_ENABLED=true`, logs catalog size as a stand-in metric.
Wire TrueIn / WMS APIs here when contracts exist.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text

from app.config.settings import settings
from app.db.models import WorkflowDefinition
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


async def expiry_scan_job() -> None:
    if not settings.job_expiry_scan_enabled:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            n_defs = await db.scalar(select(func.count()).select_from(WorkflowDefinition))
        log.info(
            "expiry_scan_job: workflow_definitions_rows=%s (no SKU expiry source connected)",
            int(n_defs or 0),
        )
    except Exception:
        log.exception("expiry_scan_job failed")
