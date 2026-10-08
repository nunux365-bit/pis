"""Morning briefing — logs an operational snapshot when enabled.

Transactional email (SendGrid / SES / SMTP) is not configured here; enable only when
`JOB_DAILY_BRIEFING_ENABLED=true` and extend this job to call your provider.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text

from app.config.settings import settings
from app.db.models import User, WorkflowRun
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


async def daily_briefing_job() -> None:
    if not settings.job_daily_briefing_enabled:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            n_users = await db.scalar(select(func.count()).select_from(User))
            n_runs = await db.scalar(select(func.count()).select_from(WorkflowRun))
        log.info(
            "daily_briefing_job: users=%s workflow_runs=%s (no email transport wired)",
            int(n_users or 0),
            int(n_runs or 0),
        )
    except Exception:
        log.exception("daily_briefing_job failed")
