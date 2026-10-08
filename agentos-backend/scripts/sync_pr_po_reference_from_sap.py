#!/usr/bin/env python3
"""Sync SAP OData master data into ``pr_po_reference_values``.

Usage (from ``agentos-backend/`` on the app server)::

  # Yesterday delta only (cron uses --mode full; use daily for manual incremental runs)
  python scripts/sync_pr_po_reference_from_sap.py --mode daily
  python scripts/sync_pr_po_reference_from_sap.py --mode daily --domain vendor

  # One-time full catalogue load (manual)
  python scripts/sync_pr_po_reference_from_sap.py --mode full

  # Full load and delete DB rows not present in SAP QAS
  python scripts/sync_pr_po_reference_from_sap.py --mode full --prune

  # Single domain
  python scripts/sync_pr_po_reference_from_sap.py --mode full --domain vendor --prune

Requires ``PROCUREMENT_SAP_*`` and database URL in the server ``.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Quiet noisy HTTP libraries unless debugging.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _redact_db_target(url: str) -> str:
    try:
        p = urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        db = (p.path or "/").lstrip("/") or "?"
        return f"{host}{port}/{db}"
    except Exception:
        return "(unparseable)"


def main() -> int:
    p = argparse.ArgumentParser(description="SAP → pr_po_reference_values sync")
    p.add_argument(
        "--mode",
        choices=["daily", "full"],
        default="daily",
        help="daily = delta material/CC/vendor/asset + full small catalogues",
    )
    p.add_argument(
        "--domain",
        action="append",
        dest="domains",
        help="Limit to domain(s); repeat flag allowed. Default: all SAP domains.",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="After full sync, delete pr_po_reference_values rows not returned by SAP (full mode only).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging (includes httpx).",
    )
    args = p.parse_args()

    _configure_logging(args.verbose)

    from app.config.settings import settings
    from app.procurement.reference_sync.odata_client import sap_master_configured
    from app.procurement.reference_sync.sync import (
        run_reference_sync_daily,
        run_reference_sync_full,
    )

    domains = args.domains or None
    if args.prune and args.mode != "full":
        print("--prune requires --mode full", file=sys.stderr)
        return 2

    if not sap_master_configured():
        print("PROCUREMENT_SAP_* not configured", file=sys.stderr)
        return 2

    sap = (settings.procurement_sap_base_url or "").rstrip("/")
    print(
        f"SAP reference sync starting: mode={args.mode} "
        f"domains={domains or 'ALL'} prune={args.prune}\n"
        f"  SAP={sap}\n"
        f"  DB={_redact_db_target(settings.database_url)}\n"
        f"  DB_SYNC={_redact_db_target(settings.database_url_sync)}",
        flush=True,
    )

    async def _run():
        if args.mode == "full":
            return await run_reference_sync_full(domains=domains, prune=args.prune)
        return await run_reference_sync_daily(domains=domains)

    t0 = time.monotonic()
    report = asyncio.run(_run())
    elapsed = time.monotonic() - t0

    print(f"\n=== SAP reference sync ({report.mode}) {elapsed:.1f}s ===\n", flush=True)
    failed = 0
    for d in report.domains:
        status = "OK" if not d.error else "FAIL"
        if d.error:
            failed += 1
        print(
            f"  [{status}] {d.domain:20} fetched={d.fetched:6}  "
            f"inserted={d.inserted:6}  updated={d.updated:6}  pruned={d.pruned:6}"
            + (f"  err={d.error!r}" if d.error else ""),
            flush=True,
        )
    if failed:
        print(f"\n{failed} domain(s) failed", file=sys.stderr, flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
