"""Readiness snapshot — DB, Redis, integration registry, operational counters."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CatalogAgent,
    IntegrationHealth,
    PostHitlOutbox,
    PostHitlOutboxStatus,
    UserOAuthToken,
)


def integration_status() -> dict[str, Any]:
    """Backward-compatible stub; prefer `readiness_snapshot` from HTTP `/api/health/ready`."""
    return {"note": "use GET /api/health/ready for live snapshot"}


async def readiness_snapshot(db: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {"postgres": False, "redis": False}

    try:
        await db.execute(text("SELECT 1"))
        out["postgres"] = True
    except Exception as e:
        out["postgres_error"] = str(e)[:300]
        return out

    n_int = await db.scalar(select(func.count()).select_from(IntegrationHealth))
    out["integrations_registered"] = int(n_int or 0)

    n_catalog = await db.scalar(select(func.count()).select_from(CatalogAgent))
    out["catalog_agents"] = int(n_catalog or 0)

    n_pending = await db.scalar(
        select(func.count())
        .select_from(PostHitlOutbox)
        .where(PostHitlOutbox.status == PostHitlOutboxStatus.PENDING.value)
    )
    out["post_hitl_outbox_pending"] = int(n_pending or 0)

    n_oauth = await db.scalar(select(func.count()).select_from(UserOAuthToken))
    out["oauth_connections"] = int(n_oauth or 0)

    try:
        from app.infra.redis_client import get_redis

        r = get_redis()
        await r.ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)[:200]

    out["status"] = "ready" if out["postgres"] and out["redis"] else "degraded"
    return out
