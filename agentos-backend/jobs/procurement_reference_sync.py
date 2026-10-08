"""Nightly SAP full master-data sync → ``pr_po_reference_values``."""

from __future__ import annotations

import logging

from app.config.settings import settings
from app.procurement.reference_sync.sync import run_reference_sync_full

log = logging.getLogger(__name__)


async def procurement_reference_sync_job() -> None:
    if not settings.procurement_reference_sync_enabled:
        return
    if not (settings.procurement_sap_base_url or "").strip():
        log.debug("procurement_reference_sync: SAP not configured, skipping")
        return

    try:
        report = await run_reference_sync_full()
        for d in report.domains:
            if d.error:
                log.error(
                    "procurement_reference_sync %s FAILED: %s",
                    d.domain,
                    d.error,
                )
            else:
                log.info(
                    "procurement_reference_sync %s ok fetched=%s ins=%s upd=%s",
                    d.domain,
                    d.fetched,
                    d.inserted,
                    d.updated,
                )
        if not report.ok:
            log.error("procurement_reference_sync: one or more domains failed")
    except Exception:
        log.exception("procurement_reference_sync_job failed")
