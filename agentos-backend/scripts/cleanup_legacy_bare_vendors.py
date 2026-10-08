#!/usr/bin/env python3
"""Remove legacy bare ``vendor`` rows superseded by ``vendor|company`` composite keys.

Usage (from ``agentos-backend/``)::

  python scripts/cleanup_legacy_bare_vendors.py          # dry-run counts
  python scripts/cleanup_legacy_bare_vendors.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_DELETE_SQL = """
DELETE FROM pr_po_reference_values AS v
WHERE v.domain = 'vendor'
  AND v.code NOT LIKE '%|%'
  AND EXISTS (
    SELECT 1 FROM pr_po_reference_values AS c
    WHERE c.domain = 'vendor'
      AND c.code LIKE '%|%'
      AND split_part(c.code, '|', 1) = v.code
  )
"""

_COUNT_SQL = """
SELECT count(*) FROM pr_po_reference_values AS v
WHERE v.domain = 'vendor'
  AND v.code NOT LIKE '%|%'
  AND EXISTS (
    SELECT 1 FROM pr_po_reference_values AS c
    WHERE c.domain = 'vendor'
      AND c.code LIKE '%|%'
      AND split_part(c.code, '|', 1) = v.code
  )
"""


def main() -> int:
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    p = argparse.ArgumentParser(description="Drop bare vendor rows that have a composite row.")
    p.add_argument("--execute", action="store_true", help="Apply DELETE (default is dry-run).")
    args = p.parse_args()

    async def _run() -> int:
        async with AsyncSessionLocal() as session:
            n = (await session.execute(text(_COUNT_SQL))).scalar_one()
            print(f"Removable bare vendor rows (composite exists): {n}")
            if not args.execute:
                print("Dry-run only — pass --execute to delete.")
                return 0
            if n == 0:
                return 0
            await session.execute(text(_DELETE_SQL))
            await session.commit()
            print(f"Deleted {n} row(s).")
            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
