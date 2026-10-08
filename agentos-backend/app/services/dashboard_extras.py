"""Lightweight home-dashboard counts on the main app DB (notifications, runs, PR/PO)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    IntegrationHealth,
    Notification,
    ProcurementTicket,
    User,
    WorkflowRun,
    WorkflowRunStatus,
)


def _wf_active_statuses() -> tuple[str, ...]:
    return (
        WorkflowRunStatus.QUEUED.value,
        WorkflowRunStatus.RUNNING.value,
        WorkflowRunStatus.AWAITING_HITL.value,
    )


async def build_operator_surface(db: AsyncSession, user: User) -> dict[str, Any]:
    """In-app activity the dashboard should surface without extra client round-trips."""

    uid: UUID = user.id
    since_30d = datetime.now(UTC) - timedelta(days=30)

    unread = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == uid, Notification.read.is_(False))
            )
        ).scalar()
        or 0
    )

    active_runs = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkflowRun)
                .where(WorkflowRun.user_id == uid, WorkflowRun.status.in_(_wf_active_statuses()))
            )
        ).scalar()
        or 0
    )
    failed_30d = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkflowRun)
                .where(
                    WorkflowRun.user_id == uid,
                    WorkflowRun.status == WorkflowRunStatus.FAILED.value,
                    WorkflowRun.created_at >= since_30d,
                )
            )
        ).scalar()
        or 0
    )

    my_tickets = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ProcurementTicket)
                .where(ProcurementTicket.created_by_user_id == uid)
            )
        ).scalar()
        or 0
    )
    pending_sap = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ProcurementTicket)
                .where(
                    ProcurementTicket.created_by_user_id == uid,
                    ProcurementTicket.sap_id.is_(None),
                )
            )
        ).scalar()
        or 0
    )

    return {
        "notifications_unread": unread,
        "workflow": {
            "active_runs": active_runs,
            "failed_30d": failed_30d,
        },
        "procurement": {
            "my_tickets": my_tickets,
            "pending_sap_id": pending_sap,
        },
    }


async def integration_health_snapshot(db: AsyncSession) -> list[dict[str, Any]]:
    """Flat projection of :class:`IntegrationHealth` for the dashboard strip.

    Returned ordered by name for a deterministic UI. Safe for all users — the
    table is global (there's one row per integration, not per-user).
    """

    rows = (
        await db.execute(
            select(
                IntegrationHealth.name,
                IntegrationHealth.system_type,
                IntegrationHealth.status,
                IntegrationHealth.health_pct,
                IntegrationHealth.last_sync_at,
                IntegrationHealth.last_error,
                IntegrationHealth.updated_at,
            ).order_by(IntegrationHealth.name)
        )
    ).all()

    def _iso(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    out: list[dict[str, Any]] = []
    for name, system_type, status, health_pct, last_sync_at, last_error, updated_at in rows:
        try:
            pct = float(health_pct or 0)
        except (TypeError, ValueError):
            pct = 0.0
        out.append(
            {
                "name": name,
                "system_type": system_type,
                "status": status,
                "health_pct": pct,
                "last_sync_at": _iso(last_sync_at),
                "last_error": last_error,
                "updated_at": _iso(updated_at),
            }
        )
    return out
