"""Outreach reply intelligence — classify inbound replies to sent outreach emails."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, OutreachLead, OutreachReply
from app.email_automation import gmail_sa
from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.engine import CHW_OUTREACH_PROMPT_VERSION
from app.email_automation.outreach.outreach_llm import classify_outreach_thread_transcript

log = logging.getLogger(__name__)
_MAX_THREADS_PER_TICK = 20


def _header(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").strip().lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""


async def process_outreach_intelligence_batch(
    db: AsyncSession,
    campaigns: list[CampaignConfig],
) -> dict[str, Any]:
    """Classify reply threads for all active campaigns. Called from scan cycle."""
    if not settings.outreach_enabled:
        return {"skipped": "feature_disabled"}

    sender_emails = {cfg.sender_email.lower() for cfg in campaigns}
    campaign_map = {cfg.campaign_name: cfg for cfg in campaigns}

    stmt = (
        select(EmailAutomationMessage.thread_id, OutreachLead.id,
               OutreachLead.campaign_name, OutreachLead.gmail_thread_id)
        .join(OutreachLead, EmailAutomationMessage.thread_id == OutreachLead.gmail_thread_id)
        .where(
            EmailAutomationMessage.status == "received",
            OutreachLead.gmail_thread_id.isnot(None),
        )
        .distinct(EmailAutomationMessage.thread_id)
        .limit(_MAX_THREADS_PER_TICK)
    )
    rows = (await db.execute(stmt)).all()

    classified, errors = 0, 0
    for thread_id, lead_id, campaign_name, _ in rows:
        cfg = campaign_map.get(campaign_name)
        if not cfg:
            continue
        try:
            async with db.begin_nested():
                result = await _classify_thread(
                    db=db,
                    thread_id=thread_id,
                    lead_id=lead_id,
                    cfg=cfg,
                    sender_emails=sender_emails,
                )
                if result.get("ok") and not result.get("skipped"):
                    classified += 1
        except Exception:
            errors += 1
            log.exception("outreach intelligence: thread %s failed", thread_id)

    await db.commit()
    return {"threads_considered": len(rows), "threads_classified": classified, "errors": errors}


async def _classify_thread(
    *,
    db: AsyncSession,
    thread_id: str,
    lead_id: int,
    cfg: CampaignConfig,
    sender_emails: set[str],
) -> dict[str, Any]:
    t_full = await asyncio.to_thread(gmail_sa.fetch_thread_full, thread_id)
    messages = sorted(
        t_full.get("messages") or [],
        key=lambda m: gmail_sa.message_resource_internal_date_ms(m),
    )

    inbound = [
        m for m in messages
        if _header(m, "From").lower().split("<")[-1].strip(">") not in sender_emails
    ]
    if not inbound:
        return {"ok": False, "reason": "no_inbound"}

    latest_id = str(inbound[-1].get("id") or "")

    existing = (await db.execute(
        select(OutreachReply).where(
            OutreachReply.outreach_lead_id == lead_id,
            OutreachReply.gmail_thread_id == thread_id,
        ).order_by(OutreachReply.classified_at.desc()).limit(1)
    )).scalar_one_or_none()

    if existing and existing.gmail_message_id == latest_id:
        return {"ok": True, "skipped": True, "reason": "unchanged"}

    lines = []
    for m in messages:
        body = await asyncio.to_thread(gmail_sa.message_resource_plain_text, m)
        lines.append(
            f"--- From: {_header(m, 'From')}\nSubject: {_header(m, 'Subject')}\n\n{body}\n"
        )
    transcript = "\n".join(lines)

    payload = await classify_outreach_thread_transcript(
        transcript,
        prompt_path=cfg.category_prompt_path,
        prompt_version=CHW_OUTREACH_PROMPT_VERSION,
    )

    now = datetime.now(timezone.utc)
    stmt = insert(OutreachReply).values(
        outreach_lead_id=lead_id,
        campaign_name=cfg.campaign_name,
        gmail_thread_id=thread_id,
        gmail_message_id=latest_id,
        category=payload["category"],
        confidence=payload["confidence"],
        justification=payload["justification"],
        prompt_version=payload["prompt_version"],
        classified_at=now,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["gmail_message_id"],
        set_={
            "category": stmt.excluded.category,
            "confidence": stmt.excluded.confidence,
            "justification": stmt.excluded.justification,
            "prompt_version": stmt.excluded.prompt_version,
            "classified_at": stmt.excluded.classified_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)

    lead = (await db.execute(select(OutreachLead).where(OutreachLead.id == lead_id))).scalar_one()
    lead.last_reply_at = now
    lead.last_reply_category = payload["category"]
    lead.reply_count = (lead.reply_count or 0) + 1

    return {"ok": True, "category": payload["category"]}
