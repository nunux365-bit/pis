"""Background SAP retry sweep for procurement tickets (live PR when configured).

Each ticket is claimed with ``FOR UPDATE SKIP LOCKED``, SAP HTTP runs, then
the transaction commits so the next ticket is not pinned behind the first.
No process-wide advisory lock.
"""

from __future__ import annotations

import logging

from app.db.session import AsyncSessionLocal
from app.procurement import service as proc_service

log = logging.getLogger(__name__)


async def procurement_sap_retry_job() -> None:
    try:
        async with AsyncSessionLocal() as db:
            n = await proc_service.sap_retry_job_batch(db)
            n_att = await proc_service.attachment_retry_job_batch(db)
            await db.commit()
            if n:
                log.info("procurement_sap_retry_job: attempted SAP for %s ticket(s)", n)
            if n_att:
                log.info("procurement_sap_retry_job: attempted attachments for %s ticket(s)", n_att)
    except Exception:
        log.exception("procurement_sap_retry_job failed")
