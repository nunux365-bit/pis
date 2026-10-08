"""Daily GSheet → Postgres sync for all active outreach campaigns.

Unique (campaign, email) on insert; concurrent reconciles last-write-wins.
"""
from __future__ import annotations

import logging

from app.agents.outreach import run_outreach_sync_async
from app.config.settings import settings

log = logging.getLogger(__name__)


async def outreach_sync_job() -> None:
    if not settings.outreach_enabled:
        return
    await _run_sync()


async def _run_sync() -> None:
    out = await run_outreach_sync_async()
    if out.get("disabled"):
        return
    for r in out.get("sync_results") or []:
        if r.get("error"):
            log.warning("outreach_sync [%s]: %s", r["campaign"], r["error"])
        else:
            log.info(
                "outreach_sync [%s]: inserted=%d updated=%d",
                r["campaign"],
                r.get("inserted", 0),
                r.get("updated", 0),
            )
