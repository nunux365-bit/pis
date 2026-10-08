"""Procurement reference-values API — plant list filtered by purchasing_org."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.routes import procurement as procurement_routes
from app.db.models import User, UserRole
from app.db.session import get_db

app = FastAPI()
app.include_router(procurement_routes.router, prefix="/api/procurement")


def _plant_row(code: str) -> MagicMock:
    row = MagicMock()
    row.code = code
    row.label = f"Plant {code}"
    row.document_type = ""
    row.applies_to_kind = ""
    row.extra = None
    return row


def _org_row(code: str) -> MagicMock:
    row = MagicMock()
    row.code = code
    row.label = code
    row.document_type = ""
    row.applies_to_kind = ""
    row.extra = None
    return row


ALL_PLANTS = (
    _plant_row("0001"),
    _plant_row("H001"),
    _plant_row("H002"),
    _plant_row("T001"),
    _plant_row("L001"),
)


@pytest.fixture
def any_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="proc-ref@test.example.com",
        roles=[UserRole.EMPLOYEE.value],
        is_active=True,
    )


@pytest.fixture
def client_plant_catalog(any_user: User):
    """HTTP client with DB session returning a fixed plant catalogue (no Postgres required)."""

    async def _get_db() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(ALL_PLANTS)
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[get_current_user] = lambda: any_user
    app.dependency_overrides[get_db] = _get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client_mixed_domains(any_user: User):
    """Catalogue rows depend on requested domain (plant vs purchasing_org)."""

    async def _get_db() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()

        async def _execute(stmt):  # noqa: ANN001 — SQLAlchemy statement
            result = MagicMock()
            domain = stmt.compile().params.get("domain_1", "")
            if domain == "plant":
                result.scalars.return_value.all.return_value = list(ALL_PLANTS)
            elif domain == "purchasing_org":
                result.scalars.return_value.all.return_value = [
                    _org_row("1MGH"),
                    _org_row("1MGT"),
                    _org_row("1LFS"),
                ]
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = _execute
        yield session

    app.dependency_overrides[get_current_user] = lambda: any_user
    app.dependency_overrides[get_db] = _get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.clear()


def _codes(body: list[dict]) -> list[str]:
    return [str(r["code"]) for r in body]


@pytest.mark.asyncio
async def test_api_plant_list_unfiltered_without_purchasing_org(client_plant_catalog: AsyncClient) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={"domain": "plant", "document_type": "YUNB", "ticket_kind": "PR"},
    )
    assert r.status_code == 200
    codes = _codes(r.json())
    assert set(codes) == {"0001", "H001", "H002", "T001", "L001"}


@pytest.mark.asyncio
async def test_api_plant_list_filtered_1mgh(client_plant_catalog: AsyncClient) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={
            "domain": "plant",
            "document_type": "YUNB",
            "ticket_kind": "PR",
            "purchasing_org": "1MGH",
        },
    )
    assert r.status_code == 200
    codes = _codes(r.json())
    assert "H001" in codes and "H002" in codes and "0001" in codes
    assert "T001" not in codes and "L001" not in codes


@pytest.mark.asyncio
async def test_api_plant_list_filtered_1mgt(client_plant_catalog: AsyncClient) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={
            "domain": "plant",
            "document_type": "YSER",
            "purchasing_org": "1MGT",
        },
    )
    assert r.status_code == 200
    codes = _codes(r.json())
    assert codes == ["0001", "T001"]


@pytest.mark.asyncio
async def test_api_plant_list_filtered_1lfs(client_plant_catalog: AsyncClient) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={
            "domain": "plant",
            "document_type": "YAST",
            "purchasing_org": "1LFS",
        },
    )
    assert r.status_code == 200
    codes = _codes(r.json())
    assert codes == ["0001", "L001"]


@pytest.mark.asyncio
async def test_api_purchasing_org_domain_ignores_plant_filter_param(
    client_mixed_domains: AsyncClient,
) -> None:
    """``purchasing_org`` query param only filters ``domain=plant`` rows."""
    r = await client_mixed_domains.get(
        "/api/procurement/reference-values",
        params={
            "domain": "purchasing_org",
            "document_type": "YUNB",
            "purchasing_org": "1MGH",
        },
    )
    assert r.status_code == 200
    assert _codes(r.json()) == ["1MGH", "1MGT", "1LFS"]


@pytest.mark.asyncio
async def test_api_storage_location_list_with_plant_builds_json_filter(
    client_plant_catalog: AsyncClient,
) -> None:
    """Regression: extra['plant'].astext 500'd because extra is JSON-with-JSONB-variant."""
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={
            "domain": "storage_location",
            "document_type": "YUNB",
            "ticket_kind": "PR",
            "plant": "H001",
        },
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_api_storage_location_list_without_plant_is_empty(
    client_plant_catalog: AsyncClient,
) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={"domain": "storage_location", "document_type": "YUNB"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_api_reference_values_requires_document_type(client_plant_catalog: AsyncClient) -> None:
    r = await client_plant_catalog.get(
        "/api/procurement/reference-values",
        params={"domain": "plant"},
    )
    assert r.status_code == 422
