"""Batch tick: page pending RCA dumps, fetch chats, run eval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.responder_eval.chat_source import fetch_chats_async
from app.agents.responder_eval.constants import (
    EVAL_STATUS_DONE,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_PENDING,
    EVAL_STATUS_PROCESSING,
    EVAL_STATUS_WAITING,
    EVAL_VERSION,
)
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun
from app.agents.responder_eval.pipeline import abandon_eval, fail_eval, persist_eval_run, run_eval_for_chat
from app.agents.responder_eval.rca_dump import (
    DUMP_WITHOUT_RESPONSE,
    eval_artifact_for_dump,
    get_dump_by_chat_id,
    mark_dump_eval_status,
)
from app.agents.responder_eval.redact import redact_chat_async
from app.config.settings import settings
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DumpWorkItem:
    chat_id: str
    order_id: str
    run_id: str
    eval_artifact: dict[str, Any]


def _to_work_item(dump: OrderRcaEvalDump) -> DumpWorkItem:
    return DumpWorkItem(
        chat_id=dump.chat_id,
        order_id=dump.order_id,
        run_id=str(dump.run_id or ""),
        eval_artifact=dict(dump.eval_artifact or {}),
    )


async def _pending_dumps(db: AsyncSession, limit: int) -> list[OrderRcaEvalDump]:
    """Claim work with pending/failed ahead of waiting_chat (avoids open-chat HOL)."""
    now = datetime.now(UTC)
    min_age = timedelta(hours=max(0, settings.responder_eval_min_age_hours))
    cutoff = now - min_age
    stale_cutoff = now - timedelta(
        minutes=max(5, settings.responder_eval_processing_stale_minutes)
    )
    waiting_cutoff = now - timedelta(
        minutes=max(5, settings.responder_eval_waiting_recheck_minutes)
    )
    eligible = or_(
        and_(
            OrderRcaEvalDump.eval_status.in_([EVAL_STATUS_PENDING, EVAL_STATUS_FAILED]),
            OrderRcaEvalDump.created_at <= cutoff,
        ),
        and_(
            OrderRcaEvalDump.eval_status == EVAL_STATUS_WAITING,
            OrderRcaEvalDump.created_at <= cutoff,
            OrderRcaEvalDump.updated_at <= waiting_cutoff,
        ),
        and_(
            OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING,
            OrderRcaEvalDump.updated_at <= stale_cutoff,
        ),
    )
    # Prefer actionable dumps over waiting_chat so closed chats are not starved.
    claim_priority = case(
        (
            OrderRcaEvalDump.eval_status.in_([EVAL_STATUS_PENDING, EVAL_STATUS_FAILED]),
            0,
        ),
        (OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING, 1),
        else_=2,
    )
    q = (
        select(OrderRcaEvalDump)
        .options(DUMP_WITHOUT_RESPONSE)
        .where(eligible)
        .order_by(
            claim_priority.asc(),
            OrderRcaEvalDump.created_at.asc(),
            OrderRcaEvalDump.id.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    r = await db.execute(q)
    return list(r.scalars().all())


async def _already_evaluated(
    db: AsyncSession, dumps: list[OrderRcaEvalDump]
) -> set[str]:
    if not dumps:
        return set()
    chat_ids = [d.chat_id for d in dumps]
    q = select(ResponderEvalRun.chat_id, ResponderEvalRun.rca_run_id).where(
        ResponderEvalRun.chat_id.in_(chat_ids),
        ResponderEvalRun.eval_version == EVAL_VERSION,
    )
    r = await db.execute(q)
    existing = {str(row.chat_id): str(row.rca_run_id or "") for row in r.all()}
    return {
        dump.chat_id
        for dump in dumps
        if dump.chat_id in existing and existing[dump.chat_id] == str(dump.run_id or "")
    }


async def _mark_dumps_done_batch(db: AsyncSession, chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    now = datetime.now(UTC)
    await db.execute(
        update(OrderRcaEvalDump)
        .where(OrderRcaEvalDump.chat_id.in_(chat_ids))
        .values(
            eval_status=EVAL_STATUS_DONE,
            last_error=None,
            retry_count=0,
            response={},
            updated_at=now,
        )
    )


async def _mark_dumps_processing_batch(db: AsyncSession, chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    now = datetime.now(UTC)
    await db.execute(
        update(OrderRcaEvalDump)
        .where(OrderRcaEvalDump.chat_id.in_(chat_ids))
        .values(eval_status=EVAL_STATUS_PROCESSING, updated_at=now)
    )


async def _reset_dumps_to_pending_batch(db: AsyncSession, chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    now = datetime.now(UTC)
    await db.execute(
        update(OrderRcaEvalDump)
        .where(
            OrderRcaEvalDump.chat_id.in_(chat_ids),
            OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING,
        )
        .values(eval_status=EVAL_STATUS_PENDING, updated_at=now)
    )


async def _claimed_dump_with_artifact(item: DumpWorkItem) -> DumpWorkItem | None:
    """Reload claimed dump; build GT from staged response (or stored artifact on retry).

    Session is closed before Presidio. Staging JSON is a detached dict; it is
    never assigned back onto the ORM row, so in-place clear cannot flush to DB.
    """
    staging: dict[str, Any] | None = None
    stored: dict[str, Any] = {}
    chat_id = item.chat_id
    order_id = item.order_id
    run_id = item.run_id
    async with AsyncSessionLocal() as db:
        async with db.begin():
            dump = await get_dump_by_chat_id(
                chat_id, db=db, include_response=True
            )
            if dump is None:
                return None
            if dump.eval_status != EVAL_STATUS_PROCESSING:
                return None
            if str(dump.run_id or "") != run_id:
                return None
            order_id = dump.order_id
            run_id = str(dump.run_id or "")
            staging = dump.response if isinstance(dump.response, dict) else None
            stored = dict(dump.eval_artifact or {})
    dump = None
    artifact = await eval_artifact_for_dump(response=staging, eval_artifact=stored)
    staging = None
    stored = None
    return DumpWorkItem(
        chat_id=chat_id,
        order_id=order_id,
        run_id=run_id,
        eval_artifact=artifact,
    )


async def _load_batch_work_items(batch: int) -> list[DumpWorkItem]:
    """Claim pending dumps with FOR UPDATE SKIP LOCKED; mark processing in the same txn."""
    async with AsyncSessionLocal() as db:
        async with db.begin():
            dumps = await _pending_dumps(db, batch)
            if not dumps:
                return []
            done = await _already_evaluated(db, dumps)
            if done:
                await _mark_dumps_done_batch(db, sorted(done))
            to_process = [d for d in dumps if d.chat_id not in done]
            if to_process:
                await _mark_dumps_processing_batch(db, [d.chat_id for d in to_process])
            return [_to_work_item(d) for d in to_process]


async def run_responder_eval_batch() -> dict[str, int | str]:
    if not settings.responder_eval_enabled:
        return {"disabled": 1}

    batch = max(1, settings.responder_eval_batch_size)
    work_items = await _load_batch_work_items(batch)
    if not work_items:
        return {"candidates": 0, "evaluated": 0}

    chat_ids = [w.chat_id for w in work_items]

    try:
        fetch_result = await fetch_chats_async(chat_ids)
    except Exception as exc:
        log.exception("responder_eval chat fetch failed")
        try:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await _reset_dumps_to_pending_batch(db, chat_ids)
        except Exception:
            log.exception("responder_eval reset after fetch error failed")
        return {
            "candidates": len(work_items),
            "evaluated": 0,
            "fetch_error": str(exc)[:200],
        }

    evaluated = 0
    skipped_not_closed = 0
    abandoned = 0
    skipped_superseded = 0
    pii_blocked = 0

    for item in work_items:
        if item.chat_id in fetch_result.not_closed_ids:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await mark_dump_eval_status(
                        item.chat_id,
                        EVAL_STATUS_WAITING,
                        last_error="chat_not_closed",
                        expected_run_id=item.run_id,
                        from_status=EVAL_STATUS_PROCESSING,
                        db=db,
                    )
            skipped_not_closed += 1
            continue
        if item.chat_id in fetch_result.invalid_ids:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await abandon_eval(
                        item.chat_id,
                        "invalid_chat_id",
                        db=db,
                        expected_run_id=item.run_id,
                    )
            abandoned += 1
            continue
        chat = fetch_result.chats.pop(item.chat_id, None)
        if not chat:
            reason = (
                "conversation_not_found"
                if item.chat_id in fetch_result.missing_ids
                else "chat_empty"
                if item.chat_id in fetch_result.empty_closed_ids
                else "chat_unavailable"
            )
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await abandon_eval(
                        item.chat_id, reason, db=db, expected_run_id=item.run_id
                    )
            abandoned += 1
            continue

        try:
            fresh_item = await _claimed_dump_with_artifact(item)
        except Exception as e:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await fail_eval(
                        item.chat_id, str(e), db=db, expected_run_id=item.run_id
                    )
            continue
        if fresh_item is None:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await _reset_dumps_to_pending_batch(db, [item.chat_id])
            skipped_superseded += 1
            continue
        item = fresh_item
        dump_proxy = None
        result = None
        try:
            chat = await redact_chat_async(chat)
            dump_proxy = _dump_proxy(item)
            result = await run_eval_for_chat(dump_proxy, chat, chat_redacted=True)
            if result.get("persist_blocked"):
                async with AsyncSessionLocal() as db:
                    async with db.begin():
                        await fail_eval(
                            item.chat_id,
                            "pii_incident",
                            db=db,
                            expected_run_id=item.run_id,
                        )
                pii_blocked += 1
                continue
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    persisted = await persist_eval_run(result, db=db)
            if persisted:
                evaluated += 1
            else:
                async with AsyncSessionLocal() as db:
                    async with db.begin():
                        await _reset_dumps_to_pending_batch(db, [item.chat_id])
                skipped_superseded += 1
        except Exception as e:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    await fail_eval(
                        item.chat_id, str(e), db=db, expected_run_id=item.run_id
                    )
        finally:
            chat = None
            item = None
            dump_proxy = None
            result = None

    out: dict[str, int | str] = {
        "candidates": len(work_items),
        "evaluated": evaluated,
        "skipped_not_closed": skipped_not_closed,
        "abandoned": abandoned,
    }
    if skipped_superseded:
        out["skipped_superseded"] = skipped_superseded
    if pii_blocked:
        out["pii_blocked"] = pii_blocked
    return out


def _dump_proxy(item: DumpWorkItem) -> Any:
    """Minimal dump shape for run_eval_for_chat (avoids detached ORM instances)."""

    class _Proxy:
        chat_id = item.chat_id
        order_id = item.order_id
        run_id = item.run_id
        eval_artifact = item.eval_artifact

    return _Proxy()
