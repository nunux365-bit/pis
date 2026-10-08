"""E2E: denormalized storage from persist through dashboard API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agents.responder_eval.constants import EVAL_STATUS_DONE, EVAL_STATUS_PROCESSING, EVAL_VERSION, RUN_STATUS_COMPLETED
from app.agents.responder_eval.models import OrderRcaEvalDump, ResponderEvalRun
from app.agents.responder_eval.pipeline import persist_eval_run
from app.agents.responder_eval.segments import SEGMENT_BOT, SEGMENT_HUMAN_AGENT
from app.config.settings import settings
from tests.test_responder_eval_integration import _chat_id, _insert_pending_dump, _scrub

pytest_plugins = [
    "tests.test_responder_eval_api",
    "tests.test_responder_eval_dashboard_db",
    "tests.test_responder_eval_integration",
]


@pytest.mark.asyncio
async def test_persist_writes_denormalized_columns(pg_or_skip):
    chat_id = _chat_id()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    try:
        await _insert_pending_dump(chat_id, run_id=run_id, eval_status=EVAL_STATUS_PROCESSING)
        chat = {
            "messages": [
                {"role": "user", "content": "Where is my order?", "created_at": "2026-05-18T10:00:00Z"},
                {"role": "bot", "content": "It was delivered.", "created_at": "2026-05-18T10:00:05Z"},
            ],
        }
        ground_truth = {"preflight": {"order_status": "Delivered"}}
        ok = await persist_eval_run(
            {
                "chat_id": chat_id,
                "order_id": "PO-E2E-001",
                "rca_run_id": run_id,
                "eval_version": EVAL_VERSION,
                "graded": True,
                "composite_score": 48,
                "letter_grade": "C",
                "resolution": "no",
                "violations": [{"rule": "no_pii", "turns": [1]}],
                "chat": chat,
                "eval_ground_truth": ground_truth,
                "segment_evals": {
                    SEGMENT_BOT: {
                        "graded": True,
                        "letter_grade": "D",
                        "composite_score": 42,
                        "resolution": "no",
                        "turn_indexes": [1],
                    },
                    SEGMENT_HUMAN_AGENT: {
                        "graded": False,
                        "letter_grade": "-",
                    },
                },
            }
        )
        assert ok is True

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(select(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id))
            ).scalar_one()
            dump = (
                await s.execute(
                    select(OrderRcaEvalDump).where(OrderRcaEvalDump.chat_id == chat_id)
                )
            ).scalar_one()

        assert dump.eval_status == EVAL_STATUS_DONE
        assert dump.response == {}
        assert dump.eval_artifact == ground_truth
        assert row.eval_status == RUN_STATUS_COMPLETED
        assert row.composite_resolution == "no"
        assert row.bot_resolution == "no"
        assert "chat" not in (row.eval_json or {})
        assert "eval_ground_truth" not in (row.eval_json or {})
        assert row.chat_json == chat
        assert row.ground_truth_json == ground_truth
        assert "guardrail:no_pii" in (row.composite_issues or [])
        assert row.bot_issues
        assert row.human_issues == []
    finally:
        await _scrub(chat_id)


@pytest.mark.asyncio
async def test_api_charts_and_detail_after_denormalized_persist(
    pg_or_skip, responder_eval_reviewer_app, monkeypatch
):
    chat_id = _chat_id()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "responder_eval_enabled", True)

    try:
        await _insert_pending_dump(chat_id, run_id=run_id, eval_status=EVAL_STATUS_PROCESSING)
        await persist_eval_run(
            {
                "chat_id": chat_id,
                "order_id": "PO-CHARTS-001",
                "rca_run_id": run_id,
                "eval_version": EVAL_VERSION,
                "graded": True,
                "composite_score": 45,
                "letter_grade": "D",
                "resolution": "no",
                "eval_ground_truth": {"preflight": {"order_status": "Shipped"}},
                "chat": {"messages": [{"role": "user", "content": "help"}]},
                "segment_evals": {
                    SEGMENT_BOT: {
                        "graded": True,
                        "letter_grade": "D",
                        "composite_score": 45,
                        "resolution": "no",
                        "turn_indexes": [0],
                    },
                },
            }
        )

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(select(ResponderEvalRun).where(ResponderEvalRun.chat_id == chat_id))
            ).scalar_one()
            run_uuid = row.id

        async with AsyncClient(
            transport=ASGITransport(app=responder_eval_reviewer_app),
            base_url="http://test",
        ) as client:
            charts = await client.get("/api/responder-evals/charts?days=30&granularity=day")
            assert charts.status_code == 200
            body = charts.json()
            assert "summary" in body
            assert "grades" in body
            assert "top_issues" in body
            assert body["resolutions"]["composite"]["slices"]

            detail = await client.get(f"/api/responder-evals/runs/{run_uuid}")
            assert detail.status_code == 200
            d = detail.json()
            assert d["chat_id"] == chat_id
            assert d["eval_ground_truth"] is not None
            assert d["eval_ground_truth"]["preflight"]["order_status"] == "Shipped"
            assert d["chat"] is not None
            assert d["eval"]["resolution"] == "no"
    finally:
        await _scrub(chat_id)
