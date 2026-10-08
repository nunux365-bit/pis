#!/usr/bin/env python3
"""Local runner for the **KAM Dashboard** metrics.

Points at the automation Gmail inbox (``automation.agents@1mg.com`` by default),
runs the KAM reply-accuracy analysis (Path K — the SAME code cron runs), then
aggregates and prints the per-KAM dashboard metrics:

    Name | Total Overdue (₹ L) | Reply Rate | Accuracy Rate | Avg Reply time

This script is **read + classify only** — it never sends email, so there is no
client-safety gate. It reuses production helpers verbatim:

* ``process_kam_reply_intelligence_batch`` / ``classify_kam_reply_thread`` (Path K)
* ``load_kam_directory`` (Google-Sheet KAM ↔ HANA + emails)
* ``aggregate_kam_reply_metrics`` (per-KAM rollup over a classified_at window)

Run from ``agentos-backend/`` (loads ``.env`` from this directory)::

  export PYTHONPATH=.
  # Classify recent inbox threads, then print L7D metrics:
  python scripts/kam_dashboard_local.py --force

  # Override the KAM directory sheet / tabs and just print metrics from existing rows:
  python scripts/kam_dashboard_local.py --metrics-only \
      --sheet-id 12hyo3F5UQVml6x5LMWhTY9Y5bUTyLGoS \
      --mapping-tab "KAM Client Mapping" --emails-tab "KAM EmailIDs"

  # Classify from sent-reminder anchors (when there are no `received` inbox rows):
  python scripts/kam_dashboard_local.py --anchors --only-missing --force --window 30d

  # Drop all existing scores and fully recompute (e.g. after a prompt/model change):
  python scripts/kam_dashboard_local.py --reset --anchors

FLAGS OF NOTE
- ``--sheet-id`` / ``--mapping-tab`` / ``--emails-tab`` override the KAM directory
  location (otherwise ``settings.kam_directory_*`` / ``.env`` are used).
- ``--mailbox`` overrides which inbox is impersonated (default: settings value).
- ``--model`` overrides the OpenAI model used for accuracy scoring.
- ``--force`` flips ``email_automation_enabled`` + ``kam_reply_accuracy_enabled`` ON
  **in-process only** (``.env`` is untouched) so Path K runs even when disabled on disk.
- ``--window`` (24h|7d|30d) is the rolling window for the printed metrics (default 7d).
- ``--database-url`` (or ``KAM_DATABASE_URL`` env) points at a different Postgres than
  ``DATABASE_URL`` — handy for reading a remote/staging DB.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from sqlalchemy import desc, select  # noqa: E402

from app.config.settings import settings  # noqa: E402

log = logging.getLogger("kam_dashboard_local")


def _set(name: str, value) -> None:
    """Override a settings field in-process only (pydantic BaseSettings is strict)."""
    object.__setattr__(settings, name, value)


def _apply_overrides(args: argparse.Namespace) -> None:
    """Apply CLI / env overrides to ``settings`` before any helper reads them."""

    db_url = (args.database_url or os.environ.get("KAM_DATABASE_URL") or "").strip()
    if db_url:
        _set("database_url", db_url)
        log.info("DB override: %s", db_url.split("@")[-1])  # host/db only, no creds

    if args.mailbox:
        _set("email_automation_impersonated_user", args.mailbox.strip())
    if args.sheet_id:
        _set("kam_directory_sheet_id", args.sheet_id.strip())
    if args.mapping_tab:
        _set("kam_directory_mapping_tab", args.mapping_tab)
    if args.emails_tab:
        _set("kam_directory_emails_tab", args.emails_tab)
    if args.model:
        _set("kam_reply_accuracy_openai_model", args.model.strip())

    if args.force:
        _set("email_automation_enabled", True)
        _set("kam_reply_accuracy_enabled", True)
        log.warning(
            "--force: email_automation_enabled + kam_reply_accuracy_enabled set TRUE "
            "in-process (.env unchanged)."
        )

    # The directory loader caches per sheet id for 5 min; drop it so overrides take effect.
    from app.email_automation import kam_directory

    kam_directory.clear_cache()


# ---------------------------------------------------------------------------
# Classification (populate gmail_intelligence kam_reply_accuracy rows)
# ---------------------------------------------------------------------------


async def _reset_kam_rows() -> int:
    """Delete every ``kam_reply_accuracy`` row so the next run re-scores from scratch.

    Needed after a prompt/model change: the per-thread ``unchanged_trigger`` skip
    otherwise keeps the old scores. Only the KAM kind is touched — collections
    (``collections_reply``) intelligence is left intact.
    """

    from sqlalchemy import delete
    from app.db.models import GmailIntelligence
    from app.db.session import AsyncSessionLocal
    from app.email_automation.kam_reply_llm import KAM_REPLY_ACCURACY_KIND

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            delete(GmailIntelligence).where(
                GmailIntelligence.kind == KAM_REPLY_ACCURACY_KIND
            )
        )
        await db.commit()
        return res.rowcount or 0


async def _scan_rounds(*, max_rounds: int, no_progress_limit: int) -> None:
    """Loop ``process_kam_reply_intelligence_batch`` over `received` threads until idle."""

    from app.db.session import AsyncSessionLocal
    from app.email_automation.pipeline.kam_reply_intelligence import (
        process_kam_reply_intelligence_batch,
    )

    no_progress = 0
    for rnd in range(1, max_rounds + 1):
        async with AsyncSessionLocal() as db:
            summary = await process_kam_reply_intelligence_batch(db)
        log.info("scan round %d: %s", rnd, summary)

        if summary.get("disabled"):
            log.error("email_automation_enabled is false — pass --force (or enable in .env).")
            return
        if summary.get("skipped") == "feature_disabled":
            log.error("kam_reply_accuracy_enabled is false — pass --force (or enable in .env).")
            return
        if summary.get("skipped") == "empty_directory":
            log.error(
                "KAM directory is empty — check --sheet-id / tabs and that the sheet is "
                "shared with the service account's client_email."
            )
            return

        if int(summary.get("threads_considered") or 0) == 0:
            log.info("scan: no more `received` threads in batch window; done.")
            return
        moved = int(summary.get("threads_scored") or 0) + int(summary.get("errors") or 0)
        if moved == 0:
            no_progress += 1
            if no_progress >= no_progress_limit:
                log.warning(
                    "scan: stopping after %d no-progress rounds (threads already scored / "
                    "unchanged, or not KAM-owned). Try --anchors.",
                    no_progress_limit,
                )
                return
        else:
            no_progress = 0
    log.warning("scan: stopped after max_rounds=%d", max_rounds)


async def _classify_anchor_threads(*, max_threads: int, only_missing: bool) -> dict[str, int]:
    """Score distinct sent-reminder anchor threads via ``classify_kam_reply_thread``.

    Populates kam_reply_accuracy rows even when there are no `received` inbox rows
    (mirrors the collections backfill ``--classify-anchors`` path).
    """

    from app.db.models import EmailAutomationSend, GmailIntelligence
    from app.db.session import AsyncSessionLocal
    from app.email_automation.kam_directory import load_kam_directory
    from app.email_automation.kam_reply_llm import KAM_REPLY_ACCURACY_KIND
    from app.email_automation.pipeline.kam_reply_intelligence import classify_kam_reply_thread

    directory = await asyncio.to_thread(load_kam_directory)
    if directory.is_empty:
        log.error(
            "--anchors: KAM directory is empty — check --sheet-id / tabs and SA sharing."
        )
        return {"threads": 0, "scored": 0, "skipped": 0, "errors": 0}

    scored = skipped = errors = no_owner = no_inbound = 0
    async with AsyncSessionLocal() as db:
        stmt = (
            select(EmailAutomationSend.gmail_thread_id)
            .where(
                EmailAutomationSend.status == "sent",
                EmailAutomationSend.gmail_thread_id.isnot(None),
            )
            .distinct()
            .limit(max_threads)
        )
        thread_ids = [str(r[0]).strip() for r in (await db.execute(stmt)).all() if r[0]]

        if only_missing:
            existing = {
                str(r[0])
                for r in (
                    await db.execute(
                        select(GmailIntelligence.gmail_thread_id).where(
                            GmailIntelligence.kind == KAM_REPLY_ACCURACY_KIND
                        )
                    )
                ).all()
                if r[0]
            }
            thread_ids = [t for t in thread_ids if t not in existing]

        log.info("--anchors: threads_to_process=%d (only_missing=%s)", len(thread_ids), only_missing)
        for tid in thread_ids:
            try:
                async with db.begin_nested():
                    out = await classify_kam_reply_thread(db, tid, directory)
                if out.get("ok") and not out.get("skipped"):
                    scored += 1
                elif out.get("skipped"):
                    skipped += 1
                else:
                    reason = out.get("reason") or ""
                    if reason in ("no_kam_owner", "no_matching_sent"):
                        no_owner += 1
                    elif reason in ("no_inbound", "empty_thread"):
                        no_inbound += 1
                    else:
                        skipped += 1
            except Exception:
                errors += 1
                log.exception("--anchors: thread %s failed", tid)
        await db.commit()

    summary = {
        "threads": len(thread_ids),
        "scored": scored,
        "skipped": skipped,
        "errors": errors,
        "no_kam_owner_or_anchor": no_owner,
        "no_inbound_or_empty": no_inbound,
    }
    log.info("--anchors: done %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Metrics (same computation as GET /intelligence/kam-metrics)
# ---------------------------------------------------------------------------


async def _overdue_by_code(db) -> dict[str, float]:
    """Latest receivables snapshot → ``{HANA_CODE: overdue_lakh}`` (TDS-adjusted)."""

    from app.db.models import ReceivableDashboardSnapshot

    row = (
        await db.execute(
            select(ReceivableDashboardSnapshot)
            .order_by(desc(ReceivableDashboardSnapshot.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {}

    payload = row.payload or {}
    party_root = payload.get("all_parties") or payload.get("top_parties", {}) or {}
    parties = party_root.get("all", []) if isinstance(party_root, dict) else []

    out: dict[str, float] = {}
    for p in parties:
        code = str(p.get("code") or "").strip().upper()
        if not code:
            continue
        out[code] = out.get(code, 0.0) + float(p.get("net_due_lakh", 0.0) or 0.0)
    return out


def _fmt_reply_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{round(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{round(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{round(hours)}h"
    return f"{round(hours / 24)}d"


async def _build_rows(*, window: str) -> list[dict]:
    from app.db.session import AsyncSessionLocal
    from app.email_automation.kam_directory import load_kam_directory
    from app.email_automation.pipeline.kam_metrics import aggregate_kam_reply_metrics

    directory = await asyncio.to_thread(load_kam_directory)
    async with AsyncSessionLocal() as db:
        aggs = await aggregate_kam_reply_metrics(db, window=window)
        overdue = {} if directory.is_empty else await _overdue_by_code(db)

    keys = set(directory.kams_by_key) | set(aggs)
    rows: list[dict] = []
    for key in keys:
        kam = directory.kams_by_key.get(key)
        agg = aggs.get(key)
        name = (kam.name if kam else "") or (agg.kam_name if agg else "") or key
        managed = directory.hanas_for_key(key)
        total_overdue_lakh = round(sum(overdue.get(h, 0.0) for h in managed), 2)
        rows.append(
            {
                "kam_name": name,
                "total_overdue_lakh": total_overdue_lakh,
                "reply_rate": agg.reply_rate if agg else 0.0,
                "accuracy_rate": agg.accuracy_rate if agg else 0.0,
                "avg_reply_seconds": agg.avg_reply_seconds if agg else None,
                "threads_scored": agg.threads_scored if agg else 0,
                "client_replied_threads": agg.client_replied if agg else 0,
                "no_client_reply_threads": agg.no_client_reply_threads if agg else 0,
            }
        )
    rows.sort(key=lambda r: (-r["total_overdue_lakh"], r["kam_name"].lower()))
    return rows


def _print_table(rows: list[dict], *, window: str) -> None:
    print(f"\nKAM Dashboard — window={window}  ({len(rows)} KAM(s))\n")
    if not rows:
        print("  (no KAMs / no scored threads — run classification first, or check the sheet)\n")
        return
    header = (
        f"{'Name':<28}{'Total Overdue (₹L)':>20}{'Reply Rate':>13}{'Accuracy':>11}"
        f"{'Avg Reply':>12}{'No Client Reply':>17}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['kam_name'][:27]:<28}"
            f"{r['total_overdue_lakh']:>20.2f}"
            f"{r['reply_rate']:>12.1f}%"
            f"{r['accuracy_rate']:>10.1f}%"
            f"{_fmt_reply_time(r['avg_reply_seconds']):>12}"
            f"{r['no_client_reply_threads']:>17}"
        )
    print(
        "\n  Reply Rate / Accuracy Rate are computed only over eligible threads "
        "(client replied at least once); 'No Client Reply' threads are excluded "
        "from both and tracked separately.\n"
    )


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.insecure_local_tls:
        from _tls_local import relax_strict_tls_verification

        relax_strict_tls_verification()

    _apply_overrides(args)

    log.info(
        "mailbox=%s sheet_id=%s mapping_tab=%r emails_tab=%r model=%s",
        settings.email_automation_impersonated_user,
        settings.kam_directory_sheet_id,
        settings.kam_directory_mapping_tab,
        settings.kam_directory_emails_tab,
        settings.kam_reply_accuracy_openai_model,
    )

    if args.metrics_only and (args.anchors or args.force or args.reset):
        log.warning(
            "--metrics-only skips classification, so --anchors/--force/--reset are ignored "
            "and NO rows are written. Drop --metrics-only to (re)populate kam_reply_accuracy."
        )

    if args.reset and not args.metrics_only:
        deleted = await _reset_kam_rows()
        log.warning("--reset: deleted %d existing kam_reply_accuracy rows", deleted)

    if not args.metrics_only:
        if args.anchors:
            await _classify_anchor_threads(
                max_threads=args.max_threads, only_missing=args.only_missing
            )
        else:
            await _scan_rounds(
                max_rounds=args.max_rounds,
                no_progress_limit=args.no_progress_rounds,
            )

    rows = await _build_rows(window=args.window)
    if args.json:
        import json

        print(json.dumps({"window": args.window, "rows": rows}, indent=2))
    else:
        _print_table(rows, window=args.window)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prepare + print KAM Dashboard metrics locally.")

    # KAM directory overrides
    p.add_argument("--sheet-id", default=None, help="Override kam_directory_sheet_id.")
    p.add_argument("--mapping-tab", default=None, help="Override the KAM↔HANA mapping tab name.")
    p.add_argument("--emails-tab", default=None, help="Override the KAM↔email tab name.")

    # Mailbox / model / DB
    p.add_argument("--mailbox", default=None, help="Override the impersonated Gmail inbox.")
    p.add_argument("--model", default=None, help="Override the OpenAI accuracy model.")
    p.add_argument("--database-url", default=None, help="Override DATABASE_URL (async URL).")

    # Run mode
    p.add_argument(
        "--reset",
        action="store_true",
        help="DELETE all existing kam_reply_accuracy rows first, then re-classify from "
        "scratch (forces a full re-score after a prompt/model change). Combine with "
        "--anchors, e.g. `--reset --anchors`. Ignored under --metrics-only.",
    )
    p.add_argument(
        "--metrics-only",
        action="store_true",
        help="Skip classification; just aggregate + print existing kam_reply_accuracy rows.",
    )
    p.add_argument(
        "--anchors",
        action="store_true",
        help="Classify distinct sent-reminder anchor threads (use when there are no "
        "`received` inbox rows). Default mode classifies `received` threads.",
    )
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="With --anchors: skip threads that already have a kam_reply_accuracy row.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Set email_automation_enabled + kam_reply_accuracy_enabled TRUE in-process "
        "(.env untouched) so Path K runs even when disabled on disk.",
    )
    p.add_argument(
        "--window",
        choices=("24h", "7d", "30d"),
        default="7d",
        help="Rolling classified_at window for the printed metrics (default 7d / L7D).",
    )

    # Caps
    p.add_argument("--max-rounds", type=int, default=80, help="Max scan batch rounds (40 threads each).")
    p.add_argument("--no-progress-rounds", type=int, default=3, help="Stop after N idle scan rounds.")
    p.add_argument("--max-threads", type=int, default=2000, help="Max anchor threads for --anchors.")

    p.add_argument("--json", action="store_true", help="Print metrics as JSON instead of a table.")

    p.add_argument(
        "--insecure-local-tls",
        action="store_true",
        help="DEV ONLY: relax strict X.509 verification (VERIFY_X509_STRICT) so outbound "
        "HTTPS works behind a corporate TLS-intercepting proxy whose CA cert isn't marked "
        "critical. Cert verification stays ON. Never pass this in production. Default: off.",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_async_main(_parse_args())))
