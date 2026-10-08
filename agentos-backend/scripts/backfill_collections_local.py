#!/usr/bin/env python3
"""Temporary local backfill for collections Path C prerequisites + classification.

Reuses production helpers:

* ``gmail_sa.fetch_message_thread_id`` — same as dispatch after send.
* ``process_collections_intelligence_batch`` — same Path C batch as cron/graph.
* ``classify_collections_thread`` — per-thread Path C (same as production) for
  ``--classify-anchors`` when you have no ``received`` rows with ``thread_id``.

Run from ``agentos-backend/`` (loads ``.env`` from this directory)::

  export PYTHONPATH=.
  python scripts/backfill_collections_local.py --sends --classify
  python scripts/backfill_collections_local.py --classify-anchors --skip-alembic

To point at a **different Postgres** than ``DATABASE_URL`` in ``.env`` (e.g. remote
backfill), set ``BACKFILL_DATABASE_URL`` to an async SQLAlchemy URL (same format as
``DATABASE_URL``, typically ``postgresql+asyncpg://...``). It is applied **after**
``.env`` is loaded and overrides ``settings.database_url`` before the DB engine is
created — Gmail / OpenAI keys still come from ``.env``.

Steps:

1. By default: ``alembic upgrade head`` (idempotent; skip with ``--skip-alembic``).
2. ``--sends``: fill ``email_automation_sends.gmail_thread_id`` for ``sent`` rows
   with ``provider_message_id`` and NULL thread id (Gmail minimal get).
3. ``--classify``: loop ``process_collections_intelligence_batch`` until idle /
   no-progress cap (requires ``EMAIL_AUTOMATION_COLLECTIONS_INTELLIGENCE_ENABLED`` in ``.env``).
   If the global kill switch is off, pass ``--force-classify`` so Path C still runs for this
   script only (does not rewrite ``.env``). This path is the **same** thread selection as
   production (distinct ``thread_id`` from ``email_automation_messages`` with ``status=received``).
4. ``--classify-anchors``: distinct ``gmail_thread_id`` from ``sent`` sends (with thread id),
   then ``classify_collections_thread`` each — populates ``gmail_intelligence`` even when
   there are no ``received`` inbox rows (**not** the same work queue as production Path C).
   Optional ``--classify-anchors-only-missing`` skips
   threads that already have a ``collections_reply`` row.
5. ``--reclassify-all``: every ``gmail_thread_id`` that already has a ``collections_reply``
   row is re-classified with the normal ``classify_collections_thread`` (production code
   unchanged). The script clears ``trigger_message_id`` on that row in-session first so the
   usual ``unchanged_trigger`` skip does not fire; the upsert then writes fresh classification.
   Use after prompt changes. Requires Gmail + OpenAI; rate is one LLM call per thread.
6. ``--classify-received-subject SUBSTRING``: distinct ``thread_id`` from
   ``email_automation_messages`` whose ``subject`` ILIKE ``'%' || SUBSTRING || '%'``
   (case-insensitive; matches ``Re:``, ``Fwd:``, ``FW:``, etc. anywhere in the subject;
   ``%`` / ``_`` in SUBSTRING are escaped). Default message ``status`` filter is
   ``received`` only; override with ``--classify-subject-message-statuses`` (comma
   list, e.g. ``received,skipped,processed``). Optional ``--classify-received-subject-only-missing``
   skips ``thread_id`` values that already have a ``collections_reply`` row. Requires a
   **sent** row on ``email_automation_sends`` with ``gmail_thread_id`` equal to that
   ``thread_id``. Runs ``classify_collections_thread`` only (Path C / ``gmail_intelligence``
   — does **not** run Path R). Respects ``unchanged_trigger`` like prod.

**Order:** backfill sends before classify, or many threads will stay
``no_matching_sent``.

Do not commit secrets. Delete this script when backfill is done if you prefer
not to keep one-off tooling in the tree.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from sqlalchemy import select, update  # noqa: E402

from app.config.settings import settings  # noqa: E402

_bf_url = (os.environ.get("BACKFILL_DATABASE_URL") or "").strip()
if _bf_url:
    object.__setattr__(settings, "database_url", _bf_url)

from app.db.models import (  # noqa: E402
    EmailAutomationMessage,
    EmailAutomationSend,
    GmailIntelligence,
)
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.email_automation import gmail_sa  # noqa: E402
from app.email_automation.collections_llm import COLLECTIONS_REPLY_KIND  # noqa: E402
from app.email_automation.pipeline.collections_intelligence import (  # noqa: E402
    classify_collections_thread,
    process_collections_intelligence_batch,
)

log = logging.getLogger("backfill_collections_local")


def _run_alembic_upgrade() -> None:
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        check=True,
    )


async def _backfill_send_thread_ids(*, max_rows: int, dry_run: bool) -> int:
    """Return number of rows updated (0 in dry-run)."""

    updated = 0
    async with AsyncSessionLocal() as db:
        stmt = (
            select(EmailAutomationSend)
            .where(
                EmailAutomationSend.status == "sent",
                EmailAutomationSend.gmail_thread_id.is_(None),
                EmailAutomationSend.provider_message_id.isnot(None),
            )
            .limit(max_rows)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        log.info("sends: candidate rows=%d (dry_run=%s)", len(rows), dry_run)

        commit_every = 10
        pending = 0
        for row in rows:
            mid = (row.provider_message_id or "").strip()
            if not mid:
                continue
            tid = await asyncio.to_thread(gmail_sa.fetch_message_thread_id, mid)
            if dry_run:
                log.info(
                    "  dry-run send_id=%s provider_message_id=%s -> thread_id=%r",
                    row.id,
                    mid,
                    tid,
                )
                continue
            row.gmail_thread_id = (tid or "").strip() or None
            if tid:
                updated += 1
            else:
                log.warning(
                    "sends: no thread_id from Gmail for send_id=%s message_id=%s",
                    row.id,
                    mid,
                )
            pending += 1
            if not dry_run and pending >= commit_every:
                await db.commit()
                pending = 0
        if not dry_run and pending:
            await db.commit()
    return updated if not dry_run else 0


_MESSAGE_STATUS_ALLOW = frozenset(
    {
        "received",
        "skipped",
        "processed",
        "failed",
        "classified",
        "processed_with_errors",
    }
)


def _parse_message_statuses(raw: str) -> tuple[str, ...]:
    """Comma-separated statuses for subject-based Path C; must be allowlisted."""

    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if not parts:
        return ("received",)
    bad = [p for p in parts if p not in _MESSAGE_STATUS_ALLOW]
    if bad:
        raise ValueError(
            f"Invalid message status(es) {bad!r}; "
            f"allowed: {', '.join(sorted(_MESSAGE_STATUS_ALLOW))}"
        )
    return tuple(parts)


def _ilike_pattern(substring: str) -> str:
    """Wrap user text for ILIKE; escape SQL wildcards."""
    esc = substring.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


async def _classify_subject_threads_with_sent_anchor(
    *,
    subject_substring: str,
    message_statuses: tuple[str, ...],
    max_threads: int,
    force_automation_enabled: bool,
    only_missing: bool,
) -> dict[str, int]:
    """Path C per thread: messages matching subject (+ status filter) + sent on same thread."""

    if not settings.email_automation_collections_intelligence_enabled:
        log.error(
            "classify-received-subject: "
            "EMAIL_AUTOMATION_COLLECTIONS_INTELLIGENCE_ENABLED=false; abort."
        )
        return {"threads": 0, "classified": 0, "skipped": 0, "errors": 0}

    prev_auto: bool | None = None
    if force_automation_enabled:
        prev_auto = settings.email_automation_enabled
        object.__setattr__(settings, "email_automation_enabled", True)
        log.warning(
            "classify-received-subject: --force-classify set; temporarily "
            "email_automation_enabled=True (restore after run; .env unchanged)."
        )

    pat = _ilike_pattern(subject_substring.strip())
    classified = 0
    skipped = 0
    errors = 0
    no_anchor = 0
    no_inbound = 0
    thread_ids: list[str] = []
    try:
        async with AsyncSessionLocal() as db:
            if not settings.email_automation_enabled:
                log.error(
                    "classify-received-subject: EMAIL_AUTOMATION_ENABLED=false; "
                    "pass --force-classify or enable in .env."
                )
                return {"threads": 0, "classified": 0, "skipped": 0, "errors": 0}

            stmt = (
                select(EmailAutomationMessage.thread_id)
                .join(
                    EmailAutomationSend,
                    EmailAutomationSend.gmail_thread_id == EmailAutomationMessage.thread_id,
                )
                .where(
                    EmailAutomationSend.status == "sent",
                    EmailAutomationMessage.status.in_(message_statuses),
                    EmailAutomationMessage.thread_id.isnot(None),
                    EmailAutomationMessage.subject.isnot(None),
                    EmailAutomationMessage.subject.ilike(pat, escape="\\"),
                )
                .distinct()
            )
            if only_missing:
                existing_threads = select(GmailIntelligence.gmail_thread_id).where(
                    GmailIntelligence.kind == COLLECTIONS_REPLY_KIND,
                )
                stmt = stmt.where(~EmailAutomationMessage.thread_id.in_(existing_threads))
            stmt = stmt.limit(max_threads)
            thread_ids = [
                str(r[0]).strip()
                for r in (await db.execute(stmt)).all()
                if r[0] and str(r[0]).strip()
            ]
            log.info(
                "classify-received-subject: threads=%d substring=%r statuses=%s only_missing=%s",
                len(thread_ids),
                subject_substring.strip(),
                message_statuses,
                only_missing,
            )
            for tid in thread_ids:
                try:
                    async with db.begin_nested():
                        out = await classify_collections_thread(db, tid)
                    if out.get("ok") and not out.get("skipped"):
                        classified += 1
                    elif out.get("skipped"):
                        skipped += 1
                    else:
                        reason = out.get("reason") or ""
                        if reason == "no_matching_sent":
                            no_anchor += 1
                        elif reason in ("no_inbound", "empty_thread"):
                            no_inbound += 1
                        else:
                            skipped += 1
                except Exception:
                    errors += 1
                    log.exception("classify-received-subject: thread %s failed", tid)
            await db.commit()
    finally:
        if force_automation_enabled and prev_auto is not None:
            object.__setattr__(settings, "email_automation_enabled", prev_auto)

    summary = {
        "threads": len(thread_ids),
        "classified": classified,
        "skipped": skipped,
        "errors": errors,
        "no_anchor": no_anchor,
        "no_inbound_or_empty": no_inbound,
    }
    log.info("classify-received-subject: done %s", summary)
    return summary


async def _run_classify_rounds(
    *,
    max_rounds: int,
    no_progress_limit: int,
    force_automation_enabled: bool,
) -> None:
    prev_auto: bool | None = None
    if force_automation_enabled:
        prev_auto = settings.email_automation_enabled
        object.__setattr__(settings, "email_automation_enabled", True)
        log.warning(
            "classify: --force-classify set; temporarily treating email_automation_enabled=True "
            "(restore after run; .env unchanged)."
        )
    try:
        no_progress = 0
        for rnd in range(1, max_rounds + 1):
            async with AsyncSessionLocal() as db:
                summary = await process_collections_intelligence_batch(db)
            log.info("classify round %d: %s", rnd, summary)

            if summary.get("disabled"):
                log.error(
                    "Email automation disabled (EMAIL_AUTOMATION_ENABLED=false); abort classify."
                )
                return
            if summary.get("skipped") == "feature_disabled":
                log.error(
                    "Collections intelligence disabled "
                    "(EMAIL_AUTOMATION_COLLECTIONS_INTELLIGENCE_ENABLED=false); abort classify."
                )
                return

            considered = int(summary.get("threads_considered") or 0)
            if considered == 0:
                log.info("classify: no more threads in batch window; done.")
                return

            moved = int(summary.get("threads_classified") or 0) + int(
                summary.get("errors") or 0
            )
            if moved == 0:
                no_progress += 1
                if no_progress >= no_progress_limit:
                    log.warning(
                        "classify: stopping after %d no-progress rounds "
                        "(likely skipped unchanged / no anchor — run --sends first or check data).",
                        no_progress_limit,
                    )
                    return
            else:
                no_progress = 0

        log.warning("classify: stopped after max_rounds=%d", max_rounds)
    finally:
        if force_automation_enabled and prev_auto is not None:
            object.__setattr__(settings, "email_automation_enabled", prev_auto)


async def _distinct_anchor_thread_ids(
    db, *, limit: int, only_missing: bool
) -> list[str]:
    stmt = (
        select(EmailAutomationSend.gmail_thread_id)
        .where(
            EmailAutomationSend.status == "sent",
            EmailAutomationSend.gmail_thread_id.isnot(None),
        )
        .distinct()
        .limit(limit)
    )
    raw = [str(r[0]).strip() for r in (await db.execute(stmt)).all() if r[0]]
    if not only_missing:
        return raw
    ex_stmt = select(GmailIntelligence.gmail_thread_id).where(
        GmailIntelligence.kind == COLLECTIONS_REPLY_KIND,
    )
    existing = {str(r[0]) for r in (await db.execute(ex_stmt)).all() if r[0]}
    return [t for t in raw if t not in existing]


async def _backfill_classify_anchor_threads(
    *,
    max_threads: int,
    only_missing: bool,
) -> dict[str, int]:
    """Run ``classify_collections_thread`` for distinct sent-anchor thread ids."""

    classified = 0
    skipped = 0
    errors = 0
    no_anchor = 0
    no_inbound = 0
    async with AsyncSessionLocal() as db:
        thread_ids = await _distinct_anchor_thread_ids(
            db, limit=max_threads, only_missing=only_missing
        )
        log.info(
            "classify-anchors: threads_to_process=%d (only_missing=%s)",
            len(thread_ids),
            only_missing,
        )
        for tid in thread_ids:
            try:
                async with db.begin_nested():
                    out = await classify_collections_thread(db, tid)
                if out.get("ok") and not out.get("skipped"):
                    classified += 1
                elif out.get("skipped"):
                    skipped += 1
                else:
                    reason = out.get("reason") or ""
                    if reason == "no_matching_sent":
                        no_anchor += 1
                    elif reason in ("no_inbound", "empty_thread"):
                        no_inbound += 1
                    else:
                        skipped += 1
            except Exception:
                errors += 1
                log.exception("classify-anchors: thread %s failed", tid)
        await db.commit()
    summary = {
        "threads": len(thread_ids),
        "classified": classified,
        "skipped": skipped,
        "errors": errors,
        "no_anchor": no_anchor,
        "no_inbound_or_empty": no_inbound,
    }
    log.info("classify-anchors: done %s", summary)
    return summary


async def _reclassify_all_existing(*, max_threads: int) -> dict[str, int]:
    """Re-run Path C for every thread that already has a collections_reply row (prompt backfill)."""

    classified = 0
    skipped = 0
    errors = 0
    no_anchor = 0
    no_inbound = 0
    async with AsyncSessionLocal() as db:
        stmt = (
            select(GmailIntelligence.gmail_thread_id)
            .where(GmailIntelligence.kind == COLLECTIONS_REPLY_KIND)
            .distinct()
            .limit(max_threads)
        )
        thread_ids = [str(r[0]).strip() for r in (await db.execute(stmt)).all() if r[0]]
        log.info(
            "reclassify-all: threads_to_process=%d (cap=%d)",
            len(thread_ids),
            max_threads,
        )
        for tid in thread_ids:
            try:
                async with db.begin_nested():
                    await db.execute(
                        update(GmailIntelligence)
                        .where(
                            GmailIntelligence.kind == COLLECTIONS_REPLY_KIND,
                            GmailIntelligence.gmail_thread_id == tid,
                        )
                        .values(trigger_message_id=None)
                    )
                    await db.flush()
                    out = await classify_collections_thread(db, tid)
                if out.get("ok") and not out.get("skipped"):
                    classified += 1
                elif out.get("skipped"):
                    skipped += 1
                else:
                    reason = out.get("reason") or ""
                    if reason == "no_matching_sent":
                        no_anchor += 1
                    elif reason in ("no_inbound", "empty_thread"):
                        no_inbound += 1
                    else:
                        skipped += 1
            except Exception:
                errors += 1
                log.exception("reclassify-all: thread %s failed", tid)
        await db.commit()
    summary = {
        "threads": len(thread_ids),
        "classified": classified,
        "skipped": skipped,
        "errors": errors,
        "no_anchor": no_anchor,
        "no_inbound_or_empty": no_inbound,
    }
    log.info("reclassify-all: done %s", summary)
    return summary


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if (
        not args.sends
        and not args.classify
        and not args.classify_anchors
        and not args.reclassify_all
        and not (args.classify_received_subject or "").strip()
    ):
        log.error(
            "Nothing to do: pass --sends, --classify, --classify-anchors, --reclassify-all, "
            "and/or --classify-received-subject SUBSTRING"
        )
        return 2

    subj_arg = (args.classify_received_subject or "").strip()
    if args.classify_received_subject is not None and not subj_arg:
        log.error("--classify-received-subject requires a non-empty substring")
        return 2

    try:
        subject_message_statuses = _parse_message_statuses(
            args.classify_subject_message_statuses or ""
        )
    except ValueError as e:
        log.error("%s", e)
        return 2

    if not args.skip_alembic:
        log.info("Running: alembic upgrade head (cwd=%s)", _ROOT)
        _run_alembic_upgrade()
        log.info("Alembic upgrade head finished.")

    log.info(
        "Settings snapshot: email_automation_enabled=%s collections_enabled=%s",
        settings.email_automation_enabled,
        settings.email_automation_collections_intelligence_enabled,
    )

    if args.sends:
        n = await _backfill_send_thread_ids(
            max_rows=args.max_sends,
            dry_run=args.dry_run,
        )
        log.info("sends: rows with thread_id set (excl. dry-run) ≈ %d", n)

    if args.classify:
        await _run_classify_rounds(
            max_rounds=args.classify_max_rounds,
            no_progress_limit=args.classify_no_progress_rounds,
            force_automation_enabled=args.force_classify,
        )

    if args.classify_anchors:
        await _backfill_classify_anchor_threads(
            max_threads=args.max_anchor_threads,
            only_missing=args.classify_anchors_only_missing,
        )

    if args.reclassify_all:
        await _reclassify_all_existing(max_threads=args.max_reclassify)

    if subj_arg:
        await _classify_subject_threads_with_sent_anchor(
            subject_substring=subj_arg,
            message_statuses=subject_message_statuses,
            max_threads=args.max_received_subject_threads,
            force_automation_enabled=args.force_classify,
            only_missing=args.classify_received_subject_only_missing,
        )

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--skip-alembic",
        action="store_true",
        help="Do not run alembic upgrade head before work (default: upgrade runs once).",
    )
    p.add_argument("--sends", action="store_true", help="Backfill gmail_thread_id on sent rows.")
    p.add_argument(
        "--classify",
        action="store_true",
        help="Run Path C batches (process_collections_intelligence_batch) until idle.",
    )
    p.add_argument(
        "--force-classify",
        action="store_true",
        help="With --classify or --classify-received-subject: temporarily set "
        "email_automation_enabled=True in-process so Path C runs even when "
        "EMAIL_AUTOMATION_ENABLED=false in .env (kill switch stays off on disk).",
    )
    p.add_argument(
        "--classify-received-subject",
        metavar="SUBSTRING",
        default=None,
        help="Path C only: distinct thread_id from messages whose subject contains SUBSTRING "
        "(ILIKE %%SUBSTRING%% — case-insensitive; matches Re:/Fwd:/FW: prefixes because the "
        "phrase can appear anywhere). Requires sent anchor on same thread. Default statuses: "
        "received only; add skipped etc. via --classify-subject-message-statuses.",
    )
    p.add_argument(
        "--classify-subject-message-statuses",
        metavar="CSV",
        default="received",
        help="Comma-separated email_automation_messages.status values for "
        "--classify-received-subject (default: received). Example: "
        "received,skipped,processed,processed_with_errors,classified,failed",
    )
    p.add_argument(
        "--classify-received-subject-only-missing",
        action="store_true",
        help="With --classify-received-subject: skip gmail_thread_id that already have a "
        "collections_reply gmail_intelligence row.",
    )
    p.add_argument(
        "--max-received-subject-threads",
        type=int,
        default=5000,
        help="Max distinct threads for --classify-received-subject (default 5000).",
    )
    p.add_argument(
        "--classify-anchors",
        action="store_true",
        help="Populate gmail_intelligence by classifying distinct gmail_thread_id from sent "
        "sends (uses classify_collections_thread — same as production).",
    )
    p.add_argument(
        "--classify-anchors-only-missing",
        action="store_true",
        help="With --classify-anchors: skip threads that already have a collections_reply row.",
    )
    p.add_argument(
        "--max-anchor-threads",
        type=int,
        default=2000,
        help="Max distinct threads for --classify-anchors (default 2000).",
    )
    p.add_argument(
        "--reclassify-all",
        action="store_true",
        help="Re-classify every gmail_thread_id that already has a collections_reply row "
        "(script clears trigger_message_id so production unchanged-trigger skip is bypassed).",
    )
    p.add_argument(
        "--max-reclassify",
        type=int,
        default=50_000,
        help="Max threads for --reclassify-all (default 50000).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="For --sends: log Gmail thread ids only; no DB writes.",
    )
    p.add_argument("--max-sends", type=int, default=500, help="Max send rows to touch.")
    p.add_argument(
        "--classify-max-rounds",
        type=int,
        default=80,
        help="Max Path C batch invocations (40 threads max each).",
    )
    p.add_argument(
        "--classify-no-progress-rounds",
        type=int,
        default=3,
        help="Stop classify after this many consecutive rounds with 0 classified and 0 errors.",
    )
    args = p.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
