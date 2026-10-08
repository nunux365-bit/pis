"""Aggregates for :func:`~app.api.routes.email_automation` intelligence endpoints.

DEPRECATED: This module is slated for removal along with the Replies tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GmailIntelligence
from app.email_automation.collections_llm import COLLECTIONS_REPLY_KIND
from app.email_automation.pipeline.metrics import _bucket_key_utc

Window = Literal["24h", "7d", "30d"]
_WINDOWS: dict[Window, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

BucketGranularity = Literal["hour", "day"]


def _intel_window_spec(
    window: Window,
) -> tuple[timedelta, BucketGranularity, int]:
    """Aligns with pipeline metrics: 24 hourly buckets, 7 / 30 daily buckets."""

    if window == "24h":
        return timedelta(hours=24), "hour", 24
    if window == "7d":
        return timedelta(days=7), "day", 7
    if window == "30d":
        return timedelta(days=30), "day", 30
    raise ValueError(f"unsupported intelligence window: {window!r}")


def _gap_fill_classified_timeline(
    *,
    rows: list[tuple[datetime | None, int]],
    now: datetime,
    granularity: BucketGranularity,
    bucket_count: int,
) -> list[tuple[datetime, int]]:
    """Dense ``bucket_count`` points ending at the current bucket (UTC), zeros for gaps."""

    have: dict[datetime, int] = {}
    for bucket_, count in rows:
        if bucket_ is None:
            continue
        k = _bucket_key_utc(granularity, bucket_)
        have[k] = have.get(k, 0) + int(count)

    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    anchor = _bucket_key_utc(granularity, now)
    filled: list[tuple[datetime, int]] = []
    for i in range(bucket_count - 1, -1, -1):
        bucket = _bucket_key_utc(granularity, anchor - step * i)
        filled.append((bucket, have.get(bucket, 0)))
    return filled


def _window_start(window: Window, *, now: datetime) -> datetime:
    return now - _WINDOWS[window]


def intelligence_window_start(window: Window, *, now: datetime | None = None) -> datetime:
    """UTC lower bound for ``classified_at`` filters (same rolling windows as metrics)."""
    return _window_start(window, now=now or datetime.now(timezone.utc))


@dataclass(slots=True)
class IntelligenceTimelinePoint:
    bucket: datetime
    count: int


@dataclass(slots=True)
class IntelligencePriorPeriod:
    """Same-length window immediately before the primary ``[start, now)`` range (UTC)."""

    total_threads: int
    by_category: dict[str, int]
    by_confidence: dict[str, int]
    low_confidence_count: int


@dataclass(slots=True)
class IntelligenceMetrics:
    window: Window
    generated_at: datetime
    bucket_granularity: BucketGranularity
    total_threads: int
    by_category: dict[str, int]
    by_confidence: dict[str, int]
    low_confidence_count: int
    timeline: list[IntelligenceTimelinePoint]
    prior_period: IntelligencePriorPeriod


async def _rollup_intelligence_range(
    db: AsyncSession,
    *,
    kind: str,
    range_start: datetime,
    range_end_exclusive: datetime,
) -> tuple[int, dict[str, int], dict[str, int], int]:
    """Counts in ``[range_start, range_end_exclusive)`` for one intelligence kind."""

    base = and_(
        GmailIntelligence.kind == kind,
        GmailIntelligence.classified_at >= range_start,
        GmailIntelligence.classified_at < range_end_exclusive,
    )

    total_q = await db.execute(select(func.count()).select_from(GmailIntelligence).where(base))
    total_threads = int(total_q.scalar_one() or 0)

    cat_rows = (
        (
            await db.execute(
                select(GmailIntelligence.category, func.count())
                .where(base)
                .group_by(GmailIntelligence.category)
            )
        )
        .all()
    )
    by_category = {str(r[0]): int(r[1]) for r in cat_rows}

    conf_rows = (
        (
            await db.execute(
                select(GmailIntelligence.confidence, func.count())
                .where(base)
                .group_by(GmailIntelligence.confidence)
            )
        )
        .all()
    )
    by_confidence = {str(r[0]): int(r[1]) for r in conf_rows if r[0] is not None}

    low_q = await db.execute(
        select(func.count())
        .select_from(GmailIntelligence)
        .where(
            base,
            GmailIntelligence.confidence == "low",
        )
    )
    low_confidence_count = int(low_q.scalar_one() or 0)

    return total_threads, by_category, by_confidence, low_confidence_count


async def compute_intelligence_metrics(
    db: AsyncSession,
    *,
    window: Window = "30d",
    kind: str = COLLECTIONS_REPLY_KIND,
) -> IntelligenceMetrics:
    """DEPRECATED: Dashboard aggregates — indexed columns + gap-filled timeline."""

    now = datetime.now(timezone.utc)
    duration, granularity, bucket_count = _intel_window_spec(window)
    cur_start = now - duration
    prior_start = cur_start - duration

    total_threads, by_category, by_confidence, low_confidence_count = await _rollup_intelligence_range(
        db,
        kind=kind,
        range_start=cur_start,
        range_end_exclusive=now,
    )

    p_total, p_cat, p_conf, p_low = await _rollup_intelligence_range(
        db,
        kind=kind,
        range_start=prior_start,
        range_end_exclusive=cur_start,
    )
    prior_period = IntelligencePriorPeriod(
        total_threads=p_total,
        by_category=p_cat,
        by_confidence=p_conf,
        low_confidence_count=p_low,
    )

    ca_bucket = func.date_trunc(granularity, GmailIntelligence.classified_at)
    timeline_rows = (
        await db.execute(
            select(ca_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                GmailIntelligence.kind == kind,
                GmailIntelligence.classified_at >= cur_start,
                GmailIntelligence.classified_at < now,
            )
            .group_by("bucket")
        )
    ).all()
    dense = _gap_fill_classified_timeline(
        rows=[(r[0], int(r[1] or 0)) for r in timeline_rows],
        now=now,
        granularity=granularity,
        bucket_count=bucket_count,
    )
    timeline = [IntelligenceTimelinePoint(bucket=b, count=c) for b, c in dense]

    return IntelligenceMetrics(
        window=window,
        generated_at=now,
        bucket_granularity=granularity,
        total_threads=total_threads,
        by_category=by_category,
        by_confidence=by_confidence,
        low_confidence_count=low_confidence_count,
        timeline=timeline,
        prior_period=prior_period,
    )
