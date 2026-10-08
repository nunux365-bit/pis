"""JIT-hold order tracker — writes must never raise; list filters for Responder."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.agents.whatsapp_jit_hold import order_tracker
from app.agents.whatsapp_jit_hold.models import STATUS_SPLIT_DONE, STATUS_TRIGGERED


@pytest_asyncio.fixture
async def pg_jit_or_skip():
    import asyncio

    from sqlalchemy import select

    from app.agents.whatsapp_jit_hold.models import WhatsappJitHoldOrder
    from app.db.session import AsyncSessionLocal, engine

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(WhatsappJitHoldOrder.__table__.create, checkfirst=True)
            async with AsyncSessionLocal() as s:
                await s.execute(select(WhatsappJitHoldOrder).limit(1))
            return
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.15)
    pytest.skip(f"Postgres / whatsapp_jit_hold_orders not available: {last_exc}")


@pytest.mark.asyncio
async def test_record_triggered_swallows_db_errors(monkeypatch):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Boom)
    await order_tracker.record_triggered("PO1")
    await order_tracker.record_terminal("PO1", STATUS_SPLIT_DONE)


@pytest.mark.asyncio
async def test_record_skips_blank_and_invalid_status(monkeypatch):
    db = AsyncMock()

    class Sess:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Sess)
    await order_tracker.record_triggered("  ")
    await order_tracker.record_terminal("PO1", STATUS_TRIGGERED)
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_jit_hold_orders_filters(pg_jit_or_skip):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from app.agents.whatsapp_jit_hold.models import STATUS_KEPT_ORIGINAL, WhatsappJitHoldOrder
    from app.db.session import AsyncSessionLocal

    oid = f"POTEST{datetime.now(UTC).strftime('%H%M%S%f')}"
    old = f"{oid}OLD"
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as s:
        s.add(WhatsappJitHoldOrder(parent_order_id=oid, status=STATUS_SPLIT_DONE, triggered_at=now))
        s.add(
            WhatsappJitHoldOrder(
                parent_order_id=old,
                status=STATUS_KEPT_ORIGINAL,
                triggered_at=now - timedelta(days=20),
            )
        )
        await s.commit()
        try:
            out = await order_tracker.query_jit_hold_orders(s, days=7, search=oid[:8])
            ids = {r["parent_order_id"] for r in out["items"]}
            assert oid in ids
            assert old not in ids
            assert out["stats"]["split_done"] >= 1
            filtered = await order_tracker.query_jit_hold_orders(
                s, days=7, status=STATUS_SPLIT_DONE, search=oid
            )
            assert filtered["total"] == 1
            assert filtered["items"][0]["status"] == STATUS_SPLIT_DONE
            assert filtered["stats"]["total"] >= filtered["total"]
        finally:
            await s.execute(
                delete(WhatsappJitHoldOrder).where(WhatsappJitHoldOrder.parent_order_id.in_([oid, old]))
            )
            await s.commit()


@pytest.mark.asyncio
async def test_record_terminal_does_not_clobber_split_done(pg_jit_or_skip):
    from datetime import UTC, datetime

    from sqlalchemy import delete, select

    from app.agents.whatsapp_jit_hold.models import STATUS_KEPT_ORIGINAL, WhatsappJitHoldOrder
    from app.db.session import AsyncSessionLocal

    oid = f"POLOCK{datetime.now(UTC).strftime('%H%M%S%f')}"
    await order_tracker.record_triggered(oid)
    await order_tracker.record_terminal(oid, STATUS_SPLIT_DONE)
    await order_tracker.record_terminal(oid, STATUS_KEPT_ORIGINAL)
    await order_tracker.record_triggered(oid)
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(select(WhatsappJitHoldOrder).where(WhatsappJitHoldOrder.parent_order_id == oid))
        ).scalar_one()
        assert row.status == STATUS_SPLIT_DONE
        await s.execute(delete(WhatsappJitHoldOrder).where(WhatsappJitHoldOrder.parent_order_id == oid))
        await s.commit()
