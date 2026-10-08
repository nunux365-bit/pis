"""Employee context rebuild — Darwinbox/SAP not assumed.

When `JOB_CONTEXT_REFRESH_ENABLED=true`, records DB health + user row count as a
placeholder until HRIS connectors populate profiles.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text

from app.config.settings import settings
from app.db.models import User
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


async def context_refresh_job() -> None:
    if not settings.job_context_refresh_enabled:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            n_users = await db.scalar(select(func.count()).select_from(User))
        log.info(
            "context_refresh_job: users=%s (no external HR profile sync)",
            int(n_users or 0),
        )
    except Exception:
        log.exception("context_refresh_job failed")
