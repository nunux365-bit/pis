"""Prosight API routes — anomaly dashboard endpoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agents.optimus import config
from app.agents.optimus.prosight.databricks_sync import sync_prosight_from_databricks
from app.config.settings import settings
from app.agents.optimus.prosight.service import (
    DEFAULT_ACTIONABLES_LIMIT,
    MAX_ACTIONABLES_LIMIT,
    get_latest_snapshot,
    get_snapshot_by_date,
    is_databricks_configured,
    list_actionables,
    list_snapshots,
    update_actionable_feedback,
    upsert_snapshot,
)
from app.api.deps import get_current_user, require_roles
from app.db.models import Feature, User, UserRole

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prosight", tags=["prosight"])

# Timeout for synchronous Databricks sync (5 minutes)
SYNC_TIMEOUT_SECONDS = 300


# ─────────────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────────────


def _require_prosight() -> None:
    """Check if Prosight is enabled."""
    if not config.PROSIGHT_ENABLED:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prosight is disabled",
        )


async def require_prosight_access(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Prosight access check — feature flag, then the shared access policy.

    Mirrors ``require_optimus_access``: authentication plus the feature flag are
    the only gates. The rule and the reasoning behind it (the 1mg boundary is
    the SSO domain allowlist, so ``prosight_user`` is vestigial) live once in
    ``FEATURE_ACCESS`` in ``app.security.rbac``. Admin operations remain gated
    by ``require_prosight_admin``.
    """
    _require_prosight()

    if user.has_feature_access(Feature.PROSIGHT):
        return user

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail="Prosight access required. Contact your administrator.",
    )


def require_prosight_admin(user: Annotated[User, Depends(require_roles(UserRole.OPTIMUS_ADMIN))]) -> User:
    """Require system_admin or optimus_admin for Prosight management operations."""
    _require_prosight()
    return user


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────


class SnapshotUploadRequest(BaseModel):
    """Request to upload a Prosight snapshot."""

    snapshot_date: date
    data: dict[str, Any]
    model_version: str | None = None
    total_series: int | None = None
    qualified_flagged: int | None = None


class ActionableFeedbackRequest(BaseModel):
    """User feedback on an actionable. Null clears an answer."""

    is_actionable: bool | None = None
    days_saved: float | None = Field(default=None, ge=0, le=365)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/runs/latest")
async def get_latest_run(
    user: Annotated[User, Depends(require_prosight_access)],
):
    """Get the latest Prosight dashboard snapshot.

    Returns the full dashboard JSON payload including summary, distribution,
    top5, and tree structure.
    """
    snapshot = await get_latest_snapshot()
    if not snapshot:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="No Prosight data available. Sync from Databricks or upload a snapshot.",
        )

    # Return just the data payload (what the frontend expects)
    return snapshot["data"]


@router.get("/runs/{snapshot_date}")
async def get_run_by_date(
    snapshot_date: date,
    user: Annotated[User, Depends(require_prosight_access)],
):
    """Get a specific date's Prosight dashboard snapshot."""
    snapshot = await get_snapshot_by_date(snapshot_date)
    if not snapshot:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No Prosight data for {snapshot_date}",
        )

    return snapshot["data"]


@router.get("/snapshots")
async def list_all_snapshots(
    user: Annotated[User, Depends(require_prosight_access)],
    limit: int = 30,
):
    """List available Prosight snapshots (metadata only)."""
    snapshots = await list_snapshots(limit=limit)
    return {"snapshots": snapshots, "total": len(snapshots)}


# ─────────────────────────────────────────────────────────────────────────────
# Admin Endpoints — Sync & Upload
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/sync/trigger")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(require_prosight_admin)],
):
    """Manually trigger a Databricks sync. Admin only.

    Runs the sync in the background and returns immediately.
    """
    if not is_databricks_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Databricks not configured. Set PROSIGHT_DATABRICKS_* environment variables.",
        )

    background_tasks.add_task(sync_prosight_from_databricks)

    log.info("Prosight sync triggered by admin %s", user.email)
    return {
        "status": "triggered",
        "message": "Databricks sync started in background",
    }


@router.post("/sync/now")
async def sync_now(
    user: Annotated[User, Depends(require_prosight_admin)],
):
    """Synchronously sync from Databricks. Admin only.

    Waits for sync to complete and returns result.
    Times out after 5 minutes to prevent hanging requests.
    """
    if not is_databricks_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Databricks not configured. Set PROSIGHT_DATABRICKS_* environment variables.",
        )

    log.info("Prosight sync (sync) triggered by admin %s", user.email)

    try:
        result = await asyncio.wait_for(
            sync_prosight_from_databricks(),
            timeout=SYNC_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        log.error("Prosight sync timed out after %d seconds", SYNC_TIMEOUT_SECONDS)
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Databricks sync timed out after {SYNC_TIMEOUT_SECONDS} seconds. Try using /sync/trigger for background sync.",
        )

    if result["status"] == "skipped":
        # Another sync (the daily job, or a concurrent admin request) holds the
        # lock. 409 rather than 500 — nothing failed, the work is already
        # underway and this request simply has nothing to do.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="A Prosight sync is already running. Wait for it to finish and retry.",
        )

    if result["status"] == "error":
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.get("error", "Sync failed"),
        )

    return result


