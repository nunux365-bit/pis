"""Contract analytics for the home dashboard (billing DB, read-only).

Renewal watch uses **Asia/Kolkata calendar "today"** for ``days_remaining`` and
urgency bands so the dashboard lines up with other O2C surfaces.

* **expired** (red) — ``effective_to`` before today; includes a rolling lookback
  of recently ended contracts that are still marked active / approved / pending.
* **orange** — due in the next **90 days** (0–3 months).
* **yellow** — due in **91–180 days** (between 3 and 6 months inclusive of the
  upper bound on the ``effective_to`` date).

The **Portfolio by stage** pie (``contract_stage_distribution``) counts **only**
**site-mapped** terms: ≥1 **active** ``contract_rate_line`` with non-null
``service_site_id``. **Rejected** terms are excluded (no commercial portfolio).

Bucketing order in SQL (first match wins, IST *today*):

1. No ``effective_to`` → ``no_end_date``
2. ``effective_to`` < today (calendar) → ``expired`` (lapsed end, may still be open in data)
3. ``effective_to`` within 90 days (inclusive) → ``expiring_0_90d``
4. ``effective_to`` within 180 days, beyond 90 → ``expiring_91_180d``
5. All **future** ends **beyond** 180 days: by **status** → ``pending`` *or* ``active_open_ended``
   (draft/signed/active/approved), else raw status lowercased in ``other``

So a near-dated *pending* contract appears under **expiring** slices, not **pending**;
``pending`` is for long-horizon terms still in that workflow state.
Case-insensitive status labels in the ``CASE`` expression.

Renewal lists exclude only **rejected** and **expired** statuses — the same idea
as ``mis_drafts`` contract selection, so draft/signed terms still appear.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal

log = logging.getLogger(__name__)


async def contracts_renewal_watch(
    *,
    days_ahead: int = 180,
    expired_lookback_days: int = 730,
    limit: int = 24,
) -> list[dict[str, Any]] | None:
    """Contracts in the next ``days_ahead`` days on the calendar, plus expired.

    Each row includes ``urgency``: ``expired`` | ``orange`` | ``yellow`` | ``ok``.
    """

    d = max(1, min(int(days_ahead), 365 * 2))
    lb = max(30, min(int(expired_lookback_days), 3650))
    n = max(1, min(int(limit), 50))
    sql = """
        WITH ist_today AS (
            SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date AS d
        ),
        expiring AS (
            SELECT ctv.id AS contract_id,
                   ctv.billing_client_id,
                   ctv.effective_to,
                   ctv.contract_kind,
                   (ctv.effective_to - (SELECT d FROM ist_today))::int AS days_remaining,
                   CASE
                     WHEN ctv.effective_to < (SELECT d FROM ist_today) THEN 'expired'
                     WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 90 THEN 'orange'
                     WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 180 THEN 'yellow'
                     ELSE 'ok'
                   END AS urgency
            FROM contract_terms_version ctv
            WHERE ctv.effective_to IS NOT NULL
              /* Match MIS / contract pickers (see mis_drafts): not rejected/expired; NULL status allowed. */
              AND coalesce(lower(ctv.status::text), '') NOT IN ('rejected', 'expired')
              AND (
                (
                    ctv.effective_to >= (SELECT d FROM ist_today)
                    AND ctv.effective_to <= (SELECT d FROM ist_today) + (:d || ' days')::interval
                )
                OR (
                    ctv.effective_to < (SELECT d FROM ist_today)
                    AND ctv.effective_to >= (SELECT d FROM ist_today) - (:lb || ' days')::interval
                )
              )
        ),
        rate_estimate AS (
            SELECT crl.contract_terms_version_id AS contract_id,
                   SUM(
                       COALESCE(crl.rate_amount, 0)
                     * COALESCE(crl.contracted_quantity, 1)
                   )::numeric AS monthly_estimate,
                   COUNT(DISTINCT crl.service_site_id)::int AS sites
            FROM contract_rate_line crl
            WHERE crl.is_active = true
              AND crl.contract_terms_version_id IN (SELECT contract_id FROM expiring)
            GROUP BY crl.contract_terms_version_id
        )
        SELECT e.contract_id::text AS contract_id,
               COALESCE(NULLIF(bc.short_name, ''), bc.name) AS client_name,
               to_char(e.effective_to, 'YYYY-MM-DD') AS effective_to,
               e.days_remaining,
               e.contract_kind AS kind,
               e.urgency,
               COALESCE(r.monthly_estimate, 0)::numeric AS monthly_estimate,
               COALESCE(r.sites, 0) AS sites
        FROM expiring e
        JOIN billing_client bc ON bc.id = e.billing_client_id
        LEFT JOIN rate_estimate r USING (contract_id)
        ORDER BY
            CASE e.urgency
                WHEN 'expired' THEN 0
                WHEN 'orange' THEN 1
                WHEN 'yellow' THEN 2
                ELSE 3
            END,
            CASE WHEN e.urgency = 'expired' THEN e.effective_to END DESC NULLS LAST,
            e.effective_to ASC
        LIMIT :n
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(
                    text(sql), {"d": d, "lb": lb, "n": n}
                )
                rows = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: renewal watch skipped (%s)", exc)
        return None

    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    return [
        {
            "contract_id": str(row.get("contract_id") or ""),
            "client_name": str(row.get("client_name") or "Unknown"),
            "effective_to": str(row.get("effective_to") or ""),
            "days_remaining": int(row.get("days_remaining") or 0),
            "kind": str(row.get("kind") or ""),
            "urgency": str(row.get("urgency") or "ok"),
            "monthly_estimate": _num(row.get("monthly_estimate")),
            "sites": int(row.get("sites") or 0),
        }
        for row in rows
    ]


