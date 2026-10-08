"""Compliance call-quality dashboard — read-only queries on ``compliance_call_runs``.

List/count/summary/timeseries read the projection table (narrow rows, indexed columns).
Run detail still loads full ``workflow_runs`` JSON (transcript, eval, vendor payloads).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import asc, case, desc, exists, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import ComplianceCallRun, WorkflowRun, WorkflowRunStatus
from app.services.workflow_runner import CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY

ComplianceSortKey = Literal["created_at", "doctor_slug", "status", "composite_pct", "grade", "filename"]
ComplianceGranularity = Literal["day", "week"]

_SORT_KEYS: frozenset[str] = frozenset(
    {"created_at", "doctor_slug", "status", "composite_pct", "grade", "filename"}
)


def _parse_since_until(
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return since, until


def _search_clause(search: str | None) -> ColumnElement[bool] | None:
    """Substring match on projection columns and MySQL second-opinion conversation id in ``input_data``."""
    if not search or not (t := search.strip()):
        return None
    pat = f"%{t}%"
    fn = ComplianceCallRun.filename
    ds = ComplianceCallRun.doctor_slug
    dn = ComplianceCallRun.doctor_name
    sid = ComplianceCallRun.source_file_id
    conv_in_input = or_(
        WorkflowRun.input_data["mysql_second_opinion_conversation_id"].as_string().ilike(pat),
        WorkflowRun.input_data["second_opinion_conversation_id"].as_string().ilike(pat),
    )
    soc_match = exists(
        select(1).where(
            WorkflowRun.id == ComplianceCallRun.workflow_run_id,
            WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
            conv_in_input,
        )
    )
    return or_(fn.ilike(pat), ds.ilike(pat), dn.ilike(pat), sid.ilike(pat), soc_match)


def _doctor_clause(doctor: str | None) -> ColumnElement[bool] | None:
    """Case-insensitive substring match on doctor name or slug."""
    if not doctor or not (t := doctor.strip()):
        return None
    pat = f"%{t}%"
    return or_(
        ComplianceCallRun.doctor_name.ilike(pat),
        ComplianceCallRun.doctor_slug.ilike(pat),
    )


def _score_clauses(
    score_min: float | None,
    score_max: float | None,
) -> list[ColumnElement[bool]]:
    """Bracket bounds on ``composite_pct``.

    Lower bound is inclusive; upper bound is exclusive so adjacent preset brackets
    (0-25, 25-50, 50-75, 75-100) do not double-count a row sitting on the boundary.
    The top bracket is closed so a perfect 100 is still selectable.
    """
    parts: list[ColumnElement[bool]] = []
    if score_min is not None:
        parts.append(ComplianceCallRun.composite_pct >= score_min)
    if score_max is not None:
        if score_max >= 100:
            parts.append(ComplianceCallRun.composite_pct <= score_max)
        else:
            parts.append(ComplianceCallRun.composite_pct < score_max)
    return parts


def _base_where(
    *,
    since: datetime | None,
    until: datetime | None,
    status_filter: str | None,
    search: str | None,
    grades: Sequence[str] | None = None,
    doctor: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sheet_appended: bool | None = None,
) -> list[ColumnElement[bool]]:
    since, until = _parse_since_until(since, until)
    parts: list[ColumnElement[bool]] = []
    if since is not None:
        parts.append(ComplianceCallRun.created_at >= since)
    if until is not None:
        parts.append(ComplianceCallRun.created_at <= until)
    if status_filter:
        parts.append(ComplianceCallRun.status == status_filter)
    sc = _search_clause(search)
    if sc is not None:
        parts.append(sc)
    if grades:
        wanted = [g.strip() for g in grades if g and g.strip()]
        if wanted:
            parts.append(ComplianceCallRun.grade.in_(wanted))
    dc = _doctor_clause(doctor)
    if dc is not None:
        parts.append(dc)
    parts.extend(_score_clauses(score_min, score_max))
    if sheet_appended is not None:
        parts.append(ComplianceCallRun.sheet_appended.is_(sheet_appended))
    return parts


def _composite_pct_sort_expr() -> Any:
    col = ComplianceCallRun.composite_pct
    return col


def _order_by_clause(sort_key: str, sort_dir: str) -> Any:
    asc_mode = sort_dir.lower() == "asc"
    sk = sort_key if sort_key in _SORT_KEYS else "created_at"

    if sk == "composite_pct":
        col = _composite_pct_sort_expr()
        return nulls_last(asc(col)) if asc_mode else nulls_last(desc(col))
    if sk == "grade":
        col = ComplianceCallRun.grade
        return nulls_last(asc(col)) if asc_mode else nulls_last(desc(col))
    if sk == "filename":
        col = ComplianceCallRun.filename
        return asc(col) if asc_mode else desc(col)
    if sk == "doctor_slug":
        col = ComplianceCallRun.doctor_slug
        return asc(col) if asc_mode else desc(col)
    if sk == "status":
        col = ComplianceCallRun.status
        return asc(col) if asc_mode else desc(col)
    col = ComplianceCallRun.created_at
    return asc(col) if asc_mode else desc(col)


def _row_to_list_item(
    r: ComplianceCallRun,
    *,
    second_opinion_conversation_id: int | None = None,
) -> dict[str, Any]:
    pct = r.composite_pct
    pct_out: float | None = None
    if pct is not None:
        try:
            pct_out = float(pct)
        except Exception:
            pct_out = None
    return {
        "id": str(r.workflow_run_id),
        "status": r.status,
        "created_at": r.created_at.isoformat(),
        "doctor_slug": r.doctor_slug,
        "doctor_name": r.doctor_name,
        "filename": r.filename,
        "source_file_id": r.source_file_id,
        "source_type": r.source_type,
        "composite_pct": pct_out,
        "grade": r.grade,
        "grade_label": r.grade_label,
        "error_message": r.error_message,
        "sheet_appended": r.sheet_appended,
        "mysql_second_opinion_conversation_id": second_opinion_conversation_id,
    }


async def compliance_dashboard_summary(db: AsyncSession, *, days: int) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    n_ok = await db.scalar(
        select(func.count()).where(
            ComplianceCallRun.created_at >= since,
            ComplianceCallRun.status == WorkflowRunStatus.COMPLETED.value,
        )
    )
    n_bad = await db.scalar(
        select(func.count()).where(
            ComplianceCallRun.created_at >= since,
            ComplianceCallRun.status == WorkflowRunStatus.FAILED.value,
        )
    )
    return {
        "window_days": days,
        "completed": int(n_ok or 0),
        "failed": int(n_bad or 0),
        "workflow_key": CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
    }


async def count_compliance_dashboard_runs(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    grades: Sequence[str] | None = None,
    doctor: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sheet_appended: bool | None = None,
) -> int:
    where_parts = _base_where(
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grades,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
    )
    q = select(func.count()).select_from(ComplianceCallRun)
    if where_parts:
        q = q.where(*where_parts)
    n = await db.scalar(q)
    return int(n or 0)


async def list_compliance_dashboard_runs(
    db: AsyncSession,
    *,
    limit: int,
    offset: int = 0,
    since: datetime | None = None,
    until: datetime | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    grades: Sequence[str] | None = None,
    doctor: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sheet_appended: bool | None = None,
    sort_key: str = "created_at",
    sort_dir: str = "desc",
) -> tuple[list[dict[str, Any]], int]:
    cap = min(max(1, limit), 200)
    offset = max(0, offset)
    where_parts = _base_where(
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grades,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
    )
    total = await count_compliance_dashboard_runs(
        db,
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grades,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
    )
    q = select(ComplianceCallRun)
    if where_parts:
        q = q.where(*where_parts)
    q = q.order_by(_order_by_clause(sort_key, sort_dir)).offset(offset).limit(cap)
    rows = (await db.execute(q)).scalars().all()

    # Surface MySQL second_opinion_conversation_id in summary rows without schema changes.
    by_wr_id: dict[UUID, int | None] = {}
    wr_ids = [r.workflow_run_id for r in rows if r.workflow_run_id]
    if wr_ids:
        in_q = select(WorkflowRun.id, WorkflowRun.input_data).where(WorkflowRun.id.in_(wr_ids))
        for rid, inp in (await db.execute(in_q)).all():
            v: int | None = None
            if isinstance(inp, dict):
                raw = inp.get("mysql_second_opinion_conversation_id")
                if raw is None:
                    raw = inp.get("second_opinion_conversation_id")
                if raw is not None:
                    try:
                        v = int(raw)
                    except (TypeError, ValueError):
                        v = None
            by_wr_id[rid] = v

    return [
        _row_to_list_item(
            r,
            second_opinion_conversation_id=by_wr_id.get(r.workflow_run_id),
        )
        for r in rows
    ], total


async def list_compliance_dashboard_doctors(
    db: AsyncSession,
    *,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Distinct doctors seen in the projection, for the searchable doctor filter."""
    cap = min(max(1, limit), 5000)
    q = (
        select(
            ComplianceCallRun.doctor_slug,
            ComplianceCallRun.doctor_name,
            func.count().label("n"),
        )
        .where(
            or_(
                ComplianceCallRun.doctor_slug.isnot(None),
                ComplianceCallRun.doctor_name.isnot(None),
            )
        )
        .group_by(ComplianceCallRun.doctor_slug, ComplianceCallRun.doctor_name)
        .order_by(desc(func.count()))
        .limit(cap)
    )
    rows = (await db.execute(q)).all()
    return [
        {
            "doctor_slug": r.doctor_slug,
            "doctor_name": r.doctor_name,
            "label": r.doctor_name or r.doctor_slug or "—",
            "run_count": int(r.n or 0),
        }
        for r in rows
    ]