@router.post("/snapshots/upload")
async def upload_snapshot(
    body: SnapshotUploadRequest,
    user: Annotated[User, Depends(require_prosight_admin)],
):
    """Directly upload a Prosight snapshot. Admin only.

    Use this to manually upload data without Databricks integration.
    """
    # Extract summary stats from data if not provided
    summary = body.data.get("summary", {})
    total_series = body.total_series or summary.get("total_series")
    qualified_flagged = body.qualified_flagged or summary.get("qualified_flagged")
    model_version = body.model_version or summary.get("model_version")

    snapshot_id = await upsert_snapshot(
        snapshot_date=body.snapshot_date,
        data=body.data,
        model_version=model_version,
        total_series=total_series,
        qualified_flagged=qualified_flagged,
    )

    log.info(
        "Prosight snapshot uploaded by %s: date=%s, id=%s",
        user.email,
        body.snapshot_date,
        snapshot_id,
    )

    return {
        "status": "uploaded",
        "snapshot_id": snapshot_id,
        "snapshot_date": body.snapshot_date.isoformat(),
    }


@router.get("/status")
async def prosight_status(
    user: Annotated[User, Depends(get_current_user)],
):
    """Get Prosight service status.

    Uses lightweight metadata query instead of fetching full snapshot data.
    """
    _require_prosight()

    # Use list_snapshots(limit=1) to get metadata only - avoids fetching large data blob
    snapshots = await list_snapshots(limit=1)
    latest = snapshots[0] if snapshots else None

    return {
        "service": "prosight",
        "status": "active" if latest else "no_data",
        "databricks_configured": is_databricks_configured(),
        "latest_snapshot": latest["snapshot_date"] if latest else None,
        "sync_hour": settings.prosight_sync_hour,  # Hour (IST) when daily sync runs
        "description": "Anomaly detection dashboard with Databricks integration",
    }


# ─────────────────────────────────────────────────────────────────────────────
# News Dashboard Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/news")
async def get_news_data(
    user: Annotated[User, Depends(require_prosight_access)],
):
    """Get Prosight news dashboard data (N dashboard format).

    Reads from the latest snapshot in the prosight_snapshots table.
    The snapshot's `data` column should contain the news format:
    - dates: list of available dates
    - data_by_date: daily data including today_rows, summary, etc.
    - series_timeseries: timeseries data for each series
    - feature_importance: model feature importance data

    This data is synced daily from Databricks.
    """
    snapshot = await get_latest_snapshot()
    if not snapshot:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="No Prosight data available. Sync from Databricks or upload a snapshot.",
        )

    data = snapshot.get("data")
    if not data:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="Prosight snapshot has no data. Re-sync from Databricks.",
        )

    # Validate the data has the expected news format
    if not isinstance(data, dict) or "dates" not in data:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Prosight snapshot data is not in expected news format. Expected keys: dates, data_by_date, series_timeseries.",
        )

    return JSONResponse(content=data)


# ─────────────────────────────────────────────────────────────────────────────
# Actionables Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/actionables")
async def get_actionables(
    user: Annotated[User, Depends(require_prosight_access)],
    bu: str | None = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_ACTIONABLES_LIMIT)
    ] = DEFAULT_ACTIONABLES_LIMIT,
):
    """List current actionables for a BU/day with user feedback, ordered by rank.

    status is "ok" (rows returned), "no_actionables" (day processed, upstream
    marker says nothing to act on), or "not_processed" (no rows for this BU/day).
    Newest days first; `truncated` is true when `limit` cut the result short.
    """
    result = await list_actionables(bu=bu, on_date=on_date, limit=limit)
    result["total"] = len(result["actionables"])
    return result


@router.put("/actionables/{action_hash}/feedback")
async def save_actionable_feedback(
    action_hash: str,
    body: ActionableFeedbackRequest,
    user: Annotated[User, Depends(require_prosight_access)],
):
    """Record feedback (actionable insight? / days of analysis saved) on an actionable.

    Shared team-wide — last write wins, with attribution. 404 if the hash no
    longer matches a current actionable (list changed after a re-sync).
    """
    updated = await update_actionable_feedback(
        action_hash=action_hash,
        is_actionable=body.is_actionable,
        days_saved=body.days_saved,
        user_email=user.email,
    )
    if updated is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="Actionable not found — the list may have been refreshed. Reload and retry.",
        )

    log.info(
        "Prosight actionable feedback by %s: hash=%s actionable=%s days=%s",
        user.email,
        action_hash[:12],
        body.is_actionable,
        body.days_saved,
    )
    return updated
