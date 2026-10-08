#!/usr/bin/env python3
"""Load non-SAP procurement reference master from JSON + SQL supplements.

Run **after** SAP sync (``sync_pr_po_reference_from_sap.py --mode full``) on fresh envs,
or anytime to refresh policy domains (company, org, payment terms, doc types, etc.).

Usage (from ``agentos-backend/``)::

  python scripts/load_pr_po_reference_fixed_master.py
  python scripts/load_pr_po_reference_fixed_master.py --dry-run
  python scripts/load_pr_po_reference_fixed_master.py --skip-sql
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
    from app.procurement.reference_fixed_loader import load_fixed_master

    p = argparse.ArgumentParser(description="Load fixed procurement reference master (JSON + SQL).")
    p.add_argument("--json", type=Path, default=None, help="Override reference_fixed_master.json path")
    p.add_argument("--sql", type=Path, default=None, help="Override reference_supplements.sql path")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-sql", action="store_true", help="Skip SQL supplements (JSON only)")
    args = p.parse_args()

    result = asyncio.run(
        load_fixed_master(
            json_path=args.json,
            sql_path=args.sql,
            dry_run=args.dry_run,
            skip_sql=args.skip_sql,
        )
    )
    prefix = "dry-run: " if result["dry_run"] else ""
    print(
        f"{prefix}json_rows={result['json_rows']} "
        f"inserted={result['inserted']} updated={result['updated']} "
        f"sql_statements={result['sql_statements']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
