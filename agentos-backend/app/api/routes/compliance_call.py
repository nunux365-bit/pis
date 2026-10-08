"""Compliance call quality — list/detail workflow_runs for the dashboard."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_compliance_dashboard_access
from app.db.models import User
from app.db.session import get_db
from app.services.compliance_dashboard import (
    EXPORT_COLUMNS,
    compliance_dashboard_grade_distribution,
    compliance_dashboard_summary,
    compliance_dashboard_timeseries,
    export_compliance_dashboard_runs,
    get_compliance_dashboard_run,
    iso_to_ist_display,
    list_compliance_dashboard_doctors,
    list_compliance_dashboard_runs,
)

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("/runs/summary")
async def compliance_runs_summary(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(7, ge=1, le=90),
):
    return await compliance_dashboard_summary(db, days=days)


@router.get("/runs/timeseries")
async def compliance_runs_timeseries(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
    granularity: Literal["day", "week"] = Query("day"),
):
    return await compliance_dashboard_timeseries(db, days=days, granularity=granularity)


@router.get("/runs/grade_distribution")
async def compliance_runs_grade_distribution(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
):
    return await compliance_dashboard_grade_distribution(db, days=days)


@router.get("/runs")
async def list_compliance_runs(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    since: datetime | None = Query(None, description="Inclusive lower bound (UTC) on created_at"),
    until: datetime | None = Query(None, description="Inclusive upper bound (UTC) on created_at"),
    search: str | None = Query(
        None,
        description="Case-insensitive match on filename, doctor, source file id, or MySQL second-opinion conversation id",
    ),
    status_filter: str | None = Query(None, alias="status"),
    grade: Annotated[list[str] | None, Query(description="Repeatable; multi-select grade filter")] = None,
    doctor: str | None = Query(None, description="Case-insensitive match on doctor name or slug"),
    score_min: float | None = Query(None, ge=0, le=100, description="Inclusive lower bound on composite_pct"),
    score_max: float | None = Query(
        None, ge=0, le=100, description="Upper bound on composite_pct (exclusive below 100, inclusive at 100)"
    ),
    sheet_appended: bool | None = Query(None, description="Filter on the SHEET column (Yes/No)"),
    sort_key: str = Query(
        "created_at",
        description="created_at|doctor_slug|status|composite_pct|grade|filename",
    ),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
):
    runs, total = await list_compliance_dashboard_runs(
        db,
        limit=limit,
        offset=offset,
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grade,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )
    return {"runs": runs, "total": total, "limit": limit, "offset": offset}


@router.get("/runs/doctors")
async def compliance_runs_doctors(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(1000, ge=1, le=5000),
):
    """Distinct doctors for the searchable doctor filter."""
    return {"doctors": await list_compliance_dashboard_doctors(db, limit=limit)}


@router.get("/runs/export")
async def export_compliance_runs(
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    search: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    grade: Annotated[list[str] | None, Query()] = None,
    doctor: str | None = Query(None),
    score_min: float | None = Query(None, ge=0, le=100),
    score_max: float | None = Query(None, ge=0, le=100),
    sheet_appended: bool | None = Query(None),
    sort_key: str = Query("created_at"),
    sort_dir: Literal["asc", "desc"] = Query("desc"),
):
    """Bulk export of the currently filtered result set as CSV."""
    rows = await export_compliance_dashboard_runs(
        db,
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grade,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for _, header in EXPORT_COLUMNS])
    for r in rows:
        out: list[str] = []
        for key, _ in EXPORT_COLUMNS:
            if key == "created_at_ist":
                # Derived here so the CSV always matches the IST-based dashboard.
                out.append(iso_to_ist_display(r.get("created_at")))
                continue
            v = r.get(key)
            if v is None:
                out.append("")
            elif key == "sheet_appended":
                out.append("Yes" if v is True else "No")
            else:
                out.append(str(v))
        writer.writerow(out)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"call-quality-{stamp}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}")
async def get_compliance_run(
    run_id: UUID,
    user: Annotated[User, Depends(require_compliance_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await get_compliance_dashboard_run(db, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row
