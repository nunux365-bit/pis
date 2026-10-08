"""Home dashboard aggregate — single endpoint, fan-out via ``asyncio.gather``.

Composes lightweight rollups from:

* :mod:`app.services.analytics_summary_core` — pending approvals, automation %,
  department breakdown, catalog agents (role-scoped).
* :mod:`app.services.dashboard_approvals` — aging histogram, top pending
  originators.
* :mod:`app.services.dashboard_workflow` — workflow run status distribution,
  failure hot-spots by ``workflow_key``.
* :mod:`app.services.dashboard_extras` — unread notifications, workflow active /
  failed, procurement tickets, integration health snapshot.
* :mod:`app.services.dashboard_o2c` — MIS revenue totals + month progress
  (current IST month), the last-6-month MIS series (trend + tables), and
  per-month top billing clients / top service sites (same IST month grid).
* :mod:`app.email_automation.pipeline` — email metrics (14d primary + prior 14d
  ghost line for the "emails sent" delta chip).

Everything fans out in one ``asyncio.gather`` so wall time is bounded by the
slowest query, not their sum. Any block that depends on an optional external
system (billing DB, integration health) is wrapped so one failing block does
not blank the whole dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.email_automation import pipeline
from app.email_automation.pipeline.metrics import Metrics, TrendPoint
from app.services.analytics_summary_core import build_analytics_summary
from app.services.dashboard_approvals import (
    approval_aging_buckets,
    approval_top_originators,
)
from app.services.dashboard_contracts import (
    contract_stage_distribution,
    contracts_renewal_summary,
    contracts_renewal_watch,
)
from app.services.dashboard_extras import (
    build_operator_surface,
    integration_health_snapshot,
)
from app.services.dashboard_o2c import (
    build_o2c_period,
    mis_monthly_series,
    top_billing_clients_6m,
    top_service_sites_6m,
)
from app.services.dashboard_procurement import (
    procurement_monthly_series,
    procurement_ticket_throughput,
)
from app.services.dashboard_workflow import (
    workflow_failures_by_key,
    workflow_status_distribution,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers — shape the outbound payload (DTOs, not business logic)
# ---------------------------------------------------------------------------


def _trend_points(points: list[TrendPoint]) -> list[dict[str, Any]]:
    return [
        {
            "bucket": p.bucket.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if p.bucket.tzinfo is not None
            else p.bucket.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            "sent": p.sent,
            "failed": p.failed,
            "ingested": p.ingested,
        }
        for p in points
    ]


def _email_block_from_metrics(
    m: Metrics, *, ghost_trend: list[TrendPoint]
) -> dict[str, Any]:
    """Slim DTO: headline counts + 14d trend + ghost; omits per-variant matrix."""

    if not m.attention:
        attention: dict[str, int] = {
            "sends_at_attempt_cap": 0,
            "messages_processed_with_errors_open": 0,
            "sends_stuck_sending": 0,
        }
    else:
        attention = {
            "sends_at_attempt_cap": m.attention.sends_at_attempt_cap,
            "messages_processed_with_errors_open": m.attention.messages_processed_with_errors_open,
            "sends_stuck_sending": m.attention.sends_stuck_sending,
        }

    return {
        "window": m.window,
        "bucket_granularity": m.bucket_granularity,
        "health": {
            "enabled": m.health.enabled,
            "test_mode": m.health.test_mode,
            "last_message_received_at": m.health.last_message_received_at,
            "last_send_sent_at": m.health.last_send_sent_at,
        },
        "messages_by_status": m.messages_by_status,
        "sends_by_status": m.sends_by_status,
        "sends_skipped_by_reason": m.sends_skipped_by_reason,
        "sends_failed_by_reason": m.sends_failed_by_reason,
        "attention": attention,
        "trend": _trend_points(m.trend),
        "trend_prior": _trend_points(ghost_trend),
    }


def _mis_slice_from_row(row: dict[str, Any], period_scope: str) -> dict[str, Any]:
    """Project one row of :func:`mis_monthly_series` into the DTO the table uses.

    Keeps the UI type (``{scope, pending_human, approved, rejected, total}``)
    stable while switching the source of truth to a single query.
    """
    return {
        "scope": period_scope,
        "pending_human": int(row.get("pending_human") or 0),
        "approved": int(row.get("approved") or 0),
        "rejected": int(row.get("rejected") or 0),
        "total": int(row.get("total") or 0),
    }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=dict)
async def dashboard_summary(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """One-shot dashboard payload. All blocks computed in parallel."""

    (
        base,
        metrics_14d,
        email_14d_prior,
        email_30d,
        operator,
        integrations,
        aging,
        originators,
        wf_status,
        wf_failures,
        monthly_series,
        o2c_cur,
        top_clients,
        top_sites,
        renewal_list,
        renewal_summary,
        contract_stages,
        proc_throughput,
        proc_monthly_6m,
    ) = await asyncio.gather(
        build_analytics_summary(db, user),
        pipeline.compute_metrics(db, window="14d"),
        pipeline.compute_trend_only(db, window="14d", offset_windows=1),
        pipeline.message_and_send_status_counts(db, window="30d"),
        build_operator_surface(db, user),
        integration_health_snapshot(db),
        approval_aging_buckets(db, user),
        approval_top_originators(db, user, limit=5),
        workflow_status_distribution(db, user, days=30),
        workflow_failures_by_key(db, user, days=30, limit=6),
        mis_monthly_series(months=6),
        build_o2c_period("current_month"),
        top_billing_clients_6m(limit=6, months=6),
        top_service_sites_6m(limit=6, months=6),
        contracts_renewal_watch(
            days_ahead=180, expired_lookback_days=730, limit=30
        ),
        contracts_renewal_summary(
            days_ahead=180, expired_lookback_days=730
        ),
        contract_stage_distribution(),
        procurement_ticket_throughput(db, days=30),
        procurement_monthly_series(db, months=6),
    )

    email_block = _email_block_from_metrics(metrics_14d, ghost_trend=email_14d_prior)

    # The MIS breakdown tables, the prior-month distribution donut and the
    # month-over-month trend all read from ``monthly_series`` so they cannot
    # drift — same SQL, same calendar bucketing.
    mis_block: dict[str, Any] | None = None
    if monthly_series:
        cur_row = monthly_series[-1] if monthly_series else None
        prior_row = monthly_series[-2] if len(monthly_series) >= 2 else None
        mis_block = {
            "current_month": _mis_slice_from_row(cur_row, "current_month") if cur_row else None,
            "prior_month": _mis_slice_from_row(prior_row, "prior_month") if prior_row else None,
            "monthly": monthly_series,
        }

    # Always return the O2C envelope so the home page can render new cards even
    # when the billing DB is down or a month has zero runs — empty arrays read
    # as "no data yet", not a missing feature.
    o2c_block: dict[str, Any] = {
        "current_month": o2c_cur,
        "top_clients": list(top_clients or []),
        "top_sites": list(top_sites or []),
    }

    renewals_block: dict[str, Any] = {
        "summary": renewal_summary,
        "contracts": list(renewal_list or []),
        "stages": list(contract_stages or []),
    }

    return {
        **base,
        "approvals": {
            "aging": aging,
            "top_originators": originators,
        },
        "email": email_block,
        "email_30d": {
            "window": "30d",
            "messages_by_status": email_30d.get("messages_by_status", {}),
            "sends_by_status": email_30d.get("sends_by_status", {}),
        },
        "operator": operator,
        "workflow_runs": {
            "status_distribution": wf_status,
            "failures_by_key": wf_failures,
        },
        "mis": mis_block,
        "o2c": o2c_block,
        "contracts": renewals_block,
        "procurement": {**proc_throughput, "monthly_6m": proc_monthly_6m or []},
        "integrations": integrations,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
