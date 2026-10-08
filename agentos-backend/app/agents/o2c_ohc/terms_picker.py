"""Billable contract_terms_version selection for MIS drafts (Phase 3 picker rules)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_BILLABLE_TERMS_SQL = """
SELECT
    ctv.id AS contract_terms_version_id,
    ctv.billing_client_id,
    ctv.status,
    COALESCE(ctv.billing_profile, 'generic') AS billing_profile,
    ctv.created_at,
    count(crl.id)::int AS line_count
FROM contract_terms_version ctv
JOIN contract_rate_line crl ON crl.contract_terms_version_id = ctv.id
    AND crl.is_active = true
    AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
WHERE ctv.billing_client_id = CAST(:bc AS uuid)
  AND ctv.status IN ('approved', 'pending')
  AND ctv.superseded_by_id IS NULL
  AND ctv.effective_from <= CAST(:pe AS date)
  AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
GROUP BY ctv.id, ctv.billing_client_id, ctv.status, ctv.billing_profile, ctv.created_at
"""

_COUNT_APPROVED_SITE_SQL = """
SELECT count(DISTINCT ctv.id)::int AS n
FROM contract_terms_version ctv
WHERE ctv.billing_client_id = CAST(:bc AS uuid)
  AND ctv.status = 'approved'
  AND ctv.superseded_by_id IS NULL
  AND EXISTS (
    SELECT 1 FROM contract_rate_line crl
    WHERE crl.contract_terms_version_id = ctv.id
      AND crl.is_active = true
      AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
  )
"""

_COUNT_APPROVED_SQL = """
SELECT count(DISTINCT ctv.id)::int AS n
FROM contract_terms_version ctv
WHERE ctv.billing_client_id = CAST(:bc AS uuid)
  AND ctv.status = 'approved'
  AND ctv.superseded_by_id IS NULL
  AND ctv.effective_from <= CAST(:pe AS date)
  AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
  AND EXISTS (
    SELECT 1 FROM contract_rate_line crl
    WHERE crl.contract_terms_version_id = ctv.id
      AND crl.is_active = true
      AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
  )
"""


def _created_at_sort_key(created: Any) -> datetime:
    if isinstance(created, datetime):
        return created.replace(tzinfo=None) if created.tzinfo else created
    return datetime.min


def pick_from_billable_rows(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Phase 3 rules:
      - 2+ approved → None (ambiguous)
      - 1 approved (+ any pending) → approved
      - 0 approved, 1 pending → that pending
      - 0 approved, 2+ pending → most rate lines; tie → newest created_at
    """
    if not rows:
        return None
    approved = [r for r in rows if str(r.get("status") or "").lower() == "approved"]
    pending = [r for r in rows if str(r.get("status") or "").lower() == "pending"]
    if len(approved) >= 2:
        return None
    if len(approved) == 1:
        return approved[0]
    if len(pending) == 1:
        return pending[0]
    if len(pending) >= 2:

        def _sort_key(r: dict[str, Any]) -> tuple[int, datetime, str]:
            return (
                int(r.get("line_count") or 0),
                _created_at_sort_key(r.get("created_at")),
                str(r.get("contract_terms_version_id") or ""),
            )

        return max(pending, key=_sort_key)
    return None


async def list_billable_terms_for_site_async(
    session: AsyncSession,
    billing_client_id: str,
    service_site_id: str,
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    r = await session.execute(
        text(_BILLABLE_TERMS_SQL),
        {
            "bc": billing_client_id,
            "pe": period_end,
            "ps": period_start,
            "ssid": service_site_id,
        },
    )
    return [dict(row) for row in r.mappings().all()]


async def count_approved_terms_for_site_async(
    session: AsyncSession,
    billing_client_id: str,
    service_site_id: str,
) -> int:
    """Approved CTVs with site billable lines (any effective window)."""
    r = await session.execute(
        text(_COUNT_APPROVED_SITE_SQL),
        {"bc": billing_client_id, "ssid": service_site_id},
    )
    return int((r.mappings().first() or {}).get("n") or 0)


async def count_approved_billable_terms_async(
    session: AsyncSession,
    billing_client_id: str,
    service_site_id: str,
    *,
    period_start: date,
    period_end: date,
) -> int:
    r = await session.execute(
        text(_COUNT_APPROVED_SQL),
        {
            "bc": billing_client_id,
            "pe": period_end,
            "ps": period_start,
            "ssid": service_site_id,
        },
    )
    return int((r.mappings().first() or {}).get("n") or 0)
