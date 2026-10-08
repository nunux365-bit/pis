"""Contract ingest site_alias guard vs approved MIS."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.agents.o2c_ohc import site_alias_ingest_guard as guard


class _FakeResult:
    def __init__(self, row: dict | None = None, *, scalar: object | None = None) -> None:
        self._row = row
        self._scalar = scalar

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict | None:
        return self._row

    def scalar(self) -> object | None:
        return self._scalar


class _FakeSession:
    def __init__(self, approved_site_id: str | None) -> None:
        self.approved_site_id = approved_site_id
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, stmt: object, params: dict | None = None) -> _FakeResult:
        self.calls.append((str(stmt), params))
        sql = str(stmt)
        if "EXISTS" in sql and params:
            proposed = str(params.get("proposed") or "")
            if self.approved_site_id and proposed and proposed != self.approved_site_id:
                return _FakeResult(scalar=True)
            return _FakeResult(scalar=False)
        if self.approved_site_id and params and params.get("csk"):
            return _FakeResult({"service_site_id": self.approved_site_id})
        return _FakeResult(None)


BC = "99999999-9999-9999-9999-999999999999"


@pytest.mark.asyncio
async def test_approved_lookup_returns_none_when_empty_key() -> None:
    session = _FakeSession("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert (
        await guard.approved_mis_service_site_id_for_client_site_key_async(
            session, "", billing_client_id=BC
        )
        is None
    )
    assert session.calls == []


@pytest.mark.asyncio
async def test_approved_lookup_returns_site_id() -> None:
    approved = "11111111-1111-1111-1111-111111111111"
    session = _FakeSession(approved)
    got = await guard.approved_mis_service_site_id_for_client_site_key_async(
        session, "TCS-Noida", billing_client_id=BC
    )
    assert got == approved
    assert session.calls[0][1]["csk"] == "TCS-Noida"
    assert session.calls[0][1]["bc"] == BC


@pytest.mark.asyncio
async def test_skip_when_approved_on_different_site() -> None:
    approved = "11111111-1111-1111-1111-111111111111"
    proposed = UUID("22222222-2222-2222-2222-222222222222")
    session = _FakeSession(approved)
    assert await guard.should_skip_ingest_site_alias_upsert_async(
        session,
        alias_code="TCS-Noida",
        proposed_service_site_id=proposed,
        billing_client_id=BC,
    )
    assert "EXISTS" in session.calls[0][0]


@pytest.mark.asyncio
async def test_no_skip_when_same_site_as_approved() -> None:
    approved = "11111111-1111-1111-1111-111111111111"
    session = _FakeSession(approved)
    assert not await guard.should_skip_ingest_site_alias_upsert_async(
        session,
        alias_code="TCS-Noida",
        proposed_service_site_id=approved,
        billing_client_id=BC,
    )


@pytest.mark.asyncio
async def test_no_skip_when_no_approved_mis() -> None:
    session = _FakeSession(None)
    assert not await guard.should_skip_ingest_site_alias_upsert_async(
        session,
        alias_code="New-Site",
        proposed_service_site_id="22222222-2222-2222-2222-222222222222",
        billing_client_id=BC,
    )
