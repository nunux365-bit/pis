"""MySQL conversation grouping — fetch all call legs and collapse batch rows."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.compliance_call.batch_item import ComplianceBatchItem
from app.agents.compliance_call.fingerprint import (
    make_ingest_fingerprint_mysql,
    make_ingest_fingerprint_mysql_conversation,
)
from app.db.models import WorkflowRun, WorkflowRunStatus
from app.services.workflow_runner import CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY
from app.agents.compliance_call.mysql_candidates import (
    _mysql_datetime_as_read_from_db,
    _parse_opinion_ids,
    _window_naive_local,
)
from app.config.settings import settings
from app.infra.mysql_compliance import get_mysql_compliance_engine

log = logging.getLogger(__name__)


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def leg_sort_key(leg: dict[str, Any]) -> tuple[str, int]:
    """Order legs by ``calls.updated_at`` then ``calls.id`` (stable merge order)."""
    upd = str(leg.get("mysql_call_updated_at") or "").strip()
    try:
        cid = int(leg.get("mysql_call_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    return (upd, cid)


def mysql_call_leg_dict(item: ComplianceBatchItem) -> dict[str, Any]:
    return {
        "mysql_call_id": int(item.mysql_call_id or 0),
        "mysql_room_name": item.mysql_room_name,
        "mysql_metadata": item.mysql_metadata if isinstance(item.mysql_metadata, dict) else {},
        "mysql_call_updated_at": item.mysql_call_updated_at,
    }


async def fetch_mysql_legs_for_conversation(conversation_id: int) -> list[dict[str, Any]]:
    """All ``calls`` legs for a conversation within the compliance MySQL time window."""
    if not settings.compliance_call_mysql_enabled:
        return []
    if not (settings.compliance_call_mysql_url or "").strip():
        return []
    opinion_ids = _parse_opinion_ids(settings.compliance_call_second_opinion_ids or "")
    if not opinion_ids:
        return []

    start_naive, end_naive = _window_naive_local()
    sql = (
        text(
            """
            SELECT c.id, c.room_name, c.metadata, c.updated_at AS mysql_call_updated_at
            FROM calls c
            INNER JOIN second_opinion_conversations soc ON soc.id = c.second_opinion_conversation_id
            WHERE soc.id = :conv_id
              AND soc.second_opinion_id IN :opinion_ids
              AND soc.status = :st
              AND soc.updated_at >= :start_at
              AND soc.updated_at <= :end_at
              AND c.metadata IS NOT NULL
            ORDER BY c.updated_at ASC, c.id ASC
            """
        )
        .bindparams(bindparam("opinion_ids", expanding=True))
    )
    engine = get_mysql_compliance_engine()
    legs: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sql,
                {
                    "conv_id": int(conversation_id),
                    "opinion_ids": opinion_ids,
                    "st": 4,
                    "start_at": start_naive,
                    "end_at": end_naive,
                },
            )
        ).mappings().all()
    for r in rows:
        rn = r.get("room_name")
        room_s = str(rn).strip() if rn is not None else ""
        legs.append(
            {
                "mysql_call_id": int(r["id"]),
                "mysql_room_name": room_s or None,
                "mysql_metadata": _parse_metadata(r.get("metadata")),
                "mysql_call_updated_at": _mysql_datetime_as_read_from_db(r.get("mysql_call_updated_at")),
            }
        )
    return legs


def _pick_representative_item(items: list[ComplianceBatchItem]) -> ComplianceBatchItem:
    """Use the latest leg in the batch slice for doctor / filename metadata."""
    return max(
        items,
        key=lambda it: (
            str(it.mysql_call_updated_at or ""),
            int(it.mysql_call_id or 0),
        ),
    )


async def collapse_mysql_items_by_conversation(
    mysql_items: list[ComplianceBatchItem],
) -> list[ComplianceBatchItem]:
    """One batch item per ``second_opinion_conversation_id`` with all legs attached."""
    if not settings.compliance_mysql_merge_conversation_transcripts:
        return mysql_items

    by_conv: dict[int, list[ComplianceBatchItem]] = {}
    singles: list[ComplianceBatchItem] = []
    for it in mysql_items:
        if it.source != "mysql_call":
            singles.append(it)
            continue
        soc = it.mysql_second_opinion_conversation_id
        if soc is None:
            singles.append(it)
            continue
        by_conv.setdefault(int(soc), []).append(it)

    out: list[ComplianceBatchItem] = list(singles)
    for conv_id, group in by_conv.items():
        rep = _pick_representative_item(group)
        try:
            legs = await fetch_mysql_legs_for_conversation(conv_id)
        except Exception:
            log.exception("compliance mysql: failed to load legs for conversation_id=%s", conv_id)
            legs = [mysql_call_leg_dict(x) for x in group]
        if not legs:
            legs = [mysql_call_leg_dict(x) for x in group]
        legs = sorted(legs, key=leg_sort_key)
        call_ids = [int(x["mysql_call_id"]) for x in legs]
        max_cid = max(call_ids) if call_ids else int(rep.mysql_call_id or 0)
        out.append(
            ComplianceBatchItem(
                source="mysql_call",
                mysql_call_id=max_cid,
                mysql_second_opinion_id=rep.mysql_second_opinion_id,
                mysql_second_opinion_conversation_id=conv_id,
                mysql_room_name=rep.mysql_room_name,
                mysql_metadata=rep.mysql_metadata,
                mysql_provider_reference_id=rep.mysql_provider_reference_id,
                mysql_doctor_id=rep.mysql_doctor_id,
                mysql_call_updated_at=rep.mysql_call_updated_at,
                filename=f"conversation_{conv_id}.media",
                mime=rep.mime,
                relative_path=f"mysql/conversations/{conv_id}",
                doctor_slug=rep.doctor_slug,
                doctor_name=rep.doctor_name,
                mysql_call_legs=legs,
            )
        )
    return out


def merge_transcript_parts(parts: list[tuple[int, str, str]]) -> tuple[str, str]:
    """Join canonical and grading transcript segments in leg order."""
    canon_lines: list[str] = []
    grad_lines: list[str] = []
    for cid, canon, grad in parts:
        header = f"--- call {cid} ---"
        c = (canon or "").strip()
        g = (grad or c).strip()
        if c:
            canon_lines.append(f"{header}\n{c}")
        if g:
            grad_lines.append(f"{header}\n{g}")
    return "\n\n".join(canon_lines), "\n\n".join(grad_lines)


async def mysql_conversation_skip_reason(
    db: AsyncSession,
    *,
    conversation_id: int,
    leg_call_ids: list[int],
) -> str | None:
    """Skip merged scoring when conversation or every leg was already completed.

    - ``conversation_already_completed``: ``mysql:conv:{id}`` fingerprint done.
    - ``all_calls_already_completed``: each leg has a completed legacy ``mysql:{call_id}`` run.
    - ``conversation_already_scored``: any completed run with this conversation id in ``input_data``
      (covers legacy per-call rows that stored ``mysql_second_opinion_conversation_id``).
    """
    conv_fp = make_ingest_fingerprint_mysql_conversation(conversation_id=int(conversation_id))
    conv_done = (
        await db.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                WorkflowRun.ingest_fingerprint == conv_fp,
                WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if conv_done is not None:
        return "conversation_already_completed"

    conv_s = str(int(conversation_id))
    legacy_done = (
        await db.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
                (
                    WorkflowRun.input_data["mysql_second_opinion_conversation_id"].as_string()
                    == conv_s
                )
                | (
                    WorkflowRun.input_data["second_opinion_conversation_id"].as_string()
                    == conv_s
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if legacy_done is not None:
        return "conversation_already_scored"

    if not leg_call_ids:
        return None

    leg_fps = [make_ingest_fingerprint_mysql(mysql_call_id=int(cid)) for cid in leg_call_ids]
    n_done = await db.scalar(
        select(func.count())
        .select_from(WorkflowRun)
        .where(
            WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
            WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
            WorkflowRun.ingest_fingerprint.in_(leg_fps),
        )
    )
    if int(n_done or 0) >= len(leg_call_ids):
        return "all_calls_already_completed"
    return None


def merge_deepgram_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    if not summaries:
        return {}
    if len(summaries) == 1:
        return dict(summaries[0])
    total_dur = 0.0
    for s in summaries:
        try:
            total_dur += float(s.get("duration") or 0)
        except (TypeError, ValueError):
            pass
    base = dict(summaries[-1])
    base["duration"] = total_dur
    base["merged_leg_count"] = len(summaries)
    return base
