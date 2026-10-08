"""O2C month-close analytics for the home dashboard (billing DB, read-only).

Lives in ``app/services`` rather than ``app/agents/o2c_ohc`` because the queries
here are dashboard-specific rollups, not part of the MIS/billing pipeline.
All reads go through :class:`AgenosAsyncSessionLocal` (the billing DB pool)
and every call is wrapped so a degraded billing DB returns ``None`` instead of
breaking the rest of the dashboard.

Three blocks returned:

* **daily activity** — per calendar-day of the IST month, counts of MIS runs
  transitioning into each status (created / approved / rejected).
* **revenue totals** — billed vs at-risk for the same month, driven by the
  ``summary_json.totals.final_amount_total`` cached on each MIS run.
* **month progress** — sites in scope for the month, how many already have an
  approved MIS, and current IST day-of-month with days remaining.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal

log = logging.getLogger(__name__)

PeriodScope = Literal["current_month", "prior_month"]


def _period_where_fragment(scope: PeriodScope) -> str:
    """Same IST calendar-month filter as :mod:`app.agents.o2c_ohc.mis_db`."""

    if scope == "prior_month":
        return """
            to_char(mr.billing_period_start, 'YYYY-MM') = to_char(
                ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - INTERVAL '1 month')::date,
                'YYYY-MM'
            )
        """
    return """
        to_char(mr.billing_period_start, 'YYYY-MM')
            = to_char((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date, 'YYYY-MM')
    """


async def mis_daily_activity(scope: PeriodScope) -> list[dict[str, Any]] | None:
    """Per-day MIS activity for the month — created / approved / rejected (IST bucketed).

    One SQL using conditional aggregation — no N+1 risk. Days with no activity
    still appear (left join against a generated calendar).
    """

    period_where = _period_where_fragment(scope)
    # NB: ``generate_series`` gives us the full IST calendar for the chosen month,
    # then we left-join to the three MIS timestamp columns bucketed by IST date.
    sql = f"""
        WITH
        -- IST month anchor (first day of current or prior month)
        month_anchor AS (
            SELECT CASE
                WHEN :scope = 'prior_month'
                    THEN date_trunc('month',
                        ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - INTERVAL '1 month')
                    )::date
                ELSE date_trunc('month',
                        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    )::date
            END AS month_start
        ),
        days AS (
            SELECT (month_start + (n || ' day')::interval)::date AS d
            FROM month_anchor,
                 generate_series(
                    0,
                    (date_trunc('month', month_start + INTERVAL '1 month') - INTERVAL '1 day')::date - month_start
                 ) AS n
        ),
        created_by_day AS (
            SELECT (mr.created_at AT TIME ZONE 'Asia/Kolkata')::date AS d, COUNT(*)::int AS n
            FROM o2c_mis_run mr
            WHERE {period_where}
            GROUP BY 1
        ),
        approved_by_day AS (
            SELECT (mr.approved_at AT TIME ZONE 'Asia/Kolkata')::date AS d, COUNT(*)::int AS n
            FROM o2c_mis_run mr
            WHERE mr.approved_at IS NOT NULL AND {period_where}
            GROUP BY 1
        ),
        rejected_by_day AS (
            SELECT (mr.rejected_at AT TIME ZONE 'Asia/Kolkata')::date AS d, COUNT(*)::int AS n
            FROM o2c_mis_run mr
            WHERE mr.rejected_at IS NOT NULL AND {period_where}
            GROUP BY 1
        )
        SELECT to_char(days.d, 'YYYY-MM-DD') AS bucket,
               COALESCE(c.n, 0) AS created,
               COALESCE(a.n, 0) AS approved,
               COALESCE(r.n, 0) AS rejected
        FROM days
        LEFT JOIN created_by_day c ON c.d = days.d
        LEFT JOIN approved_by_day a ON a.d = days.d
        LEFT JOIN rejected_by_day r ON r.d = days.d
        -- only include days up to today (IST) for the current month so the
        -- chart doesn't grow a visual tail of 10 future zero-days.
        WHERE days.d <= (
            CASE WHEN :scope = 'current_month'
                THEN (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                ELSE (SELECT (date_trunc('month', month_start + INTERVAL '1 month') - INTERVAL '1 day')::date FROM month_anchor)
            END
        )
        ORDER BY days.d
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"scope": scope})
                return [dict(row) for row in r.mappings().all()]
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: O2C daily activity (%s) skipped (%s)", scope, exc)
        return None


