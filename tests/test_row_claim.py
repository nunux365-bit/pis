"""Skip-locked resource claims: miss skips, hold is txn-scoped."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.infra import row_claim as rc


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_try_claim_row_true_when_select_returns_name():
    from sqlalchemy.dialects import postgresql

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result("refsync:plant"))
    assert await rc.try_claim_row(session, "refsync:plant") is True
    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql


@pytest.mark.asyncio
async def test_try_claim_row_false_when_skip_locked_misses():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result(None))
    assert await rc.try_claim_row(session, "refsync:plant") is False


@pytest.mark.asyncio
async def test_claimed_session_yields_none_when_busy(monkeypatch):
    monkeypatch.setattr(rc, "acquire_claim", AsyncMock(return_value=False))

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _Session:
        def begin(self):
            return _Txn()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(rc, "AsyncSessionLocal", lambda: _Session())
    async with rc.claimed_session("prosight:actionables") as session:
        assert session is None


@pytest.mark.asyncio
async def test_claimed_session_yields_session_when_free(monkeypatch):
    monkeypatch.setattr(rc, "acquire_claim", AsyncMock(return_value=True))

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class _Session:
        def begin(self):
            return _Txn()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    sess = _Session()
    monkeypatch.setattr(rc, "AsyncSessionLocal", lambda: sess)
    async with rc.claimed_session("prosight:actionables") as session:
        assert session is sess


@pytest.mark.asyncio
async def test_acquire_claim_does_not_insert_when_row_exists_and_locked(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(rc, "ensure_claim_row", ensure)
    monkeypatch.setattr(rc, "try_claim_row", AsyncMock(return_value=False))
    session = AsyncMock()
    session.scalar = AsyncMock(return_value="refsync:plant")
    assert await rc.acquire_claim(session, "refsync:plant") is False
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_claim_inserts_only_when_row_missing(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(rc, "ensure_claim_row", ensure)
    monkeypatch.setattr(rc, "try_claim_row", AsyncMock(side_effect=[False, True]))
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    assert await rc.acquire_claim(session, "refsync:new") is True
    ensure.assert_awaited_once_with("refsync:new")
