"""Reopen approved MIS runs to pending_human for human edits."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agents.o2c_ohc.mis_db import set_mis_status


def _mock_session_factory(session_cls: type):
    class _Begin:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Wrapped(session_cls):
        def begin(self):
            return _Begin()

    class _Ctx:
        async def __aenter__(self):
            return _Wrapped()

        async def __aexit__(self, *args):
            return None

    return lambda: _Ctx()


@pytest.mark.asyncio
async def test_reopen_approved_clears_approval_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    mid = uuid4()
    updates: list[str] = []

    class _Result:
        def __init__(self, row=None, n: int = 1):
            self._row = row
            self.rowcount = n

        def mappings(self):
            return self

        def first(self):
            return self._row

    class _Session:
        async def execute(self, query, params=None):
            sql = str(query)
            if "FOR UPDATE" in sql:
                return _Result({"status": "approved"})
            if "SET status = 'pending_human'" in sql:
                updates.append(sql)
                return _Result(n=1)
            return _Result(n=0)

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    await set_mis_status(
        mis_run_id=mid,
        status="pending_human",
        actor="reviewer@test",
    )
    assert len(updates) == 1
    assert "approved_at = NULL" in updates[0]
    assert "approved_by = NULL" in updates[0]


@pytest.mark.asyncio
async def test_reopen_idempotent_when_already_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    mid = uuid4()
    update_calls = 0

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {"status": "pending_human"}

    class _Session:
        async def execute(self, query, params=None):
            nonlocal update_calls
            if "FOR UPDATE" in str(query):
                return _Result()
            update_calls += 1
            return _Result()

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    await set_mis_status(mis_run_id=mid, status="pending_human", actor="human")
    assert update_calls == 0


@pytest.mark.asyncio
async def test_reopen_rejected_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    mid = uuid4()

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {"status": "rejected"}

    class _Session:
        async def execute(self, query, params=None):
            return _Result()

    monkeypatch.setattr(
        "app.agents.o2c_ohc.mis_db.AgenosAsyncSessionLocal",
        _mock_session_factory(_Session),
    )

    with pytest.raises(ValueError, match="Only approved"):
        await set_mis_status(mis_run_id=mid, status="pending_human", actor="human")
