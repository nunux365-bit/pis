"""Unit tests for the dashboard metrics aggregation.

Two layers:

* **SQL dialect smoke** \u2014 every statement :func:`compute_metrics` issues is
  compiled against the Postgres dialect at test time. This is the regression
  guard from the ``cardinality(jsonb)`` runtime crash: if someone introduces
  a function that doesn't exist in Postgres (or a JSONB op masquerading as
  an array op), compilation fails here instead of wedging dispatch in prod.

* **Response assembly** \u2014 feed ``compute_metrics`` a fake
  :class:`AsyncSession` whose ``execute`` returns canned rows in the
  order ``compute_metrics`` issues them. Asserts the merge/gap-fill math
  and the final dataclass shape so dashboards see a stable contract.

Nothing here touches Postgres or Gmail. Pure in-process.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.config.settings import settings
from app.email_automation.pipeline import metrics as _metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in for SQLAlchemy ``Result`` with just the two shapes we use."""

    def __init__(self, rows: list[tuple] | tuple | None):
        self._rows = rows

    def all(self):
        return list(self._rows or [])

    def scalar_one(self):
        if isinstance(self._rows, tuple):
            return self._rows[0]
        if isinstance(self._rows, list) and self._rows:
            first = self._rows[0]
            return first[0] if isinstance(first, tuple) else first
        return 0

    def scalar_one_or_none(self):
        if self._rows is None:
            return None
        if isinstance(self._rows, tuple):
            return self._rows[0]
        if isinstance(self._rows, list) and self._rows:
            first = self._rows[0]
            return first[0] if isinstance(first, tuple) else first
        return None


class _FakeSession:
    """Returns canned results in FIFO order, also captures the raw statements.

    Storing ``executed`` lets the dialect-compile test iterate every
    statement the production code path issued \u2014 simpler than manually
    keeping the list in lockstep with refactors.
    """

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.executed: list = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        if not self._results:
            return _FakeResult([])
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# Window spec
# ---------------------------------------------------------------------------


def test_window_spec_bucket_counts_are_fixed():
    """Dashboard columns / chart widths depend on these \u2014 pinning prevents
    a well-meaning refactor from turning 24h into "24h but 12 buckets"."""

    assert _metrics._window_spec("24h") == (timedelta(hours=24), "hour", 24)
    assert _metrics._window_spec("7d") == (timedelta(days=7), "day", 7)
    assert _metrics._window_spec("30d") == (timedelta(days=30), "day", 30)