async def mis_revenue_totals(scope: PeriodScope) -> dict[str, Any] | None:
    """Billed (approved) vs at-risk (pending_human) revenue for the month.

    Uses ``summary_json->'totals'->>'final_amount_total'`` computed by the MIS
    pipeline — always the same source of truth as the XLSX export, no risk of
    drift. Service-site counts are derived from the same rows.
    """

    period_where = _period_where_fragment(scope)
    sql = f"""
        SELECT
            COUNT(DISTINCT CASE WHEN mr.status = 'approved' THEN mr.service_site_id END)::int
                AS sites_approved,
            COUNT(DISTINCT CASE WHEN mr.status = 'pending_human' THEN mr.service_site_id END)::int
                AS sites_pending,
            COUNT(DISTINCT mr.service_site_id)::int AS sites_in_scope,
            COALESCE(SUM(CASE WHEN mr.status = 'approved'
                              THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                         END), 0)::numeric AS revenue_billed,
            COALESCE(SUM(CASE WHEN mr.status = 'pending_human'
                              THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                         END), 0)::numeric AS revenue_at_risk
        FROM o2c_mis_run mr
        WHERE {period_where}
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"scope": scope})
                one = r.mappings().first()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: O2C revenue totals (%s) skipped (%s)", scope, exc)
        return None

    if one is None:
        return None

    def _num(v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {
        "sites_approved": int(one.get("sites_approved") or 0),
        "sites_pending": int(one.get("sites_pending") or 0),
        "sites_in_scope": int(one.get("sites_in_scope") or 0),
        "revenue_billed": _num(one.get("revenue_billed")),
        "revenue_at_risk": _num(one.get("revenue_at_risk")),
    }


async def month_progress_ist(scope: PeriodScope) -> dict[str, Any] | None:
    """IST day-of-month + total days for hero ring animation and labels."""

    sql = """
        WITH anchor AS (
            SELECT CASE
                WHEN :scope = 'prior_month'
                    THEN date_trunc('month',
                        ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - INTERVAL '1 month')
                    )::date
                ELSE date_trunc('month',
                        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    )::date
            END AS month_start
        )
        SELECT
            to_char(month_start, 'YYYY-MM') AS month_label,
            (date_trunc('month', month_start + INTERVAL '1 month') - INTERVAL '1 day')::date - month_start + 1
                AS days_in_month,
            CASE WHEN :scope = 'current_month'
                 THEN LEAST(
                    ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date - month_start + 1)::int,
                    (date_trunc('month', month_start + INTERVAL '1 month') - INTERVAL '1 day')::date - month_start + 1
                 )
                 ELSE (date_trunc('month', month_start + INTERVAL '1 month') - INTERVAL '1 day')::date - month_start + 1
            END AS day_of_month
        FROM anchor
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"scope": scope})
                one = r.mappings().first()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: O2C month progress (%s) skipped (%s)", scope, exc)
        return None

    if one is None:
        return None

    return {
        "scope": scope,
        "month_label": str(one.get("month_label") or ""),
        "days_in_month": int(one.get("days_in_month") or 0),
        "day_of_month": int(one.get("day_of_month") or 0),
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


async def build_o2c_period(scope: PeriodScope) -> dict[str, Any] | None:
    """Bundle daily activity + revenue + month progress for one period."""

    daily = await mis_daily_activity(scope)
    revenue = await mis_revenue_totals(scope)
    progress = await month_progress_ist(scope)
    if daily is None and revenue is None and progress is None:
        return None
    return {
        "scope": scope,
        "daily": daily or [],
        "revenue": revenue,
        "progress": progress,
    }


async def mis_monthly_series(*, months: int = 6) -> list[dict[str, Any]] | None:
    """Last ``months`` IST calendar months of MIS activity — counts + INR.

    One SQL: generate the last N month anchors, then LEFT JOIN each anchor to
    conditional aggregates over ``o2c_mis_run`` bucketed by
    ``to_char(billing_period_start, 'YYYY-MM')`` in Asia/Kolkata. This is the
    single source of truth for the dashboard's MIS tables, prior-month donut,
    and month-over-month trend chart — no risk of drift between panels.

    Returned rows are oldest→newest so the tail is the current month.
    """

    n = max(1, min(int(months), 24))
    sql = """
        WITH months AS (
            SELECT to_char(
                       (date_trunc('month',
                            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                        ) - (offset_i || ' month')::interval)::date,
                       'YYYY-MM'
                   ) AS month_key,
                   offset_i
            FROM generate_series(0, :n - 1) AS offset_i
        ),
        agg AS (
            SELECT to_char(mr.billing_period_start, 'YYYY-MM') AS month_key,
                   COUNT(*)::int AS total,
                   SUM(CASE WHEN mr.status = 'approved' THEN 1 ELSE 0 END)::int
                       AS approved,
                   SUM(CASE WHEN mr.status = 'rejected' THEN 1 ELSE 0 END)::int
                       AS rejected,
                   SUM(CASE WHEN mr.status = 'pending_human' THEN 1 ELSE 0 END)::int
                       AS pending_human,
                   COUNT(DISTINCT mr.service_site_id)::int AS sites_in_scope,
                   COUNT(DISTINCT CASE WHEN mr.status = 'approved'
                                       THEN mr.service_site_id END)::int AS sites_approved,
                   COUNT(DISTINCT CASE WHEN mr.status = 'pending_human'
                                       THEN mr.service_site_id END)::int AS sites_pending,
                   COALESCE(SUM(CASE WHEN mr.status = 'approved'
                                     THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                                END), 0)::numeric AS revenue_billed,
                   COALESCE(SUM(CASE WHEN mr.status = 'pending_human'
                                     THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                                END), 0)::numeric AS revenue_at_risk
            FROM o2c_mis_run mr
            WHERE to_char(mr.billing_period_start, 'YYYY-MM') IN (SELECT month_key FROM months)
            GROUP BY 1
        ),
        human_c AS (
            /* Distinct MIS runs that have at least one human-edited summary row. */
            SELECT to_char(mr.billing_period_start, 'YYYY-MM') AS month_key,
                   COUNT(DISTINCT mr.id)::int AS human_corrected_runs
            FROM o2c_mis_run mr
            INNER JOIN o2c_mis_summary_row sr ON sr.mis_run_id = mr.id
            WHERE sr.human_correction IS NOT NULL
              AND to_char(mr.billing_period_start, 'YYYY-MM') IN (SELECT month_key FROM months)
            GROUP BY 1
        )
        SELECT m.month_key,
               COALESCE(a.total, 0) AS total,
               COALESCE(a.approved, 0) AS approved,
               COALESCE(a.rejected, 0) AS rejected,
               COALESCE(a.pending_human, 0) AS pending_human,
               COALESCE(a.sites_in_scope, 0) AS sites_in_scope,
               COALESCE(a.sites_approved, 0) AS sites_approved,
               COALESCE(a.sites_pending, 0) AS sites_pending,
               COALESCE(a.revenue_billed, 0) AS revenue_billed,
               COALESCE(a.revenue_at_risk, 0) AS revenue_at_risk,
               COALESCE(h.human_corrected_runs, 0) AS human_corrected_runs
        FROM months m
        LEFT JOIN agg a USING (month_key)
        LEFT JOIN human_c h USING (month_key)
        ORDER BY m.offset_i DESC
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"n": n})
                rows = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: MIS monthly series skipped (%s)", exc)
        return None

    def _num(v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    out: list[dict[str, Any]] = []
    for row in rows:
        total = int(row.get("total") or 0)
        approved = int(row.get("approved") or 0)
        out.append(
            {
                "month_key": str(row.get("month_key") or ""),
                "total": total,
                "approved": approved,
                "rejected": int(row.get("rejected") or 0),
                "pending_human": int(row.get("pending_human") or 0),
                "sites_in_scope": int(row.get("sites_in_scope") or 0),
                "sites_approved": int(row.get("sites_approved") or 0),
                "sites_pending": int(row.get("sites_pending") or 0),
                "revenue_billed": _num(row.get("revenue_billed")),
                "revenue_at_risk": _num(row.get("revenue_at_risk")),
                "human_corrected_runs": int(row.get("human_corrected_runs") or 0),
                "approval_rate": (approved / total) if total > 0 else 0.0,
            }
        )
    return out


async def top_billing_clients_6m(
    *, limit: int = 6, months: int = 6
) -> list[dict[str, Any]] | None:
    """Top billing clients per IST month — same month grid as :func:`mis_monthly_series`.

    Returns one object per month (oldest → newest), each with ``month_key`` and ``rows`` (up to
    ``limit`` clients ranked by billed + at-risk INR for that month).
    """

    n = max(1, min(int(limit), 20))
    n_m = max(1, min(int(months), 24))
    sql = """
        WITH months AS (
            SELECT to_char(
                       (date_trunc('month',
                            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                        ) - (offset_i || ' month')::interval)::date,
                       'YYYY-MM'
                   ) AS month_key,
                   offset_i
            FROM generate_series(0, :m - 1) AS offset_i
        ),
        per_client AS (
            SELECT
                to_char(mr.billing_period_start, 'YYYY-MM') AS month_key,
                bc.id::text AS client_id,
                COALESCE(NULLIF(bc.short_name, ''), bc.name) AS client_name,
                COUNT(DISTINCT mr.service_site_id)::int AS sites,
                SUM(CASE WHEN mr.status = 'approved' THEN 1 ELSE 0 END)::int
                    AS approved_runs,
                SUM(CASE WHEN mr.status = 'pending_human' THEN 1 ELSE 0 END)::int
                    AS pending_runs,
                COALESCE(SUM(CASE WHEN mr.status = 'approved'
                                  THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                             END), 0)::numeric AS revenue_billed,
                COALESCE(SUM(CASE WHEN mr.status = 'pending_human'
                                  THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                             END), 0)::numeric AS revenue_at_risk
            FROM o2c_mis_run mr
            JOIN billing_client bc ON bc.id = mr.billing_client_id
            WHERE to_char(mr.billing_period_start, 'YYYY-MM') IN (SELECT month_key FROM months)
            GROUP BY 1, 2, 3
            HAVING COUNT(*) > 0
        ),
        ranked AS (
            SELECT
                p.*,
                ROW_NUMBER() OVER (
                    PARTITION BY p.month_key
                    ORDER BY (COALESCE(p.revenue_billed, 0) + COALESCE(p.revenue_at_risk, 0)) DESC
                ) AS rn
            FROM per_client p
        )
        SELECT
            m.month_key,
            r.client_id,
            r.client_name,
            r.sites,
            r.approved_runs,
            r.pending_runs,
            r.revenue_billed,
            r.revenue_at_risk,
            r.rn
        FROM months m
        LEFT JOIN ranked r
            ON r.month_key = m.month_key AND r.rn IS NOT NULL AND r.rn <= :n
        ORDER BY m.offset_i DESC, r.rn NULLS LAST
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"n": n, "m": n_m})
                flat = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: top billing clients 6m skipped (%s)", exc)
        return None

    return _pivot_left_join_to_monthly_buckets(flat, _map_top_client_row)


