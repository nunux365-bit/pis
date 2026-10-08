"""Collections reply intelligence — thread classification (Path C).

Runs in a separate graph node **before** receivable classification (Path R) so
Path R outcomes are unchanged. Failures here never block Path R.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, EmailAutomationSend, GmailIntelligence
from app.email_automation import gmail_sa
from app.email_automation.collections_llm import (
    COLLECTIONS_REPLY_KIND,
    PROMPT_VERSION,
    classify_collections_thread_transcript,
    extract_primary_email,
)

from ._shared import jsonable, now_utc

log = logging.getLogger(__name__)

_MAX_THREADS_PER_TICK = 40


def _is_mail_from_automation_mailbox(from_header: str | None) -> bool:
    addr = extract_primary_email(from_header or "")
    imp = (settings.email_automation_impersonated_user or "").strip().lower()
    sf = (settings.email_automation_send_from or "").strip().lower()
    if addr and imp and addr == imp:
        return True
    if addr and sf and addr == sf:
        return True
    return False


def _header(msg: dict[str, Any], name: str) -> str:
    payload = msg.get("payload") or {}
    for h in payload.get("headers") or []:
        if (h.get("name") or "").strip().lower() == name.lower():
            return (h.get("value") or "").strip()
    return ""


async def _anchor_send_for_thread(db: AsyncSession, thread_id: str) -> EmailAutomationSend | None:
    stmt = (
        select(EmailAutomationSend)
        .where(
            EmailAutomationSend.status == "sent",
            EmailAutomationSend.gmail_thread_id == thread_id,
        )
        .order_by(desc(EmailAutomationSend.sent_at))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _existing_row(db: AsyncSession, *, thread_id: str) -> GmailIntelligence | None:
    stmt = select(GmailIntelligence).where(
        GmailIntelligence.kind == COLLECTIONS_REPLY_KIND,
        GmailIntelligence.gmail_thread_id == thread_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def classify_collections_thread(db: AsyncSession, thread_id: str) -> dict[str, Any]:
    """Classify one Gmail thread if it belongs to a sent reminder and has new inbound mail."""

    tid = (thread_id or "").strip()
    if not tid:
        return {"ok": False, "reason": "no_thread"}

    anchor = await _anchor_send_for_thread(db, tid)
    if anchor is None:
        return {"ok": False, "reason": "no_matching_sent"}

    t_full = await asyncio.to_thread(gmail_sa.fetch_thread_full, tid)
    messages = list(t_full.get("messages") or [])
    if not messages:
        return {"ok": False, "reason": "empty_thread"}

    messages.sort(key=lambda m: gmail_sa.message_resource_internal_date_ms(m))

    inbound: list[dict[str, Any]] = []
    for m in messages:
        from_h = _header(m, "From")
        if not _is_mail_from_automation_mailbox(from_h):
            inbound.append(m)

    if not inbound:
        return {"ok": False, "reason": "no_inbound"}

    latest_inbound = inbound[-1]
    latest_id = str(latest_inbound.get("id") or "").strip()
    if not latest_id:
        return {"ok": False, "reason": "no_inbound_id"}

    prev = await _existing_row(db, thread_id=tid)
    if prev is not None and (prev.trigger_message_id or "") == latest_id:
        return {"ok": True, "skipped": True, "reason": "unchanged_trigger"}

    lines: list[str] = []
    for m in messages:
        mid = m.get("id")
        ts = gmail_sa.message_resource_internal_date_ms(m)
        from_h = _header(m, "From")
        cc_h = _header(m, "Cc")
        subj = _header(m, "Subject")
        body = await asyncio.to_thread(gmail_sa.message_resource_plain_text, m)
        lines.append(
            f"--- message id={mid} internalDate={ts}\n"
            f"From: {from_h}\n"
            f"Cc: {cc_h or '(none)'}\n"
            f"Subject: {subj}\n\n{body}\n"
        )
    transcript = "\n".join(lines)

    llm_payload = await classify_collections_thread_transcript(transcript)

    payload_extra = dict(llm_payload.get("extras") or {})
    payload_extra.update(
        {
            "anchor_send_id": str(anchor.id),
            "thread_message_count": len(messages),
        }
    )

    classified_at = now_utc()
    row_payload = {
        "id": uuid.uuid4(),
        "kind": COLLECTIONS_REPLY_KIND,
        "scope": "thread",
        "gmail_thread_id": tid,
        "trigger_message_id": latest_id,
        "anchor_send_id": anchor.id,
        "workflow_type": anchor.workflow_type,
        "variant": anchor.variant,
        "business_key": anchor.business_key,
        "category": llm_payload["category"],
        "confidence": llm_payload["confidence"],
        "justification": llm_payload["justification"],
        "payment_refs": jsonable(llm_payload.get("payment_refs") or []),
        "prompt_version": llm_payload.get("prompt_version") or PROMPT_VERSION,
        "payload_schema_version": 1,
        "payload": jsonable(payload_extra),
        "classified_at": classified_at,
        "updated_at": classified_at,
    }

    stmt = insert(GmailIntelligence).values(**row_payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "gmail_thread_id"],
        set_={
            "trigger_message_id": stmt.excluded.trigger_message_id,
            "anchor_send_id": stmt.excluded.anchor_send_id,
            "workflow_type": stmt.excluded.workflow_type,
            "variant": stmt.excluded.variant,
            "business_key": stmt.excluded.business_key,
            "category": stmt.excluded.category,
            "confidence": stmt.excluded.confidence,
            "justification": stmt.excluded.justification,
            "payment_refs": stmt.excluded.payment_refs,
            "prompt_version": stmt.excluded.prompt_version,
            "payload_schema_version": stmt.excluded.payload_schema_version,
            "payload": stmt.excluded.payload,
            "classified_at": stmt.excluded.classified_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)
    return {"ok": True, "thread_id": tid, "category": llm_payload["category"]}


async def process_collections_intelligence_batch(db: AsyncSession) -> dict[str, Any]:
    """Process distinct ``received`` message threads — Path C."""

    if not settings.email_automation_enabled:
        await db.commit()
        return {"disabled": True, "threads_considered": 0, "threads_classified": 0}
    if not settings.email_automation_collections_intelligence_enabled:
        await db.commit()
        return {"threads_considered": 0, "threads_classified": 0, "skipped": "feature_disabled"}

    stmt = (
        select(EmailAutomationMessage.thread_id)
        .where(
            EmailAutomationMessage.status == "received",
            EmailAutomationMessage.thread_id.isnot(None),
        )
        .distinct()
        .limit(_MAX_THREADS_PER_TICK)
    )
    thread_ids = [str(r[0]) for r in (await db.execute(stmt)).all() if r[0]]

    classified = 0
    errors = 0
    for tid in thread_ids:
        try:
            async with db.begin_nested():
                out = await classify_collections_thread(db, tid)
                if out.get("ok") and not out.get("skipped"):
                    classified += 1
        except Exception:
            errors += 1
            log.exception("collections_intelligence: thread %s failed", tid)

    await db.commit()
    return {
        "threads_considered": len(thread_ids),
        "threads_classified": classified,
        "errors": errors,
    }


__all__ = [
    "COLLECTIONS_REPLY_KIND",
    "classify_collections_thread",
    "process_collections_intelligence_batch",
]
