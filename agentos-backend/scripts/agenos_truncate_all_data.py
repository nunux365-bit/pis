#!/usr/bin/env python3
"""
Truncate all rows from Agenos O2C billing tables (see agenos_truncate_all_data.sql).

Wipes contracts (all client kinds), ``service_site``, ``site_alias``, MIS, invoices,
attendance, ``contract_extraction_run``, and ``failed_contract_parsing`` so you can reingest
everything. (``o2c_ingestion_state`` is not in the bundled SQL — add it there if your DB has it.)

``--dry-run`` prints per-table row counts (tables missing in DB are skipped).
``--yes`` (or env ``AGENOS_TRUNCATE_ALL_YES=1``) performs the truncate.
Uses ``settings.agenos_database_url_sync``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _tables_from_truncate_sql(sql: str) -> list[str]:
    m = re.search(
        r"TRUNCATE\s+TABLE\s+([\s\S]+?)\s+RESTART\s+IDENTITY\s+CASCADE\s*;",
        sql,
        re.IGNORECASE,
    )
    if not m:
        raise ValueError("Could not parse TRUNCATE list from agenos_truncate_all_data.sql")
    raw = m.group(1)
    return [p.strip() for p in re.split(r",\s*", raw.replace("\n", " ")) if p.strip()]


def _dry_run_counts(cur, tables: list[str]) -> None:
    from psycopg2 import sql as psql

    print("Dry run — row counts (public schema); missing tables omitted:\n")
    total = 0
    for name in tables:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (name,),
        )
        if cur.fetchone() is None:
            print(f"  {name}: (no such table)")
            continue
        cur.execute(psql.SQL("SELECT COUNT(*)::int FROM {}").format(psql.Identifier(name)))
        n = int(cur.fetchone()[0])
        total += n
        print(f"  {name}: {n}")
    print(f"\nTotal rows (sum of counts above): {total}")


def main() -> int:
    p = argparse.ArgumentParser(description="Truncate all Agenos O2C billing table rows.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print per-table row counts; do not truncate.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive truncate (or set AGENOS_TRUNCATE_ALL_YES=1).",
    )
    args = p.parse_args()

    confirmed = args.yes or os.environ.get("AGENOS_TRUNCATE_ALL_YES", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    sql_path = Path(__file__).resolve().parent / "agenos_truncate_all_data.sql"
    sql = sql_path.read_text(encoding="utf-8")
    tables = _tables_from_truncate_sql(sql)

    from app.config.settings import settings

    url = (settings.agenos_database_url_sync or "").strip()
    if not url:
        print("agenos_database_url_sync is empty.", file=sys.stderr)
        return 1

    import psycopg2

    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            if args.dry_run:
                _dry_run_counts(cur, tables)
                conn.rollback()
                print("\nNo changes made. Re-run with --yes to truncate.")
                return 0

            if not confirmed:
                print(
                    "Refusing to truncate without --yes or AGENOS_TRUNCATE_ALL_YES=1.\n"
                    "Pass --dry-run to see row counts first.",
                    file=sys.stderr,
                )
                return 2

            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("OK: truncated all listed Agenos/O2C tables (see agenos_truncate_all_data.sql).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
