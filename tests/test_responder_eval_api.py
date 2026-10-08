"""Responder eval API route tests."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config.settings import settings
from app.main import app


@pytest.fixture
def responder_eval_auth_override():
    """Responder eval reviewer — dashboard access gate."""
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
async def test_responder_eval_routes_disabled(responder_eval_auth_override):
    settings.responder_eval_enabled = False
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/summary")
        assert r.status_code == 503
    finally:
        settings.responder_eval_enabled = False


@pytest.mark.asyncio
async def test_responder_eval_requires_reviewer_role(employee_auth_override):
    settings.responder_eval_enabled = True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/summary")
        assert r.status_code == 403
    finally:
        settings.responder_eval_enabled = False


@pytest.mark.asyncio
async def test_responder_eval_summary_enabled(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {"window_days": 30, "evaluated": 2, "pending_dumps": 1, "avg_composite_score": 81.5}
    with patch(
        "app.api.routes.responder_eval.eval_summary",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/summary?days=30")
    assert r.status_code == 200
    assert r.json()["evaluated"] == 2


@pytest.mark.asyncio
async def test_responder_eval_pending_dumps_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {"items": [], "total": 0}
    with patch(
        "app.api.routes.responder_eval.eval_pending_dumps",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/pending-dumps")
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_responder_eval_detail_includes_segment_evals(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    run_id = uuid.uuid4()
    payload = {
        "id": str(run_id),
        "chat_id": "c1",
        "eval": {
            "segment_evals": {
                "bot": {"composite_score": 72, "letter_grade": "C", "resolution": "partial", "graded": True},
                "human_agent": {"composite_score": 55, "letter_grade": "D", "resolution": "no", "graded": True},
            },
            "resolution": "partial",
        },
        "composite_score": 65,
        "letter_grade": "C",
    }
    with patch(
        "app.api.routes.responder_eval.get_responder_eval_run",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/responder-evals/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["eval"]["segment_evals"]["bot"]["composite_score"] == 72
    assert body["eval"]["segment_evals"]["human_agent"]["letter_grade"] == "D"


@pytest.mark.asyncio
async def test_responder_eval_detail_uses_runs_prefix(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    run_id = uuid.uuid4()
    with patch(
        "app.api.routes.responder_eval.get_responder_eval_run",
        new_callable=AsyncMock,
        return_value={"id": str(run_id), "chat_id": "c1"},
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/responder-evals/runs/{run_id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_retry_eval_dump_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    with patch(
        "app.api.routes.responder_eval.retry_dump_eval",
        new_callable=AsyncMock,
        return_value=True,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/responder-evals/dumps/chat-1/retry")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_responder_eval_list_enabled(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    run_id = uuid.uuid4()
    row = type(
        "Row",
        (),
        {
            "id": run_id,
            "chat_id": "chat-1",
            "order_id": "PO1",
            "composite_score": 88,
            "letter_grade": "B",
            "bot_grade": "B",
            "human_grade": "C",
            "eval_status": "completed",
            "eval_version": "responder_eval_v1.1",
            "created_at": type("DT", (), {"isoformat": lambda self: "2026-05-18T10:00:00+00:00"})(),
        },
    )()
    with patch(
        "app.api.routes.responder_eval.list_responder_eval_runs",
        new_callable=AsyncMock,
        return_value=([row], 1),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["chat_id"] == "chat-1"
    assert body["items"][0]["bot_grade"] == "B"
    assert body["items"][0]["human_grade"] == "C"


@pytest.mark.asyncio
async def test_responder_eval_grade_distribution_segment_shape(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "window_days": 30,
        "composite": {"slices": [{"grade": "B", "count": 3}], "na_count": 2},
        "bot": {"slices": [{"grade": "C", "count": 1}], "na_count": 5},
        "human": {"slices": [{"grade": "A", "count": 2}], "na_count": 1},
    }
    with patch(
        "app.api.routes.responder_eval.eval_grade_distribution",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/grade-distribution?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["composite"]["slices"][0]["grade"] == "B"
    assert body["bot"]["slices"][0]["count"] == 1


@pytest.mark.asyncio
async def test_responder_eval_top_issues_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "window_days": 7,
        "composite": {
            "E": [
                {
                    "issue": "hallucination",
                    "count": 2,
                    "attributes": [{"id": "false_refund", "count": 2}],
                }
            ],
            "C": [],
            "D": [],
        },
        "bot": {"E": [], "D": [], "C": [{"issue": "guardrail:no_pii", "count": 1}]},
        "bot_closed": {"E": [], "D": [], "C": []},
        "bot_pre_handoff": {"E": [], "D": [], "C": []},
        "human": {"E": [], "D": [], "C": []},
    }
    with patch(
        "app.api.routes.responder_eval.eval_top_issues",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/top-issues?days=7&limit=5")
    assert r.status_code == 200
    assert r.json()["composite"]["E"][0]["issue"] == "hallucination"
    assert r.json()["composite"]["E"][0]["attributes"][0]["id"] == "false_refund"


@pytest.mark.asyncio
async def test_responder_eval_hand_off_analysis_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "reference_date": "2026-08-21",
        "day_labels": ["D-2", "D-1", "Today"],
        "volume": {"D-2": 10, "D-1": 12, "Today": 3},
        "days": [],
        "bucket_order": ["delivery_disputes"],
    }
    with patch(
        "app.api.routes.responder_eval.eval_handoff_analysis",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/hand-off-analysis")
    assert r.status_code == 200
    assert r.json()["volume"]["D-1"] == 12


@pytest.mark.asyncio
async def test_responder_eval_hand_off_analysis_chat_ids_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "since": "2026-08-15",
        "until": "2026-08-21",
        "total": 2,
        "chat_ids": ["111", "222"],
    }
    with patch(
        "app.api.routes.responder_eval.eval_handoff_chat_ids",
        new_callable=AsyncMock,
        return_value=fake,
    ) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/api/responder-evals/hand-off-analysis/chat-ids?bucket=delivery_disputes"
            )
    assert r.status_code == 200
    assert r.json()["chat_ids"] == ["111", "222"]
    assert mocked.await_args.kwargs == {
        "bucket": "delivery_disputes",
        "sub_bucket": None,
        "scope": "last_7",
        "day": None,
    }


@pytest.mark.asyncio
async def test_responder_eval_hand_off_analysis_chat_ids_day_scope(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "since": "2026-09-09",
        "until": "2026-09-09",
        "total": 23,
        "chat_ids": [str(i) for i in range(23)],
    }
    with patch(
        "app.api.routes.responder_eval.eval_handoff_chat_ids",
        new_callable=AsyncMock,
        return_value=fake,
    ) as mocked:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/api/responder-evals/hand-off-analysis/chat-ids"
                "?bucket=delivery_disputes&sub_bucket=delivery_disputes.late_delivery"
                "&scope=day&day=2026-09-09"
            )
    assert r.status_code == 200
    assert r.json()["total"] == 23
    assert len(r.json()["chat_ids"]) == 23
    assert mocked.await_args.kwargs == {
        "bucket": "delivery_disputes",
        "sub_bucket": "delivery_disputes.late_delivery",
        "scope": "day",
        "day": date(2026, 9, 9),
    }


@pytest.mark.asyncio
async def test_responder_eval_hand_off_analysis_chat_ids_day_scope_requires_day(
    responder_eval_auth_override,
):
    settings.responder_eval_enabled = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            "/api/responder-evals/hand-off-analysis/chat-ids?bucket=delivery_disputes&scope=day"
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_responder_eval_charts_route(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    fake = {
        "summary": {"window_days": 30, "evaluated": 1},
        "grades": {"window_days": 30, "composite": {"slices": [], "na_count": 0}},
        "timeseries": {"window_days": 30, "granularity": "day", "points": []},
        "buckets": {"window_days": 30, "composite": {"buckets": []}},
        "resolutions": {"window_days": 30, "composite": {"slices": []}},
        "top_issues": {
            "window_days": 30,
            "composite": {"E": [], "D": [], "C": []},
            "bot": {"E": [], "D": [], "C": []},
            "bot_closed": {"E": [], "D": [], "C": []},
            "bot_pre_handoff": {"E": [], "D": [], "C": []},
            "human": {"E": [], "D": [], "C": []},
        },
    }
    with patch(
        "app.api.routes.responder_eval.eval_charts",
        new_callable=AsyncMock,
        return_value=fake,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/charts?days=30&granularity=day")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["evaluated"] == 1
    assert "top_issues" in body


@pytest.mark.asyncio
async def test_responder_eval_list_passes_segment(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    with patch(
        "app.api.routes.responder_eval.list_responder_eval_runs",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as list_mock:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals?segment=bot&grade=C")
    assert r.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs["segment"] == "bot"
    assert list_mock.await_args.kwargs["grade"] == "C"


@pytest.mark.asyncio
async def test_responder_eval_list_passes_reason(responder_eval_auth_override):
    settings.responder_eval_enabled = True
    with patch(
        "app.api.routes.responder_eval.list_responder_eval_runs",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as list_mock:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/api/responder-evals?segment=composite&reason=traceability:unsupported_refund"
            )
    assert r.status_code == 200
    list_mock.assert_awaited_once()
    assert list_mock.await_args.kwargs["reason"] == "traceability:unsupported_refund"


@pytest.mark.asyncio
async def test_jit_hold_orders_route_defaults_seven_days(responder_eval_auth_override):
    fake = {
        "items": [],
        "total": 0,
        "stats": {"total": 0, "triggered": 0, "split_done": 0, "kept_original": 0},
    }
    with patch(
        "app.api.routes.responder_eval.query_jit_hold_orders",
        new_callable=AsyncMock,
        return_value=fake,
    ) as q:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/responder-evals/jit-hold-orders")
    assert r.status_code == 200
    q.assert_awaited_once()
    assert q.await_args.kwargs["days"] == 7
    assert q.await_args.kwargs["status"] is None


@pytest.mark.asyncio
async def test_jit_hold_orders_route_when_eval_disabled(responder_eval_auth_override):
    settings.responder_eval_enabled = False
    fake = {
        "items": [
            {
                "parent_order_id": "PO1",
                "status": "split_done",
                "triggered_at": "2026-09-08T10:00:00+00:00",
                "updated_at": "2026-09-08T10:05:00+00:00",
            }
        ],
        "total": 1,
        "stats": {"total": 1, "triggered": 0, "split_done": 1, "kept_original": 0},
    }
    try:
        with patch(
            "app.api.routes.responder_eval.query_jit_hold_orders",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(
                    "/api/responder-evals/jit-hold-orders?days=30&status=split_done&search=PO"
                )
        assert r.status_code == 200
        assert r.json()["items"][0]["parent_order_id"] == "PO1"
    finally:
        settings.responder_eval_enabled = False


@pytest.mark.asyncio
async def test_jit_hold_orders_requires_reviewer(employee_auth_override):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/responder-evals/jit-hold-orders")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_jit_hold_orders_rejects_invalid_status(responder_eval_auth_override):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/responder-evals/jit-hold-orders?status=nope")
    assert r.status_code == 422
