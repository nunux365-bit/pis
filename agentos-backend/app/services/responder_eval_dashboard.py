"""Responder eval dashboard — list/detail and aggregate queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.agents.responder_eval.cohorts import BOT_CLOSED, BOT_PRE_HANDOFF, BOT_PRESENT, HUMAN_PRESENT
from app.agents.responder_eval.constants import (
    EVAL_VERSION,
    LETTER_GRADE_NOT_GRADED,
    RUN_STATUS_COMPLETED,
)
from app.agents.responder_eval.denormalized import merge_eval_detail_json
from app.agents.responder_eval.models import ResponderEvalRun

GradeSegment = Literal["composite", "bot", "bot_closed", "bot_pre_handoff", "human"]
_LOW_GRADES = frozenset({"C", "D", "E"})


def _grade_column(segment: GradeSegment):
    if segment in ("bot", "bot_closed", "bot_pre_handoff"):
        return ResponderEvalRun.bot_grade
    if segment == "human":
        return ResponderEvalRun.human_grade
    return ResponderEvalRun.letter_grade


def _normalize_grade_filter(grade: str | None) -> str | None:
    if not grade:
        return None
    key = grade.upper()
    if key in {"N/A", "NA", "NG", "-"}:
        return LETTER_GRADE_NOT_GRADED
    return key


def _issues_column(segment: GradeSegment):
    if segment in ("bot", "bot_closed", "bot_pre_handoff"):
        return ResponderEvalRun.bot_issues
    if segment == "human":
        return ResponderEvalRun.human_issues
    return ResponderEvalRun.composite_issues


def _normalize_reason_filter(reason: str | None) -> str | None:
    if not reason or not (tag := reason.strip()):
        return None
    return tag[:256]


async def list_responder_eval_runs(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    days: int | None = None,
    grade: str | None = None,
    segment: GradeSegment = "composite",
    eval_status: str | None = None,
    search: str | None = None,
    reason: str | None = None,
) -> tuple[list[ResponderEvalRun], int]:
    q = (
        select(ResponderEvalRun)
        .options(
            load_only(
                ResponderEvalRun.id,
                ResponderEvalRun.chat_id,
                ResponderEvalRun.order_id,
                ResponderEvalRun.composite_score,
                ResponderEvalRun.letter_grade,
                ResponderEvalRun.bot_grade,
                ResponderEvalRun.human_grade,
                ResponderEvalRun.eval_status,
                ResponderEvalRun.eval_version,
                ResponderEvalRun.created_at,
            )
        )
        .where(ResponderEvalRun.eval_version == EVAL_VERSION)
    )
    cq = (
        select(func.count())
        .select_from(ResponderEvalRun)
        .where(ResponderEvalRun.eval_version == EVAL_VERSION)
    )
    if days is not None and days > 0:
        until = datetime.now(UTC)
        since = until - timedelta(days=days)
        window = (ResponderEvalRun.created_at >= since, ResponderEvalRun.created_at < until)
        q = q.where(*window)
        cq = cq.where(*window)

    grade_key = _normalize_grade_filter(grade)
    is_na_grade = grade_key == LETTER_GRADE_NOT_GRADED
    reason_key = _normalize_reason_filter(reason)

    # Scope table to the selected chart cohort (including N/A — matches na_count).
    if segment == "bot_closed":
        q = q.where(BOT_CLOSED)
        cq = cq.where(BOT_CLOSED)
    elif segment == "bot_pre_handoff":
        q = q.where(BOT_PRE_HANDOFF)
        cq = cq.where(BOT_PRE_HANDOFF)
    elif segment == "human":
        q = q.where(HUMAN_PRESENT)
        cq = cq.where(HUMAN_PRESENT)
    elif segment == "bot" and not is_na_grade and grade_key is not None:
        q = q.where(BOT_PRESENT)
        cq = cq.where(BOT_PRESENT)

    # Cohort charts are graded-only; keep the table on the same population unless the
    # user explicitly filters Status or N/A grade.
    if (
        segment in ("bot_closed", "bot_pre_handoff", "human")
        and not is_na_grade
        and not (eval_status and eval_status.strip())
    ):
        q = q.where(ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED)
        cq = cq.where(ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED)

    if grade_key is not None:
        grade_col = _grade_column(segment)
        q = q.where(grade_col == grade_key)
        cq = cq.where(grade_col == grade_key)
        # A–E and N/A both match graded chart slices / na_count (completed only).
        q = q.where(ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED)
        cq = cq.where(ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED)

    if eval_status and (status_key := eval_status.strip()):
        q = q.where(ResponderEvalRun.eval_status == status_key)
        cq = cq.where(ResponderEvalRun.eval_status == status_key)
    if reason_key:
        grade_col = _grade_column(segment)
        issues_col = _issues_column(segment)
        q = q.where(
            grade_col.in_(_LOW_GRADES),
            issues_col.contains([reason_key]),
            ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED,
        )
        cq = cq.where(
            grade_col.in_(_LOW_GRADES),
            issues_col.contains([reason_key]),
            ResponderEvalRun.eval_status == RUN_STATUS_COMPLETED,
        )
    if search and (t := search.strip()[:128]):
        # Case-sensitive prefix LIKE (order ids are PO… caps; chat_id is numeric).
        # Matches ix_*_prefix text_pattern_ops indexes; avoid ILIKE / lower().
        pat = f"{t}%"
        filt = or_(ResponderEvalRun.chat_id.like(pat), ResponderEvalRun.order_id.like(pat))
        q = q.where(filt)
        cq = cq.where(filt)
    total = int((await db.execute(cq)).scalar_one())
    q = q.order_by(ResponderEvalRun.created_at.desc(), ResponderEvalRun.id.desc()).offset(offset).limit(limit)
    rows = list((await db.execute(q)).scalars().all())
    return rows, total


async def get_responder_eval_run(db: AsyncSession, run_id: UUID) -> dict[str, Any] | None:
    row = await db.get(ResponderEvalRun, run_id)
    if not row:
        return None
    ev = merge_eval_detail_json(
        row.eval_json,
        chat_json=row.chat_json,
        ground_truth_json=row.ground_truth_json,
    )
    return {
        "id": str(row.id),
        "chat_id": row.chat_id,
        "order_id": row.order_id,
        "rca_run_id": row.rca_run_id,
        "composite_score": row.composite_score,
        "letter_grade": row.letter_grade,
        "eval_version": row.eval_version,
        "eval_status": row.eval_status,
        "eval": ev,
        "chat": ev.get("chat"),
        "eval_ground_truth": ev.get("eval_ground_truth"),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
