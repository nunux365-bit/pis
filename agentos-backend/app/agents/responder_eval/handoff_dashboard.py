"""Hand-off daily dashboard aggregates (bucket / sub-bucket)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, NamedTuple

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.responder_eval.cohorts import HUMAN_PRESENT
from app.agents.responder_eval.constants import EVAL_VERSION
from app.agents.responder_eval.handoff_taxonomy import (
    BUCKET_ORDER,
    HANDOFF_TAXONOMY,
    bucket_label,
    sub_bucket_label,
)
from app.agents.responder_eval.models import ResponderEvalRun

_DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_MONTH_NAMES = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
_MOVER_NOISE_MIN = 5
_MOVER_LIMIT = 5


class _ReferenceWindow(NamedTuple):
    """D-1-centric reference dates shared by the matrix and the chat-ids lookup.

    A single definition of "today/D-1/D-2/last-7-days/MTD" so the two can't drift apart.
    """

    today: date
    d1: date
    d2: date
    last7_days: list[date]
    mtd_start: date
    mtd_days: list[date]


def _reference_window() -> _ReferenceWindow:
    today = datetime.now(UTC).date()
    d1 = today - timedelta(days=1)
    d2 = today - timedelta(days=2)
    last7_days = [d1 - timedelta(days=offset) for offset in range(6, -1, -1)]
    mtd_start = d1.replace(day=1)
    mtd_days = [mtd_start + timedelta(days=i) for i in range((d1 - mtd_start).days + 1)]
    return _ReferenceWindow(
        today=today, d1=d1, d2=d2, last7_days=last7_days, mtd_start=mtd_start, mtd_days=mtd_days
    )


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _utc_day(value: datetime | date) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(UTC).date()
    return value


def day_header(day: date) -> str:
    return f"{day.day} {_MONTH_NAMES[day.month - 1]} {_DAY_NAMES[day.weekday()]}"


def _pct(n: int, den: int, *, digits: int = 1) -> float:
    if den <= 0 or n <= 0:
        return 0.0
    return round(100.0 * n / den, digits)


def _day_rows(
    counts: dict[tuple[str, str], int],
    *,
    total: int,
) -> list[dict[str, Any]]:
    bucket_totals: dict[str, int] = defaultdict(int)
    sub_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (bucket, sub), n in counts.items():
        bucket_totals[bucket] += n
        sub_counts[bucket][sub] += n

    buckets: list[dict[str, Any]] = []
    for bucket in BUCKET_ORDER:
        b_total = bucket_totals.get(bucket, 0)
        if b_total == 0 and not any(sub_counts.get(bucket, {}).values()):
            continue
        subs_out: list[dict[str, Any]] = []
        shown: set[str] = set()
        for sub in HANDOFF_TAXONOMY.get(bucket, {}):
            n = sub_counts[bucket].get(sub, 0)
            if n == 0:
                continue
            shown.add(sub)
            subs_out.append(
                {
                    "id": sub,
                    "label": sub_bucket_label(sub),
                    "count": n,
                    "pct": _pct(n, total),
                }
            )
        for sub, n in sorted(sub_counts[bucket].items()):
            if sub in shown or n <= 0:
                continue
            subs_out.append(
                {
                    "id": sub,
                    "label": sub_bucket_label(sub),
                    "count": n,
                    "pct": _pct(n, total),
                }
            )
        buckets.append(
            {
                "bucket": bucket,
                "label": bucket_label(bucket),
                "count": b_total,
                "pct": _pct(b_total, total),
                "sub_buckets": subs_out,
            }
        )
    extras = [b for b in bucket_totals if b not in HANDOFF_TAXONOMY]
    for bucket in extras:
        b_total = bucket_totals[bucket]
        if b_total <= 0:
            continue
        buckets.append(
            {
                "bucket": bucket,
                "label": bucket_label(bucket),
                "count": b_total,
                "pct": _pct(b_total, total),
                "sub_buckets": [
                    {
                        "id": sub,
                        "label": sub_bucket_label(sub),
                        "count": n,
                        "pct": _pct(n, total),
                    }
                    for sub, n in sorted(sub_counts[bucket].items())
                    if n > 0
                ],
            }
        )
    return buckets


def build_movers(
    *,
    d1_counts: dict[tuple[str, str], int],
    last7_counts: dict[tuple[str, str], int],
    noise_min: int = _MOVER_NOISE_MIN,
    limit: int = _MOVER_LIMIT,
) -> dict[str, list[dict[str, Any]]]:
    """Top sub-buckets by D-1 vs last-7-day average. Under `noise_min` D-1 hand-offs are dropped."""
    keys = set(d1_counts) | set(last7_counts)
    rising: list[dict[str, Any]] = []
    falling: list[dict[str, Any]] = []
    for bucket, sub in keys:
        d1 = d1_counts.get((bucket, sub), 0)
        if d1 < noise_min:
            continue
        avg = last7_counts.get((bucket, sub), 0) / 7.0
        delta = d1 - avg
        rounded = int(round(delta))
        if rounded == 0:
            continue
        delta_pct = round(100.0 * delta / avg) if avg > 0 else None
        rec = {
            "id": sub,
            "label": sub_bucket_label(sub),
            "bucket": bucket,
            "bucket_label": bucket_label(bucket),
            "count": d1,
            "delta": rounded,
            "delta_pct": delta_pct,
        }
        if rounded > 0:
            rising.append(rec)
        else:
            falling.append(rec)
    rising.sort(key=lambda r: (r["delta_pct"] is not None, r["delta_pct"] or 0, r["delta"]), reverse=True)
    falling.sort(key=lambda r: (r["delta_pct"] is None, r["delta_pct"] if r["delta_pct"] is not None else 0, r["delta"]))
    return {"rising": rising[:limit], "falling": falling[:limit]}


def _series_for(
    counts_by_day: dict[date, dict[tuple[str, str], int]],
    days: list[date],
    key: tuple[str, str] | None,
    bucket: str | None,
) -> list[int]:
    out: list[int] = []
    for day in days:
        day_map = counts_by_day.get(day, {})
        if key is not None:
            out.append(day_map.get(key, 0))
        elif bucket is not None:
            out.append(sum(n for (b, _), n in day_map.items() if b == bucket))
        else:
            out.append(sum(day_map.values()))
    return out


def _mtd_total(
    counts_by_day: dict[date, dict[tuple[str, str], int]],
    mtd_days: list[date],
    key: tuple[str, str] | None,
    bucket: str | None,
) -> int:
    total = 0
    for day in mtd_days:
        day_map = counts_by_day.get(day, {})
        if key is not None:
            total += day_map.get(key, 0)
        elif bucket is not None:
            total += sum(n for (b, _), n in day_map.items() if b == bucket)
        else:
            total += sum(day_map.values())
    return total


def _matrix_row(
    *,
    last7: list[int],
    mtd: int,
    chats_last7: list[int],
    chats_mtd: int,
) -> dict[str, Any]:
    last7_total = sum(last7)
    avg = last7_total / 7.0
    d1 = last7[-1] if last7 else 0
    delta = int(round(d1 - avg))
    last7_chats = sum(chats_last7)
    return {
        "days": last7,
        "pct_days": [_pct(n, den) for n, den in zip(last7, chats_last7, strict=True)],
        "last_7": last7_total,
        "pct_last_7": _pct(last7_total, last7_chats),
        "mtd": mtd,
        "pct_mtd": _pct(mtd, chats_mtd),
        "delta": delta,
    }


_ChatIdsScope = Literal["day", "last_7", "mtd"]


def _chat_ids_window(*, scope: _ChatIdsScope, day: date | None) -> tuple[date, date]:
    """Inclusive (since, until) day range for a chat-ids lookup, matching the matrix's own math."""
    if scope == "day":
        if day is None:
            raise ValueError("day is required when scope='day'")
        return day, day
    if scope == "mtd":
        ref = _reference_window()
        return ref.mtd_start, ref.d1
    if scope == "last_7":
        ref = _reference_window()
        return ref.last7_days[0], ref.d1
    raise ValueError(f"unrecognized scope: {scope!r}")


