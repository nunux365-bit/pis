"""Automation pattern suggestions — reads rule volume when enabled.

Does not train ML models; extend with `automation_rules` analytics when product
defines thresholds and feature stores.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text

from app.config.settings import settings
from app.db.models import AutomationRule
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


async def pattern_analysis_job() -> None:
    if not settings.job_pattern_analysis_enabled:
        return
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            n_rules = await db.scalar(select(func.count()).select_from(AutomationRule))
            n_enabled = await db.scalar(
                select(func.count())
                .select_from(AutomationRule)
                .where(AutomationRule.enabled.is_(True))
            )
        log.info(
            "pattern_analysis_job: automation_rules total=%s enabled=%s",
            int(n_rules or 0),
            int(n_enabled or 0),
        )
    except Exception:
        log.exception("pattern_analysis_job failed")