EXPORT_MAX_ROWS = 20_000

#: Dashboard dates are IST calendar days (fixed UTC+5:30, no DST); rows are stored UTC.
IST = timezone(timedelta(hours=5, minutes=30))


def iso_to_ist_display(iso: str | None) -> str:
    """Render a stored UTC timestamp as IST, matching what the dashboard shows."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_at_ist", "Time (IST)"),
    ("created_at", "Time (UTC ISO)"),
    ("doctor_name", "Doctor"),
    ("doctor_slug", "Doctor slug"),
    ("sheet_appended", "Sheet"),
    ("status", "Status"),
    ("composite_pct", "Score %"),
    ("grade", "Grade"),
    ("grade_label", "Grade label"),
    ("filename", "File"),
    ("source_type", "Source type"),
    ("source_file_id", "Source file id"),
    ("mysql_second_opinion_conversation_id", "SO Conv ID"),
    ("error_message", "Error"),
    ("id", "Run id"),
)


async def export_compliance_dashboard_runs(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    grades: Sequence[str] | None = None,
    doctor: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    sheet_appended: bool | None = None,
    sort_key: str = "created_at",
    sort_dir: str = "desc",
    max_rows: int = EXPORT_MAX_ROWS,
) -> list[dict[str, Any]]:
    """All rows matching the current filter set, for CSV export (bounded by ``max_rows``)."""
    cap = min(max(1, max_rows), EXPORT_MAX_ROWS)
    rows, _ = await list_compliance_dashboard_runs(
        db,
        limit=200,
        offset=0,
        since=since,
        until=until,
        status_filter=status_filter,
        search=search,
        grades=grades,
        doctor=doctor,
        score_min=score_min,
        score_max=score_max,
        sheet_appended=sheet_appended,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )
    out = list(rows)
    # list_* is capped at 200/page; keep paging until the filter set is exhausted.
    while len(out) < cap and len(rows) == 200:
        rows, _ = await list_compliance_dashboard_runs(
            db,
            limit=200,
            offset=len(out),
            since=since,
            until=until,
            status_filter=status_filter,
            search=search,
            grades=grades,
            doctor=doctor,
            score_min=score_min,
            score_max=score_max,
            sheet_appended=sheet_appended,
            sort_key=sort_key,
            sort_dir=sort_dir,
        )
        if not rows:
            break
        out.extend(rows)
    return out[:cap]


async def get_compliance_dashboard_run(
    db: AsyncSession,
    run_id: UUID,
) -> dict[str, Any] | None:
    r = await db.get(WorkflowRun, run_id)
    if not r or r.workflow_key != CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY:
        return None
    inp = r.input_data if isinstance(r.input_data, dict) else {}
    outp = r.output_data if isinstance(r.output_data, dict) else {}
    return {
        "id": str(r.id),
        "status": r.status,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "input_data": inp,
        "output_data": outp,
        "error_message": r.error_message,
    }


async def compliance_dashboard_timeseries(
    db: AsyncSession,
    *,
    days: int,
    granularity: ComplianceGranularity = "day",
) -> dict[str, Any]:
    """Bucketed run counts by ``created_at`` for compliance_call projection rows."""
    days = min(max(1, days), 366)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trunc_unit = "week" if granularity == "week" else "day"
    bucket = func.date_trunc(trunc_unit, ComplianceCallRun.created_at).label("bucket")

    completed_val = WorkflowRunStatus.COMPLETED.value
    failed_val = WorkflowRunStatus.FAILED.value

    q = (
        select(
            bucket,
            func.count().label("total"),
            func.sum(case((ComplianceCallRun.status == completed_val, 1), else_=0)).label("completed"),
            func.sum(case((ComplianceCallRun.status == failed_val, 1), else_=0)).label("failed"),
        )
        .where(ComplianceCallRun.created_at >= since)
        .group_by(bucket)
        .order_by(bucket.asc())
    )
    rows = (await db.execute(q)).all()
    points: list[dict[str, Any]] = []
    for row in rows:
        b = row.bucket
        if b is None:
            continue
        iso = b.isoformat() if hasattr(b, "isoformat") else str(b)
        points.append(
            {
                "bucket_start": iso,
                "total": int(row.total or 0),
                "completed": int(row.completed or 0),
                "failed": int(row.failed or 0),
            }
        )

    return {
        "window_days": days,
        "granularity": granularity,
        "points": points,
    }


async def compliance_dashboard_grade_distribution(
    db: AsyncSession,
    *,
    days: int,
) -> dict[str, Any]:
    """Count completed runs by letter grade from projection ``grade``."""
    days = min(max(1, days), 366)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    grade_col = ComplianceCallRun.grade.label("grade")

    q = (
        select(grade_col, func.count().label("n"))
        .where(
            ComplianceCallRun.created_at >= since,
            ComplianceCallRun.status == WorkflowRunStatus.COMPLETED.value,
        )
        .group_by(grade_col)
        .order_by(desc(func.count()))
    )
    rows = (await db.execute(q)).all()
    slices: list[dict[str, Any]] = []
    for row in rows:
        label = row.grade if row.grade is not None else "—"
        slices.append({"grade": str(label), "count": int(row.n or 0)})
    return {"window_days": days, "slices": slices}


__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_MAX_ROWS",
    "ComplianceGranularity",
    "ComplianceSortKey",
    "compliance_dashboard_grade_distribution",
    "compliance_dashboard_summary",
    "compliance_dashboard_timeseries",
    "count_compliance_dashboard_runs",
    "export_compliance_dashboard_runs",
    "get_compliance_dashboard_run",
    "iso_to_ist_display",
    "list_compliance_dashboard_doctors",
    "list_compliance_dashboard_runs",
]
