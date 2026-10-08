"""Integration tests — responder eval against real Postgres (skipped when DB unavailable).

Exercises claim → processing → persist → done, API list/detail/retry, and run-id guards
with real SQLAlchemy sessions. Gmail/OpenAI/chat DB are mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.agents.responder_eval import tick
from app.agents.responder_eval.chat_source import ChatFetchResult
from app.agents.responder_eval.constants import (
    EVAL_STATUS_DONE,
    EVAL_STATUS_PENDING,
    EVAL_STATUS_PROCESSING,
    EVAL_STATUS_WAITING,
    EVAL_VERSION,
    RUN_STATUS_COMPLETED,
)
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun
from app.agents.responder_eval.pipeline import persist_eval_run
from app.config.settings import settings
from app.main import app

SAMPLE_CHAT = {
    "chat_id": "chat-fixture-001",
    "messages": [
        {"role": "user", "content": "Where is my order PO13326295207344?"},
        {"role": "assistant", "content": "Your order has been delivered on 17 May."},
    ],
}


def _chat_id() -> str:
    return f"integ-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def pg_or_skip():
    from app.db.session import AsyncSessionLocal

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            async with AsyncSessionLocal() as s:
                await s.execute(select(OrderRcaEvalDump).limit(1))
            return
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.15)
    pytest.skip(f"Postgres / responder_eval schema not available: {last_exc}")


async def _scrub(chat_id: str) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(delete(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id))
            await s.execute(
                delete(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
            )


def _staging_run_doc(chat_id: str, *, run_id: str, order_id: str) -> dict[str, Any]:
    """Unredacted RCA JSON staged in ``response`` until closed-chat eval (production shape)."""
    return {
        "chat_id": chat_id,
        "run_id": run_id,
        "order_id": order_id,
        "status": "completed",
        "report": {
            "facts": {
                "preflight": {"order_status": "Delivered"},
                "perfect_order": {"overall_pass": True},
            },
            "order_details": {
                "payment_summary": {"total_refund_due": 0},
            },
        },
    }


def _done_eval_artifact() -> dict[str, Any]:
    return {
        "preflight": {"order_status": "Delivered"},
        "perfect_order": {"overall_pass": True},
        "order_ops": {"payment_summary": {"total_refund_due": 0}},
    }


async def _insert_pending_dump(
    chat_id: str,
    *,
    run_id: str = "run-integ-1",
    order_id: str = "PO13326295207344",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    eval_status: str = EVAL_STATUS_PENDING,
) -> None:
    from app.db.session import AsyncSessionLocal

    now = created_at or (datetime.now(UTC) - timedelta(hours=2))
    if eval_status == EVAL_STATUS_DONE:
        response: dict[str, Any] = {}
        eval_artifact = _done_eval_artifact()
    else:
        response = _staging_run_doc(chat_id, run_id=run_id, order_id=order_id)
        eval_artifact = {}
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(
                OrderRcaEvalDump(
                    chat_id=chat_id,
                    response=response,
                    eval_artifact=eval_artifact,
                    order_id=order_id,
                    run_id=run_id,
                    eval_status=eval_status,
                    retry_count=0,
                    created_at=now,
                    updated_at=updated_at or now,
                )
            )


@pytest.fixture
def responder_eval_reviewer_app():
    from app.api.deps import get_current_user

    class FakeUser:
        id = uuid.uuid4()
        roles = ["responder_eval_reviewer"]
        email = "glp@test.example.com"
        is_active = True
        department = "General"
        full_name = "GLP Reviewer"
        hashed_password = "x"

        @property
        def is_admin(self) -> bool:
            return False

        @property
        def role_set(self) -> frozenset[str]:
            return frozenset(self.roles)

        def has_role(self, role: str) -> bool:
            return role in self.role_set

        def has_any_role(self, *roles: str) -> bool:
            return bool(self.role_set & {r for r in roles})

        def data_scope(self) -> str:
            return "self"

        @property
        def primary_role(self) -> str:
            return self.roles[0]

    async def _user():
        return FakeUser()

    app.dependency_overrides[get_current_user] = _user
    yield app
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_claim_marks_dump_processing_in_db(pg_or_skip, monkeypatch):
    chat_id = _chat_id()
    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 0)
    try:
        await _insert_pending_dump(chat_id)
        work = await tick._load_batch_work_items(5)
        assert work is not None
        assert any(w.chat_id == chat_id for w in work)

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
                )
            ).scalar_one()
        assert row.eval_status == EVAL_STATUS_PROCESSING
    finally:
        await _scrub(chat_id)


@pytest.mark.asyncio
async def test_pending_not_starved_by_older_waiting_chat(pg_or_skip, monkeypatch):
    """Closed-chat pending must claim ahead of older waiting_chat (HOL regression)."""
    from app.db.session import AsyncSessionLocal

    waiting_id = _chat_id()
    pending_id = _chat_id()
    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 0)
    monkeypatch.setattr(settings, "responder_eval_waiting_recheck_minutes", 60)
    older = datetime.now(UTC) - timedelta(days=2)
    newer = datetime.now(UTC) - timedelta(hours=1)
    try:
        await _insert_pending_dump(
            waiting_id,
            run_id="run-waiting",
            created_at=older,
            updated_at=older,
            eval_status=EVAL_STATUS_WAITING,
        )
        await _insert_pending_dump(
            pending_id,
            run_id="run-pending",
            created_at=newer,
            updated_at=newer,
            eval_status=EVAL_STATUS_PENDING,
        )
        async with AsyncSessionLocal() as s:
            async with s.begin():
                dumps = await tick._pending_dumps(s, 500)
                ours = [d.chat_id for d in dumps if d.chat_id in {waiting_id, pending_id}]
        assert ours == [pending_id, waiting_id], ours
    finally:
        await _scrub(waiting_id)
        await _scrub(pending_id)


@pytest.mark.asyncio
async def test_fresh_waiting_chat_backed_off_until_recheck(pg_or_skip, monkeypatch):
    """waiting_chat updated recently must not be re-claimed every tick."""
    from app.db.session import AsyncSessionLocal

    waiting_id = _chat_id()
    pending_id = _chat_id()
    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 0)
    monkeypatch.setattr(settings, "responder_eval_waiting_recheck_minutes", 60)
    age = datetime.now(UTC) - timedelta(hours=3)
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    try:
        await _insert_pending_dump(
            waiting_id,
            run_id="run-waiting-fresh",
            created_at=age,
            updated_at=fresh,
            eval_status=EVAL_STATUS_WAITING,
        )
        await _insert_pending_dump(
            pending_id,
            run_id="run-pending-only",
            created_at=age,
            updated_at=age,
            eval_status=EVAL_STATUS_PENDING,
        )
        async with AsyncSessionLocal() as s:
            async with s.begin():
                dumps = await tick._pending_dumps(s, 500)
                ours = {d.chat_id for d in dumps if d.chat_id in {waiting_id, pending_id}}
        assert ours == {pending_id}
    finally:
        await _scrub(waiting_id)
        await _scrub(pending_id)


@pytest.mark.asyncio
async def test_batch_eval_persists_run_and_marks_dump_done(pg_or_skip, monkeypatch):
    chat_id = _chat_id()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_min_age_hours", 0)
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)

    sample_chat = {
        "chat_id": chat_id,
        "messages": list(SAMPLE_CHAT["messages"]),
    }
    monkeypatch.setattr(
        tick,
        "fetch_chats_async",
        AsyncMock(return_value=ChatFetchResult(chats={chat_id: sample_chat})),
    )

    try:
        await _insert_pending_dump(chat_id, run_id=run_id)
        out = await tick.run_responder_eval_batch()
        assert out.get("evaluated") == 1

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            dump = (
                await s.execute(
                    select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
                )
            ).scalar_one()
            run = (
                await s.execute(
                    select(ResponderEvalRun).where(
                        ResponderEvalRun.chat_id == chat_id,
                        ResponderEvalRun.eval_version == EVAL_VERSION,
                    )
                )
            ).scalar_one()

        assert dump.eval_status == EVAL_STATUS_DONE
        assert dump.run_id == run_id
        assert dump.response == {}
        assert dump.eval_artifact.get("preflight", {}).get("order_status") == "Delivered"
        assert run.ground_truth_json == dump.eval_artifact
        assert run.eval_status == RUN_STATUS_COMPLETED
        assert run.rca_run_id == run_id
        assert run.composite_score is not None
        from app.agents.responder_eval.segments import segment_row_fields

        expected = segment_row_fields(run.eval_json)
        assert run.bot_score == expected["bot_score"]
        assert run.human_score == expected["human_score"]
        assert run.bot_grade == expected["bot_grade"]
        assert run.human_grade == expected["human_grade"]
    finally:
        await _scrub(chat_id)


@pytest.mark.asyncio
async def test_persist_skips_when_run_id_superseded(pg_or_skip):
    chat_id = _chat_id()
    try:
        await _insert_pending_dump(chat_id, run_id="new-run")
        ok = await persist_eval_run(
            {
                "chat_id": chat_id,
                "rca_run_id": "stale-run",
                "eval_version": EVAL_VERSION,
                "graded": True,
                "composite_score": 80,
                "letter_grade": "B",
                "resolution": "yes",
            }
        )
        assert ok is False

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            count = (
                await s.execute(
                    select(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id)
                )
            ).scalar_one_or_none()
        assert count is None
    finally:
        await _scrub(chat_id)


async def _mark_dump_status(chat_id: str, *, run_id: str, status: str) -> None:
    from app.db.session import AsyncSessionLocal

    now = datetime.now(UTC)
    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(
                update(OrderRcaEvalDump)
                .where(OrderRcaEvalDump.chat_id == chat_id, OrderRcaEvalDump.run_id == run_id)
                .values(eval_status=status, updated_at=now)
            )


@pytest.mark.asyncio
async def test_api_list_and_detail_round_trip(
    pg_or_skip, responder_eval_reviewer_app, monkeypatch
):
    chat_id = _chat_id()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "responder_eval_enabled", True)
    monkeypatch.setattr(settings, "responder_eval_mock_judge", True)

    try:
        await _insert_pending_dump(chat_id, run_id=run_id, eval_status=EVAL_STATUS_PROCESSING)
        persisted = await persist_eval_run(
            {
                "chat_id": chat_id,
                "order_id": "PO13326295207344",
                "rca_run_id": run_id,
                "eval_version": EVAL_VERSION,
                "graded": True,
                "composite_score": 88,
                "letter_grade": "B",
                "resolution": "yes",
                "eval_ground_truth": {
                    "preflight": {"order_status": "Delivered"},
                    "order_ops": {"payment_summary": {"total_refund_due": 0}},
                },
            }
        )
        assert persisted is True

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    select(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id)
                )
            ).scalar_one()
            eval_run_id = row.id

        async with AsyncClient(
            transport=ASGITransport(app=responder_eval_reviewer_app),
            base_url="http://test",
        ) as client:
            list_r = await client.get("/api/responder-evals?search=" + chat_id)
            assert list_r.status_code == 200
            body = list_r.json()
            assert body["total"] >= 1
            assert any(i["chat_id"] == chat_id for i in body["items"])

            detail_r = await client.get(f"/api/responder-evals/runs/{eval_run_id}")
            assert detail_r.status_code == 200
            detail = detail_r.json()
            assert detail["chat_id"] == chat_id
            assert detail["rca_run_id"] == run_id
            assert detail["eval_ground_truth"] is not None
    finally:
        await _scrub(chat_id)


@pytest.mark.asyncio
async def test_fail_eval_skips_when_dump_already_done(pg_or_skip):
    from app.agents.responder_eval.pipeline import fail_eval

    chat_id = _chat_id()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    try:
        await _insert_pending_dump(chat_id, run_id=run_id, eval_status=EVAL_STATUS_DONE)
        changed = await fail_eval(chat_id, "stale_worker_error", expected_run_id=run_id)
        assert changed is False

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
                )
            ).scalar_one()
        assert row.eval_status == EVAL_STATUS_DONE
    finally:
        await _scrub(chat_id)


@pytest.mark.asyncio
async def test_api_retry_failed_dump(pg_or_skip, responder_eval_reviewer_app, monkeypatch):
    chat_id = _chat_id()
    monkeypatch.setattr(settings, "responder_eval_enabled", True)

    try:
        await _insert_pending_dump(chat_id)
        from app.db.session import AsyncSessionLocal
        from app.agents.responder_eval.rca_dump import _apply_dump_status

        async with AsyncSessionLocal() as s:
            async with s.begin():
                await _apply_dump_status(
                    s, chat_id, "failed", last_error="judge_timeout"
                )

        async with AsyncClient(
            transport=ASGITransport(app=responder_eval_reviewer_app),
            base_url="http://test",
        ) as client:
            r = await client.post(f"/api/responder-evals/dumps/{chat_id}/retry")
            assert r.status_code == 204

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
                )
            ).scalar_one()
        assert row.eval_status == EVAL_STATUS_PENDING
        assert row.last_error is None
    finally:
        await _scrub(chat_id)
