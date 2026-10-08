"""Responder eval dashboard API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.responder_eval.dashboard import (
    eval_charts,
    eval_grade_distribution,
    eval_pending_dumps,
    eval_resolution_distribution,
    eval_score_buckets,
    eval_summary,
    eval_timeseries,
    eval_top_issues,
)
from app.agents.responder_eval.handoff_dashboard import (
    eval_handoff_analysis,
    eval_handoff_chat_ids,
)
from app.agents.responder_eval.rca_dump import retry_dump_eval
from app.agents.whatsapp_jit_hold.order_tracker import query_jit_hold_orders
from app.api.deps import require_responder_eval_dashboard_access
from app.config.settings import settings
from app.db.models import User
from app.db.session import get_db
from app.services.responder_eval_dashboard import get_responder_eval_run, list_responder_eval_runs

router = APIRouter(prefix="/responder-evals", tags=["responder-evals"])


class EvalRunSummary(BaseModel):
    id: str
    chat_id: str
    order_id: str | None
    composite_score: int | None
    letter_grade: str
    bot_grade: str
    human_grade: str
    eval_status: str
    eval_version: str
    created_at: str


class EvalRunList(BaseModel):
    items: list[EvalRunSummary]
    total: int


def _require_enabled() -> None:
    if not settings.responder_eval_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Responder eval is disabled")


class JitHoldOrderRow(BaseModel):
    parent_order_id: str
    status: str
    triggered_at: str
    updated_at: str


class JitHoldOrderStats(BaseModel):
    total: int
    triggered: int
    split_done: int
    kept_original: int


class JitHoldOrderList(BaseModel):
    items: list[JitHoldOrderRow]
    total: int
    stats: JitHoldOrderStats


@router.get("/jit-hold-orders", response_model=JitHoldOrderList)
async def jit_hold_orders(
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(7, ge=1, le=366),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order_status: Literal["triggered", "split_done", "kept_original"] | None = Query(
        None, alias="status"
    ),
    search: str | None = Query(None, max_length=64),
):
    return await query_jit_hold_orders(
        db, days=days, status=order_status, search=search, limit=limit, offset=offset
    )


@router.get("/hand-off-analysis")
async def hand_off_analysis(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await eval_handoff_analysis(db)


@router.get("/hand-off-analysis/chat-ids")
async def hand_off_analysis_chat_ids(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    bucket: str = Query(..., min_length=1, max_length=64),
    sub_bucket: str | None = Query(None, max_length=128),
    scope: Literal["day", "last_7", "mtd"] = Query("last_7"),
    day: date | None = Query(None),
):
    if scope == "day" and day is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="day is required when scope=day")
    return await eval_handoff_chat_ids(db, bucket=bucket, sub_bucket=sub_bucket, scope=scope, day=day)


@router.get("/charts")
async def runs_charts(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
    granularity: Literal["day", "week"] = "day",
    issues_limit: int = Query(10, ge=1, le=20),
):
    return await eval_charts(
        db, days=days, granularity=granularity, issues_limit=issues_limit
    )


@router.get("/summary")
async def runs_summary(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
):
    return await eval_summary(db, days=days)


@router.get("/grade-distribution")
async def runs_grade_distribution(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
):
    return await eval_grade_distribution(db, days=days)


@router.get("/timeseries")
async def runs_timeseries(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
    granularity: Literal["day", "week"] = "day",
):
    return await eval_timeseries(db, days=days, granularity=granularity)


@router.get("/score-buckets")
async def runs_score_buckets(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
):
    return await eval_score_buckets(db, days=days)


@router.get("/resolution-distribution")
async def runs_resolution_distribution(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
):
    return await eval_resolution_distribution(db, days=days)


@router.get("/top-issues")
async def runs_top_issues(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(30, ge=1, le=366),
    limit: int = Query(10, ge=1, le=20),
):
    return await eval_top_issues(db, days=days, limit=limit)


@router.get("/pending-dumps")
async def runs_pending_dumps(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await eval_pending_dumps(db, limit=limit, offset=offset)


@router.post("/dumps/{chat_id}/retry", status_code=status.HTTP_204_NO_CONTENT)
async def retry_eval_dump(
    chat_id: str,
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
):
    ok = await retry_dump_eval(chat_id)
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="Dump not found or not in a retriable state",
        )


@router.get("", response_model=EvalRunList)
async def list_eval_runs(
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    days: int | None = Query(None, ge=1, le=366),
    grade: str | None = None,
    segment: Literal["composite", "bot", "bot_closed", "bot_pre_handoff", "human"] = Query(
        "composite"
    ),
    eval_status: str | None = None,
    search: str | None = Query(None, max_length=128),
    reason: str | None = Query(None, max_length=256),
):
    rows, total = await list_responder_eval_runs(
        db,
        limit=limit,
        offset=offset,
        days=days,
        grade=grade,
        segment=segment,
        eval_status=eval_status,
        search=search,
        reason=reason,
    )
    items = [
        EvalRunSummary(
            id=str(r.id),
            chat_id=r.chat_id,
            order_id=r.order_id,
            composite_score=r.composite_score,
            letter_grade=r.letter_grade,
            bot_grade=r.bot_grade,
            human_grade=r.human_grade,
            eval_status=r.eval_status,
            eval_version=r.eval_version,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
    return EvalRunList(items=items, total=total)


@router.get("/runs/{run_id}")
async def get_eval_run(
    run_id: uuid.UUID,
    _enabled: Annotated[None, Depends(_require_enabled)],
    _user: Annotated[User, Depends(require_responder_eval_dashboard_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await get_responder_eval_run(db, run_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Eval run not found")
    return row
