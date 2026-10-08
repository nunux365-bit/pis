"""Dispatch — reclaim stuck rows + claim-and-send approved rows.

Two public entries:

* :func:`reclaim_stuck_sending` — safety sweep for rows stuck in ``sending``
  past the grace window. Indeterminate cases (no ``provider_message_id``) are
  flipped to ``failed`` with a ``reclaim_indeterminate`` reason and logged at
  ``error`` level so ops alerts fire.
* :func:`dispatch_approved` — reclaim + claim + send in one call, used by the
  dispatch cron job and the HTTP ``/dispatch`` endpoint.

:func:`dispatch_claim_and_send` is exposed separately for the LangGraph
dispatch branch, which runs reclaim and claim-and-send on distinct sessions.

Attempt counting (F3): every claim increments ``send_attempt_count``. When
it reaches :attr:`settings.email_automation_send_max_attempts` the row is
left in ``failed`` and **not** re-eligible for auto-retry on the next tick;
ops have to move it back manually (the retry endpoint resets the counter).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, EmailAutomationSend
from app.email_automation import gmail_sa
from app.email_automation.engine import sender as _sender

from ._shared import jsonable, log, now_utc


# Rows stuck in ``sending`` for longer than this are reclaimed by the next
# dispatcher tick — covers process crashes between the ``sending`` commit
# and the terminal status write.
SENDING_RECLAIM_AFTER_MINUTES = 15

# Default ``LIMIT`` for one dispatch tick (claim + send). LangGraph, cron, and
# workflow runner should stay aligned on this single constant.
DEFAULT_DISPATCH_SEND_BATCH = 30


def _raw_message_id_from_headers(raw_headers: dict[str, Any] | None) -> str | None:
    """First ``Message-ID`` from persisted Gmail headers (case-insensitive key)."""

    if not raw_headers:
        return None
    for key, val in raw_headers.items():
        if str(key).lower() != "message-id":
            continue
        if val is None:
            continue
        out = str(val).strip()
        return out or None
    return None


def resolve_thread_parent_raw_message_id(
    *,
    was_skipped: bool,
    workflow_type: str,
    variant: str,
    business_key: str,
    source_message_id: UUID | None,
    prior_rfc_by_key: dict[tuple[str, str, str], str | None],
    raw_mid_by_source: dict[UUID, str | None],
) -> str | None:
    """Choose the raw ``Message-ID`` string used as ``parent_message_id`` on send.

    **Customer sends** (``was_skipped=False``): prefer the cached RFC id from the
    latest prior ``sent`` row (weekly chain). If absent, fall back to the inbound
    trigger message's ``Message-ID``.

    **Skipped AR-notify sends**: skip the weekly chain — only the inbound trigger
    applies so customer reminder threading is not altered by AR mail.
    """

    parent_raw: str | None = None
    if not was_skipped:
        parent_raw = prior_rfc_by_key.get((workflow_type, variant, business_key))
    if parent_raw is None and source_message_id is not None:
        parent_raw = raw_mid_by_source.get(source_message_id)
    return parent_raw


_RFC_MID_DB_LEN = 512


async def _store_outbound_rfc_message_id(provider_gmail_id: str | None) -> str | None:
    """Fetch Gmail's RFC ``Message-ID`` for a sent message; never raises."""

    if not (provider_gmail_id or "").strip():
        return None
    try:
        raw = await asyncio.to_thread(
            gmail_sa.fetch_message_rfc_message_id,
            provider_gmail_id.strip(),
        )
        if not raw:
            return None
        if len(raw) > _RFC_MID_DB_LEN:
            return raw[:_RFC_MID_DB_LEN]
        return raw
    except Exception:
        log.warning(
            "dispatch: failed to fetch outbound RFC Message-ID for gmail message %s",
            (provider_gmail_id or "")[:24],
            exc_info=True,
        )
        return None


