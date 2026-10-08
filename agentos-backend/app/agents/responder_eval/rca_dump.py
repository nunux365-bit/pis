"""Persist redacted RCA eval dumps keyed by chat_id."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.agents.responder_eval.constants import (
    EVAL_STATUS_ABANDONED,
    EVAL_STATUS_DONE,
    EVAL_STATUS_FAILED,
    EVAL_STATUS_PENDING,
    EVAL_STATUS_PROCESSING,
    EVAL_STATUS_WAITING,
    EVAL_VERSION,
    TERMINAL_RCA_STATUSES,
)
from app.agents.responder_eval.ground_truth import build_eval_artifact
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun
from app.agents.responder_eval.redact import redact_dict_async
from app.config.settings import settings
from app.db.session import AsyncSessionLocal

# API/UI/claim queries omit ``response``. Closed-chat eval loads it explicitly.

log = logging.getLogger(__name__)

_MAX_ERROR_LEN = 500
_REQUEUE_STATUSES = (
    EVAL_STATUS_FAILED,
    EVAL_STATUS_WAITING,
    EVAL_STATUS_ABANDONED,
)
DUMP_WITHOUT_RESPONSE = load_only(
    OrderRcaEvalDump.id,
    OrderRcaEvalDump.chat_id,
    OrderRcaEvalDump.eval_artifact,
    OrderRcaEvalDump.order_id,
    OrderRcaEvalDump.run_id,
    OrderRcaEvalDump.eval_status,
    OrderRcaEvalDump.last_error,
    OrderRcaEvalDump.retry_count,
    OrderRcaEvalDump.created_at,
    OrderRcaEvalDump.updated_at,
)


async def get_dump_by_chat_id(
    chat_id: str,
    *,
    db: AsyncSession | None = None,
    for_update: bool = False,
    include_response: bool = False,
) -> OrderRcaEvalDump | None:
    q = select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id).limit(1)
    if not include_response:
        q = q.options(DUMP_WITHOUT_RESPONSE)
    if for_update:
        q = q.with_for_update()

    async def _load(session: AsyncSession) -> OrderRcaEvalDump | None:
        return (await session.execute(q)).scalars().first()

    if db is not None:
        return await _load(db)
    async with AsyncSessionLocal() as session:
        return await _load(session)


async def _apply_dump_status(
    session: AsyncSession,
    chat_id: str,
    status: str,
    *,
    last_error: str | None = None,
    clear_error: bool = False,
    expected_run_id: str | None = None,
    from_status: str | None = None,
) -> bool:
    values: dict[str, Any] = {
        "eval_status": status,
        "updated_at": datetime.now(UTC),
    }
    if clear_error:
        values["last_error"] = None
        values["retry_count"] = 0
    elif last_error is not None:
        values["last_error"] = last_error[:_MAX_ERROR_LEN]
    q = update(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
    if expected_run_id is not None:
        q = q.where(OrderRcaEvalDump.run_id == expected_run_id)
    if from_status is not None:
        q = q.where(OrderRcaEvalDump.eval_status == from_status)
    result = await session.execute(q.values(**values))
    return bool(result.rowcount)


async def mark_dump_eval_status(
    chat_id: str,
    status: str,
    *,
    last_error: str | None = None,
    clear_error: bool = False,
    expected_run_id: str | None = None,
    from_status: str | None = None,
    db: AsyncSession | None = None,
) -> bool:
    if db is not None:
        return await _apply_dump_status(
            db,
            chat_id,
            status,
            last_error=last_error,
            clear_error=clear_error,
            expected_run_id=expected_run_id,
            from_status=from_status,
        )
    async with AsyncSessionLocal() as session:
        async with session.begin():
            return await _apply_dump_status(
                session,
                chat_id,
                status,
                last_error=last_error,
                clear_error=clear_error,
                expected_run_id=expected_run_id,
                from_status=from_status,
            )


def _staging_response(response: Any) -> dict[str, Any]:
    return response if isinstance(response, dict) else {}


async def eval_artifact_for_dump(
    dump: OrderRcaEvalDump | None = None,
    *,
    response: dict[str, Any] | None = None,
    eval_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build redacted GT from staged RCA JSON; retry after success uses stored artifact.

    ``response`` must be a detached dict (session already closed). In-place
    ``clear()`` only drops Python refs; it does not UPDATE Postgres.
    """
    raw = _staging_response(response if response is not None else (dump.response if dump is not None else None))
    stored = eval_artifact if eval_artifact is not None else (dict(dump.eval_artifact or {}) if dump is not None else {})
    try:
        if raw:
            return await redact_dict_async(build_eval_artifact(raw))
        return dict(stored or {})
    finally:
        raw.clear()


