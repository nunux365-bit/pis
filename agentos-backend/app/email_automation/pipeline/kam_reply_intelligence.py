"""KAM reply-accuracy intelligence — thread scoring (Path K).

Runs in its own graph node **after** collections intelligence (Path C) and before
receivable classification (Path R). Failures here never block Path R.

Per thread it:
  * resolves the assigned KAM from the anchored reminder's ``business_key`` via the
    Google-Sheet KAM directory (skips threads not owned by any KAM);
  * computes reply timing structurally (client message -> KAM follow-up) so reply
    rate / avg reply time don't depend on the LLM;
  * runs the v3 KAM accuracy scorer for the 0/1 accuracy score;
  * upserts one ``GmailIntelligence`` row (``kind=kam_reply_accuracy``). No message
    bodies are persisted — only structured columns + payload JSON.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, GmailIntelligence
from app.email_automation import gmail_sa
from app.email_automation.collections_llm import extract_all_emails, extract_primary_email
from app.email_automation.kam_directory import KamDirectory, KamInfo, load_kam_directory
from app.email_automation.kam_reply_llm import (
    KAM_REPLY_ACCURACY_KIND,
    PROMPT_VERSION,
    classify_kam_reply_thread_transcript,
)
from app.email_automation.pipeline.collections_intelligence import (
    _anchor_send_for_thread,
    _header,
    _is_mail_from_automation_mailbox,
)

from ._shared import jsonable, now_utc

log = logging.getLogger(__name__)

_MAX_THREADS_PER_TICK = 40


def _sender_role(from_header: str, directory: KamDirectory) -> str:
    """Classify a message author: ``central`` | ``kam`` | ``internal`` | ``client``.

    * central   — automation / shared mailbox (never a KAM).
    * kam       — the assigned KAM's personal address (per the directory).
    * internal  — some other ``@1mg.com`` human (not the KAM, not central).
    * client    — external sender (the counterparty we chase).
    """

    if _is_mail_from_automation_mailbox(from_header):
        return "central"
    addr = extract_primary_email(from_header)
    if directory.is_kam_email(addr):
        return "kam"
    if addr.endswith("@1mg.com"):
        return "internal"
    return "client"


def _is_client_facing(msg: dict[str, Any]) -> bool:
    """True unless To/Cc are positively known to be 1mg-only (an internal-only mail).

    Missing/unparseable recipient headers default to True so incomplete header data
    never silently zeroes out a real client-facing reply.
    """

    recipients = extract_all_emails(_header(msg, "To")) + extract_all_emails(_header(msg, "Cc"))
    if not recipients:
        return True
    return any(not addr.endswith("@1mg.com") for addr in recipients)


async def _existing_row(db: AsyncSession, *, thread_id: str) -> GmailIntelligence | None:
    stmt = select(GmailIntelligence).where(
        GmailIntelligence.kind == KAM_REPLY_ACCURACY_KIND,
        GmailIntelligence.gmail_thread_id == thread_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _compute_reply_timing(
    messages: list[dict[str, Any]], directory: KamDirectory
) -> dict[str, Any]:
    """Structural reply metrics over chronologically-sorted thread messages.

    ``kam_replied`` is True when the assigned KAM personally sent a client-facing
    message *after* the latest client message; ``reply_seconds`` is the gap between
    that client message and the KAM's first follow-up.

    ``team_replied`` is the broader reply-rate signal: True when the KAM OR any other
    1mg human (Central/Finance/other team member) sent a client-facing message after
    the latest client message. The automation mailbox (``central`` role) never counts
    — it is a reminder template, not a reply — and a message with no external
    recipient (an internal forward/escalation) never counts either.

    ``client_responsive`` is True when any client message exists.
    """

    roles: list[tuple[int, str, bool]] = []  # (internalDate_ms, role, client_facing)
    for m in messages:
        ts = gmail_sa.message_resource_internal_date_ms(m)
        role = _sender_role(_header(m, "From"), directory)
        client_facing = _is_client_facing(m) if role != "client" else False
        roles.append((ts, role, client_facing))

    client_ts = [ts for ts, r, _ in roles if r == "client"]
    if not client_ts:
        return {
            "client_responsive": False,
            "kam_replied": False,
            "team_replied": False,
            "reply_seconds": None,
            "last_client_at_ms": None,
            "last_kam_reply_at_ms": None,
            "last_team_reply_at_ms": None,
        }

    last_client_ms = max(client_ts)
    kam_after = [
        ts for ts, r, cf in roles if r == "kam" and cf and ts > last_client_ms
    ]
    team_after = [
        ts for ts, r, cf in roles if r in ("kam", "internal") and cf and ts > last_client_ms
    ]

    out: dict[str, Any] = {
        "client_responsive": True,
        "kam_replied": bool(kam_after),
        "team_replied": bool(team_after),
        "reply_seconds": None,
        "last_client_at_ms": last_client_ms,
        "last_kam_reply_at_ms": None,
        "last_team_reply_at_ms": None,
    }
    if kam_after:
        first_kam_ms = min(kam_after)
        out["reply_seconds"] = max(0, int((first_kam_ms - last_client_ms) / 1000))
        out["last_kam_reply_at_ms"] = first_kam_ms
    if team_after:
        out["last_team_reply_at_ms"] = min(team_after)
    return out


def _kam_directory_block(hana: str, kam: KamInfo) -> str:
    email = kam.email or "(no personal email on file)"
    return f"HANA {hana} -> KAM {kam.name} <{email}>"


async def classify_kam_reply_thread(
    db: AsyncSession, thread_id: str, directory: KamDirectory
) -> dict[str, Any]:
    """Score one Gmail thread for KAM reply accuracy + timing, if KAM-owned."""

    tid = (thread_id or "").strip()
    if not tid:
        return {"ok": False, "reason": "no_thread"}

    anchor = await _anchor_send_for_thread(db, tid)
    if anchor is None:
        return {"ok": False, "reason": "no_matching_sent"}

    hana = (anchor.business_key or "").strip().upper()
    kam = directory.kam_for_hana(hana)
    if kam is None:
        return {"ok": False, "reason": "no_kam_owner"}

    t_full = await asyncio.to_thread(gmail_sa.fetch_thread_full, tid)
    messages = list(t_full.get("messages") or [])
    if not messages:
        return {"ok": False, "reason": "empty_thread"}
    messages.sort(key=lambda m: gmail_sa.message_resource_internal_date_ms(m))

    # Trigger == latest message id; skip re-scoring an unchanged thread.
    latest_id = str(messages[-1].get("id") or "").strip()
    prev = await _existing_row(db, thread_id=tid)
    if prev is not None and (prev.trigger_message_id or "") == latest_id:
        return {"ok": True, "skipped": True, "reason": "unchanged_trigger"}

    timing = _compute_reply_timing(messages, directory)

    lines: list[str] = []
    for m in messages:
        ts = gmail_sa.message_resource_internal_date_ms(m)
        body = await asyncio.to_thread(gmail_sa.message_resource_plain_text, m)
        lines.append(
            f"--- message id={m.get('id')} internalDate={ts}\n"
            f"From: {_header(m, 'From')}\n"
            f"Cc: {_header(m, 'Cc') or '(none)'}\n"
            f"Subject: {_header(m, 'Subject')}\n\n{body}\n"
        )
    transcript = "\n".join(lines)

    llm = await classify_kam_reply_thread_transcript(
        transcript, kam_directory_block=_kam_directory_block(hana, kam)
    )

    payload = {
        "kam_key": kam.key,
        "kam_name": kam.name,
        "kam_email": kam.email,
        "hana_code": hana,
        "score": llm["score"],
        "client_responsive": bool(timing["client_responsive"]),
        "kam_replied": bool(timing["kam_replied"]),
        "team_replied": bool(timing["team_replied"]),
        "reply_seconds": timing["reply_seconds"],
        "last_client_at_ms": timing["last_client_at_ms"],
        "last_kam_reply_at_ms": timing["last_kam_reply_at_ms"],
        "last_team_reply_at_ms": timing["last_team_reply_at_ms"],
        "kam_actively_engaged": llm["kam_actively_engaged"],
        "clear_cta": llm["clear_cta"],
        "scenarios": llm["scenarios"],
        "open_asks": llm["open_asks"],
        "asks_addressed": llm.get("asks_addressed", []),
        "asks_missed": llm["asks_missed"],
        "failure_reason": llm["failure_reason"],
        "evidence_used": llm.get("evidence_used", []),
        "thread_message_count": len(messages),
        "anchor_send_id": str(anchor.id),
        "model": (llm.get("extras") or {}).get("model"),
    }

    classified_at = now_utc()
    row = {
        "id": uuid.uuid4(),
        "kind": KAM_REPLY_ACCURACY_KIND,
        "scope": "thread",
        "gmail_thread_id": tid,
        "trigger_message_id": latest_id,
        "anchor_send_id": anchor.id,
        "workflow_type": anchor.workflow_type,
        "variant": anchor.variant,
        "business_key": hana,
        "category": "accurate" if llm["score"] == 1 else "inaccurate",
        "confidence": llm["confidence"],
        "justification": llm["justification"],
        "payment_refs": jsonable([]),
        "prompt_version": llm.get("prompt_version") or PROMPT_VERSION,
        "payload_schema_version": 1,
        "payload": jsonable(payload),
        "classified_at": classified_at,
        "updated_at": classified_at,
    }

    stmt = insert(GmailIntelligence).values(**row)
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
            "prompt_version": stmt.excluded.prompt_version,
            "payload_schema_version": stmt.excluded.payload_schema_version,
            "payload": stmt.excluded.payload,
            "classified_at": stmt.excluded.classified_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)
    return {"ok": True, "thread_id": tid, "score": llm["score"], "kam": kam.key}


async def process_kam_reply_intelligence_batch(db: AsyncSession) -> dict[str, Any]:
    """Process distinct ``received`` message threads for KAM scoring — Path K."""

    if not settings.email_automation_enabled:
        await db.commit()
        return {"disabled": True, "threads_considered": 0, "threads_scored": 0}
    if not settings.kam_reply_accuracy_enabled:
        await db.commit()
        return {"threads_considered": 0, "threads_scored": 0, "skipped": "feature_disabled"}

    directory = await asyncio.to_thread(load_kam_directory)
    if directory.is_empty:
        await db.commit()
        return {"threads_considered": 0, "threads_scored": 0, "skipped": "empty_directory"}

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

    scored = 0
    errors = 0
    for tid in thread_ids:
        try:
            async with db.begin_nested():
                out = await classify_kam_reply_thread(db, tid, directory)
                if out.get("ok") and not out.get("skipped"):
                    scored += 1
        except Exception:
            errors += 1
            log.exception("kam_reply_intelligence: thread %s failed", tid)

    await db.commit()
    return {
        "threads_considered": len(thread_ids),
        "threads_scored": scored,
        "errors": errors,
    }


__all__ = [
    "KAM_REPLY_ACCURACY_KIND",
    "classify_kam_reply_thread",
    "process_kam_reply_intelligence_batch",
]
