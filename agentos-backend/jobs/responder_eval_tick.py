"""Scheduled responder eval batch tick."""

from __future__ import annotations

import logging

from app.agents.responder_eval.tick import run_responder_eval_batch
from app.config.settings import settings

log = logging.getLogger(__name__)


async def responder_eval_tick_job() -> None:
    if not settings.responder_eval_enabled:
        return
    try:
        out = await run_responder_eval_batch()
        # Always log (including empty) so silent stalls are visible.
        log.info("responder_eval_tick: %s", out)
    except Exception:
        log.exception("responder_eval_tick failed")
