"""Scan Postgres for sent outreach leads with unclassified or updated reply threads."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutreachLead, OutreachReply

log = logging.getLogger(__name__)

_MAX_THREADS_PER_TICK = 50


async def scan_unclassified_threads(
    db: AsyncSession,
    max_threads: int = _MAX_THREADS_PER_TICK,
) -> list[dict[str, Any]]:
    """Return sent leads whose thread may have new inbound messages.

    Returns a list of candidate dicts:
      {"lead_id": int, "thread_id": str, "campaign_name": str,
       "latest_reply_message_id": str | None}
    """
    # Subquery: latest classified message_id per lead
    latest_reply_sq = (
        select(
            OutreachReply.outreach_lead_id.label("lead_id"),
            func.max(OutreachReply.gmail_message_id).label("latest_msg_id"),
        )
        .group_by(OutreachReply.outreach_lead_id)
        .subquery()
    )

    stmt = (
        select(
            OutreachLead.id.label("lead_id"),
            OutreachLead.gmail_thread_id,
            OutreachLead.campaign_name,
            latest_reply_sq.c.latest_msg_id.label("latest_reply_message_id"),
        )
        .outerjoin(latest_reply_sq, latest_reply_sq.c.lead_id == OutreachLead.id)
        .where(
            OutreachLead.status == "sent",
            OutreachLead.gmail_thread_id.isnot(None),
        )
        .limit(max_threads)
    )

    rows = (await db.execute(stmt)).all()
    return [
        {
            "lead_id": row.lead_id,
            "thread_id": row.gmail_thread_id,
            "campaign_name": row.campaign_name,
            "latest_reply_message_id": row.latest_reply_message_id,
        }
        for row in rows
    ]
