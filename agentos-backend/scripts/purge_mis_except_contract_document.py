#!/usr/bin/env python3
"""
Delete ``o2c_mis_summary_row`` and ``o2c_mis_run`` for every contract except those tied to a
given ``contract_document.id`` (via ``contract_terms_document`` → ``contract_terms_version_id``).

Deletes summary rows first, then MIS runs (matches FK cascade, but makes summary cleanup explicit).

Requires ``--yes`` or env ``AGENOS_PURGE_MIS_YES=1`` to execute; otherwise dry-run counts only.

Uses ``settings.agenos_database_url_sync``.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description="Purge MIS runs/summary except one contract document.")
    p.add_argument("contract_document_id", type=uuid.UUID, help="contract_document.id to keep.")
    p.add_argument("--dry-run", action="store_true", help="Print counts only.")
    p.add_argument("--yes", action="store_true", help="Execute deletes.")
    args = p.parse_args()

    confirmed = args.yes or os.environ.get("AGENOS_PURGE_MIS_YES", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    dry_run = args.dry_run or not confirmed

    from app.config.settings import settings

    url = (settings.agenos_database_url_sync or "").strip()
    if not url:
        print("agenos_database_url_sync is empty.", file=sys.stderr)
        return 1

    import psycopg2

    keep_doc = str(args.contract_document_id)
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*)::int FROM contract_document WHERE id = %s", (keep_doc,))
            if int(cur.fetchone()[0]) == 0:
                print(f"contract_document not found: {keep_doc}", file=sys.stderr)
                return 1

            cur.execute(
                "SELECT contract_terms_version_id::text FROM contract_terms_document "
                "WHERE contract_document_id = %s",
                (keep_doc,),
            )
            keep_ctv = [r[0] for r in cur.fetchall()]
            if not keep_ctv:
                print(
                    "No contract_terms_document rows for that document; refusing to wipe all MIS.",
                    file=sys.stderr,
                )
                return 1

            cur.execute(
                """
                SELECT COUNT(*)::int FROM o2c_mis_summary_row sr
                JOIN o2c_mis_run mr ON mr.id = sr.mis_run_id
                WHERE mr.contract_terms_version_id <> ALL(%s::uuid[])
                """,
                (keep_ctv,),
            )
            n_sr = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*)::int FROM o2c_mis_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv,),
            )
            n_mr = int(cur.fetchone()[0])

            print(f"Keep contract_document_id={keep_doc}")
            print(f"Keep contract_terms_version_id(s): {keep_ctv}")
            print(f"Would delete o2c_mis_summary_row: {n_sr}")
            print(f"Would delete o2c_mis_run: {n_mr}")

            if dry_run:
                print("\nDry run. Re-run with --yes or AGENOS_PURGE_MIS_YES=1.", file=sys.stderr)
                conn.rollback()
                return 0

            cur.execute(
                """
                DELETE FROM o2c_mis_summary_row sr
                USING o2c_mis_run mr
                WHERE sr.mis_run_id = mr.id
                  AND mr.contract_terms_version_id <> ALL(%s::uuid[])
                """,
                (keep_ctv,),
            )
            print(f"Deleted o2c_mis_summary_row: {cur.rowcount}")

            cur.execute(
                "DELETE FROM o2c_mis_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv,),
            )
            print(f"Deleted o2c_mis_run: {cur.rowcount}")

            cur.execute(
                """
                DELETE FROM o2c_mis_summary_row sr
                WHERE NOT EXISTS (SELECT 1 FROM o2c_mis_run mr WHERE mr.id = sr.mis_run_id)
                """
            )
            if cur.rowcount:
                print(f"Deleted orphan o2c_mis_summary_row: {cur.rowcount}")

        conn.commit()
        print("OK: committed.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
