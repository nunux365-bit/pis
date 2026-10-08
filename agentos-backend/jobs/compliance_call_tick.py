"""Scheduled compliance_call batch tick (Drive → graph per file)."""

from __future__ import annotations

import logging

from app.agents.compliance_call.run import run_compliance_batch_async
from app.config.settings import settings

log = logging.getLogger(__name__)


async def compliance_call_tick_job() -> None:
    if not settings.compliance_call_enabled:
        return

    try:
        out = await run_compliance_batch_async()
        if out.get("disabled"):
            return
        if out.get("graph_error"):
            log.warning("compliance_call_tick: %s", out.get("graph_error"))
            return
        if out.get("candidates", 0) or out.get("results"):
            log.info("compliance_call_tick: %s", out.get("message"))
    except Exception:
        log.exception("compliance_call_tick failed")
