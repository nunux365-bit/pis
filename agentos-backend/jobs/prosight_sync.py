"""Prosight daily sync job — fetches data from Databricks and stores in PostgreSQL."""

import logging

from app.agents.optimus.prosight.databricks_sync import sync_prosight_from_databricks
from app.agents.optimus.prosight.service import is_databricks_configured

log = logging.getLogger(__name__)


async def prosight_sync_job() -> None:
    """Scheduled job to sync Prosight data from Databricks.

    Runs daily at the configured hour (default 11 AM IST).
    Skips if Databricks is not configured.
    """
    if not is_databricks_configured():
        log.debug("Prosight sync skipped: Databricks not configured")
        return

    log.info("Prosight sync job starting")
    result = await sync_prosight_from_databricks()

    if result["status"] == "success":
        log.info(
            "Prosight sync completed: snapshot_id=%s, date=%s",
            result.get("snapshot_id"),
            result.get("snapshot_date"),
        )
    elif result["status"] == "skipped":
        # An admin sync was already running. Not a failure — the data is being
        # refreshed by that run, so this must not page anyone.
        log.info("Prosight sync job skipped: %s", result.get("reason"))
    else:
        log.error("Prosight sync failed: %s", result.get("error"))