async def reclaim_stuck_sending(db: AsyncSession) -> dict[str, int]:
    """Heal rows stuck in ``sending`` past the grace window.

    Two cases:

    * **Provider id already set** — Gmail accepted; local commit failed. The
      email went out; fast-forward to ``sent`` and audit. Re-queueing would
      duplicate the customer's inbox.
    * **No provider id** — indeterminate. We cannot tell whether Gmail got
      the message before we crashed. Leave as ``failed`` with a
      ``reclaim_indeterminate`` reason — ops must check Gmail Sent and
      decide. Silent re-queue risks a double send.

    Every indeterminate row is logged at ``error`` level (F1) with the send
    id and business key so alerting rules can fire without having to walk
    the table.
    """

    cutoff = now_utc() - timedelta(minutes=SENDING_RECLAIM_AFTER_MINUTES)

    stuck = (
        await db.execute(
            select(EmailAutomationSend)
            .options(
                load_only(
                    EmailAutomationSend.id,
                    EmailAutomationSend.status,
                    EmailAutomationSend.updated_at,
                    EmailAutomationSend.provider_message_id,
                    EmailAutomationSend.review_reasons,
                    EmailAutomationSend.error,
                    EmailAutomationSend.sent_at,
                    EmailAutomationSend.workflow_type,
                    EmailAutomationSend.variant,
                    EmailAutomationSend.business_key,
                    EmailAutomationSend.period_key,
                )
            )
            .where(
                EmailAutomationSend.status == "sending",
                EmailAutomationSend.updated_at < cutoff,
            )
        )
    ).scalars().all()

    flagged_for_review = 0
    completed = 0
    now = now_utc()
    for row in stuck:
        reasons = list(row.review_reasons or [])
        if row.provider_message_id:
            reasons.append(
                {
                    "code": "reclaim_already_sent",
                    "at": now.isoformat(),
                    "detail": (
                        "row was stuck in 'sending' but provider_message_id is already set; "
                        "marked 'sent' to avoid duplicate delivery"
                    ),
                    "human_message": (
                        "Recovered after a crash: Gmail had already accepted this "
                        "email; we marked it sent to avoid sending it twice."
                    ),
                    "suggested_action": "No action needed — verified delivered.",
                }
            )
            row.status = "sent"
            row.sent_at = row.sent_at or now
            rfc_mid = await _store_outbound_rfc_message_id(row.provider_message_id)
            if rfc_mid:
                row.provider_rfc_message_id = rfc_mid
            completed += 1
        else:
            reasons.append(
                {
                    "code": "reclaim_indeterminate",
                    "at": now.isoformat(),
                    "detail": (
                        "row was stuck in 'sending' with no provider id past the "
                        f"{SENDING_RECLAIM_AFTER_MINUTES}-minute grace window; "
                        "Gmail acceptance state is unknown — marked 'failed' to "
                        "avoid risk of duplicate delivery"
                    ),
                    "human_message": (
                        "A send attempt crashed before we could record the "
                        "outcome; the email may or may not have gone out."
                    ),
                    "suggested_action": (
                        "Check Gmail Sent (automation.agents@1mg.com) for this "
                        "customer + period. If not found, use the retry endpoint."
                    ),
                }
            )
            row.status = "failed"
            row.error = jsonable(
                {
                    "error": "reclaim_indeterminate",
                    "type": "ReclaimTimeout",
                    "detail": reasons[-1]["detail"],
                }
            )
            # F1: error-level log per indeterminate reclaim so ops alerting can
            # page without needing a DB scan. Includes the minimum facts needed
            # to triage (send_id + business_key + workflow/variant + period).
            log.error(
                "reclaim_indeterminate send_id=%s workflow=%s variant=%s "
                "business_key=%s period=%s — Gmail acceptance unknown, marked failed",
                row.id, row.workflow_type, row.variant,
                row.business_key, row.period_key,
            )
            flagged_for_review += 1
        row.review_reasons = jsonable(reasons)

    return {"requeued": flagged_for_review, "completed": completed}


