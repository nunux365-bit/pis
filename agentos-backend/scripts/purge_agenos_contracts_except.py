#!/usr/bin/env python3
"""
Delete Agenos O2C contract graph rows and MIS / invoice / recon queues, keeping one
``contract_terms_version`` or ``contract_document`` (and everything that must stay
for FK integrity).

After contract rate lines are removed, deletes **orphan** ``service_site`` rows (no remaining
references from ``contract_rate_line``, ``o2c_mis_run``, invoice tables when present,
``attendance_row``, ``o2c_attendance_site_recon``). ``site_alias`` rows CASCADE with their site.
Pass ``--keep-orphan-service-sites`` to skip this step.

The keep UUID may be either:
  - ``contract_terms_version.id`` (only that version is kept), or
  - ``contract_document.id`` (that document and every ``contract_terms_version`` linked
    via ``contract_terms_document`` are kept).

If the same UUID exists in both tables (possible in PostgreSQL), pass ``--disambiguate-terms``
or ``--disambiguate-document``.

Requires ``--yes`` (or env ``AGENOS_PURGE_CONTRACTS_YES=1``) to execute; without it, or with
``--dry-run``, only prints planned row counts.

Uses ``settings.agenos_database_url_sync`` (same as ``agenos_connection``).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _orphan_service_site_predicates_post_rate_line_purge(cur) -> list[str]:
    """
    ``NOT EXISTS`` fragments for ``service_site ss`` after non-kept ``contract_rate_line``
    rows are removed (no remaining rate line may reference deleted terms versions).
    """
    parts: list[str] = [
        "NOT EXISTS (SELECT 1 FROM contract_rate_line crl WHERE crl.service_site_id = ss.id)"
    ]
    if _table_exists(cur, "o2c_mis_run") and _column_exists(cur, "o2c_mis_run", "service_site_id"):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM o2c_mis_run m WHERE m.service_site_id = ss.id)"
        )
    if _table_exists(cur, "invoice_run") and _column_exists(cur, "invoice_run", "service_site_id"):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM invoice_run ir WHERE ir.service_site_id = ss.id)"
        )
    if _table_exists(cur, "invoice_line") and _column_exists(cur, "invoice_line", "service_site_id"):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM invoice_line il WHERE il.service_site_id = ss.id)"
        )
    if _table_exists(cur, "attendance_row") and _column_exists(cur, "attendance_row", "resolved_site_id"):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM attendance_row ar WHERE ar.resolved_site_id = ss.id)"
        )
    if _table_exists(cur, "o2c_attendance_site_recon") and _column_exists(
        cur, "o2c_attendance_site_recon", "resolved_service_site_id"
    ):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM o2c_attendance_site_recon r "
            "WHERE r.resolved_service_site_id = ss.id)"
        )
    return parts


def _orphan_service_site_where_post_rate_line_purge(cur) -> str | None:
    if not _table_exists(cur, "service_site"):
        return None
    return " AND ".join(_orphan_service_site_predicates_post_rate_line_purge(cur))


def _orphan_service_site_estimate_sql_params(
    cur, keep_ctv_str: list[str]
) -> tuple[str, tuple[Any, ...]] | None:
    """
    Pre-purge estimate of ``DELETE FROM service_site``: no remaining link via **kept**
    ``contract_terms_version`` on rate lines / MIS / invoices, plus no other FKs (recon, etc.).
    """
    if not _table_exists(cur, "service_site"):
        return None
    parts: list[str] = []
    params: list[Any] = []
    parts.append(
        "NOT EXISTS (SELECT 1 FROM contract_rate_line crl WHERE crl.service_site_id = ss.id "
        "AND crl.contract_terms_version_id = ANY(%s::uuid[]))"
    )
    params.append(keep_ctv_str)

    if (
        _table_exists(cur, "o2c_mis_run")
        and _column_exists(cur, "o2c_mis_run", "service_site_id")
        and _column_exists(cur, "o2c_mis_run", "contract_terms_version_id")
    ):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM o2c_mis_run m WHERE m.service_site_id = ss.id "
            "AND m.contract_terms_version_id = ANY(%s::uuid[]))"
        )
        params.append(keep_ctv_str)

    if (
        _table_exists(cur, "invoice_run")
        and _column_exists(cur, "invoice_run", "service_site_id")
        and _column_exists(cur, "invoice_run", "contract_terms_version_id")
    ):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM invoice_run ir WHERE ir.service_site_id = ss.id "
            "AND ir.contract_terms_version_id = ANY(%s::uuid[]))"
        )
        params.append(keep_ctv_str)

    if (
        _table_exists(cur, "invoice_line")
        and _column_exists(cur, "invoice_line", "service_site_id")
        and _table_exists(cur, "invoice_run")
        and _column_exists(cur, "invoice_run", "contract_terms_version_id")
    ):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM invoice_line il "
            "JOIN invoice_run ir ON ir.id = il.invoice_run_id "
            "WHERE il.service_site_id = ss.id "
            "AND ir.contract_terms_version_id = ANY(%s::uuid[]))"
        )
        params.append(keep_ctv_str)

    if _table_exists(cur, "attendance_row") and _column_exists(cur, "attendance_row", "resolved_site_id"):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM attendance_row ar WHERE ar.resolved_site_id = ss.id)"
        )
    if _table_exists(cur, "o2c_attendance_site_recon") and _column_exists(
        cur, "o2c_attendance_site_recon", "resolved_service_site_id"
    ):
        parts.append(
            "NOT EXISTS (SELECT 1 FROM o2c_attendance_site_recon r "
            "WHERE r.resolved_service_site_id = ss.id)"
        )

    return " AND ".join(parts), tuple(params)


def _count_where(cur, sql: str, params: tuple[Any, ...] | None = None) -> int:
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Purge Agenos contracts except one id.")
    p.add_argument(
        "keep_id",
        type=uuid.UUID,
        help="UUID to keep (contract_terms_version.id or contract_document.id).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show counts; do not delete (default if --yes not given).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Execute deletes (or set AGENOS_PURGE_CONTRACTS_YES=1).",
    )
    p.add_argument(
        "--disambiguate-terms",
        action="store_true",
        help="If UUID exists in both tables, treat it as contract_terms_version.id.",
    )
    p.add_argument(
        "--disambiguate-document",
        action="store_true",
        help="If UUID exists in both tables, treat it as contract_document.id.",
    )
    p.add_argument(
        "--keep-failed-parsing",
        action="store_true",
        help="Do not delete rows from failed_contract_parsing.",
    )
    p.add_argument(
        "--keep-attendance-recon",
        action="store_true",
        help="Do not delete rows from o2c_attendance_site_recon.",
    )
    p.add_argument(
        "--keep-orphan-service-sites",
        action="store_true",
        help="Do not delete service_site rows that become unreferenced after the purge.",
    )
    args = p.parse_args()

    if args.disambiguate_terms and args.disambiguate_document:
        print("Use only one of --disambiguate-terms / --disambiguate-document.", file=sys.stderr)
        return 2

    confirmed = args.yes or os.environ.get("AGENOS_PURGE_CONTRACTS_YES", "").strip().lower() in (
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

    keep = args.keep_id
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int FROM contract_terms_version WHERE id = %s",
                (str(keep),),
            )
            n_ctv = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*)::int FROM contract_document WHERE id = %s",
                (str(keep),),
            )
            n_cd = int(cur.fetchone()[0])

            if n_ctv and n_cd:
                if args.disambiguate_terms:
                    n_cd = 0
                elif args.disambiguate_document:
                    n_ctv = 0
                else:
                    print(
                        "That UUID exists as both contract_terms_version.id and contract_document.id. "
                        "Pass --disambiguate-terms or --disambiguate-document.",
                        file=sys.stderr,
                    )
                    return 2

            if not n_ctv and not n_cd:
                print(
                    f"No row with id {keep} in contract_terms_version or contract_document.",
                    file=sys.stderr,
                )
                return 1

            keep_ctv: list[uuid.UUID]
            keep_doc: list[uuid.UUID]
            if n_ctv:
                keep_ctv = [keep]
                cur.execute(
                    "SELECT contract_document_id FROM contract_terms_document "
                    "WHERE contract_terms_version_id = %s",
                    (str(keep),),
                )
                keep_doc = [uuid.UUID(str(r[0])) for r in cur.fetchall()]
            else:
                keep_doc = [keep]
                cur.execute(
                    "SELECT contract_terms_version_id FROM contract_terms_document "
                    "WHERE contract_document_id = %s",
                    (str(keep),),
                )
                keep_ctv = [uuid.UUID(str(r[0])) for r in cur.fetchall()]
                if not keep_ctv:
                    print(
                        "contract_document has no contract_terms_document links; "
                        "nothing to keep in the terms graph. Aborting.",
                        file=sys.stderr,
                    )
                    return 1

            keep_ctv_str = [str(x) for x in keep_ctv]
            keep_doc_str = [str(x) for x in keep_doc]

            has_invoice = _table_exists(cur, "invoice_run")
            has_invoice_line = _table_exists(cur, "invoice_line")

            # Planned impact (approximate; excludes cascades counted separately)
            n_mis_run = _count_where(
                cur,
                "SELECT COUNT(*)::int FROM o2c_mis_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            n_inv = 0
            if has_invoice:
                n_inv = _count_where(
                    cur,
                    "SELECT COUNT(*)::int FROM invoice_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                    (keep_ctv_str,),
                )
            n_ctv_del = _count_where(
                cur,
                "SELECT COUNT(*)::int FROM contract_terms_version WHERE id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            n_cd_remove = _count_where(
                cur,
                """
                SELECT COUNT(*)::int FROM contract_document cd
                WHERE NOT EXISTS (
                    SELECT 1 FROM contract_terms_document ctd
                    WHERE ctd.contract_document_id = cd.id
                      AND ctd.contract_terms_version_id = ANY(%s::uuid[])
                )
                """,
                (keep_ctv_str,),
            )

            print(f"Keep contract_terms_version.id: {keep_ctv_str}")
            print(f"Keep contract_document.id (linked): {keep_doc_str or '(none)'}")
            print(f"Would delete o2c_mis_run rows: {n_mis_run}")
            print(f"Would delete contract_terms_version rows: {n_ctv_del}")
            if has_invoice:
                print(f"Would delete invoice_run rows: {n_inv}")
            print(
                "Would delete contract_document rows (no link to any kept terms version after purge): "
                f"{n_cd_remove}"
            )
            if not args.keep_failed_parsing and _table_exists(cur, "failed_contract_parsing"):
                n_fail = _count_where(cur, "SELECT COUNT(*)::int FROM failed_contract_parsing")
                print(f"Would delete failed_contract_parsing rows: {n_fail}")
            if not args.keep_attendance_recon and _table_exists(cur, "o2c_attendance_site_recon"):
                n_recon = _count_where(cur, "SELECT COUNT(*)::int FROM o2c_attendance_site_recon")
                print(f"Would delete o2c_attendance_site_recon rows: {n_recon}")

            n_ss_orphan = 0
            orphan_ss_where_delete = _orphan_service_site_where_post_rate_line_purge(cur)
            ss_est = _orphan_service_site_estimate_sql_params(cur, keep_ctv_str)
            if not args.keep_orphan_service_sites and ss_est is not None:
                est_sql, est_params = ss_est
                n_ss_orphan = _count_where(
                    cur,
                    f"SELECT COUNT(*)::int FROM service_site ss WHERE {est_sql}",
                    est_params,
                )
                print(
                    f"Would delete orphan service_site rows (estimate from kept CTV refs): "
                    f"{n_ss_orphan}"
                )

            if dry_run:
                if not confirmed:
                    print("\nDry run (no --yes). Re-run with --yes to execute.", file=sys.stderr)
                else:
                    print("\nDry run (--dry-run); no changes made.")
                conn.rollback()
                return 0

            # 1) MIS: summary rows cascade when mis_run is deleted
            cur.execute(
                "DELETE FROM o2c_mis_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )

            # 2) Invoices (optional tables)
            if has_invoice_line and has_invoice:
                cur.execute(
                    """
                    DELETE FROM invoice_line
                    WHERE invoice_run_id IN (
                        SELECT id FROM invoice_run
                        WHERE contract_terms_version_id <> ALL(%s::uuid[])
                    )
                    """,
                    (keep_ctv_str,),
                )
            if has_invoice:
                cur.execute(
                    "DELETE FROM invoice_run WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                    (keep_ctv_str,),
                )

            # 3) Break self-FKs on versions we are about to remove
            cur.execute(
                """
                UPDATE contract_terms_version
                SET superseded_by_id = NULL
                WHERE superseded_by_id IS NOT NULL
                  AND superseded_by_id <> ALL(%s::uuid[])
                """,
                (keep_ctv_str,),
            )

            # 4) Contract graph for non-kept versions
            cur.execute(
                "DELETE FROM contract_rate_line WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            cur.execute(
                "DELETE FROM payment_terms WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            cur.execute(
                "DELETE FROM contract_party WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            cur.execute(
                "DELETE FROM contract_terms_document WHERE contract_terms_version_id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )
            cur.execute(
                "DELETE FROM contract_terms_version WHERE id <> ALL(%s::uuid[])",
                (keep_ctv_str,),
            )

            # 5) Documents no longer linked to any terms version (RESTRICT requires unlink first)
            cur.execute(
                """
                DELETE FROM contract_document cd
                WHERE NOT EXISTS (
                    SELECT 1 FROM contract_terms_document ctd
                    WHERE ctd.contract_document_id = cd.id
                )
                """,
            )

            # 6) service_site rows only referenced by removed contracts (site_alias cascades in DDL)
            if not args.keep_orphan_service_sites and orphan_ss_where_delete:
                cur.execute(f"DELETE FROM service_site ss WHERE {orphan_ss_where_delete}")

            if not args.keep_failed_parsing and _table_exists(cur, "failed_contract_parsing"):
                cur.execute("DELETE FROM failed_contract_parsing")

            if not args.keep_attendance_recon and _table_exists(cur, "o2c_attendance_site_recon"):
                cur.execute("DELETE FROM o2c_attendance_site_recon")

        conn.commit()
        print("OK: purge committed.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
