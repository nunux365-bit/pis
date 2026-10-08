"""Eval pipeline for a single chat."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.responder_eval.constants import (
    EVAL_STATUS_ABANDONED,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_PROCESSING,
    EVAL_VERSION,
    LETTER_GRADE_NOT_GRADED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_NOT_GRADED,
    is_agent_side_role,
)
from app.agents.responder_eval.deterministic import run_deterministic
from app.agents.responder_eval.handoff_classifier import run_handoff_classifier
from app.agents.responder_eval.judge import run_judge
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun
from app.agents.responder_eval.rca_dump import (
    _apply_dump_status,
    get_dump_by_chat_id,
    mark_dump_eval_succeeded,
)
from app.agents.responder_eval.redact import redact_chat_messages, redact_dict
from app.agents.responder_eval.hard_gates import (
    apply_hard_gates,
    merge_refund_fact_check_hal,
    resolve_hallucination_gate,
)
from app.agents.responder_eval.traceability_reasons import compute_traceability_failure_reasons
from app.agents.responder_eval.pii_verify import verify_no_pii_leak
from app.agents.responder_eval.scoring import apply_scores
from app.agents.responder_eval.denormalized import build_persist_row_fields, split_eval_storage
from app.config.settings import settings
from app.db.session import AsyncSessionLocal
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)


def _has_agent_turns(chat: dict[str, Any]) -> bool:
    return any(is_agent_side_role(m.get("role")) for m in (chat.get("messages") or []))


def _build_not_graded_result(
    dump: Any,
    chat: dict[str, Any],
    det: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        **det,
        "resolution": "not_applicable",
        "graded": False,
        "not_graded": True,
        "not_graded_reason": reason,
        "composite_score": None,
        "letter_grade": LETTER_GRADE_NOT_GRADED,
        "eval_version": EVAL_VERSION,
        "chat_id": dump.chat_id,
        "order_id": dump.order_id,
        "rca_run_id": dump.run_id,
        "chat": chat,
        "eval_ground_truth": dump.eval_artifact,
    }


def _prepare_eval_cpu(
    dump: Any,
    chat: dict[str, Any],
    *,
    chat_redacted: bool,
) -> tuple[dict[str, Any], Any, dict[str, Any], Any, bool]:
    """Sync prep: optional redact + deterministic flags + PII check (thread-pooled)."""
    if not chat_redacted:
        chat = redact_chat_messages(chat) if chat.get("messages") else redact_dict(chat)
    artifact = dump.eval_artifact
    det = run_deterministic(chat, artifact)
    pii_check = verify_no_pii_leak(chat, artifact)
    return chat, artifact, det, pii_check, _has_agent_turns(chat)


def _finalize_not_graded_cpu(
    dump: Any,
    chat: dict[str, Any],
    det: dict[str, Any],
    pii_check: Any,
) -> dict[str, Any]:
    result = _build_not_graded_result(dump, chat, det, reason="no_agent_turns")
    return apply_hard_gates(result, pii_check=pii_check, hallucination_severity=None)


def _finalize_graded_cpu(
    dump: Any,
    chat: dict[str, Any],
    artifact: Any,
    det: dict[str, Any],
    llm: dict[str, Any],
    pii_check: Any,
    *,
    handoff: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Sync post-judge scoring, hard gates, and traceability reasons (thread-pooled)."""
    scored = merge_refund_fact_check_hal(apply_scores(det, llm, chat))
    if handoff:
        scored.update(handoff)
    hal_severity, hal_modes = resolve_hallucination_gate(chat, artifact, scored)
    result = apply_hard_gates(
        scored,
        pii_check=pii_check,
        hallucination_severity=hal_severity,
        hallucination_failure_modes=hal_modes,
    )
    if result.get("traceability_pass") is False:
        result["traceability_failure_reasons"] = compute_traceability_failure_reasons(
            chat, artifact, result
        )
    result["eval_version"] = EVAL_VERSION
    result["chat_id"] = dump.chat_id
    result["order_id"] = dump.order_id
    result["rca_run_id"] = dump.run_id
    result["chat"] = chat
    result["eval_ground_truth"] = artifact
    return result


async def run_eval_for_chat(
    dump: Any,
    chat: dict[str, Any],
    *,
    chat_redacted: bool = False,
) -> dict[str, Any]:
    chat, artifact, det, pii_check, has_agent = await run_blocking(
        lambda: _prepare_eval_cpu(dump, chat, chat_redacted=chat_redacted)
    )
    if not has_agent:
        return await run_blocking(
            lambda: _finalize_not_graded_cpu(dump, chat, det, pii_check)
        )
    llm, handoff = await asyncio.gather(
        run_judge(chat, artifact, det),
        run_handoff_classifier(chat),
    )
    return await run_blocking(
        lambda: _finalize_graded_cpu(dump, chat, artifact, det, llm, pii_check, handoff=handoff)
    )


