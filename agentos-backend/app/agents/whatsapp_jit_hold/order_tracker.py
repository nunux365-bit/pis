"""Persist JIT-hold parent orders for the Responder list. Writes never raise."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.whatsapp_jit_hold.models import (
    STATUS_KEPT_ORIGINAL,
    STATUS_SPLIT_DONE,
    STATUS_TRIGGERED,
    STATUSES,
    TERMINAL_STATUSES,
    WhatsappJitHoldOrder,
)

log = logging.getLogger(__name__)


def _oid(order_id: str) -> str:
    return order_id.strip().upper()


async def record_triggered(order_id: str) -> None:
    oid = _oid(order_id)
    if not oid:
        return
    await _write(
        insert(WhatsappJitHoldOrder)
        .values(parent_order_id=oid, status=STATUS_TRIGGERED)
        .on_conflict_do_nothing(index_elements=[WhatsappJitHoldOrder.parent_order_id])
    )


async def record_terminal(order_id: str, status: str) -> None:
    oid = _oid(order_id)
    if not oid or status not in TERMINAL_STATUSES:
        return
    await _write(
        insert(WhatsappJitHoldOrder)
        .values(parent_order_id=oid, status=status)
        .on_conflict_do_update(
            index_elements=[WhatsappJitHoldOrder.parent_order_id],
            set_={"status": status, "updated_at": func.now()},
            where=WhatsappJitHoldOrder.status == STATUS_TRIGGERED,
        )
    )


async def _write(stmt) -> None:
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(stmt)
            await db.commit()
    except Exception:
        log.exception("jit_hold order tracker write failed")


def _window(days: int) -> tuple[datetime, datetime]:
    until = datetime.now(UTC)
    return until - timedelta(days=max(1, days)), until


def _filters(
    days: int,
    status: str | None,
    search: str | None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
):
    if since is None or until is None:
        since, until = _window(days)
    clauses = [
        WhatsappJitHoldOrder.triggered_at >= since,
        WhatsappJitHoldOrder.triggered_at < until,
    ]
    if status and status in STATUSES:
        clauses.append(WhatsappJitHoldOrder.status == status)
    if search and (q := search.strip().upper()[:64]):
        clauses.append(WhatsappJitHoldOrder.parent_order_id.like(f"{q}%"))
    return clauses


async def query_jit_hold_orders(
    db: AsyncSession,
    *,
    days: int = 7,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    since, until = _window(days)
    list_f = _filters(days, status, search, since=since, until=until)
    stats_f = _filters(days, None, None, since=since, until=until)
    total = int((await db.execute(select(func.count()).select_from(WhatsappJitHoldOrder).where(*list_f))).scalar_one())
    rows = list(
        (
            await db.execute(
                select(WhatsappJitHoldOrder)
                .where(*list_f)
                .order_by(WhatsappJitHoldOrder.triggered_at.desc(), WhatsappJitHoldOrder.parent_order_id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    counts = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(WhatsappJitHoldOrder.status == STATUS_TRIGGERED).label("triggered"),
                func.count().filter(WhatsappJitHoldOrder.status == STATUS_SPLIT_DONE).label("split_done"),
                func.count().filter(WhatsappJitHoldOrder.status == STATUS_KEPT_ORIGINAL).label("kept_original"),
            )
            .select_from(WhatsappJitHoldOrder)
            .where(*stats_f)
        )
    ).one()
    return {
        "items": [
            {
                "parent_order_id": r.parent_order_id,
                "status": r.status,
                "triggered_at": r.triggered_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "stats": {
            "total": int(counts.total or 0),
            "triggered": int(counts.triggered or 0),
            "split_done": int(counts.split_done or 0),
            "kept_original": int(counts.kept_original or 0),
        },
    }
