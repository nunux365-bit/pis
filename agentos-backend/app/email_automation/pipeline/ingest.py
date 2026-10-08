"""Inbox ingest \u2014 Gmail ``messages.list`` + ``messages.get`` \u2192 DB rows.

Separated from classification/processing so the graph's ``ingest_messages``
node does exactly one observable thing: pull new ids and land them in
``EmailAutomationMessage(status='received')``. The next node picks them up.

Flow per tick (read-only on Gmail \u2014 we never modify labels):

1. ``users.messages.list`` with the configured filter (sender, subject,
   ``newer_than``). Gmail returns ids **newest-first**.
2. Bulk-check our DB: ``SELECT provider_message_id WHERE id = ANY(:ids)``
   to find which of the listed ids are already persisted.
3. Insert the new ones as ``EmailAutomationMessage(status='received')``,
   each in its own SAVEPOINT so a duplicate doesn't roll back the batch.
4. **Stop condition**: if the page contained at least one id we already
   had \u2014 we've caught up to history; older pages are also already in
   the DB. Break the loop.
5. Otherwise loop until either Gmail returns a short page (no more
   matching mail), or a per-tick safety cap is hit.

Why this beats marking-as-read:

  * No ``gmail.modify`` scope, no DWD authorization changes.
  * No "marked-read but failed to persist" race \u2014 Gmail state never
    changes, so a crash mid-tick is invisible to Gmail.
  * Cheap: ids-only ``messages.list`` is 1 quota unit; the per-id
    ``messages.get`` is paid only for messages we actually intend to
    insert.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import EmailAutomationMessage
from app.email_automation import gmail_sa

from ._shared import jsonable, log


# Per-tick safety cap on ingest_new_messages. At 25 messages per batch and
# 10 minute polling, 500 messages/tick absorbs even a catastrophic sender
# misconfiguration (AR accidentally forwarded a mailing list) without the
# scheduler thread blocking for minutes. Adjust via the env setting if you
# ever need more headroom; the loop is bounded, not infinite.
_INGEST_TICK_CAP_DEFAULT = 500


@dataclass(slots=True)
class IngestedMessage:
    """Pair of the newly-inserted DB row and the Gmail payload we fetched.

    Kept per-tick in memory so classify+process can avoid a second ``fetch_message``
    round-trip. Across ticks we always reconstruct from the DB row.
    """

    row: EmailAutomationMessage
    fetched: gmail_sa.FetchedMessage


@dataclass(slots=True)
class IngestPage:
    """One page of ``messages.list`` after dedup against the DB.

    ``hit_existing`` tells the outer loop "we've caught up to history
    on this page" \u2014 since Gmail returns ids newest-first, an id we
    already have means everything older in subsequent pages is also
    already persisted. Caller breaks the loop.
    """

    fresh: list[IngestedMessage]
    hit_existing: bool
    page_size: int  # how many ids Gmail returned for this page


async def ingest_inbox(
    db: AsyncSession, *, query: str, max_results: int
) -> IngestPage:
    """Fetch one page of matching Gmail ids, skip ones we already have, insert the rest.

    Each insert runs in its own SAVEPOINT so a single duplicate
    ``provider_message_id`` (rare \u2014 the DB pre-check already filters
    most dups, but a concurrent ingest could race) never rolls back
    earlier successful inserts.
    """

    ids: list[str] = await asyncio.to_thread(
        gmail_sa.list_inbox_messages, query, max_results
    )
    if not ids:
        return IngestPage(fresh=[], hit_existing=False, page_size=0)

    # Bulk dedup against the DB so we (a) avoid paying Gmail
    # ``messages.get`` cost for messages we won't insert, and (b)
    # know whether we've caught up to history.
    result = await db.execute(
        select(EmailAutomationMessage.provider_message_id).where(
            EmailAutomationMessage.provider_message_id.in_(ids)
        )
    )
    already: set[str] = {r[0] for r in result.all()}
    new_ids = [i for i in ids if i not in already]
    hit_existing = bool(already)

    fresh: list[IngestedMessage] = []
    for mid in new_ids:
        try:
            fetched = await asyncio.to_thread(gmail_sa.fetch_message, mid)
        except Exception:
            log.exception("ingest: fetch_message failed for %s", mid)
            continue
        row = EmailAutomationMessage(
            provider="gmail",
            provider_message_id=fetched.id,
            thread_id=fetched.thread_id,
            mailbox=settings.email_automation_impersonated_user,
            sender=fetched.sender,
            subject=fetched.subject,
            received_at=(
                datetime.fromtimestamp(fetched.received_at_ms / 1000, tz=timezone.utc)
                if fetched.received_at_ms
                else None
            ),
            status="received",
            attachments=jsonable([a.as_jsonable() for a in fetched.attachments]),
            raw_headers=jsonable(fetched.headers),
        )
        sp = await db.begin_nested()
        db.add(row)
        try:
            await db.flush()
            await sp.commit()
        except IntegrityError:
            # Concurrent ingest landed the same id between our pre-check
            # and our flush \u2014 treat as "we already had it".
            await sp.rollback()
            hit_existing = True
            continue
        fresh.append(IngestedMessage(row=row, fetched=fetched))
    return IngestPage(fresh=fresh, hit_existing=hit_existing, page_size=len(ids))


async def ingest_new_messages(
    db: AsyncSession,
    *,
    query: str | None = None,
    max_results: int = 25,
    tick_cap: int | None = None,
) -> dict[str, Any]:
    """Public ingest-only entry point used by the graph's ``ingest_messages`` node.

    Loops :func:`ingest_inbox` until one of three stop conditions:

      * **Caught up** \u2014 the page contained at least one id we already
        have. Gmail returns ids newest-first, so older pages are also
        already in the DB.
      * **Drained** \u2014 the page was shorter than ``max_results``;
        nothing left in Gmail matching the filter.
      * **Capped** \u2014 the per-tick safety backstop fired. Rare; only
        triggers when a misconfiguration dumps thousands of messages
        on us in one tick.

    Returns a plain-JSON summary so the graph state stays serializable.
    """

    if not settings.email_automation_enabled:
        return {"scanned_ids": [], "message_count": 0, "disabled": True}

    q = query or settings.email_automation_inbox_query
    cap = tick_cap or _INGEST_TICK_CAP_DEFAULT

    all_ids: list[str] = []
    pages = 0
    stop_reason = "unknown"
    while len(all_ids) < cap:
        batch_size = min(max_results, cap - len(all_ids))
        page = await ingest_inbox(db, query=q, max_results=batch_size)
        await db.commit()
        pages += 1

        all_ids.extend(i.fetched.id for i in page.fresh)

        if page.page_size == 0:
            stop_reason = "drained"
            break
        if page.hit_existing:
            # Even if the page also had some new ids (which we just
            # inserted), the presence of a known id proves we're past
            # the "new traffic" frontier. Stop.
            stop_reason = "caught_up"
            break
        if page.page_size < batch_size:
            stop_reason = "drained"
            break
    else:
        stop_reason = "capped"

    sup = (settings.email_automation_supplemental_inbox_query or "").strip()
    if sup and len(all_ids) < cap:
        sup_pages = 0
        sup_stop = "unknown"
        while len(all_ids) < cap:
            batch_size = min(max_results, cap - len(all_ids))
            page = await ingest_inbox(db, query=sup, max_results=batch_size)
            await db.commit()
            sup_pages += 1
            all_ids.extend(i.fetched.id for i in page.fresh)
            if page.page_size == 0:
                sup_stop = "drained"
                break
            if page.hit_existing:
                sup_stop = "caught_up"
                break
            if page.page_size < batch_size:
                sup_stop = "drained"
                break
        else:
            sup_stop = "capped"
        if sup_pages:
            log.info(
                "email_automation ingest supplemental: pages=%d ids_total=%d stop=%s",
                sup_pages,
                len(all_ids),
                sup_stop,
            )

    if pages > 1 or stop_reason == "capped":
        log.info(
            "email_automation ingest: pages=%d ids=%d stop=%s",
            pages, len(all_ids), stop_reason,
        )

    return {
        "scanned_ids": all_ids,
        "message_count": len(all_ids),
        "stop_reason": stop_reason,
        "disabled": False,
        "supplemental_query_configured": bool(sup),
    }


__all__ = ["IngestedMessage", "IngestPage", "ingest_inbox", "ingest_new_messages"]
