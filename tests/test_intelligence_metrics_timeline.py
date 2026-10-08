"""Unit tests for intelligence metrics timeline gap-fill (no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.email_automation.pipeline.intelligence_metrics import (
    _gap_fill_classified_timeline,
    _intel_window_spec,
)


def test_intel_window_spec_bucket_counts() -> None:
    assert _intel_window_spec("24h")[2] == 24
    assert _intel_window_spec("7d")[2] == 7
    assert _intel_window_spec("30d")[2] == 30


def test_gap_fill_all_zeros_seven_days() -> None:
    now = datetime(2026, 5, 3, 15, 30, tzinfo=timezone.utc)
    _, gran, n = _intel_window_spec("7d")
    out = _gap_fill_classified_timeline(rows=[], now=now, granularity=gran, bucket_count=n)
    assert len(out) == 7
    assert all(c == 0 for _, c in out)
    # Last bucket should be start of "today" UTC
    assert out[-1][0] == datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc)


def test_gap_fill_merges_duplicate_bucket_keys() -> None:
    now = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    b0 = datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc)
    out = _gap_fill_classified_timeline(
        rows=[(b0, 2), (b0, 3)],
        now=now,
        granularity="day",
        bucket_count=7,
    )
    by_day = {d: c for d, c in out}
    assert by_day[b0] == 5
