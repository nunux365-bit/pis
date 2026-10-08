"""Daily outreach reply scan for all active campaigns.

Reply rows are unique on ``gmail_message_id``.
"""
from __future__ import annotations

import logging

from app.agents.outreach import run_outreach_replies_async
from app.config.settings import settings

log = logging.getLogger(__name__)


async def outreach_replies_job() -> None:
    if not settings.outreach_enabled:
        return
    await _run_replies()


async def _run_replies() -> None:
    out = await run_outreach_replies_async()
    if out.get("disabled"):
        return
    results = out.get("reply_db_results") or []
    log.info("outreach_replies: classified=%d", len(results))
