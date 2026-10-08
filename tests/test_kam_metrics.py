"""Per-KAM metric aggregation — math over kam_reply_accuracy payloads.

The DB layer is faked so the aggregation logic is tested without Postgres.
"""

from __future__ import annotations

import pytest

from app.email_automation.pipeline import kam_metrics


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Minimal stand-in: returns the canned (payload,) rows for any execute()."""

    def __init__(self, payloads):
        self._rows = [(p,) for p in payloads]

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def test_kam_agg_properties():
    agg = kam_metrics.KamAgg(kam_key="a@1mg.com", kam_name="A")
    agg.threads_scored = 4
    agg.client_replied = 2          # eligible (client-replied) threads
    agg.eligible_score_sum = 1      # 1/2 eligible threads accurate
    agg.kam_replied = 1
    agg.team_replied = 1            # 1/2 eligible threads got a client-facing reply
    agg.reply_seconds_sum = 7200
    agg.reply_seconds_count = 2

    assert agg.eligible_threads == 2
    assert agg.accuracy_rate == 50.0
    assert agg.reply_rate == 50.0
    assert agg.avg_reply_seconds == 3600.0


def test_kam_agg_zero_denominators():
    agg = kam_metrics.KamAgg(kam_key="x")
    assert agg.accuracy_rate == 0.0
    assert agg.reply_rate == 0.0
    assert agg.avg_reply_seconds is None


@pytest.mark.asyncio
async def test_aggregate_groups_by_kam_key():
    payloads = [
        # KAM A: 3 eligible (client-replied) threads.
        # 1) KAM personally followed up -> accurate.
        {"kam_key": "a@1mg.com", "kam_name": "A", "score": 1, "client_responsive": True,
         "kam_replied": True, "team_replied": True, "reply_seconds": 100},
        # 2) nobody followed up -> inaccurate, not a reply.
        {"kam_key": "a@1mg.com", "kam_name": "A", "score": 0, "client_responsive": True,
         "kam_replied": False, "team_replied": False, "reply_seconds": None},
        # 3) a Central/Finance teammate (not the KAM) sent the client-facing reply ->
        #    counts toward reply_rate but not toward the KAM's own kam_replied.
        {"kam_key": "a@1mg.com", "kam_name": "A", "score": 1, "client_responsive": True,
         "kam_replied": False, "team_replied": True, "reply_seconds": None},
        # KAM B: 1 thread, client unresponsive -> excluded from both rates entirely.
        {"kam_key": "b@1mg.com", "kam_name": "B", "score": 1,
         "client_responsive": False, "kam_replied": False, "team_replied": False,
         "reply_seconds": None},
        # Row with no kam_key is ignored.
        {"score": 1, "client_responsive": True, "kam_replied": True, "team_replied": True},
    ]
    aggs = await kam_metrics.aggregate_kam_reply_metrics(_FakeDB(payloads), window="7d")

    assert set(aggs) == {"a@1mg.com", "b@1mg.com"}

    a = aggs["a@1mg.com"]
    assert a.threads_scored == 3
    assert a.client_replied == 3 and a.eligible_threads == 3
    assert a.kam_replied == 1
    assert a.team_replied == 2
    assert a.accuracy_rate == round(100.0 * 2 / 3, 1)
    assert a.reply_rate == round(100.0 * 2 / 3, 1)
    assert a.avg_reply_seconds == 100.0

    b = aggs["b@1mg.com"]
    assert b.threads_scored == 1
    assert b.no_client_reply_threads == 1
    # No client reply -> excluded from the eligible base -> both rates are 0.0,
    # not a division error, and the thread does NOT count as "accurate".
    assert b.client_replied == 0
    assert b.accuracy_rate == 0.0
    assert b.reply_rate == 0.0
    assert b.avg_reply_seconds is None
