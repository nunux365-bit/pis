"""HTTP API + DB integration for responder eval dashboard filters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.config.settings import settings
from app.main import app
from tests.test_responder_eval_dashboard_db import _chat_id, _insert_run, _scrub

pytest_plugins = [
    "tests.test_responder_eval_api",
    "tests.test_responder_eval_dashboard_db",
]


@pytest.mark.asyncio
async def test_api_grade_distribution_na_count_and_list_filters(
    pg_or_skip, responder_eval_auth_override
):
    settings.responder_eval_enabled = True
    c1 = _chat_id()
    now = datetime.now(UTC)
    try:
        await _insert_run(
            c1,
            letter_grade="D",
            bot_grade="D",
            human_grade="C",
            bot_score=45,
            human_score=60,
            composite_score=48,
            created_at=now,
            eval_json={"resolution": "no"},
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            dist = (await c.get("/api/responder-evals/grade-distribution?days=7")).json()
            assert "na_count" in dist["composite"]
            assert "na_count" in dist["bot"]
            assert "na_count" in dist["human"]

            listed = (
                await c.get(
                    "/api/responder-evals?days=7&segment=bot&grade=D&limit=50&offset=0"
                )
            ).json()
            assert listed["total"] >= 1
            assert any(item["chat_id"] == c1 for item in listed["items"])

            summary = (await c.get("/api/responder-evals/summary?days=7")).json()
            assert summary["window_days"] == 7

            ts = (await c.get("/api/responder-evals/timeseries?days=7&granularity=day")).json()
            assert ts["granularity"] == "day"
            assert all("total_bot" in p and "total_human" in p for p in ts["points"])

            issues = (await c.get("/api/responder-evals/top-issues?days=7&limit=5")).json()
            assert set(issues) >= {"window_days", "composite", "bot", "human"}
    finally:
        await _scrub([c1])


@pytest.mark.asyncio
async def test_api_list_status_search_and_date_window(pg_or_skip, responder_eval_auth_override):
    settings.responder_eval_enabled = True
    c_recent, c_old = _chat_id(), _chat_id()
    order_key = f"API{uuid.uuid4().hex[:5]}"
    try:
        await _insert_run(
            c_recent,
            letter_grade="-",
            bot_grade="-",
            human_grade="-",
            bot_score=None,
            human_score=None,
            composite_score=None,
            eval_status="not_graded",
            order_id=f"{order_key}-recent",
            created_at=datetime.now(UTC),
            eval_json={},
        )
        await _insert_run(
            c_old,
            letter_grade="C",
            bot_grade="C",
            human_grade="C",
            bot_score=50,
            human_score=50,
            composite_score=50,
            order_id=f"{order_key}-old",
            created_at=datetime.now(UTC) - timedelta(days=60),
            eval_json={"resolution": "no"},
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            recent = (
                await c.get(
                    f"/api/responder-evals?days=7&eval_status=not_graded&search={order_key}&limit=50"
                )
            ).json()
            assert recent["total"] >= 1
            assert any(item["chat_id"] == c_recent for item in recent["items"])

            old = (await c.get(f"/api/responder-evals?days=7&search={order_key}-old&limit=50")).json()
            assert not any(item["chat_id"] == c_old for item in old["items"])

            old_90 = (
                await c.get(f"/api/responder-evals?days=90&search={order_key}-old&limit=50")
            ).json()
            assert any(item["chat_id"] == c_old for item in old_90["items"])
    finally:
        await _scrub([c_recent, c_old])
