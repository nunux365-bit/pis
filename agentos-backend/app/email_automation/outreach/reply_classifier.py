"""Classify inbound outreach reply threads via Gmail + OpenAI. No DB writes."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.email_automation import gmail_sa
from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.engine import CHW_OUTREACH_PROMPT_VERSION
from app.email_automation.outreach.outreach_llm import classify_outreach_thread_transcript

log = logging.getLogger(__name__)


def _header(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").strip().lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""


async def classify_threads_batch(
    candidates: list[dict[str, Any]],
    campaigns: list[CampaignConfig],
) -> list[dict[str, Any]]:
    """Fetch + classify each candidate thread. Returns classification result dicts.

    Each result:
      {"lead_id": int, "thread_id": str, "gmail_message_id": str | None,
       "category": str, "confidence": float, "justification": str,
       "prompt_version": str, "skipped": bool, "error": str | None}
    """
    campaign_map = {cfg.campaign_name: cfg for cfg in campaigns}
    sender_emails = {cfg.sender_email.lower() for cfg in campaigns}
    results = []

    for candidate in candidates:
        lead_id = candidate["lead_id"]
        thread_id = candidate["thread_id"]
        campaign_name = candidate["campaign_name"]
        known_msg_id = candidate.get("latest_reply_message_id")

        cfg = campaign_map.get(campaign_name)
        if not cfg:
            results.append({
                "lead_id": lead_id, "thread_id": thread_id, "skipped": True,
                "error": "campaign_not_found", "gmail_message_id": None,
            })
            continue

        try:
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
                results.append({
                    "lead_id": lead_id, "thread_id": thread_id, "skipped": True,
                    "error": "no_inbound", "gmail_message_id": None,
                })
                continue

            latest_msg_id = str(inbound[-1].get("id") or "")
            if known_msg_id and latest_msg_id == known_msg_id:
                results.append({
                    "lead_id": lead_id, "thread_id": thread_id, "skipped": True,
                    "error": "unchanged", "gmail_message_id": latest_msg_id,
                })
                continue

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
            results.append({
                "lead_id": lead_id,
                "thread_id": thread_id,
                "campaign_name": campaign_name,
                "gmail_message_id": latest_msg_id,
                "category": payload["category"],
                "intent_level": payload.get("intent_level", ""),
                "next_action": payload.get("next_action", ""),
                "confidence": payload["confidence"],
                "key_signals": payload.get("key_signals") or [],
                "justification": payload["justification"],
                "prompt_version": payload["prompt_version"],
                "skipped": False,
                "error": None,
            })
        except Exception as exc:
            log.exception("reply_classifier: thread %s failed", thread_id)
            results.append({
                "lead_id": lead_id, "thread_id": thread_id, "skipped": True,
                "error": str(exc), "gmail_message_id": None,
            })

    return results