async def mark_dump_eval_succeeded(
    session: AsyncSession,
    chat_id: str,
    *,
    run_id: str,
    eval_artifact: dict[str, Any],
) -> bool:
    result = await session.execute(
        update(OrderRcaEvalDump)
        .where(
            OrderRcaEvalDump.chat_id == chat_id,
            OrderRcaEvalDump.run_id == run_id,
            OrderRcaEvalDump.eval_status == EVAL_STATUS_PROCESSING,
        )
        .values(
            eval_status=EVAL_STATUS_DONE,
            response={},
            eval_artifact=eval_artifact,
            last_error=None,
            retry_count=0,
            updated_at=datetime.now(UTC),
        )
    )
    return bool(result.rowcount)


async def maybe_persist_rca_eval_dump(run_doc: dict[str, Any]) -> None:
    if not settings.responder_eval_enabled:
        return
    chat_id = (run_doc.get("chat_id") or "").strip()
    if not chat_id:
        return
    status = str(run_doc.get("status") or "")
    if status not in TERMINAL_RCA_STATUSES:
        return

    run_id = str(run_doc.get("run_id") or "")
    now = datetime.now(UTC)
    row = {
        "chat_id": chat_id,
        "response": run_doc,
        "eval_artifact": {},
        "order_id": str(run_doc.get("order_id") or ""),
        "run_id": run_id,
        "eval_status": EVAL_STATUS_PENDING,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(OrderRcaEvalDump).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[OrderRcaEvalDump.chat_id],
        set_={
            "response": case(
                (
                    and_(
                        OrderRcaEvalDump.run_id == stmt.excluded.run_id,
                        OrderRcaEvalDump.eval_status == EVAL_STATUS_DONE,
                    ),
                    OrderRcaEvalDump.response,
                ),
                else_=stmt.excluded.response,
            ),
            "eval_artifact": case(
                (
                    and_(
                        OrderRcaEvalDump.run_id == stmt.excluded.run_id,
                        OrderRcaEvalDump.eval_status == EVAL_STATUS_DONE,
                    ),
                    OrderRcaEvalDump.eval_artifact,
                ),
                else_=stmt.excluded.eval_artifact,
            ),
            "order_id": stmt.excluded.order_id,
            "run_id": stmt.excluded.run_id,
            "eval_status": case(
                (OrderRcaEvalDump.run_id != stmt.excluded.run_id, EVAL_STATUS_PENDING),
                (
                    OrderRcaEvalDump.eval_status.in_(_REQUEUE_STATUSES),
                    EVAL_STATUS_PENDING,
                ),
                else_=OrderRcaEvalDump.eval_status,
            ),
            "retry_count": case(
                (
                    OrderRcaEvalDump.run_id != stmt.excluded.run_id,
                    0,
                ),
                (
                    OrderRcaEvalDump.eval_status.in_(_REQUEUE_STATUSES),
                    0,
                ),
                else_=OrderRcaEvalDump.retry_count,
            ),
            "last_error": case(
                (OrderRcaEvalDump.run_id != stmt.excluded.run_id, None),
                (
                    OrderRcaEvalDump.eval_status.in_(_REQUEUE_STATUSES),
                    None,
                ),
                else_=OrderRcaEvalDump.last_error,
            ),
            "created_at": case(
                (OrderRcaEvalDump.run_id != stmt.excluded.run_id, now),
                else_=OrderRcaEvalDump.created_at,
            ),
            "updated_at": now,
        },
    )
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(stmt)
    log.debug("responder_eval rca dump upserted chat_id=%s run_id=%s", chat_id, run_id)


async def retry_dump_eval(chat_id: str) -> bool:
    """Re-queue a failed/abandoned dump for the scheduler (reviewer action)."""
    cid = chat_id.strip()
    if not cid:
        return False
    async with AsyncSessionLocal() as session:
        async with session.begin():
            dump = await get_dump_by_chat_id(cid, db=session, for_update=True)
            if dump is None:
                return False
            if dump.eval_status not in (
                EVAL_STATUS_FAILED,
                EVAL_STATUS_ABANDONED,
                EVAL_STATUS_WAITING,
            ):
                return False
            await session.execute(
                delete(ResponderEvalRun).where(
                    ResponderEvalRun.chat_id == cid,
                    ResponderEvalRun.eval_version == EVAL_VERSION,
                )
            )
            await _apply_dump_status(
                session,
                cid,
                EVAL_STATUS_PENDING,
                clear_error=True,
            )
    return True
