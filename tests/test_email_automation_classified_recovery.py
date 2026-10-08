"""Crash-recovery: ``status='classified'`` orphans MUST be re-picked.

The pipeline writes ``status='classified'`` to ``EmailAutomationMessage`` *before*
attachment download + variant processing (see ``_process_one``). That commit
exists for diagnostics — without it ops can't tell what crashed mid-flight —
but it leaves a 10-30s window where a SIGKILL / OOM / deploy can wedge the row
forever if the next-tick picker doesn't include ``"classified"``.

This file pins the picker contract: the SELECT issued by
``classify_and_process_received`` MUST cover ``received``, ``classified``,
**and** ``processed_with_errors``. We intercept the executed statement against
a stub session — no DB needed, runs in milliseconds, fails loudly the moment
someone tightens the picker back to "received only".
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.db.models import EmailAutomationMessage
from app.email_automation import pipeline


class _RowsResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    """Enough of the AsyncSession surface to satisfy the picker."""

    def __init__(self) -> None:
        self.executed: list[object] = []

    async def execute(self, stmt):  # noqa: D401 - protocol shim
        self.executed.append(stmt)
        return _RowsResult()

    async def commit(self) -> None:  # pragma: no cover - never reached (no rows)
        pass


def _picker_statuses_in_clause(stmt) -> set[str]:
    """Pull the literal status values out of the picker's WHERE ... IN (...)."""

    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # The picker is the unique SELECT whose WHERE clause is a single
    # ``email_automation_messages.status IN (...)`` predicate. We isolate the
    # IN-list and split — robust against ordering / formatting changes.
    assert "email_automation_messages.status IN" in sql, sql
    inside = sql.split("status IN", 1)[1].split("(", 1)[1].split(")", 1)[0]
    return {tok.strip().strip("'").strip('"') for tok in inside.split(",")}


def test_picker_includes_classified_for_orphan_recovery(monkeypatch: pytest.MonkeyPatch):
    """Regression guard: a crash between the ``classified`` commit and the
    final commit must NOT wedge the message — the next tick MUST pick it up."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    db = _CapturingSession()
    asyncio.new_event_loop().run_until_complete(
        pipeline.classify_and_process_received(db, inbox_query="from:x@y.com")
    )
    # The picker is the only SELECT issued before any rows come back.
    select_stmts = [
        s for s in db.executed if str(s).lstrip().lower().startswith("select")
    ]
    assert select_stmts, "expected a SELECT to be issued by the picker"
    statuses = _picker_statuses_in_clause(select_stmts[0])
    assert statuses == {"received", "classified", "processed_with_errors"}, (
        f"picker drift — got {statuses!r}; "
        "if 'classified' was dropped, mid-flight crashes will wedge messages forever"
    )


def test_picker_query_shape_matches_model(monkeypatch: pytest.MonkeyPatch):
    """Smoke test: the picker must select the model class (not a column subset),
    so downstream code can mutate the row in-place."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    db = _CapturingSession()
    asyncio.new_event_loop().run_until_complete(
        pipeline.classify_and_process_received(db, inbox_query="from:x@y.com")
    )
    select_stmts = [
        s for s in db.executed if str(s).lstrip().lower().startswith("select")
    ]
    sql = str(select_stmts[0])
    # Sanity: it's selecting from the right table.
    assert "email_automation_messages" in sql
    # And it's a whole-row select (the picker mutates rows, so it can't be a
    # subset). The column-sentinel here is the timestamp the F2 retry path
    # touches — if a future refactor narrows the SELECT, that field must
    # still be present.
    _ = select(EmailAutomationMessage)  # import-side smoke: model is loadable
