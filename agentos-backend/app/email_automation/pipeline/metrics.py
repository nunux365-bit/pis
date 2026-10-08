"""Dashboard metrics \u2014 server-side aggregations over messages + sends.

One public entry point: :func:`compute_metrics`. Everything the dashboard
needs comes back in a single response so the UI doesn't fan out N
requests per tile (each adding auth + round-trip overhead) and so
sensitive row-level data (recipient addresses, rendered HTML bodies)
never leaves the server.

Shape is deliberately dict-of-counts where feasible so introducing a
new FSM status later can't break an existing dashboard client \u2014 JSON
keys add, never rename.

Windowing:

  * ``24h`` \u2014 last 24 hours, hourly buckets (24 points).
  * ``7d``  \u2014 last 7 days,    daily buckets  (7 points).
  * ``30d`` \u2014 last 30 days,   daily buckets  (30 points).

The headline ``sends_by_status`` and trend ``sent``/``failed`` series use
the same event times (``sent_at``, ``updated_at``; other statuses
``created_at``) so the KPI row matches the bar chart. Message ``ingested``
buckets use ``coalesce(received_at, created_at)`` as the message status
rollups do.

Trend buckets are gap-filled in Python (a bucket with no rows still
appears with zeros) so the dashboard can plot directly without its own
calendar logic.

Why not a materialized view: the underlying tables are small (one row
per inbound email, one row per outbound), 30d of data is ~a few
thousand rows in the worst AR cycle, and the dashboard is a
low-frequency read. Aggregating on demand keeps writes simple and
removes the "refresh lagged 5 min" surprise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, EmailAutomationSend

from ._shared import now_utc
from .dispatch import SENDING_RECLAIM_AFTER_MINUTES


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


Window = Literal["24h", "7d", "14d", "30d"]
SUPPORTED_WINDOWS: tuple[Window, ...] = ("24h", "7d", "14d", "30d")


@dataclass(slots=True)
class TrendPoint:
    """One bucket on the pipeline-activity trend chart.

    ``bucket`` is the bucket start (UTC, truncated to hour or day depending
    on window). ``sent`` / ``failed`` / ``ingested`` are counts inside
    the bucket's range. ``ingested`` uses
    ``coalesce(received_at, created_at)`` so tile totals and bar sums match
    :obj:`Metrics.messages_by_status`.
    """

    bucket: datetime
    sent: int
    failed: int
    ingested: int


@dataclass(slots=True)
class Health:
    enabled: bool
    test_mode: bool
    last_message_received_at: datetime | None
    last_send_sent_at: datetime | None


@dataclass(slots=True)
class Attention:
    """Counters surfacing rows that need an ops decision."""

    sends_at_attempt_cap: int
    messages_processed_with_errors_open: int
    sends_stuck_sending: int


@dataclass(slots=True)
class Metrics:
    window: Window
    generated_at: datetime
    bucket_granularity: Literal["hour", "day"]
    health: Health
    messages_by_status: dict[str, int]
    sends_by_status: dict[str, int]
    sends_by_variant: dict[str, dict[str, int]]
    # Skipped sends in the window, grouped by the first review_reasons[].code
    # (aligns with the AR-triage banner). NULL/empty JSON → "unknown".
    sends_skipped_by_reason: dict[str, int] = field(default_factory=dict)
    # Failed sends in the window, grouped by a stable key from ``error`` JSONB:
    # prefer ``type`` (exception class), else short ``error`` (≤100 chars), else
    # ``long_error`` when the message is long, ``unknown`` when missing.
    sends_failed_by_reason: dict[str, int] = field(default_factory=dict)
    trend: list[TrendPoint] = field(default_factory=list)
    attention: Attention | None = None


# ---------------------------------------------------------------------------
# Window \u2192 (duration, granularity, bucket count)
# ---------------------------------------------------------------------------


def _window_spec(
    window: Window,
) -> tuple[timedelta, Literal["hour", "day"], int]:
    """Resolve the window to (duration, granularity, bucket count).

    Kept as a private helper so tests can pin the (24, 7, 30) bucket
    counts \u2014 if someone ever changes ``24h`` to mean "last 24 *hours*
    but 12 2-hour buckets" a test will catch the dashboard-breaking
    change.
    """

    if window == "24h":
        return timedelta(hours=24), "hour", 24
    if window == "7d":
        return timedelta(days=7), "day", 7
    if window == "14d":
        return timedelta(days=14), "day", 14
    if window == "30d":
        return timedelta(days=30), "day", 30
    raise ValueError(f"unsupported metrics window: {window!r}")


def _bucket_start(granularity: Literal["hour", "day"], when: datetime) -> datetime:
    """Python-side equivalent of Postgres ``date_trunc`` for gap-filling."""

    if granularity == "hour":
        return when.replace(minute=0, second=0, microsecond=0)
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def _send_in_metrics_window(cutoff: datetime) -> object:
    """SQLAlchemy bool for :class:`EmailAutomationSend` rows in the window (see :func:`_load_trend`).

    * **sent** \u2014 ``sent_at`` (outbound; same as green series on the trend chart)
    * **failed** \u2014 ``updated_at`` (no dedicated ``failed_at``; same as red)
    * **other** statuses (skipped, sending, \u2026) \u2014 ``created_at`` (row entered that state
      in the window, consistent with the skip-reason query)
    """

    st = EmailAutomationSend.status
    return or_(
        and_(
            st == "sent",
            EmailAutomationSend.sent_at.isnot(None),
            EmailAutomationSend.sent_at >= cutoff,
        ),
        and_(
            st == "failed",
            EmailAutomationSend.updated_at >= cutoff,
        ),
        and_(
            st.notin_(("sent", "failed")),
            EmailAutomationSend.created_at >= cutoff,
        ),
    )


def _bucket_key_utc(granularity: Literal["hour", "day"], when: datetime) -> datetime:
    """Canonical bucket start in UTC for dict lookup and gap-fill.

    Postgres ``date_trunc`` on ``timestamptz`` and asyncpg can yield datetimes
    whose ``tzinfo`` differs from Python's ``datetime.now(timezone.utc)``
    (or rarely, naive values). If gap-fill keys and DB keys don't match
    exactly, the chart becomes all zeros while headline totals are non-zero.
    """

    if when.tzinfo is None:
        w = when.replace(tzinfo=timezone.utc)
    else:
        w = when.astimezone(timezone.utc)
    return _bucket_start(granularity, w)


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------


async def compute_metrics(db: AsyncSession, *, window: Window = "24h") -> Metrics:
    """Aggregate dashboard metrics for the requested window.

    Issues a handful of grouped queries (one per tile family); the sum
    of rows returned is proportional to the number of distinct
    ``(variant, status)`` pairs plus bucket count \u2014 tiny even at 30d.
    """

    if window not in SUPPORTED_WINDOWS:
        raise ValueError(f"unsupported metrics window: {window!r}")

    duration, granularity, bucket_count = _window_spec(window)
    now = now_utc()
    cutoff = now - duration

    # Messages-by-status (window). Count as Integer because Postgres
    # returns BIGINT from COUNT(*) and the Python int round-trip via
    # asyncpg is fine either way \u2014 the cast pins it for the tests.
    # Window on message rows: prefer mailbox arrival time (matches trend
    # ``ingested``), fall back to row creation when ``received_at`` is unset.
    msg_window_time = func.coalesce(
        EmailAutomationMessage.received_at,
        EmailAutomationMessage.created_at,
    )
    msg_status_rows = (
        await db.execute(
            select(
                EmailAutomationMessage.status,
                cast(func.count(), Integer),
            )
            .where(msg_window_time >= cutoff)
            .group_by(EmailAutomationMessage.status)
        )
    ).all()
    messages_by_status = {row[0]: int(row[1]) for row in msg_status_rows}

    # Sends-by-(variant, status) \u2014 one query backs two tiles:
    # ``sends_by_status`` (sum across variants) and ``sends_by_variant``
    # (per-variant breakdown). Halves the round-trip count.
    send_rows = (
        await db.execute(
            select(
                EmailAutomationSend.variant,
                EmailAutomationSend.status,
                cast(func.count(), Integer),
            )
            .where(_send_in_metrics_window(cutoff))
            .group_by(EmailAutomationSend.variant, EmailAutomationSend.status)
        )
    ).all()
    sends_by_status: dict[str, int] = {}
    sends_by_variant: dict[str, dict[str, int]] = {}
    for variant, status_, count in send_rows:
        sends_by_status[status_] = sends_by_status.get(status_, 0) + int(count)
        sends_by_variant.setdefault(variant, {})[status_] = int(count)

    # Skipped rows by primary reason: first element of ``review_reasons``,
    # ``code`` field (same ordering as the pipeline uses for the skip banner).
    # ``review_reasons->0->>'code'`` with NULL/empty \u2192 "unknown" bucket.
    skip_reason_key = func.coalesce(
        EmailAutomationSend.review_reasons.op("->")(0).op("->>")("code"),
        literal("unknown", type_=String()),
    )
    skip_reason_rows = (
        await db.execute(
            select(
                cast(skip_reason_key, String).label("reason_key"),
                cast(func.count(), Integer),
            )
            .where(
                EmailAutomationSend.status == "skipped",
                EmailAutomationSend.created_at >= cutoff,
            )
            .group_by(cast(skip_reason_key, String))
        )
    ).all()
    sends_skipped_by_reason: dict[str, int] = {}
    for key, count in skip_reason_rows:
        if key is None or (isinstance(key, str) and not key.strip()):
            bucket = "unknown"
        else:
            bucket = str(key)
        sends_skipped_by_reason[bucket] = sends_skipped_by_reason.get(bucket, 0) + int(
            count
        )

    err_t = EmailAutomationSend.error.op("->>")("type")
    err_e = EmailAutomationSend.error.op("->>")("error")
    tr_e = func.trim(err_e)
    # Prefer the persisted ``error`` string (``str(exc)`` from dispatch) over
    # Python ``type`` so ops see *why* (e.g. empty TO) instead of a generic
    # "ValueError". Collapse the two known no-recipient guards to one bucket.
    no_wire_to = and_(
        err_e.isnot(None),
        func.coalesce(err_t, literal("")) == literal("ValueError"),
        or_(
            tr_e.like(literal("sender: no recipient%")),
            tr_e.like(literal("send_email:%must contain at least one address%")),
        ),
    )
    failed_reason_key = func.coalesce(
        case(
            (no_wire_to, literal("no_wire_recipient", type_=String())),
            (and_(err_e.isnot(None), func.length(tr_e) <= 100), tr_e),
            (err_e.isnot(None), literal("long_error", type_=String())),
        ),
        func.nullif(func.trim(err_t), literal("")),
        literal("unknown", type_=String()),
    )
    failed_reason_rows = (
        await db.execute(
            select(
                cast(failed_reason_key, String).label("reason_key"),
                cast(func.count(), Integer),
            )
            .where(
                EmailAutomationSend.status == "failed",
                EmailAutomationSend.updated_at >= cutoff,
            )
            .group_by(cast(failed_reason_key, String))
        )
    ).all()
    sends_failed_by_reason: dict[str, int] = {}
    for key, count in failed_reason_rows:
        if key is None or (isinstance(key, str) and not key.strip()):
            bucket = "unknown"
        else:
            bucket = str(key)
        sends_failed_by_reason[bucket] = sends_failed_by_reason.get(bucket, 0) + int(
            count
        )

    # Trend \u2014 three bucketed series merged into one timeline. We use
    # ``date_trunc`` in SQL (fast, timezone-aware in Postgres); Python
    # then gap-fills so the dashboard receives a complete series.
    trend = await _load_trend(db, cutoff=cutoff, granularity=granularity)

    # Health timestamps \u2014 MAX scans are cheap on small tables and a
    # stale watermark is the single strongest signal that the scheduler
    # has stopped.
    last_msg = (
        await db.execute(select(func.max(EmailAutomationMessage.received_at)))
    ).scalar_one_or_none()
    last_send = (
        await db.execute(select(func.max(EmailAutomationSend.sent_at)))
    ).scalar_one_or_none()

    # Attention counters \u2014 all three map to a numbered runbook paragraph
    # so a dashboard alert can link directly to the triage steps.
    cap = int(settings.email_automation_send_max_attempts or 5)
    stuck_cutoff = now - timedelta(minutes=SENDING_RECLAIM_AFTER_MINUTES)

    at_cap = (
        await db.execute(
            select(cast(func.count(), Integer)).where(
                EmailAutomationSend.status == "failed",
                EmailAutomationSend.send_attempt_count >= cap,
            )
        )
    ).scalar_one()
    pwe_open = (
        await db.execute(
            select(cast(func.count(), Integer)).where(
                EmailAutomationMessage.status == "processed_with_errors",
            )
        )
    ).scalar_one()
    stuck_sending = (
        await db.execute(
            select(cast(func.count(), Integer)).where(
                EmailAutomationSend.status == "sending",
                EmailAutomationSend.updated_at < stuck_cutoff,
            )
        )
    ).scalar_one()

    # Gap-fill the trend: build a zero-count series spanning
    # [cutoff, now], then overlay whatever the DB returned. Protects
    # dashboards from empty-hour gaps turning into visual holes.
    filled_trend = _gap_fill_trend(
        points=trend,
        cutoff=cutoff,
        now=now,
        granularity=granularity,
        bucket_count=bucket_count,
    )

    return Metrics(
        window=window,
        generated_at=now,
        bucket_granularity=granularity,
        health=Health(
            enabled=bool(settings.email_automation_enabled),
            test_mode=bool(settings.email_automation_test_mode),
            last_message_received_at=last_msg,
            last_send_sent_at=last_send,
        ),
        messages_by_status=messages_by_status,
        sends_by_status=sends_by_status,
        sends_by_variant=sends_by_variant,
        sends_skipped_by_reason=sends_skipped_by_reason,
        sends_failed_by_reason=sends_failed_by_reason,
        trend=filled_trend,
        attention=Attention(
            sends_at_attempt_cap=int(at_cap or 0),
            messages_processed_with_errors_open=int(pwe_open or 0),
            sends_stuck_sending=int(stuck_sending or 0),
        ),
    )


async def compute_trend_only(
    db: AsyncSession, *, window: Window, offset_windows: int = 0
) -> list[TrendPoint]:
    """Return only the gap-filled trend for ``window`` ending ``offset_windows`` windows back.

    ``offset_windows=0`` gives the current window (same series as ``compute_metrics``).
    ``offset_windows=1`` gives the immediately prior window (the "ghost line" the UI
    overlays for period-over-period comparison). Cheap: runs just the three trend
    queries, no attention or skip-reason aggregation.
    """

    if window not in SUPPORTED_WINDOWS:
        raise ValueError(f"unsupported metrics window: {window!r}")
    if offset_windows < 0:
        raise ValueError("offset_windows must be >= 0")
    duration, granularity, bucket_count = _window_spec(window)
    now = now_utc()
    end = now - duration * offset_windows
    cutoff = end - duration

    points = await _load_trend_range(
        db, start=cutoff, end=end, granularity=granularity
    )
    return _gap_fill_trend_window(
        points=points,
        end=end,
        granularity=granularity,
        bucket_count=bucket_count,
    )


async def message_and_send_status_counts(
    db: AsyncSession, *, window: Window
) -> dict[str, dict[str, int]]:
    """Message + send status rollups for ``window`` with no trend or attention queries.

    Used by the home dashboard to add a second window (e.g. 30d) without paying for
    ``compute_metrics`` twice (trend, skip reasons, health, attention are omitted).
    """

    if window not in SUPPORTED_WINDOWS:
        raise ValueError(f"unsupported metrics window: {window!r}")
    duration, _granularity, _bucket_count = _window_spec(window)
    now = now_utc()
    cutoff = now - duration

    msg_window_time = func.coalesce(
        EmailAutomationMessage.received_at,
        EmailAutomationMessage.created_at,
    )
    msg_status_rows = (
        await db.execute(
            select(
                EmailAutomationMessage.status,
                cast(func.count(), Integer),
            )
            .where(msg_window_time >= cutoff)
            .group_by(EmailAutomationMessage.status)
        )
    ).all()
    messages_by_status = {row[0]: int(row[1]) for row in msg_status_rows}

    send_rows = (
        await db.execute(
            select(
                EmailAutomationSend.variant,
                EmailAutomationSend.status,
                cast(func.count(), Integer),
            )
            .where(_send_in_metrics_window(cutoff))
            .group_by(EmailAutomationSend.variant, EmailAutomationSend.status)
        )
    ).all()
    sends_by_status: dict[str, int] = {}
    for _variant, status_, count in send_rows:
        sends_by_status[status_] = sends_by_status.get(status_, 0) + int(count)

    return {
        "messages_by_status": messages_by_status,
        "sends_by_status": sends_by_status,
    }


# ---------------------------------------------------------------------------
# Trend series
# ---------------------------------------------------------------------------


async def _load_trend(
    db: AsyncSession,
    *,
    cutoff: datetime,
    granularity: Literal["hour", "day"],
) -> list[TrendPoint]:
    """Fetch the three trend series and merge into dense ``TrendPoint`` list.

    Separate queries (rather than one giant UNION) because each uses a
    different time column \u2014 ingested buckets use the same
    ``coalesce(received_at, created_at)`` as :func:`compute_metrics` message
    rollups, sent by ``sent_at`` (wire time), and failed by ``updated_at``
    (we don't have a ``failed_at`` column and adding one is not worth the
    migration for a chart).
    """

    send_bucket = func.date_trunc(granularity, EmailAutomationSend.sent_at)
    sent_rows = (
        await db.execute(
            select(send_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                EmailAutomationSend.status == "sent",
                EmailAutomationSend.sent_at.isnot(None),
                EmailAutomationSend.sent_at >= cutoff,
            )
            .group_by("bucket")
        )
    ).all()

    fail_bucket = func.date_trunc(granularity, EmailAutomationSend.updated_at)
    failed_rows = (
        await db.execute(
            select(fail_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                EmailAutomationSend.status == "failed",
                EmailAutomationSend.updated_at >= cutoff,
            )
            .group_by("bucket")
        )
    ).all()

    msg_ingest_time = func.coalesce(
        EmailAutomationMessage.received_at,
        EmailAutomationMessage.created_at,
    )
    ingest_bucket = func.date_trunc(granularity, msg_ingest_time)
    ingested_rows = (
        await db.execute(
            select(ingest_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                msg_ingest_time.isnot(None),
                msg_ingest_time >= cutoff,
            )
            .group_by("bucket")
        )
    ).all()

    merged: dict[datetime, dict[str, int]] = {}
    for bucket_, count in sent_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["sent"] = int(count)
    for bucket_, count in failed_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["failed"] = int(count)
    for bucket_, count in ingested_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["ingested"] = int(count)

    return [
        TrendPoint(bucket=b, sent=v["sent"], failed=v["failed"], ingested=v["ingested"])
        for b, v in sorted(merged.items(), key=lambda kv: kv[0])
    ]


async def _load_trend_range(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    granularity: Literal["hour", "day"],
) -> list[TrendPoint]:
    """Same three series as :func:`_load_trend` but bounded by an arbitrary ``[start, end)``.

    Used by :func:`compute_trend_only` to fetch the *prior* window for ghost-line overlays
    without the rest of the dashboard payload.
    """

    send_bucket = func.date_trunc(granularity, EmailAutomationSend.sent_at)
    sent_rows = (
        await db.execute(
            select(send_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                EmailAutomationSend.status == "sent",
                EmailAutomationSend.sent_at.isnot(None),
                EmailAutomationSend.sent_at >= start,
                EmailAutomationSend.sent_at < end,
            )
            .group_by("bucket")
        )
    ).all()

    fail_bucket = func.date_trunc(granularity, EmailAutomationSend.updated_at)
    failed_rows = (
        await db.execute(
            select(fail_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                EmailAutomationSend.status == "failed",
                EmailAutomationSend.updated_at >= start,
                EmailAutomationSend.updated_at < end,
            )
            .group_by("bucket")
        )
    ).all()

    msg_ingest_time = func.coalesce(
        EmailAutomationMessage.received_at,
        EmailAutomationMessage.created_at,
    )
    ingest_bucket = func.date_trunc(granularity, msg_ingest_time)
    ingested_rows = (
        await db.execute(
            select(ingest_bucket.label("bucket"), cast(func.count(), Integer))
            .where(
                msg_ingest_time.isnot(None),
                msg_ingest_time >= start,
                msg_ingest_time < end,
            )
            .group_by("bucket")
        )
    ).all()

    merged: dict[datetime, dict[str, int]] = {}
    for bucket_, count in sent_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["sent"] = int(count)
    for bucket_, count in failed_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["failed"] = int(count)
    for bucket_, count in ingested_rows:
        b = _bucket_key_utc(granularity, bucket_)
        merged.setdefault(b, {"sent": 0, "failed": 0, "ingested": 0})["ingested"] = int(count)

    return [
        TrendPoint(bucket=b, sent=v["sent"], failed=v["failed"], ingested=v["ingested"])
        for b, v in sorted(merged.items(), key=lambda kv: kv[0])
    ]


def _gap_fill_trend_window(
    *,
    points: list[TrendPoint],
    end: datetime,
    granularity: Literal["hour", "day"],
    bucket_count: int,
) -> list[TrendPoint]:
    """Dense ``bucket_count`` series ending at ``end`` (exclusive).

    Variant of :func:`_gap_fill_trend` that supports ghost windows anchored in
    the past (``end = now - duration`` etc.).
    """

    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    anchor = _bucket_key_utc(granularity, end - step)

    have: dict[datetime, TrendPoint] = {}
    for p in points:
        k = _bucket_key_utc(granularity, p.bucket)
        prev = have.get(k)
        if prev is None:
            have[k] = p
        else:
            have[k] = TrendPoint(
                bucket=k,
                sent=prev.sent + p.sent,
                failed=prev.failed + p.failed,
                ingested=prev.ingested + p.ingested,
            )

    filled: list[TrendPoint] = []
    for i in range(bucket_count - 1, -1, -1):
        bucket = _bucket_key_utc(granularity, anchor - step * i)
        existing = have.get(bucket)
        if existing is not None:
            filled.append(
                TrendPoint(
                    bucket=bucket,
                    sent=existing.sent,
                    failed=existing.failed,
                    ingested=existing.ingested,
                )
            )
        else:
            filled.append(TrendPoint(bucket=bucket, sent=0, failed=0, ingested=0))
    return filled


def _gap_fill_trend(
    *,
    points: list[TrendPoint],
    cutoff: datetime,
    now: datetime,
    granularity: Literal["hour", "day"],
    bucket_count: int,
) -> list[TrendPoint]:
    """Return a dense series of ``bucket_count`` points ending at ``now``.

    Missing buckets are filled with zeros so the dashboard never sees
    timeline holes. ``bucket_count`` is authoritative (24 / 7 / 30);
    ``cutoff`` is used only as a sanity floor.
    """

    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    anchor = _bucket_key_utc(granularity, now)

    have: dict[datetime, TrendPoint] = {}
    for p in points:
        k = _bucket_key_utc(granularity, p.bucket)
        prev = have.get(k)
        if prev is None:
            have[k] = p
        else:
            have[k] = TrendPoint(
                bucket=k,
                sent=prev.sent + p.sent,
                failed=prev.failed + p.failed,
                ingested=prev.ingested + p.ingested,
            )

    filled: list[TrendPoint] = []
    for i in range(bucket_count - 1, -1, -1):
        bucket = _bucket_key_utc(granularity, anchor - step * i)
        if bucket < cutoff - step:
            continue  # shouldn't happen, but keep bounded
        existing = have.get(bucket)
        if existing is not None:
            filled.append(
                TrendPoint(
                    bucket=bucket,
                    sent=existing.sent,
                    failed=existing.failed,
                    ingested=existing.ingested,
                )
            )
        else:
            filled.append(
                TrendPoint(bucket=bucket, sent=0, failed=0, ingested=0)
            )
    return filled


__all__ = [
    "Window",
    "SUPPORTED_WINDOWS",
    "TrendPoint",
    "Health",
    "Attention",
    "Metrics",
    "compute_metrics",
    "compute_trend_only",
    "message_and_send_status_counts",
]