async def dispatch_claim_and_send(
    db: AsyncSession, *, limit: int
) -> dict[str, Any]:
    """Claim up to ``limit`` approved rows and send them via the sender.

    Attempt counting (F3): each claimed row gets ``send_attempt_count += 1``.
    If the row already exceeded the configured cap it is skipped here
    (defense in depth — the approved/rendered filter should already exclude
    them via the retry endpoint's cap check, but this is the last line).
    """

    target_statuses: list[str] = ["approved"]
    if not settings.email_automation_require_approval:
        target_statuses.append("rendered")

    max_attempts = int(settings.email_automation_send_max_attempts or 5)

    # Eligibility: ``approved``/``rendered`` (the customer-reminder happy
    # path) **plus** ``skipped`` rows that have a static AR-desk address
    # populated by the pipeline and haven't been notified yet. The
    # skipped path keeps ``status='skipped'`` across the whole send
    # cycle — we use ``provider_message_id`` / ``sent_at`` as the "did
    # we notify?" signal so the UI's skip queue (``WHERE status='skipped'``)
    # stays a faithful record of customer-side reality.
    # ``resolved_to_addrs`` is JSONB (a list of strings), NOT a Postgres
    # array \u2014 use ``jsonb_array_length`` (NOT ``cardinality``, which only
    # works on real arrays). Guard the call against NULLs because the
    # column is nullable; ``jsonb_array_length(NULL)`` would error and
    # ``jsonb_array_length(non-array-jsonb)`` raises too.
    skipped_unnotified = and_(
        EmailAutomationSend.status == "skipped",
        EmailAutomationSend.provider_message_id.is_(None),
        EmailAutomationSend.resolved_to_addrs.isnot(None),
        func.jsonb_array_length(EmailAutomationSend.resolved_to_addrs) > 0,
    )

    # Canary allowlist (Option B): when non-empty, only rows whose
    # ``business_key`` is on the list are even considered for claim. Empty
    # (default) = send everything, same as before the flag existed. We
    # trim/skip blanks to make ``"A, , B"`` behave like ``"A,B"``.
    raw_allow = (settings.email_automation_dispatch_allowlist or "").strip()
    allow_keys = [k.strip() for k in raw_allow.split(",") if k.strip()] if raw_allow else []
    eligibility = or_(
        EmailAutomationSend.status.in_(target_statuses),
        skipped_unnotified,
    )
    where_clauses = [eligibility]
    if allow_keys:
        where_clauses.append(EmailAutomationSend.business_key.in_(allow_keys))
        log.info(
            "dispatch: canary allowlist active (%d keys) — only those business_keys are eligible this tick",
            len(allow_keys),
        )

    # Omit wide columns (``aggregated_data``, ``review_reasons``, …) from the
    # claim query — dispatch only needs body + recipients + status fields.
    #
    # AsyncSession: columns not listed here stay deferred; reading them later
    # issues a sync lazy load and raises MissingGreenlet. Keep this list in
    # sync with every attribute read or written on claimed rows below (including
    # columns updated after send: ``provider_rfc_message_id``, ``gmail_thread_id``).
    _claim_load = load_only(
        EmailAutomationSend.id,
        EmailAutomationSend.status,
        EmailAutomationSend.send_attempt_count,
        EmailAutomationSend.workflow_type,
        EmailAutomationSend.variant,
        EmailAutomationSend.business_key,
        EmailAutomationSend.rendered_subject,
        EmailAutomationSend.rendered_body_html,
        EmailAutomationSend.resolved_to_addrs,
        EmailAutomationSend.resolved_cc_addrs,
        EmailAutomationSend.test_mode,
        EmailAutomationSend.provider_message_id,
        EmailAutomationSend.provider_rfc_message_id,
        EmailAutomationSend.gmail_thread_id,
        EmailAutomationSend.to_addrs,
        EmailAutomationSend.cc_addrs,
        EmailAutomationSend.bcc_addrs,
        EmailAutomationSend.sent_at,
        EmailAutomationSend.error,
        EmailAutomationSend.updated_at,
        EmailAutomationSend.source_message_id,
    )

    claimed: list[tuple[EmailAutomationSend, bool]] = []  # (row, was_skipped)
    async with db.begin():
        rows = (
            await db.execute(
                select(EmailAutomationSend)
                .options(_claim_load)
                .where(*where_clauses)
                .order_by(EmailAutomationSend.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for row in rows:
            was_skipped = row.status == "skipped"
            if (row.send_attempt_count or 0) >= max_attempts:
                # Shouldn't happen (retry endpoint gates on this), but if it
                # does, push the row to ``failed`` and never send. For
                # skipped-origin rows we leave ``status='skipped'`` and
                # only stamp the failure on ``error`` — the customer-side
                # truth (skipped) is preserved; ops sees the cap via
                # SQL on ``send_attempt_count`` + ``error``.
                if not was_skipped:
                    row.status = "failed"
                row.error = jsonable(
                    {
                        "error": "send_attempt_cap_reached",
                        "type": "AttemptCap",
                        "attempts": row.send_attempt_count or 0,
                        "cap": max_attempts,
                    }
                )
                log.error(
                    "dispatch: attempt cap reached send_id=%s attempts=%d cap=%d — "
                    "%s",
                    row.id, row.send_attempt_count or 0, max_attempts,
                    "leaving skipped (notify gave up)" if was_skipped
                    else "leaving failed",
                )
                continue
            # Only customer rows transition through ``sending``. Skipped
            # rows stay ``skipped`` for the whole cycle (the SKIP LOCKED
            # row lock above is enough to prevent two dispatchers from
            # double-notifying).
            if not was_skipped:
                row.status = "sending"
            row.send_attempt_count = (row.send_attempt_count or 0) + 1
            claimed.append((row, was_skipped))

    source_ids = {row.source_message_id for row, _ in claimed if row.source_message_id}
    raw_mid_by_source: dict[UUID, str | None] = {}
    if source_ids:
        parents = (
            await db.execute(
                select(EmailAutomationMessage)
                .options(
                    load_only(
                        EmailAutomationMessage.id,
                        EmailAutomationMessage.raw_headers,
                    )
                )
                .where(EmailAutomationMessage.id.in_(source_ids))
            )
        ).scalars().all()
        for p in parents:
            hdrs = p.raw_headers if isinstance(p.raw_headers, dict) else None
            raw_mid_by_source[p.id] = _raw_message_id_from_headers(hdrs)

    claimed_ids = {row.id for row, _ in claimed}
    customer_keys = {
        (row.workflow_type, row.variant, row.business_key)
        for row, was_skipped in claimed
        if not was_skipped
    }
    prior_rfc_by_key: dict[tuple[str, str, str], str | None] = {}
    for wf, var, bk in customer_keys:
        prior = (
            await db.execute(
                select(EmailAutomationSend.provider_rfc_message_id)
                .where(
                    EmailAutomationSend.workflow_type == wf,
                    EmailAutomationSend.variant == var,
                    EmailAutomationSend.business_key == bk,
                    EmailAutomationSend.status == "sent",
                    EmailAutomationSend.provider_rfc_message_id.isnot(None),
                    EmailAutomationSend.id.notin_(claimed_ids),
                )
                .order_by(EmailAutomationSend.sent_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        prior_rfc_by_key[(wf, var, bk)] = prior

    sent: list[UUID] = []
    failed: list[dict[str, Any]] = []
    for row, was_skipped in claimed:
        try:
            parent_raw = resolve_thread_parent_raw_message_id(
                was_skipped=was_skipped,
                workflow_type=row.workflow_type,
                variant=row.variant,
                business_key=row.business_key,
                source_message_id=row.source_message_id,
                prior_rfc_by_key=prior_rfc_by_key,
                raw_mid_by_source=raw_mid_by_source,
            )

            outcome = await asyncio.to_thread(
                _sender.send,
                _sender.OutboundEmail(
                    subject=row.rendered_subject or "",
                    body_html=row.rendered_body_html or "",
                    body_text=None,
                    resolved_to=tuple(row.resolved_to_addrs or ()),
                    resolved_cc=tuple(row.resolved_cc_addrs or ()),
                    # M5: row.id is our stable per-row UUID; threading it as
                    # X-Agentos-Send-Id gives Gmail Sent an audit trail that
                    # mirrors the DB row. If we crash between Gmail accept
                    # and the local commit, the retry cap bounds the damage
                    # (\u2264 5 dupes), and ops can search Sent for the id to
                    # confirm delivery before taking action.
                    send_id=str(row.id),
                    parent_message_id=parent_raw,
                ),
            )
            row.to_addrs = list(outcome.wire_to)
            row.cc_addrs = list(outcome.wire_cc)
            row.bcc_addrs = list(outcome.wire_bcc)
            row.test_mode = outcome.test_mode
            row.provider_message_id = outcome.provider_message_id
            row.provider_rfc_message_id = await _store_outbound_rfc_message_id(
                outcome.provider_message_id
            )
            try:
                if outcome.provider_message_id:
                    th = await asyncio.to_thread(
                        gmail_sa.fetch_message_thread_id,
                        outcome.provider_message_id.strip(),
                    )
                    row.gmail_thread_id = (th or "").strip() or None
            except Exception:
                log.warning(
                    "dispatch: could not resolve gmail_thread_id for send row %s",
                    row.id,
                    exc_info=True,
                )
            row.sent_at = now_utc()
            # Customer rows: sending → sent (terminal success).
            # Skipped rows:  stay 'skipped'; the populated provider id
            # is the "AR was notified" marker.
            if not was_skipped:
                row.status = "sent"
            sent.append(row.id)
        except Exception as e:
            log.exception("dispatch: send failed for row %s", row.id)
            # Customer rows go to 'failed' (terminal until ops retries).
            # Skipped rows stay 'skipped' — provider_message_id is still
            # NULL, so the next dispatch tick will retry until the
            # attempt cap.
            if not was_skipped:
                row.status = "failed"
            row.error = jsonable(
                {
                    "error": str(e),
                    "type": type(e).__name__,
                    "attempts": row.send_attempt_count or 0,
                }
            )
            failed.append({"id": str(row.id), "error": str(e)})
        await db.commit()
        # Drop fully-hydrated ORM instances from the session so large HTML
        # bodies are not retained in the identity map until the session closes.
        db.expunge(row)

    return {"sent": sent, "failed": failed}


async def dispatch_approved(
    db: AsyncSession, *, limit: int = DEFAULT_DISPATCH_SEND_BATCH
) -> dict[str, Any]:
    """Reclaim + claim + send — the full dispatch tick.

    Concurrency is safe across multiple dispatchers: a candidate row set is
    claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` and transitioned to
    ``sending`` inside the same transaction.
    """

    if not settings.email_automation_enabled:
        return {"sent": [], "failed": [], "reclaimed": 0, "reclaimed_completed": 0}

    reclaim = await reclaim_stuck_sending(db)
    await db.commit()
    if reclaim["requeued"] or reclaim["completed"]:
        log.warning(
            "dispatch reclaim: requeued=%d completed=%d (rows stuck > %dm)",
            reclaim["requeued"],
            reclaim["completed"],
            SENDING_RECLAIM_AFTER_MINUTES,
        )
    result = await dispatch_claim_and_send(db, limit=limit)
    return {
        "sent": result["sent"],
        "failed": result["failed"],
        "reclaimed": reclaim["requeued"],
        "reclaimed_completed": reclaim["completed"],
    }


__all__ = [
    "SENDING_RECLAIM_AFTER_MINUTES",
    "DEFAULT_DISPATCH_SEND_BATCH",
    "reclaim_stuck_sending",
    "dispatch_claim_and_send",
    "dispatch_approved",
    "resolve_thread_parent_raw_message_id",
]