async def contracts_renewal_summary(
    *,
    days_ahead: int = 180,
    expired_lookback_days: int = 730,
) -> dict[str, Any] | None:
    """Counts by urgency band and total monthly estimate (same filters as watch)."""

    d = max(1, min(int(days_ahead), 365 * 2))
    lb = max(30, min(int(expired_lookback_days), 3650))
    sql = """
        WITH ist_today AS (
            SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date AS d
        ),
        expiring AS (
            SELECT ctv.id AS contract_id,
                   ctv.effective_to,
                   (ctv.effective_to - (SELECT d FROM ist_today))::int AS days_remaining,
                   CASE
                     WHEN ctv.effective_to < (SELECT d FROM ist_today) THEN 'expired'
                     WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 90 THEN 'orange'
                     WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 180 THEN 'yellow'
                     ELSE 'ok'
                   END AS urgency
            FROM contract_terms_version ctv
            WHERE ctv.effective_to IS NOT NULL
              AND coalesce(lower(ctv.status::text), '') NOT IN ('rejected', 'expired')
              AND (
                (
                    ctv.effective_to >= (SELECT d FROM ist_today)
                    AND ctv.effective_to <= (SELECT d FROM ist_today) + (:d || ' days')::interval
                )
                OR (
                    ctv.effective_to < (SELECT d FROM ist_today)
                    AND ctv.effective_to >= (SELECT d FROM ist_today) - (:lb || ' days')::interval
                )
              )
        )
        SELECT
            (SELECT COUNT(*) FROM expiring)::int AS count_total,
            (SELECT COUNT(*) FROM expiring WHERE urgency = 'expired')::int AS count_expired,
            (SELECT COUNT(*) FROM expiring WHERE urgency = 'orange')::int AS count_orange,
            (SELECT COUNT(*) FROM expiring WHERE urgency = 'yellow')::int AS count_yellow,
            (SELECT COUNT(*) FROM expiring WHERE urgency = 'ok')::int AS count_ok,
            COALESCE((
                SELECT SUM(COALESCE(crl.rate_amount, 0) * COALESCE(crl.contracted_quantity, 1))
                FROM contract_rate_line crl
                WHERE crl.is_active = true
                  AND crl.contract_terms_version_id IN (SELECT contract_id FROM expiring)
            ), 0)::numeric AS monthly_estimate_total
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"d": d, "lb": lb})
                one = r.mappings().first()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: renewal summary skipped (%s)", exc)
        return None

    if one is None:
        return None
    try:
        monthly = float(one.get("monthly_estimate_total") or 0)
    except (TypeError, ValueError):
        monthly = 0.0
    return {
        "window_days": d,
        "count_total": int(one.get("count_total") or 0),
        "count_expired": int(one.get("count_expired") or 0),
        "count_orange": int(one.get("count_orange") or 0),
        "count_yellow": int(one.get("count_yellow") or 0),
        "count_ok": int(one.get("count_ok") or 0),
        "monthly_estimate_total": monthly,
        "as_of": date.today().isoformat(),
    }


async def contract_stage_distribution() -> list[dict[str, Any]] | None:
    """Counts of contract terms by lifecycle bucket, **site-mapped terms only** (for pie).

    A term is included iff it has ≥1 active ``contract_rate_line`` with
    ``service_site_id IS NOT NULL`` (concrete site mapping) and the term is
    not **rejected** (aligned with a commercial "portfolio" view).
    """

    sql = """
        WITH ist_today AS (
            SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date AS d
        ),
        site_mapped AS (
            SELECT DISTINCT ctv.id AS ctv_id
            FROM contract_terms_version ctv
            INNER JOIN contract_rate_line crl ON crl.contract_terms_version_id = ctv.id
            WHERE crl.is_active = true
              AND crl.service_site_id IS NOT NULL
        ),
        tagged AS (
            SELECT
                CASE
                    WHEN ctv.effective_to IS NULL THEN 'no_end_date'
                    WHEN ctv.effective_to < (SELECT d FROM ist_today) THEN 'expired'
                    WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 90 THEN 'expiring_0_90d'
                    WHEN ctv.effective_to <= (SELECT d FROM ist_today) + 180 THEN 'expiring_91_180d'
                    WHEN coalesce(lower(ctv.status::text), '') = 'pending' THEN 'pending'
                    WHEN coalesce(lower(ctv.status::text), '') IN (
                        'active', 'approved', 'draft', 'signed'
                    ) THEN 'active_open_ended'
                    WHEN coalesce(lower(ctv.status::text), '') = '' THEN 'other'
                    ELSE coalesce(lower(ctv.status::text), 'other')
                END AS stage
            FROM contract_terms_version ctv
            INNER JOIN site_mapped m ON m.ctv_id = ctv.id
            WHERE coalesce(lower(ctv.status::text), '') <> 'rejected'
        )
        SELECT stage, COUNT(*)::int AS count
        FROM tagged
        GROUP BY stage
        ORDER BY count DESC
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql))
                rows = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: contract stages skipped (%s)", exc)
        return None

    return [
        {"stage": str(row.get("stage") or "other"), "count": int(row.get("count") or 0)}
        for row in rows
    ]


__all__ = [
    "contracts_renewal_watch",
    "contracts_renewal_summary",
    "contract_stage_distribution",
]
