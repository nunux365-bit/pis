#!/usr/bin/env python3
"""Validate ``pr_po_reference_values`` for UI, API, and scoped search sanity.

Usage (from ``agentos-backend/``)::

  python scripts/validate_pr_po_reference_master.py
  python scripts/validate_pr_po_reference_master.py --warn-as-error
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from app.db.session import AsyncSessionLocal
    from app.procurement.reference_master_validate import format_report, validate_reference_master

    p = argparse.ArgumentParser(description="Validate procurement reference master data.")
    p.add_argument(
        "--warn-as-error",
        action="store_true",
        help="Treat warnings as failures (exit 1).",
    )
    args = p.parse_args()

    async def _run():
        async with AsyncSessionLocal() as session:
            return await validate_reference_master(session)

    report = asyncio.run(_run())
    print(format_report(report))
    if not report.ok:
        return 1
    if args.warn_as_error and any(i.level == "warn" for i in report.issues):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