async def top_service_sites_6m(
    *, limit: int = 6, months: int = 6
) -> list[dict[str, Any]] | None:
    """Top service sites per IST month (one row per site per month, revenue aggregated).

    Same month boundaries as :func:`mis_monthly_series` and :func:`top_billing_clients_6m`.
    ``last_status`` prefers ``pending_human`` if any run in the month is pending.
    """

    n = max(1, min(int(limit), 20))
    n_m = max(1, min(int(months), 24))
    sql = """
        WITH months AS (
            SELECT to_char(
                       (date_trunc('month',
                            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                        ) - (offset_i || ' month')::interval)::date,
                       'YYYY-MM'
                   ) AS month_key,
                   offset_i
            FROM generate_series(0, :m - 1) AS offset_i
        ),
        per_site AS (
            SELECT
                to_char(mr.billing_period_start, 'YYYY-MM') AS month_key,
                ss.id::text AS site_id,
                COALESCE(NULLIF(ss.display_name, ''), ss.canonical_name) AS site_name,
                COALESCE(NULLIF(bc.short_name, ''), bc.name) AS client_name,
                COALESCE(ss.city, '') AS city,
                CASE
                    WHEN BOOL_OR(mr.status = 'pending_human') THEN 'pending_human'
                    WHEN BOOL_OR(mr.status = 'approved') THEN 'approved'
                    WHEN BOOL_OR(mr.status = 'rejected') THEN 'rejected'
                    ELSE 'created'
                END AS last_status,
                COALESCE(SUM(CASE WHEN mr.status = 'approved'
                                  THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                             END), 0)::numeric AS revenue_billed,
                COALESCE(SUM(CASE WHEN mr.status = 'pending_human'
                                  THEN (mr.summary_json->'totals'->>'final_amount_total')::numeric
                             END), 0)::numeric AS revenue_at_risk
            FROM o2c_mis_run mr
            JOIN service_site ss ON ss.id = mr.service_site_id
            JOIN billing_client bc ON bc.id = mr.billing_client_id
            WHERE to_char(mr.billing_period_start, 'YYYY-MM') IN (SELECT month_key FROM months)
            GROUP BY
                to_char(mr.billing_period_start, 'YYYY-MM'),
                ss.id, ss.display_name, ss.canonical_name,
                bc.short_name, bc.name, ss.city
        ),
        ranked AS (
            SELECT
                p.*,
                ROW_NUMBER() OVER (
                    PARTITION BY p.month_key
                    ORDER BY (COALESCE(p.revenue_billed, 0) + COALESCE(p.revenue_at_risk, 0)) DESC
                ) AS rn
            FROM per_site p
        )
        SELECT
            m.month_key,
            r.site_id,
            r.site_name,
            r.client_name,
            r.city,
            r.last_status,
            r.revenue_billed,
            r.revenue_at_risk,
            r.rn
        FROM months m
        LEFT JOIN ranked r
            ON r.month_key = m.month_key AND r.rn IS NOT NULL AND r.rn <= :n
        ORDER BY m.offset_i DESC, r.rn NULLS LAST
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"n": n, "m": n_m})
                flat = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: top service sites 6m skipped (%s)", exc)
        return None

    return _pivot_left_join_to_monthly_buckets(flat, _map_top_site_row)


def _pivot_left_join_to_monthly_buckets(
    flat: list[Any],
    map_row: Any,
) -> list[dict[str, Any]]:
    """Collapse ordered LEFT-join rows (one block per month) to ``{month_key, rows}`` list."""

    out: list[dict[str, Any]] = []
    cur_mk: str | None = None
    cur_rows: list[dict[str, Any]] = []
    for raw in flat:
        row = dict(raw) if not isinstance(raw, dict) else raw
        mk = str(row.get("month_key") or "")
        if not mk:
            continue
        if cur_mk is None or mk != cur_mk:
            if cur_mk is not None:
                out.append({"month_key": cur_mk, "rows": cur_rows})
            cur_mk = mk
            cur_rows = []
        item = map_row(row)
        if item is not None:
            cur_rows.append(item)
    if cur_mk is not None:
        out.append({"month_key": cur_mk, "rows": cur_rows})
    return out


def _map_top_client_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("client_id"):
        return None

    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "client_id": str(row.get("client_id") or ""),
        "client_name": str(row.get("client_name") or "Unknown"),
        "sites": int(row.get("sites") or 0),
        "approved_runs": int(row.get("approved_runs") or 0),
        "pending_runs": int(row.get("pending_runs") or 0),
        "revenue_billed": _num(row.get("revenue_billed")),
        "revenue_at_risk": _num(row.get("revenue_at_risk")),
    }


def _map_top_site_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("site_id"):
        return None

    def _num(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "site_id": str(row.get("site_id") or ""),
        "site_name": str(row.get("site_name") or "Unknown"),
        "client_name": str(row.get("client_name") or ""),
        "city": str(row.get("city") or ""),
        "last_status": str(row.get("last_status") or ""),
        "revenue_billed": _num(row.get("revenue_billed")),
        "revenue_at_risk": _num(row.get("revenue_at_risk")),
    }


async def mis_cycle_time_stats() -> dict[str, Any] | None:
    """MIS approval cycle time — hours from ``created_at`` to ``approved_at``.

    Reports median + p90 for the current IST month and the prior month, plus the
    percentage delta (current vs prior) so the UI can show a single velocity KPI.
    """

    period_cur = _period_where_fragment("current_month")
    period_pri = _period_where_fragment("prior_month")
    sql = f"""
        WITH approved_cur AS (
            SELECT EXTRACT(EPOCH FROM (mr.approved_at - mr.created_at)) / 3600.0 AS hours
            FROM o2c_mis_run mr
            WHERE mr.status = 'approved'
              AND mr.approved_at IS NOT NULL
              AND {period_cur}
        ),
        approved_pri AS (
            SELECT EXTRACT(EPOCH FROM (mr.approved_at - mr.created_at)) / 3600.0 AS hours
            FROM o2c_mis_run mr
            WHERE mr.status = 'approved'
              AND mr.approved_at IS NOT NULL
              AND {period_pri}
        )
        SELECT
            (SELECT COUNT(*) FROM approved_cur)::int AS n_cur,
            (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) FROM approved_cur) AS med_cur,
            (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY hours) FROM approved_cur) AS p90_cur,
            (SELECT COUNT(*) FROM approved_pri)::int AS n_pri,
            (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) FROM approved_pri) AS med_pri,
            (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY hours) FROM approved_pri) AS p90_pri
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql))
                one = r.mappings().first()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: MIS cycle time skipped (%s)", exc)
        return None

    if one is None:
        return None

    def _num(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    med_cur = _num(one.get("med_cur"))
    med_pri = _num(one.get("med_pri"))
    delta_pct: float | None
    if med_cur is not None and med_pri and med_pri > 0:
        delta_pct = round(((med_cur - med_pri) / med_pri) * 100.0, 1)
    else:
        delta_pct = None

    return {
        "current": {
            "n": int(one.get("n_cur") or 0),
            "median_hours": med_cur,
            "p90_hours": _num(one.get("p90_cur")),
        },
        "prior": {
            "n": int(one.get("n_pri") or 0),
            "median_hours": med_pri,
            "p90_hours": _num(one.get("p90_pri")),
        },
        # Negative delta = faster (good).
        "median_delta_pct": delta_pct,
    }


async def mis_rejection_reasons(
    *, days: int = 60, limit: int = 6
) -> list[dict[str, Any]] | None:
    """Top raw ``rejection_notes`` values over a window, with count.

    Exact-string grouping on a trimmed/truncated note. We do not attempt NLP
    clustering — operators tend to paste short stock reasons, and the raw note
    is the most honest signal for the dashboard to show.
    """

    d = max(1, min(int(days), 365))
    n = max(1, min(int(limit), 20))
    sql = """
        SELECT TRIM(LEFT(mr.rejection_notes, 140)) AS reason,
               COUNT(*)::int AS cnt,
               MAX(mr.rejected_at) AS last_rejected_at
        FROM o2c_mis_run mr
        WHERE mr.status = 'rejected'
          AND mr.rejected_at IS NOT NULL
          AND mr.rejected_at >= (CURRENT_TIMESTAMP - (:d || ' days')::interval)
          AND COALESCE(TRIM(mr.rejection_notes), '') <> ''
        GROUP BY TRIM(LEFT(mr.rejection_notes, 140))
        ORDER BY cnt DESC, last_rejected_at DESC
        LIMIT :n
    """
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(text(sql), {"d": d, "n": n})
                rows = r.mappings().all()
    except Exception as exc:  # noqa: BLE001
        log.info("dashboard: MIS rejection reasons skipped (%s)", exc)
        return None

    out: list[dict[str, Any]] = []
    for row in rows:
        reason = str(row.get("reason") or "").strip()
        if not reason:
            continue
        last = row.get("last_rejected_at")
        last_iso: str | None = None
        if isinstance(last, datetime):
            last_iso = (
                last.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if last.tzinfo is not None
                else last.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
            )
        out.append(
            {
                "reason": reason,
                "count": int(row.get("cnt") or 0),
                "last_rejected_at": last_iso,
            }
        )
    return out


__all__ = [
    "PeriodScope",
    "mis_daily_activity",
    "mis_revenue_totals",
    "month_progress_ist",
    "build_o2c_period",
    "mis_monthly_series",
    "top_billing_clients_6m",
    "top_service_sites_6m",
    "mis_cycle_time_stats",
    "mis_rejection_reasons",
]