async def eval_handoff_chat_ids(
    db: AsyncSession,
    *,
    bucket: str,
    sub_bucket: str | None = None,
    scope: _ChatIdsScope = "last_7",
    day: date | None = None,
) -> dict[str, Any]:
    """Chat ids behind a bucket (or sub-bucket) for a single day, the last 7 days, or MTD."""
    since_day, until_day = _chat_ids_window(scope=scope, day=day)
    since, _ = _utc_day_bounds(since_day)
    _, until = _utc_day_bounds(until_day)

    conditions = [
        ResponderEvalRun.eval_version == EVAL_VERSION,
        HUMAN_PRESENT,
        ResponderEvalRun.handoff_bucket == bucket,
        ResponderEvalRun.created_at >= since,
        ResponderEvalRun.created_at < until,
    ]
    if sub_bucket:
        conditions.append(ResponderEvalRun.handoff_sub_bucket == sub_bucket)
    else:
        conditions.append(ResponderEvalRun.handoff_sub_bucket.isnot(None))

    rows = (
        await db.execute(
            select(ResponderEvalRun.chat_id)
            .where(*conditions)
            .order_by(ResponderEvalRun.created_at.desc())
        )
    ).all()
    chat_ids = [r.chat_id for r in rows]

    return {
        "since": since_day.isoformat(),
        "until": until_day.isoformat(),
        "total": len(chat_ids),
        "chat_ids": chat_ids,
    }


