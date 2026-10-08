"""Procurement ticket analytics for the home dashboard.

Surfaces the PR/PO pipeline as four headline numbers + a compact per-kind
breakdown. All counts are global (not scoped by user) — the dashboard's
existing ``operator`` block already exposes the current user's own ticket
totals, so this service answers the "how is the whole procurement queue
moving?" question instead.

Tickets with a non-empty ``sap_id`` are considered *posted*; tickets still
without one are ``pending_sap``. ``stuck_ge_7d`` = tickets created more than 7
days ago that still lack a SAP id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcurementTicket


async def procurement_ticket_throughput(
    db: AsyncSession, *, days: int = 30
) -> dict[str, Any]:
    """Last ``days`` of procurement ticket activity + SAP posting health.

    One SELECT with conditional aggregates — no per-kind fan-out.
    """

    if days <= 0 or days > 180:
        raise ValueError("days must be 1..180")

    since = datetime.now(UTC) - timedelta(days=days)
    stuck_cutoff = datetime.now(UTC) - timedelta(days=7)

    has_sap = ProcurementTicket.sap_id.isnot(None)
    stmt = select(
        func.count().label("total"),
        func.sum(case((has_sap, 1), else_=0)).label("posted"),
        func.sum(case((has_sap, 0), else_=1)).label("pending_sap"),
        func.sum(
            case(
                (
                    (ProcurementTicket.sap_id.is_(None))
                    & (ProcurementTicket.created_at < stuck_cutoff),
                    1,
                ),
                else_=0,
            )
        ).label("stuck_ge_7d"),
    ).where(ProcurementTicket.created_at >= since)

    row = (await db.execute(stmt)).first()

    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    total = _i(row.total) if row else 0
    posted = _i(row.posted) if row else 0
    pending = _i(row.pending_sap) if row else 0
    stuck = _i(row.stuck_ge_7d) if row else 0

    # Per-kind split (small cardinality, one extra SELECT is fine).
    by_kind_stmt = (
        select(
            ProcurementTicket.kind,
            func.count().label("n"),
            func.sum(case((has_sap, 1), else_=0)).label("posted"),
        )
        .where(ProcurementTicket.created_at >= since)
        .group_by(ProcurementTicket.kind)
        .order_by(func.count().desc())
    )
    by_kind_rows = (await db.execute(by_kind_stmt)).all()
    by_kind = [
        {
            "kind": kind or "unknown",
            "count": _i(n),
            "posted": _i(p),
        }
        for kind, n, p in by_kind_rows
    ]

    posted_pct = round((posted / total) * 100, 1) if total > 0 else 0.0

    return {
        "window_days": days,
        "created": total,
        "posted": posted,
        "pending_sap": pending,
        "stuck_ge_7d": stuck,
        "posted_pct": posted_pct,
        "by_kind": by_kind,
    }


async def procurement_monthly_series(
    db: AsyncSession, *, months: int = 6
) -> list[dict[str, Any]]:
    """Last ``months`` calendar months of ticket creation (UTC month bucket).

    ``created`` = all tickets; ``posted`` = rows with non-null ``sap_id``.
    Rows are oldest → newest (tail = current month).
    """

    n = max(1, min(int(months), 24))
    sql = """
        WITH months AS (
            SELECT to_char(
                       (date_trunc('month',
                            (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
                        ) - (offset_i || ' month')::interval)::date,
                       'YYYY-MM'
                   ) AS month_key,
                   offset_i
            FROM generate_series(0, :n - 1) AS offset_i
        ),
        agg AS (
            SELECT to_char(
                       date_trunc('month', pt.created_at AT TIME ZONE 'UTC'),
                       'YYYY-MM'
                   ) AS month_key,
                   COUNT(*)::int AS created,
                   SUM(CASE WHEN pt.sap_id IS NOT NULL THEN 1 ELSE 0 END)::int AS posted
            FROM procurement_tickets pt
            WHERE to_char(
                    date_trunc('month', pt.created_at AT TIME ZONE 'UTC'),
                    'YYYY-MM'
                ) IN (SELECT month_key FROM months)
            GROUP BY 1
        )
        SELECT m.month_key,
               COALESCE(a.created, 0) AS created,
               COALESCE(a.posted, 0) AS posted
        FROM months m
        LEFT JOIN agg a USING (month_key)
        ORDER BY m.offset_i DESC
    """
    r = await db.execute(text(sql), {"n": n})
    rows = r.mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        c = int(row.get("created") or 0)
        p = int(row.get("posted") or 0)
        out.append(
            {
                "month_key": str(row.get("month_key") or ""),
                "created": c,
                "posted": p,
                "posted_pct": round((p / c) * 100, 1) if c > 0 else 0.0,
            }
        )
    return out


__all__ = ["procurement_monthly_series", "procurement_ticket_throughput"]