async def persist_eval_run(
    result: dict[str, Any], *, db: AsyncSession | None = None
) -> bool:
    now = datetime.now(UTC)
    chat_id = result["chat_id"]
    graded = bool(result.get("graded", True))
    denorm = build_persist_row_fields(result)
    rubric_json, chat_json, ground_truth_json = split_eval_storage(result)
    row = {
        "chat_id": chat_id,
        "order_id": result.get("order_id"),
        "rca_run_id": result.get("rca_run_id"),
        "eval_version": result.get("eval_version", EVAL_VERSION),
        "composite_score": result.get("composite_score") if graded else None,
        "letter_grade": str(
            result.get("letter_grade") or (LETTER_GRADE_NOT_GRADED if not graded else "E")
        ),
        **denorm,
        "eval_status": RUN_STATUS_COMPLETED if graded else RUN_STATUS_NOT_GRADED,
        "eval_json": rubric_json,
        "chat_json": chat_json,
        "ground_truth_json": ground_truth_json,
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(ResponderEvalRun).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ResponderEvalRun.chat_id],
        set_={
            "order_id": stmt.excluded.order_id,
            "rca_run_id": stmt.excluded.rca_run_id,
            "eval_version": stmt.excluded.eval_version,
            "composite_score": stmt.excluded.composite_score,
            "letter_grade": stmt.excluded.letter_grade,
            "bot_score": stmt.excluded.bot_score,
            "human_score": stmt.excluded.human_score,
            "bot_grade": stmt.excluded.bot_grade,
            "human_grade": stmt.excluded.human_grade,
            "composite_resolution": stmt.excluded.composite_resolution,
            "bot_resolution": stmt.excluded.bot_resolution,
            "human_resolution": stmt.excluded.human_resolution,
            "composite_issues": stmt.excluded.composite_issues,
            "bot_issues": stmt.excluded.bot_issues,
            "human_issues": stmt.excluded.human_issues,
            "handoff_bucket": stmt.excluded.handoff_bucket,
            "handoff_sub_bucket": stmt.excluded.handoff_sub_bucket,
            "eval_status": stmt.excluded.eval_status,
            "eval_json": stmt.excluded.eval_json,
            "chat_json": stmt.excluded.chat_json,
            "ground_truth_json": stmt.excluded.ground_truth_json,
            "updated_at": now,
        },
    )

    async def _persist(session: AsyncSession) -> bool:
        if result.get("persist_blocked"):
            log.warning("responder_eval skip persist chat_id=%s reason=pii_incident", chat_id)
            return False
        rca_run_id = str(result.get("rca_run_id") or "")
        dump = await get_dump_by_chat_id(chat_id, db=session, for_update=True)
        if dump is None:
            return False
        if str(dump.run_id or "") != rca_run_id:
            log.warning(
                "responder_eval skip persist chat_id=%s dump_run_id=%s eval_run_id=%s",
                chat_id,
                dump.run_id,
                rca_run_id,
            )
            return False
        if dump.eval_status != EVAL_STATUS_PROCESSING:
            log.warning(
                "responder_eval skip persist chat_id=%s status=%s (expected processing)",
                chat_id,
                dump.eval_status,
            )
            return False
        await session.execute(stmt)
        marked = await mark_dump_eval_succeeded(
            session,
            chat_id,
            run_id=rca_run_id,
            eval_artifact=ground_truth_json if isinstance(ground_truth_json, dict) else {},
        )
        if not marked:
            return False
        return True

    if db is not None:
        return await _persist(db)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            return await _persist(session)


async def fail_eval(
    chat_id: str,
    error: str,
    *,
    db: AsyncSession | None = None,
    expected_run_id: str | None = None,
) -> bool:
    log.warning("responder_eval failed chat_id=%s error=%s", chat_id, error[:200])
    max_retries = max(1, settings.responder_eval_max_fail_retries)

    async def _fail(session: AsyncSession) -> bool:
        dump = await get_dump_by_chat_id(chat_id, db=session, for_update=True)
        if dump is None:
            return False
        if dump.eval_status != EVAL_STATUS_PROCESSING:
            return False
        if expected_run_id is not None and str(dump.run_id or "") != expected_run_id:
            log.warning(
                "responder_eval skip fail chat_id=%s dump_run_id=%s expected_run_id=%s",
                chat_id,
                dump.run_id,
                expected_run_id,
            )
            return False
        retry_count = int(dump.retry_count or 0) + 1
        run_id = str(dump.run_id or "")
        if retry_count >= max_retries:
            return await _apply_dump_status(
                session,
                chat_id,
                EVAL_STATUS_ABANDONED,
                last_error=f"max_retries_exceeded: {error[:400]}",
                expected_run_id=run_id,
                from_status=EVAL_STATUS_PROCESSING,
            )
        q = (
            OrderRcaEvalDump.__table__.update()
            .where(
                OrderRcaEvalDump.chat_id == chat_id,
                OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING,
                OrderRcaEvalDump.run_id == run_id,
            )
            .values(
                eval_status=EVAL_STATUS_FAILED,
                retry_count=retry_count,
                last_error=error[:500],
                updated_at=datetime.now(UTC),
            )
        )
        result = await session.execute(q)
        return bool(result.rowcount)

    if db is not None:
        return await _fail(db)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            return await _fail(session)


async def abandon_eval(
    chat_id: str,
    error: str,
    *,
    db: AsyncSession | None = None,
    expected_run_id: str | None = None,
) -> bool:
    log.warning("responder_eval abandoned chat_id=%s error=%s", chat_id, error[:200])

    async def _abandon(session: AsyncSession) -> bool:
        dump = await get_dump_by_chat_id(chat_id, db=session, for_update=True)
        if dump is None:
            return False
        if dump.eval_status != EVAL_STATUS_PROCESSING:
            return False
        if expected_run_id is not None and str(dump.run_id or "") != expected_run_id:
            log.warning(
                "responder_eval skip abandon chat_id=%s dump_run_id=%s expected_run_id=%s",
                chat_id,
                dump.run_id,
                expected_run_id,
            )
            return False
        return await _apply_dump_status(
            session,
            chat_id,
            EVAL_STATUS_ABANDONED,
            last_error=error,
            expected_run_id=str(dump.run_id or ""),
            from_status=EVAL_STATUS_PROCESSING,
        )

    if db is not None:
        return await _abandon(db)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            return await _abandon(session)
