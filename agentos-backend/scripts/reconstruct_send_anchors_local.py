#!/usr/bin/env python3
"""Reconstruct ``email_automation_sends`` anchor rows from the live mailbox.

WHY: the Reply Tracker (collections reply intelligence, Path C) classifies a
Gmail thread only when an ``email_automation_sends`` row with ``status='sent'``
anchors that thread — and it reads the ``business_key`` from that anchor. Those
rows are normally created by dispatch (actually sending mail). This script
rebuilds them **by reading the mailbox only — it sends nothing** — so the Reply
Tracker can be populated locally after a truncate without dispatching email.

HOW: searches ``settings.email_automation_impersonated_user`` for reminder
messages (subject ``Payment Reminder – Overdue Invoices | Tata1mg | <key>``),
groups by Gmail ``threadId``, parses the ``business_key`` from the subject's
trailing token, and inserts one synthetic ``sent`` anchor per thread. Then run::

    python scripts/backfill_collections_local.py --classify-anchors --force-classify --skip-alembic

to classify the anchored threads → ``collections_reply`` intelligence → stats.

Run from ``agentos-backend/`` with ``PYTHONPATH=.``::

    python scripts/reconstruct_send_anchors_local.py            # default cap 50 threads
    python scripts/reconstruct_send_anchors_local.py --limit 0  # no cap (all threads)

These rows are RECONSTRUCTED (synthetic), distinguishable by ``test_mode=True``
and a ``recon::`` ``dedupe_key`` prefix, so a later real dispatch won't collide.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import uuid

from sqlalchemy.dialects.postgresql import insert

from app.config.settings import settings
from app.db.models import EmailAutomationSend
from app.db.session import AsyncSessionLocal
from app.email_automation import gmail_sa
from app.email_automation.pipeline._shared import now_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("reconstruct_send_anchors")

WORKFLOW_TYPE = "PAYMENT_REMINDER_WEEKLY"
# Matches the reminder subject template:
#   "Payment Reminder – Overdue Invoices | Tata1mg | <business_key>"
# (also matches "Re:/Fwd:" replies — same trailing token). Token after the last
# "Tata1mg |" is the HANA business_key.
_SUBJECT_KEY_RE = re.compile(r"Tata1mg\s*\|\s*([A-Za-z0-9._\-/]+)\s*$")
# Conservative Gmail wire query — phrase match tolerates punctuation/Re: prefixes.
_SEARCH_QUERY = 'subject:"Payment Reminder" subject:"Tata1mg" newer_than:2y'


def _infer_variant(business_key: str) -> str:
    """Best-effort variant from the HANA code shape (cosmetic for the tracker)."""
    k = business_key.upper()
    if k.startswith("1MGHC"):
        return "chw"
    return "epharma"


def _ms_to_dt(ms: int | None):
    if not ms:
        return now_utc()
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def main(limit: int) -> None:
    mailbox = settings.email_automation_impersonated_user
    cap = None if limit <= 0 else limit
    log.info("reading mailbox=%s query=%r (thread cap=%s)", mailbox, _SEARCH_QUERY, cap or "none")

    # Pull a generous id set; we dedupe to distinct threads below.
    max_ids = 50_000 if cap is None else max(cap * 20, 200)
    ids = await asyncio.to_thread(gmail_sa.list_inbox_messages, _SEARCH_QUERY, max_ids)
    log.info("messages matched: %d", len(ids))

    # thread_id -> (business_key, variant, sent_at, sample_subject)
    anchors: dict[str, tuple[str, str, object, str]] = {}
    seen_threads_order: list[str] = []
    for mid in ids:
        if cap is not None and len(anchors) >= cap:
            break
        try:
            fm = await asyncio.to_thread(gmail_sa.fetch_message, mid)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_message(%s) failed: %s", mid, exc)
            continue
        tid = (fm.thread_id or "").strip()
        subj = (fm.subject or "").strip()
        if not tid or tid in anchors:
            continue
        m = _SUBJECT_KEY_RE.search(subj)
        if not m:
            continue  # subject without a parseable business_key — skip
        business_key = m.group(1).strip()
        anchors[tid] = (business_key, _infer_variant(business_key), _ms_to_dt(fm.received_at_ms), subj)
        seen_threads_order.append(tid)

    log.info("distinct reminder threads with a business_key: %d", len(anchors))
    if not anchors:
        log.info("nothing to reconstruct — done")
        return

    now = now_utc()
    inserted = 0
    async with AsyncSessionLocal() as db:
        for tid in seen_threads_order:
            business_key, variant, sent_at, _subj = anchors[tid]
            dedupe_key = f"recon::{WORKFLOW_TYPE}::{variant}::{business_key}::{tid}"[:128]
            row = {
                "id": uuid.uuid4(),
                "workflow_type": WORKFLOW_TYPE,
                "variant": variant,
                "business_key": business_key,
                "period_key": sent_at.strftime("%Y-%m"),
                "dedupe_key": dedupe_key,
                "status": "sent",
                "gmail_thread_id": tid,
                "sent_at": sent_at,
                "test_mode": True,  # mark as reconstructed, not a real dispatch
                "created_at": now,
                "updated_at": now,
            }
            stmt = insert(EmailAutomationSend).values(**row)
            # Idempotent re-runs: dedupe_key is unique.
            stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
            res = await db.execute(stmt)
            inserted += res.rowcount or 0
        await db.commit()

    log.info(
        "done: reconstructed_anchors_inserted=%d (threads_seen=%d). "
        "Next: classify with `backfill_collections_local.py --classify-anchors --force-classify --skip-alembic`",
        inserted,
        len(anchors),
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max distinct threads to reconstruct (default 50). Use 0 for no cap.",
    )
    p.add_argument(
        "--insecure-local-tls",
        action="store_true",
        help="DEV ONLY: relax strict X.509 verification (VERIFY_X509_STRICT) so outbound "
        "HTTPS works behind a corporate TLS-intercepting proxy whose CA cert isn't marked "
        "critical. Cert verification stays ON. Never pass this in production. Default: off.",
    )
    return p.parse_args()


if __name__ == "__main__":
    ns = _parse_args()
    if ns.insecure_local_tls:
        from _tls_local import relax_strict_tls_verification

        relax_strict_tls_verification()
    asyncio.run(main(ns.limit))
