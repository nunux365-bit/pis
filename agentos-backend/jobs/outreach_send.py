"""Weekly outreach email dispatch for all active campaigns.

Leads are claimed with ``FOR UPDATE SKIP LOCKED`` in dispatch_campaign.
"""
from __future__ import annotations

import logging

from app.agents.outreach import run_outreach_dispatch_async
from app.config.settings import settings

log = logging.getLogger(__name__)


async def outreach_send_job() -> None:
    if not settings.outreach_enabled:
        return
    await _run_send()


async def _run_send() -> None:
    out = await run_outreach_dispatch_async()
    if out.get("disabled"):
        return
    for r in out.get("dispatch_results") or []:
        if r.get("error"):
            log.warning("outreach_send [%s]: %s", r["campaign"], r["error"])
        else:
            log.info(
                "outreach_send [%s]: sent=%d failed=%d skipped=%d",
                r["campaign"],
                r.get("sent", 0),
                r.get("failed", 0),
                r.get("skipped", 0),
            )