async def eval_handoff_analysis(db: AsyncSession) -> dict[str, Any]:
    """D-1-centric hand-off dashboard: KPIs, movers, last-7 + MTD matrix."""
    today, d1, d2, last7_days, mtd_start, mtd_days = _reference_window()
    query_start = min(mtd_start, last7_days[0])
    since, _ = _utc_day_bounds(query_start)
    _, until = _utc_day_bounds(d1)

    utc_day = cast(func.timezone("UTC", ResponderEvalRun.created_at), Date)

    handoff_rows = (
        await db.execute(
            select(
                utc_day.label("day"),
                ResponderEvalRun.handoff_bucket,
                ResponderEvalRun.handoff_sub_bucket,
                func.count().label("n"),
            )
            .where(
                ResponderEvalRun.eval_version == EVAL_VERSION,
                HUMAN_PRESENT,
                ResponderEvalRun.handoff_bucket.isnot(None),
                ResponderEvalRun.handoff_sub_bucket.isnot(None),
                ResponderEvalRun.created_at >= since,
                ResponderEvalRun.created_at < until,
            )
            .group_by(utc_day, ResponderEvalRun.handoff_bucket, ResponderEvalRun.handoff_sub_bucket)
        )
    ).all()

    landed_rows = (
        await db.execute(
            select(utc_day.label("day"), func.count().label("n"))
            .where(
                ResponderEvalRun.eval_version == EVAL_VERSION,
                ResponderEvalRun.created_at >= since,
                ResponderEvalRun.created_at < until,
            )
            .group_by(utc_day)
        )
    ).all()

    counts_by_day: dict[date, dict[tuple[str, str], int]] = defaultdict(dict)
    for row in handoff_rows:
        if not row.handoff_bucket or not row.handoff_sub_bucket or row.day is None:
            continue
        day = _utc_day(row.day)
        counts_by_day[day][(str(row.handoff_bucket), str(row.handoff_sub_bucket))] = int(row.n or 0)

    chats_by_day: dict[date, int] = {}
    for row in landed_rows:
        if row.day is None:
            continue
        chats_by_day[_utc_day(row.day)] = int(row.n or 0)

    chats_last7 = [chats_by_day.get(day, 0) for day in last7_days]
    chats_mtd = sum(chats_by_day.get(day, 0) for day in mtd_days)
    d1_handoffs = sum(counts_by_day.get(d1, {}).values())
    d2_handoffs = sum(counts_by_day.get(d2, {}).values())
    last7_handoffs = sum(sum(counts_by_day.get(day, {}).values()) for day in last7_days)
    avg_7d = last7_handoffs / 7.0
    chats_d1 = chats_by_day.get(d1, 0)
    chats_d2 = chats_by_day.get(d2, 0)
    rate_d1 = _pct(d1_handoffs, chats_d1)
    rate_d2 = _pct(d2_handoffs, chats_d2)
    vs_avg_pct = round(100.0 * (d1_handoffs - avg_7d) / avg_7d, 1) if avg_7d > 0 else None
    rate_vs_d2_pp = round(rate_d1 - rate_d2, 2) if chats_d1 and chats_d2 else None

    d1_bucket_totals: dict[str, int] = defaultdict(int)
    for (bucket, _sub), n in counts_by_day.get(d1, {}).items():
        d1_bucket_totals[bucket] += n
    largest_id = max(d1_bucket_totals, key=d1_bucket_totals.get) if d1_bucket_totals else None
    largest = None
    if largest_id:
        n = d1_bucket_totals[largest_id]
        largest = {
            "id": largest_id,
            "label": bucket_label(largest_id),
            "count": n,
            "pct": _pct(n, d1_handoffs, digits=0) if d1_handoffs else 0.0,
        }

    kpis = {
        "handoffs_d1": d1_handoffs,
        "handoffs_vs_7d_avg_pct": vs_avg_pct,
        "handoff_rate_d1": rate_d1,
        "handoff_rate_vs_d2_pp": rate_vs_d2_pp,
        "chats_landed_d1": chats_d1,
        "largest_bucket": largest,
    }

    last7_counts: dict[tuple[str, str], int] = defaultdict(int)
    for day in last7_days:
        for key, n in counts_by_day.get(day, {}).items():
            last7_counts[key] += n
    movers = build_movers(d1_counts=counts_by_day.get(d1, {}), last7_counts=dict(last7_counts))

    day_headers = [
        {
            "date": day.isoformat(),
            "header": day_header(day),
            "handoffs": sum(counts_by_day.get(day, {}).values()),
            "chats_landed": chats_by_day.get(day, 0),
        }
        for day in last7_days
    ]

    present_keys: set[tuple[str, str]] = set()
    for day_map in counts_by_day.values():
        present_keys.update(day_map)

    matrix: list[dict[str, Any]] = []
    for bucket in BUCKET_ORDER:
        sub_ids = list(HANDOFF_TAXONOMY.get(bucket, {}))
        extras = sorted(sub for b, sub in present_keys if b == bucket and sub not in HANDOFF_TAXONOMY.get(bucket, {}))
        sub_ids.extend(extras)
        sub_rows: list[dict[str, Any]] = []
        for sub in sub_ids:
            key = (bucket, sub)
            days = _series_for(counts_by_day, last7_days, key, None)
            mtd = _mtd_total(counts_by_day, mtd_days, key, None)
            if sum(days) == 0 and mtd == 0:
                continue
            sub_rows.append(
                {
                    "id": sub,
                    "label": sub_bucket_label(sub),
                    **_matrix_row(last7=days, mtd=mtd, chats_last7=chats_last7, chats_mtd=chats_mtd),
                }
            )
        bucket_days = _series_for(counts_by_day, last7_days, None, bucket)
        bucket_mtd = _mtd_total(counts_by_day, mtd_days, None, bucket)
        if sum(bucket_days) == 0 and bucket_mtd == 0:
            continue
        matrix.append(
            {
                "id": bucket,
                "label": bucket_label(bucket),
                "sub_count": len(sub_rows),
                **_matrix_row(
                    last7=bucket_days, mtd=bucket_mtd, chats_last7=chats_last7, chats_mtd=chats_mtd
                ),
                "sub_buckets": sub_rows,
            }
        )
    extras_buckets = sorted({b for b, _ in present_keys if b not in HANDOFF_TAXONOMY})
    for bucket in extras_buckets:
        sub_ids = sorted({sub for b, sub in present_keys if b == bucket})
        sub_rows = []
        for sub in sub_ids:
            key = (bucket, sub)
            days = _series_for(counts_by_day, last7_days, key, None)
            mtd = _mtd_total(counts_by_day, mtd_days, key, None)
            if sum(days) == 0 and mtd == 0:
                continue
            sub_rows.append(
                {
                    "id": sub,
                    "label": sub_bucket_label(sub),
                    **_matrix_row(last7=days, mtd=mtd, chats_last7=chats_last7, chats_mtd=chats_mtd),
                }
            )
        bucket_days = _series_for(counts_by_day, last7_days, None, bucket)
        bucket_mtd = _mtd_total(counts_by_day, mtd_days, None, bucket)
        if sum(bucket_days) == 0 and bucket_mtd == 0:
            continue
        matrix.append(
            {
                "id": bucket,
                "label": bucket_label(bucket),
                "sub_count": len(sub_rows),
                **_matrix_row(
                    last7=bucket_days, mtd=bucket_mtd, chats_last7=chats_last7, chats_mtd=chats_mtd
                ),
                "sub_buckets": sub_rows,
            }
        )

    d1_counts = counts_by_day.get(d1, {})
    d2_counts = counts_by_day.get(d2, {})
    return {
        "reference_date": today.isoformat(),
        "focus_date": d1.isoformat(),
        "kpis": kpis,
        "movers": movers,
        "day_headers": day_headers,
        "buckets": matrix,
        "volume": {"D-2": d2_handoffs, "D-1": d1_handoffs},
        "days": [
            {
                "label": "D-2",
                "date": d2.isoformat(),
                "total": d2_handoffs,
                "buckets": _day_rows(d2_counts, total=d2_handoffs),
            },
            {
                "label": "D-1",
                "date": d1.isoformat(),
                "total": d1_handoffs,
                "buckets": _day_rows(d1_counts, total=d1_handoffs),
            },
        ],
        "bucket_order": list(BUCKET_ORDER),
        "day_labels": [h["header"] for h in day_headers],
    }