def test_compute_metrics_rejects_unsupported_window():
    with pytest.raises(ValueError, match="unsupported metrics window"):
        asyncio.run(_metrics.compute_metrics(_FakeSession([]), window="1h"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SQL dialect smoke \u2014 the important safety net
# ---------------------------------------------------------------------------


def test_every_metrics_query_compiles_against_postgres_dialect(monkeypatch):
    """Regression guard: if any query uses a function that doesn't exist in
    Postgres (or an argument type mismatch like ``cardinality(jsonb)``),
    compilation raises here so CI catches it, not a 2am wedge tick.
    """

    monkeypatch.setattr(settings, "email_automation_send_max_attempts", 5)

    # Canned results \u2014 must be at least as many as ``compute_metrics`` executes;
    # the exact shapes don't matter because we only care that the statements
    # compile. The order matches compute_metrics' call sequence.
    results = [
        _FakeResult([("processed", 5), ("failed", 1)]),          # msg-by-status
        _FakeResult([("epharma", "sent", 3), ("chw", "failed", 1)]),  # send-by-(variant,status)
        _FakeResult([]),                                         # skipped-by-reason
        _FakeResult([]),                                         # failed-by-reason
        _FakeResult([]),                                         # trend: sent
        _FakeResult([]),                                         # trend: failed
        _FakeResult([]),                                         # trend: ingested
        _FakeResult((None,)),                                    # last_msg
        _FakeResult((None,)),                                    # last_send
        _FakeResult((0,)),                                       # at_cap
        _FakeResult((0,)),                                       # pwe_open
        _FakeResult((0,)),                                       # stuck_sending
    ]
    db = _FakeSession(results)

    asyncio.run(_metrics.compute_metrics(db, window="24h"))

    # Compile every captured statement. Raises ``CompileError`` or
    # equivalent on unknown-function / wrong-type-arg.
    assert db.executed, "compute_metrics did not issue any SQL"
    for stmt in db.executed:
        compiled = stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
        sql = str(compiled)
        # Defensive: ensure we're not about to fire ``cardinality`` on a JSONB
        # column (that's the specific bug that wedged dispatch last month).
        assert "cardinality(" not in sql.lower(), (
            "cardinality() is array-only; use jsonb_array_length on JSONB columns"
        )


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def test_compute_metrics_assembles_and_gap_fills_24h(monkeypatch):
    """End-to-end assembly: messages + sends counts collapse correctly,
    per-variant breakdown is derived from the joint query, trend is
    gap-filled to exactly 24 hourly buckets ending at ``now``."""

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(settings, "email_automation_send_max_attempts", 5)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last_msg_at = now - timedelta(minutes=30)
    last_send_at = now - timedelta(minutes=5)

    # One hourly bucket 2h ago has real data on all three series.
    bucket_sent = now - timedelta(hours=2)
    bucket_failed = now - timedelta(hours=2)
    bucket_ingest = now - timedelta(hours=2)

    # Canned rows are self-consistent: message total / send totals / trend series match
    # (same invariants the production SQL now guarantees).
    results = [
        _FakeResult([("processed", 2), ("failed", 1), ("skipped", 0)]),
        _FakeResult([
            ("epharma", "sent", 4),
            ("epharma", "skipped", 1),
            ("chw", "sent", 3),
            ("chw", "failed", 1),
        ]),
        _FakeResult([("no_primary_recipient", 1)]),
        _FakeResult([("reclaim_indeterminate", 1)]),
        _FakeResult([(bucket_sent, 7)]),
        _FakeResult([(bucket_failed, 1)]),
        _FakeResult([(bucket_ingest, 3)]),
        _FakeResult((last_msg_at,)),
        _FakeResult((last_send_at,)),
        _FakeResult((2,)),  # at_cap
        _FakeResult((1,)),  # pwe_open
        _FakeResult((0,)),  # stuck_sending
    ]
    db = _FakeSession(results)

    result = asyncio.run(_metrics.compute_metrics(db, window="24h"))

    assert result.window == "24h"
    assert result.bucket_granularity == "hour"
    assert result.health.enabled is True
    assert result.health.test_mode is True
    assert result.health.last_message_received_at == last_msg_at
    assert result.health.last_send_sent_at == last_send_at

    assert result.messages_by_status == {"processed": 2, "failed": 1, "skipped": 0}

    # sends_by_status collapses across variants; sends_by_variant keeps the
    # (variant \u2192 status \u2192 count) structure.
    assert result.sends_by_status == {"sent": 7, "skipped": 1, "failed": 1}
    assert result.sends_by_variant == {
        "epharma": {"sent": 4, "skipped": 1},
        "chw":     {"sent": 3, "failed": 1},
    }
    assert result.sends_skipped_by_reason == {"no_primary_recipient": 1}
    assert result.sends_failed_by_reason == {"reclaim_indeterminate": 1}

    # Trend: 24 hourly buckets, gap-filled with zeros except the one bucket
    # we planted 2h ago which should reflect the merged sent/failed/ingest.
    assert len(result.trend) == 24
    buckets = [p.bucket for p in result.trend]
    assert buckets == sorted(buckets), "trend must be in ascending time order"
    planted = next(p for p in result.trend if p.bucket == bucket_sent)
    assert (planted.sent, planted.failed, planted.ingested) == (7, 1, 3)
    zero_buckets = [p for p in result.trend if p.bucket != bucket_sent]
    assert all(p.sent == 0 and p.failed == 0 and p.ingested == 0 for p in zero_buckets)

    assert sum(result.messages_by_status.values()) == sum(p.ingested for p in result.trend)
    assert (result.sends_by_status.get("sent") or 0) == sum(p.sent for p in result.trend)
    assert (result.sends_by_status.get("failed") or 0) == sum(p.failed for p in result.trend)

    assert result.attention.sends_at_attempt_cap == 2
    assert result.attention.messages_processed_with_errors_open == 1
    assert result.attention.sends_stuck_sending == 0


def test_compute_metrics_empty_db_returns_zero_shaped_response(monkeypatch):
    """Cold system / empty DB: response must still validate and be
    dashboard-safe. Empty dicts, 24-point all-zero trend."""

    monkeypatch.setattr(settings, "email_automation_enabled", False)
    monkeypatch.setattr(settings, "email_automation_test_mode", True)

    results = [
        _FakeResult([]),     # msg-by-status
        _FakeResult([]),     # send-by-(variant, status)
        _FakeResult([]),     # skipped-by-reason
        _FakeResult([]),     # failed-by-reason
        _FakeResult([]),     # trend sent
        _FakeResult([]),     # trend failed
        _FakeResult([]),     # trend ingested
        _FakeResult((None,)),  # last_msg
        _FakeResult((None,)),  # last_send
        _FakeResult((0,)),   # at_cap
        _FakeResult((0,)),   # pwe_open
        _FakeResult((0,)),   # stuck_sending
    ]
    db = _FakeSession(results)

    result = asyncio.run(_metrics.compute_metrics(db, window="24h"))

    assert result.messages_by_status == {}
    assert result.sends_by_status == {}
    assert result.sends_by_variant == {}
    assert result.sends_skipped_by_reason == {}
    assert result.sends_failed_by_reason == {}
    assert len(result.trend) == 24
    assert all(p.sent == 0 and p.failed == 0 and p.ingested == 0 for p in result.trend)
    assert result.attention.sends_at_attempt_cap == 0


def test_compute_metrics_7d_uses_daily_buckets(monkeypatch):
    """``7d`` must resolve to daily granularity and exactly 7 points."""

    monkeypatch.setattr(settings, "email_automation_send_max_attempts", 5)

    results = [_FakeResult([]) for _ in range(7)] + [_FakeResult((None,)), _FakeResult((None,))] + [_FakeResult((0,))] * 3
    db = _FakeSession(results)

    result = asyncio.run(_metrics.compute_metrics(db, window="7d"))

    assert result.bucket_granularity == "day"
    assert len(result.trend) == 7
    # Adjacent buckets are exactly 1 day apart.
    deltas = {
        (result.trend[i + 1].bucket - result.trend[i].bucket) for i in range(6)
    }
    assert deltas == {timedelta(days=1)}


# ---------------------------------------------------------------------------
# Gap-fill math
# ---------------------------------------------------------------------------


def test_gap_fill_keeps_real_points_and_fills_zeros():
    """``_gap_fill_trend`` is pure \u2014 test it without the DB layer."""

    now = datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc)
    anchor = now.replace(minute=0, second=0, microsecond=0)
    real = _metrics.TrendPoint(
        bucket=anchor - timedelta(hours=3), sent=5, failed=0, ingested=2,
    )
    filled = _metrics._gap_fill_trend(
        points=[real],
        cutoff=now - timedelta(hours=24),
        now=now,
        granularity="hour",
        bucket_count=24,
    )

    assert len(filled) == 24
    assert filled[-1].bucket == anchor
    assert filled[0].bucket == anchor - timedelta(hours=23)
    planted = [p for p in filled if p.bucket == real.bucket]
    assert planted and planted[0].sent == 5 and planted[0].ingested == 2


def test_gap_fill_aligns_naive_db_bucket_with_utc_series():
    """Drivers sometimes return ``date_trunc`` as naive UTC; gap-fill must still match."""

    now = datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc)
    naive_plant = datetime(2026, 4, 16, 11, 0, 0)  # no tzinfo — treat as UTC wall time
    real = _metrics.TrendPoint(bucket=naive_plant, sent=9, failed=0, ingested=0)
    filled = _metrics._gap_fill_trend(
        points=[real],
        cutoff=now - timedelta(hours=24),
        now=now,
        granularity="hour",
        bucket_count=24,
    )
    planted = [p for p in filled if p.sent == 9]
    assert len(planted) == 1
    assert planted[0].bucket == naive_plant.replace(tzinfo=timezone.utc)


def test_message_and_send_status_counts_smoke():
    """Lightweight rollups for a second window (e.g. 30d on home dashboard)."""

    results = [
        _FakeResult([("processed", 5), ("failed", 1)]),
        _FakeResult([("a", "sent", 3), ("b", "sent", 2), ("a", "failed", 1)]),
    ]
    db = _FakeSession(results)
    out = asyncio.run(_metrics.message_and_send_status_counts(db, window="30d"))
    assert out["messages_by_status"] == {"processed": 5, "failed": 1}
    assert out["sends_by_status"] == {"sent": 5, "failed": 1}
    assert len(db.executed) == 2
    for stmt in db.executed:
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
