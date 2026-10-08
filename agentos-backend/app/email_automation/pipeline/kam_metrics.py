"""Per-KAM aggregates for the KAM Dashboard.

Rolls up ``GmailIntelligence`` rows of kind ``kam_reply_accuracy`` (written by
Path K, see :mod:`.kam_reply_intelligence`) into one record per KAM over a rolling
``classified_at`` window — the same window semantics as the other intelligence
metrics. Overdue-account counts are layered on by the route from the receivables
snapshot, keeping this module free of snapshot/route coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GmailIntelligence
from app.email_automation.kam_reply_llm import KAM_REPLY_ACCURACY_KIND
from app.email_automation.pipeline.intelligence_metrics import intelligence_window_start

KamWindow = Literal["24h", "7d", "30d"]


@dataclass(slots=True)
class KamAgg:
    """Accumulated reply-tracking signals for one KAM in the window.

    Only *eligible* threads — ones where the client genuinely replied at least
    once — feed ``accuracy_rate`` / ``reply_rate``. Threads with no client reply
    at all are tracked separately via ``no_client_reply_threads`` and excluded
    from both rates so they can't distort them.
    """

    kam_key: str
    kam_name: str = ""
    threads_scored: int = 0
    client_replied: int = 0      # eligible threads: the client genuinely replied
    no_client_reply_threads: int = 0  # threads excluded: no client reply at all
    eligible_score_sum: int = 0  # of eligible threads, count with score=1 (accuracy numerator)
    kam_replied: int = 0         # of eligible threads, the KAM personally followed up
    team_replied: int = 0        # of eligible threads, KAM or another 1mg team member
                                  # sent a client-facing follow-up (reply-rate numerator)
    reply_seconds_sum: int = 0
    reply_seconds_count: int = 0

    @property
    def eligible_threads(self) -> int:
        return self.client_replied

    @property
    def accuracy_rate(self) -> float:
        if self.client_replied == 0:
            return 0.0
        return round(100.0 * self.eligible_score_sum / self.client_replied, 1)

    @property
    def reply_rate(self) -> float:
        if self.client_replied == 0:
            return 0.0
        return round(100.0 * self.team_replied / self.client_replied, 1)

    @property
    def avg_reply_seconds(self) -> float | None:
        if self.reply_seconds_count == 0:
            return None
        return round(self.reply_seconds_sum / self.reply_seconds_count, 1)


async def aggregate_kam_reply_metrics(
    db: AsyncSession, *, window: KamWindow = "7d"
) -> dict[str, KamAgg]:
    """Return ``{kam_key: KamAgg}`` over ``[now-window, now)`` on ``classified_at``."""

    since = intelligence_window_start(window)
    rows = (
        await db.execute(
            select(GmailIntelligence.payload).where(
                GmailIntelligence.kind == KAM_REPLY_ACCURACY_KIND,
                GmailIntelligence.classified_at >= since,
            )
        )
    ).all()

    aggs: dict[str, KamAgg] = {}
    for (payload,) in rows:
        p = payload or {}
        key = str(p.get("kam_key") or "").strip()
        if not key:
            continue
        agg = aggs.get(key)
        if agg is None:
            agg = KamAgg(kam_key=key, kam_name=str(p.get("kam_name") or ""))
            aggs[key] = agg
        elif not agg.kam_name:
            agg.kam_name = str(p.get("kam_name") or "")

        agg.threads_scored += 1
        if not bool(p.get("client_responsive")):
            # No client reply at all -> not eligible; excluded from both rates.
            agg.no_client_reply_threads += 1
            continue

        agg.client_replied += 1
        if int(p.get("score") or 0) == 1:
            agg.eligible_score_sum += 1
        if bool(p.get("kam_replied")):
            agg.kam_replied += 1
        if bool(p.get("team_replied")):
            agg.team_replied += 1
        secs = p.get("reply_seconds")
        if bool(p.get("kam_replied")) and isinstance(secs, (int, float)):
            agg.reply_seconds_sum += int(secs)
            agg.reply_seconds_count += 1

    return aggs


__all__ = ["KamWindow", "KamAgg", "aggregate_kam_reply_metrics"]
