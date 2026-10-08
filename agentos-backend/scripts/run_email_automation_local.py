#!/usr/bin/env python3
"""One-shot local runner for the email-automation pipeline.

Drives the **same** unified LangGraph that cron uses
(:func:`app.agents.email_automation.run_scan_async` / ``run_dispatch_async``),
so there is no split-brain between what you test here and what production runs.
The graph opens its own short-lived DB sessions per node — this script just
invokes it once (or loops) instead of waiting for the scheduler interval.

Run from ``agentos-backend/`` (loads ``.env`` from this directory)::

  export PYTHONPATH=.
  python scripts/run_email_automation_local.py            # one scan + one dispatch
  python scripts/run_email_automation_local.py --scan     # scan only
  python scripts/run_email_automation_local.py --dispatch # dispatch only
  python scripts/run_email_automation_local.py --loop 3   # 3 scan+dispatch cycles, 5s apart

CLIENT-SAFETY GUARD
-------------------
By default this script REFUSES to run unless test-mode redirect is active:

  * ``EMAIL_AUTOMATION_TEST_MODE=true``        — redirects every TO/CC/BCC
  * ``EMAIL_AUTOMATION_TEST_REDIRECT_TO=...``  — non-empty target mailbox

With those set, no email can reach a real client — every send is rewritten to
the redirect mailbox with a ``[TEST]`` subject and a "would have gone to…"
banner (see ``app/email_automation/engine/sender.py``).

To send for real (DANGER — mail goes to actual clients) you must BOTH flip
``EMAIL_AUTOMATION_TEST_MODE=false`` in ``.env`` AND pass ``--allow-live`` here.
The double gate is intentional.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("run_email_automation_local")


def _check_safety(allow_live: bool) -> None:
    """Abort unless client-safety rails are in place (or --allow-live was passed)."""

    # Imported here so .env (merged into os.environ by app.config.settings) is read first.
    from app.config.settings import settings

    if not settings.email_automation_enabled:
        sys.exit(
            "EMAIL_AUTOMATION_ENABLED is false — the pipeline no-ops. "
            "Set EMAIL_AUTOMATION_ENABLED=true in .env first."
        )

    test_mode = settings.email_automation_test_mode
    redirect = (settings.email_automation_test_redirect_to or "").strip()

    if test_mode:
        if not redirect:
            sys.exit(
                "EMAIL_AUTOMATION_TEST_MODE=true but EMAIL_AUTOMATION_TEST_REDIRECT_TO "
                "is empty — sender.send() would raise. Set a redirect mailbox in .env."
            )
        log.info(
            "SAFE: test mode ON — all mail redirects to %r (no client will be emailed).",
            redirect,
        )
        return

    # test_mode is OFF — real recipients. Require the explicit override flag.
    if not allow_live:
        sys.exit(
            "REFUSING TO RUN: EMAIL_AUTOMATION_TEST_MODE=false means mail goes to "
            "REAL CLIENTS. If that is truly what you want, re-run with --allow-live."
        )
    log.warning(
        "LIVE MODE: test mode is OFF and --allow-live was passed. "
        "Real clients WILL receive email. send_from=%r",
        settings.email_automation_send_from or "(impersonated user default)",
    )


async def _scan(max_results: int, query: str | None) -> None:
    from app.agents.email_automation import run_scan_async

    if query:
        log.info("scan: overriding inbox_query with %r", query)
    out = await run_scan_async(max_results=max_results, query=query)
    if out.get("disabled"):
        log.info("scan: pipeline disabled — nothing ran")
        return
    result = out.get("result") or {}
    coll = out.get("collections_result") or {}
    log.info(
        "scan done: ingested=%d messages=%d sends=%d collections_classified=%s",
        len(out.get("ingested") or []),
        result.get("message_count", 0),
        result.get("send_count", 0),
        coll.get("threads_classified"),
    )


async def _dispatch(limit: int) -> None:
    from app.agents.email_automation import run_dispatch_async

    out = await run_dispatch_async(limit=limit)
    if out.get("disabled"):
        log.info("dispatch: pipeline disabled — nothing ran")
        return
    reclaim = out.get("reclaim") or {}
    log.info(
        "dispatch done: sent=%d failed=%d reclaim_requeued=%d reclaim_completed=%d",
        len(out.get("sent_ids") or []),
        len(out.get("failed") or []),
        int(reclaim.get("requeued") or 0),
        int(reclaim.get("completed") or 0),
    )


async def _main(args: argparse.Namespace) -> None:
    do_scan = args.scan or not args.dispatch
    do_dispatch = args.dispatch or not args.scan

    for i in range(args.loop):
        if args.loop > 1:
            log.info("── cycle %d/%d ──", i + 1, args.loop)
        if do_scan:
            await _scan(args.max_results, args.query)
        if do_dispatch:
            await _dispatch(args.limit)
        if args.loop > 1 and i < args.loop - 1:
            await asyncio.sleep(args.interval)


def _parse_args() -> argparse.Namespace:
    from app.email_automation.pipeline import DEFAULT_DISPATCH_SEND_BATCH

    p = argparse.ArgumentParser(description="Run the email-automation pipeline locally.")
    p.add_argument("--scan", action="store_true", help="run scan only")
    p.add_argument("--dispatch", action="store_true", help="run dispatch only")
    p.add_argument("--max-results", type=int, default=25, help="Gmail messages.list cap per scan")
    p.add_argument(
        "--query",
        type=str,
        default=None,
        help="override the primary inbox_query for this scan only "
        "(e.g. 'from:bhawna.gandhi@1mg.com subject:Receivable newer_than:30d')",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_DISPATCH_SEND_BATCH,
        help="max send rows claimed per dispatch tick",
    )
    p.add_argument("--loop", type=int, default=1, help="number of scan+dispatch cycles")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between loop cycles")
    p.add_argument(
        "--allow-live",
        action="store_true",
        help="DANGER: permit running when EMAIL_AUTOMATION_TEST_MODE=false (mail to real clients)",
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
    _check_safety(ns.allow_live)
    asyncio.run(_main(ns))
