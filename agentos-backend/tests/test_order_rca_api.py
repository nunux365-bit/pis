"""Order RCA API route tests (mocked service)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.routes import order_rca as order_rca_routes
from app.config.settings import settings
from app.db.models import User, UserRole

app = FastAPI()
app.include_router(order_rca_routes.router, prefix="/api")
app.include_router(order_rca_routes.internal_router, prefix="/api")


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


@pytest.fixture
def any_user():
    return User(
        id=uuid.uuid4(),
        email="u@test.com",
        roles=[UserRole.EMPLOYEE.value],
        is_active=True,
        hashed_password="x",
        full_name="U",
    )


@pytest.mark.asyncio
async def test_diagnose_disabled(any_user):
    app.dependency_overrides[get_current_user] = lambda: any_user
    prev = settings.order_rca_enabled
    settings.order_rca_enabled = False
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/order-rca/diagnose", json={"order_id": "PO1234"})
        assert r.status_code == 503
    finally:
        app.dependency_overrides.clear()
        settings.order_rca_enabled = prev


@pytest.mark.asyncio
async def test_diagnose_accepted(any_user):
    app.dependency_overrides[get_current_user] = lambda: any_user
    prev = settings.order_rca_enabled
    settings.order_rca_enabled = True
    try:
        with patch("app.api.routes.order_rca.service.ensure_redis", new_callable=AsyncMock):
            with patch(
                "app.api.routes.order_rca.service.start_diagnosis",
                new_callable=AsyncMock,
                return_value={"run_id": str(uuid.uuid4()), "order_id": "PO1", "status": "queued"},
            ):
                with patch("app.api.routes.order_rca.graph.run_graph", new_callable=AsyncMock):
                    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                        r = await c.post("/api/order-rca/diagnose", json={"order_id": "PO13326295207344"})
        assert r.status_code == 202
    finally:
        app.dependency_overrides.clear()
        settings.order_rca_enabled = prev


@pytest.mark.asyncio
async def test_diagnose_idempotent_skips_second_graph(any_user):
    app.dependency_overrides[get_current_user] = lambda: any_user
    prev_enabled = settings.order_rca_enabled
    prev_fixtures = settings.order_rca_use_fixtures
    settings.order_rca_enabled = True
    settings.order_rca_use_fixtures = True
    fake = _FakeRedis()
    try:
        with patch("app.api.routes.order_rca.service.ensure_redis", new_callable=AsyncMock):
            with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
                with patch("app.infra.redis_client.get_redis", lambda: fake):
                    with patch("app.api.routes.order_rca.graph.run_graph", new_callable=AsyncMock) as run_graph:
                        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                            r1 = await c.post("/api/order-rca/diagnose", json={"order_id": "PO13326295207344"})
                            r2 = await c.post("/api/order-rca/diagnose", json={"order_id": "po13326295207344"})
        assert r1.status_code == 202, r1.text
        assert r2.status_code == 202, r2.text
        assert r1.json()["run_id"] == r2.json()["run_id"]
        assert run_graph.await_count == 1
    finally:
        app.dependency_overrides.clear()
        settings.order_rca_enabled = prev_enabled
        settings.order_rca_use_fixtures = prev_fixtures


@pytest.mark.asyncio
async def test_get_run_wrong_user_returns_none():
    from app.agents.order_rca import run_store, service

    fake = _FakeRedis()
    with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
        doc = await run_store.create_run("PO1", user_id="owner-a")
        other = await service.get_run(doc["run_id"], user_id="owner-b")
        assert other is None
        owner = await service.get_run(doc["run_id"], user_id="owner-a")
        assert owner is not None


@pytest.mark.asyncio
async def test_internal_diagnose_does_not_reuse_existing_run():
    settings.order_rca_enabled = True
    settings.order_rca_use_fixtures = True
    fake = _FakeRedis()
    try:
        with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
            with patch("app.agents.order_rca.service.get_redis", lambda: fake):
                with patch("app.api.routes.order_rca.graph.run_graph", new_callable=AsyncMock) as run_graph:
                    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                        r1 = await c.post(
                            "/api/__internal__/order-rca/diagnose",
                            json={"order_id": "PO13326295207344"},
                        )
                        r2 = await c.post(
                            "/api/__internal__/order-rca/diagnose",
                            json={"order_id": "po13326295207344"},
                        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["run_id"] != r2.json()["run_id"]
        assert run_graph.await_count == 2
    finally:
        settings.order_rca_use_fixtures = False


@pytest.mark.asyncio
async def test_internal_diagnose_passes_chat_id():
    settings.order_rca_enabled = True
    with patch("app.api.routes.order_rca.service.ensure_redis", new_callable=AsyncMock):
        with patch(
            "app.api.routes.order_rca.service.start_diagnosis",
            new_callable=AsyncMock,
            return_value={"run_id": "r1", "order_id": "PO1", "status": "queued"},
        ) as start_diagnosis:
            with patch("app.api.routes.order_rca.graph.run_graph", new_callable=AsyncMock):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post(
                        "/api/__internal__/order-rca/diagnose",
                        json={"order_id": "PO13326295207344", "chat_id": "chat-xyz"},
                    )
    assert r.status_code == 202
    start_diagnosis.assert_awaited_once_with(
        "PO13326295207344",
        user_id=order_rca_routes.INTERNAL_USER_ID,
        reuse_existing=False,
        chat_id="chat-xyz",
    )


@pytest.mark.asyncio
async def test_internal_diagnose_no_auth_required():
    settings.order_rca_enabled = True
    with patch("app.api.routes.order_rca.service.ensure_redis", new_callable=AsyncMock):
        with patch(
            "app.api.routes.order_rca.service.start_diagnosis",
            new_callable=AsyncMock,
            return_value={"run_id": str(uuid.uuid4()), "order_id": "PO1", "status": "queued"},
        ) as start_diagnosis:
            with patch("app.api.routes.order_rca.graph.run_graph", new_callable=AsyncMock):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post(
                        "/api/__internal__/order-rca/diagnose",
                        json={"order_id": "PO13326295207344"},
                    )
    assert r.status_code == 202
    start_diagnosis.assert_awaited_once_with(
        "PO13326295207344",
        user_id=order_rca_routes.INTERNAL_USER_ID,
        reuse_existing=False,
        chat_id=None,
    )


@pytest.mark.asyncio
async def test_internal_diagnose_disabled():
    prev = settings.order_rca_enabled
    settings.order_rca_enabled = False
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/api/__internal__/order-rca/diagnose",
                json={"order_id": "PO1234"},
            )
        assert r.status_code == 503
    finally:
        settings.order_rca_enabled = prev


@pytest.mark.asyncio
async def test_internal_get_run_no_auth_required():
    from app.agents.order_rca import run_store

    settings.order_rca_enabled = True
    fake = _FakeRedis()
    with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
        doc = await run_store.create_run("PO1", user_id=order_rca_routes.INTERNAL_USER_ID)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/__internal__/order-rca/runs/{doc['run_id']}")
    assert r.status_code == 200
    assert r.json()["run_id"] == doc["run_id"]


@pytest.mark.asyncio
async def test_internal_run_not_visible_on_public_route(any_user):
    from app.agents.order_rca import run_store

    settings.order_rca_enabled = True
    fake = _FakeRedis()
    app.dependency_overrides[get_current_user] = lambda: any_user
    try:
        with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
            doc = await run_store.create_run("PO1", user_id=order_rca_routes.INTERNAL_USER_ID)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get(f"/api/order-rca/runs/{doc['run_id']}")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_public_run_not_visible_on_internal_route(any_user):
    from app.agents.order_rca import run_store

    settings.order_rca_enabled = True
    fake = _FakeRedis()
    with patch("app.agents.order_rca.run_store.get_redis", lambda: fake):
        doc = await run_store.create_run("PO1", user_id=str(any_user.id))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"/api/__internal__/order-rca/runs/{doc['run_id']}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_start_diagnosis_patches_chat_id_on_reused_run(monkeypatch):
    from app.agents.order_rca import service

    settings.order_rca_enabled = True
    settings.order_rca_use_fixtures = True
    existing = {
        "run_id": "run-reuse",
        "order_id": "PO1",
        "status": "completed",
        "user_id": "u1",
    }
    patched = {**existing, "chat_id": "chat-42"}

    monkeypatch.setattr(
        "app.agents.order_rca.service.run_store.find_reusable_run",
        AsyncMock(return_value=existing),
    )
    patch_run = AsyncMock(return_value=patched)
    monkeypatch.setattr("app.agents.order_rca.service.run_store.patch_run", patch_run)
    monkeypatch.setattr("app.agents.order_rca.service.ensure_redis", AsyncMock())

    out = await service.start_diagnosis("PO1", user_id="u1", chat_id="chat-42", reuse_existing=True)
    assert out["run_id"] == "run-reuse"
    patch_run.assert_awaited_once_with("run-reuse", chat_id="chat-42")
    settings.order_rca_use_fixtures = False
