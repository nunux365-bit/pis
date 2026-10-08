"""Periodic kernel heartbeat — proves APScheduler is running (SQLAlchemy job store in prod)."""

import logging

log = logging.getLogger(__name__)


async def scheduler_heartbeat_job() -> None:
    log.debug("scheduler heartbeat tick")
