"""
Contract ingest: skip ``site_alias`` upserts that would remap an attendance label
already tied to an approved MIS run (any billing period).

Manual / recon APIs are unchanged; only ``ingest_async`` calls this guard.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def approved_mis_service_site_id_for_client_site_key_async(
    session: AsyncSession,
    client_site_key: str,
    *,
    billing_client_id: UUID | str,
) -> str | None:
    """Latest approved MIS ``service_site_id`` for this label under the same billing client."""
    k = (client_site_key or "").strip()
    if not k:
        return None
    r = await session.execute(
        text("""
        SELECT mr.service_site_id::text AS service_site_id
        FROM o2c_mis_run mr
        JOIN service_site ss ON ss.id = mr.service_site_id
        WHERE mr.client_site_key = :csk
          AND mr.status = 'approved'
          AND ss.billing_client_id = CAST(:bc AS uuid)
        ORDER BY mr.updated_at DESC NULLS LAST
        LIMIT 1
        """),
        {"csk": k, "bc": str(billing_client_id)},
    )
    row = r.mappings().first()
    if not row:
        return None
    sid = str(row.get("service_site_id") or "").strip()
    return sid or None


async def should_skip_ingest_site_alias_upsert_async(
    session: AsyncSession,
    *,
    alias_code: str,
    proposed_service_site_id: UUID | str,
    billing_client_id: UUID | str,
) -> bool:
    """True when ingest must not remap a label with approved MIS on another site (same client)."""
    k = (alias_code or "").strip()
    proposed = str(proposed_service_site_id).strip()
    if not k or not proposed:
        return False
    r = await session.execute(
        text("""
        SELECT EXISTS (
            SELECT 1
            FROM o2c_mis_run mr
            JOIN service_site ss ON ss.id = mr.service_site_id
            WHERE mr.client_site_key = :csk
              AND mr.status = 'approved'
              AND ss.billing_client_id = CAST(:bc AS uuid)
              AND mr.service_site_id <> CAST(:proposed AS uuid)
        ) AS blocked
        """),
        {"csk": k, "bc": str(billing_client_id), "proposed": proposed},
    )
    blocked = bool(r.scalar())
    if not blocked:
        return False
    log.info(
        "Ingest site_alias guard: skip remap for label %r (approved MIS on another site, ingest proposed %s)",
        k[:120],
        proposed,
    )
    return True
