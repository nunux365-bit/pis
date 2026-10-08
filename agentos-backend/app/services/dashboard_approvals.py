"""Role-scoped approval analytics for the home dashboard.

Aging distribution (pending now), top pending originators, and optional weekly
value rollups. All scoped using the same role rules as
:mod:`analytics_summary_core` so that role_aware display is easy to
re-introduce later without changing the query surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus, User
from app.security.rbac import scope_approval_assignee


def _scope(q: Select, user: User) -> Select:
    return scope_approval_assignee(q, user)


_AGING_BUCKETS: tuple[tuple[str, timedelta | None], ...] = (
    ("lt_1h", timedelta(hours=1)),
    ("lt_4h", timedelta(hours=4)),
    ("lt_24h", timedelta(hours=24)),
    ("lt_3d", timedelta(days=3)),
    ("lt_7d", timedelta(days=7)),
    ("gte_7d", None),
)


async def approval_aging_buckets(db: AsyncSession, user: User) -> dict[str, int]:
    """Distribution of currently-pending approvals by age-in-queue."""

    now = datetime.now(UTC)
    age_sec = func.extract("epoch", now - Approval.created_at)

    bucket_expr = case(
        (age_sec < _AGING_BUCKETS[0][1].total_seconds(), "lt_1h"),
        (age_sec < _AGING_BUCKETS[1][1].total_seconds(), "lt_4h"),
        (age_sec < _AGING_BUCKETS[2][1].total_seconds(), "lt_24h"),
        (age_sec < _AGING_BUCKETS[3][1].total_seconds(), "lt_3d"),
        (age_sec < _AGING_BUCKETS[4][1].total_seconds(), "lt_7d"),
        else_="gte_7d",
    ).label("bucket")

    q = select(bucket_expr, func.count()).where(
        Approval.status == ApprovalStatus.PENDING.value
    )
    q = _scope(q, user).group_by(bucket_expr)

    rows = (await db.execute(q)).all()
    out: dict[str, int] = {k: 0 for k, _ in _AGING_BUCKETS}
    for key, cnt in rows:
        if key in out:
            out[key] = int(cnt or 0)
    return out


async def approval_top_originators(
    db: AsyncSession, user: User, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Top ``limit`` ``agent_name`` values among currently-pending approvals."""

    if limit <= 0:
        return []

    q = (
        select(Approval.agent_name, func.count().label("n"))
        .where(Approval.status == ApprovalStatus.PENDING.value)
    )
    q = _scope(q, user).group_by(Approval.agent_name).order_by(func.count().desc()).limit(limit)
    rows = (await db.execute(q)).all()
    return [{"agent_name": name or "Unknown", "pending": int(cnt or 0)} for name, cnt in rows]


async def approval_decisions_value_weekly(
    db: AsyncSession, user: User, *, weeks_back: int = 2
) -> dict[str, Any]:
    """INR value of approval decisions by ISO week (UTC), newest→oldest.

    Only APPROVED + AUTO_APPROVED contribute to ``approved_value`` (money moved);
    REJECTED rows go into ``rejected_value`` so the dashboard can show the
    amount *not* signed off. Assumes INR — rows with other currencies are kept
    in the count but the value is reported alongside a ``currency`` field so
    the UI can show a small asterisk if multiple currencies are mixed.
    """

    if weeks_back <= 0 or weeks_back > 8:
        raise ValueError("weeks_back must be 1..8")

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # Anchor at the *start* of the current ISO week (Monday 00:00 UTC).
    monday_this = (now - timedelta(days=now.weekday())).replace(hour=0)
    # We want ``weeks_back`` buckets: this week + (weeks_back - 1) previous.
    start = monday_this - timedelta(days=7 * (weeks_back - 1))

    bucket = func.date_trunc("week", Approval.decided_at)
    q = select(
        bucket.label("bucket"),
        Approval.status,
        Approval.currency,
        func.coalesce(func.sum(Approval.amount), 0).label("value"),
        func.count().label("n"),
    ).where(
        Approval.decided_at.isnot(None),
        Approval.decided_at >= start,
        Approval.status.in_(
            (
                ApprovalStatus.APPROVED.value,
                ApprovalStatus.REJECTED.value,
                ApprovalStatus.AUTO_APPROVED.value,
            )
        ),
    )
    q = _scope(q, user).group_by("bucket", Approval.status, Approval.currency)

    rows = (await db.execute(q)).all()

    # { monday-iso : {approved_value, rejected_value, n_approved, n_rejected,
    #                 currencies: set} }
    weeks: dict[str, dict[str, Any]] = {}
    for b, st, ccy, val, n in rows:
        if b is None:
            continue
        b_utc = b.replace(tzinfo=UTC) if b.tzinfo is None else b.astimezone(UTC)
        key = b_utc.date().isoformat()
        slot = weeks.setdefault(
            key,
            {
                "approved_value": 0.0,
                "rejected_value": 0.0,
                "n_approved": 0,
                "n_rejected": 0,
                "currencies": set(),
            },
        )
        if ccy:
            slot["currencies"].add(str(ccy))
        try:
            vf = float(val or 0)
        except (TypeError, ValueError):
            vf = 0.0
        ni = int(n or 0)
        if st == ApprovalStatus.REJECTED.value:
            slot["rejected_value"] += vf
            slot["n_rejected"] += ni
        else:  # APPROVED or AUTO_APPROVED
            slot["approved_value"] += vf
            slot["n_approved"] += ni

    out: list[dict[str, Any]] = []
    for i in range(weeks_back):
        anchor = monday_this - timedelta(days=7 * i)
        key = anchor.date().isoformat()
        slot = weeks.get(
            key,
            {
                "approved_value": 0.0,
                "rejected_value": 0.0,
                "n_approved": 0,
                "n_rejected": 0,
                "currencies": set(),
            },
        )
        ccys = sorted(slot["currencies"])
        out.append(
            {
                "week_start": key,
                "approved_value": round(float(slot["approved_value"]), 2),
                "rejected_value": round(float(slot["rejected_value"]), 2),
                "n_approved": int(slot["n_approved"]),
                "n_rejected": int(slot["n_rejected"]),
                "currency": ccys[0] if len(ccys) == 1 else ("INR" if not ccys else "MIXED"),
            }
        )

    # newest → oldest (week index 0 = this week)
    this_week = out[0]
    prior_week = out[1] if len(out) > 1 else None
    delta_pct: float | None = None
    if prior_week and prior_week["approved_value"] > 0:
        delta_pct = round(
            ((this_week["approved_value"] - prior_week["approved_value"])
             / prior_week["approved_value"]) * 100.0,
            1,
        )

    return {
        "this_week": this_week,
        "prior_week": prior_week,
        "approved_value_delta_pct": delta_pct,
        "weeks": out,
    }


__all__ = [
    "approval_aging_buckets",
    "approval_top_originators",
    "approval_decisions_value_weekly",
]
