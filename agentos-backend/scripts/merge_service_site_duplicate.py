#!/usr/bin/env python3
"""
Repoint all FKs from a duplicate ``service_site`` row to a canonical row, then delete the duplicate.

Use when contract ``contract_rate_line`` rows point at site A but MIS/aliases point at site B
(same client, same display name, different ``site_key``).

Prevention: contract ingest now reuses an existing ``service_site`` when normalized
``canonical_name`` or ``display_name`` matches (see ``ingest_async._upsert_service_site_async``).
Run ``verify`` periodically to clean older duplicates created before that guard.

Examples:
  Dry-run (no changes):
    python scripts/merge_service_site_duplicate.py verify

  Merge known TACO EVCS ASAL Kurali duplicate (loser has no contract_rate_line rows):
    python scripts/merge_service_site_duplicate.py merge \\
      --loser 7e087175-eef1-477e-9d4d-aaa1021f24ce \\
      --winner fdd4b907-cade-462d-b970-d602b0f01f5a
"""

from __future__ import annotations

import argparse
import os
import sys
from uuid import UUID

import psycopg2


def _uuid_list_from_pg(val) -> list[str]:
    """Normalize ``array_agg(id)`` from psycopg2 (list/tuple or ``{uuid,uuid}`` string)."""
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    s = str(val).strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [p.strip() for p in inner.split(",") if p.strip()]
    return [s]


FK_COLUMNS: list[tuple[str, str]] = [
    ("site_alias", "service_site_id"),
    ("contract_rate_line", "service_site_id"),
    ("o2c_mis_run", "service_site_id"),
    ("invoice_run", "service_site_id"),
    ("invoice_line", "service_site_id"),
    ("attendance_row", "resolved_site_id"),
    ("o2c_attendance_site_recon", "resolved_service_site_id"),
]


def dsn() -> str:
    u = (os.environ.get("DATABASE_URL_SYNC") or "postgresql://pankaj:pankaj@localhost:5432/agentos").strip()
    return u.replace("postgresql+asyncpg://", "postgresql://", 1)


def verify(conn) -> int:
    cur = conn.cursor()
    print("=== Duplicate service_site by (billing_client_id, lower(display_name)) ===")
    cur.execute(
        """
        SELECT billing_client_id, lower(trim(display_name)) AS dn, count(*) AS n,
               array_agg(id ORDER BY site_key) AS site_ids,
               array_agg(site_key ORDER BY site_key) AS site_keys
        FROM service_site
        GROUP BY billing_client_id, lower(trim(display_name))
        HAVING count(*) > 1
        ORDER BY n DESC, dn
        """
    )
    dups = cur.fetchall()
    if not dups:
        print("None found.")
    else:
        for bc, dn, n, ids, keys in dups:
            print(f"  client={bc} display_norm={dn!r} count={n}")
            print(f"    ids={ids}")
            print(f"    site_keys={keys}")

    print("\n=== site_alias rows (count by service_site_id) for duplicate groups ===")
    for bc, dn, n, ids, keys in dups:
        id_list = _uuid_list_from_pg(ids)
        if not id_list:
            continue
        cur.execute(
            """
            SELECT sa.service_site_id, ss.site_key, count(*) AS aliases
            FROM site_alias sa
            JOIN service_site ss ON ss.id = sa.service_site_id
            WHERE sa.service_site_id = ANY(%s::uuid[])
            GROUP BY sa.service_site_id, ss.site_key
            ORDER BY aliases DESC
            """,
            (id_list,),
        )
        for row in cur.fetchall():
            print(f"  {row}")

    print("\n=== MIS runs on duplicate sites (recent) ===")
    if dups:
        all_ids: list[str] = []
        for row in dups:
            all_ids.extend(_uuid_list_from_pg(row[3]))
        cur.execute(
            """
            SELECT mr.id, mr.client_site_key, mr.service_site_id, ss.site_key, mr.status
            FROM o2c_mis_run mr
            JOIN service_site ss ON ss.id = mr.service_site_id
            WHERE mr.service_site_id = ANY(%s::uuid[])
            ORDER BY mr.created_at DESC
            LIMIT 30
            """,
            (all_ids,),
        )
        for r in cur.fetchall():
            print(f"  {r}")
    else:
        print("  (no duplicate groups)")

    cur.close()
    return 0


def count_refs(cur, col: str, table: str, loser: str) -> int:
    cur.execute(f"SELECT count(*) FROM {table} WHERE {col} = %s::uuid", (loser,))
    return int(cur.fetchone()[0])


def merge(conn, loser: UUID, winner: UUID, *, force: bool) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, billing_client_id, site_key, display_name FROM service_site WHERE id IN (%s::uuid, %s::uuid)",
        (str(loser), str(winner)),
    )
    rows = {UUID(str(r[0])): r for r in cur.fetchall()}
    if loser not in rows or winner not in rows:
        raise SystemExit("loser or winner service_site id not found")
    lrow, wrow = rows[loser], rows[winner]
    if lrow[1] != wrow[1]:
        raise SystemExit("billing_client_id must match for loser and winner")

    cur.execute(
        "SELECT count(*) FROM contract_rate_line WHERE service_site_id = %s::uuid",
        (str(loser),),
    )
    nlines = int(cur.fetchone()[0])
    if nlines and not force:
        raise SystemExit(
            f"loser has {nlines} contract_rate_line rows; refuse merge without --force "
            "(repoint those rows to winner first or resolve duplicates manually)"
        )

    print(f"Merging loser {loser} ({lrow[2]}) -> winner {winner} ({wrow[2]})")
    for table, col in FK_COLUMNS:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {col} = %s::uuid", (str(loser),))
        n = int(cur.fetchone()[0])
        if n:
            print(f"  UPDATE {table}.{col}: {n} rows")
            cur.execute(
                f"UPDATE {table} SET {col} = %s::uuid WHERE {col} = %s::uuid",
                (str(winner), str(loser)),
            )

    cur.execute("DELETE FROM service_site WHERE id = %s::uuid", (str(loser),))
    if cur.rowcount != 1:
        raise SystemExit("DELETE service_site expected 1 row")
    conn.commit()
    print("Done. Re-run MIS draft for affected runs so summary_json is regenerated.")
    cur.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", help="List duplicate sites and related aliases/MIS (read-only)")

    m = sub.add_parser("merge", help="Repoint FKs and delete loser site")
    m.add_argument("--loser", required=True, help="UUID of duplicate service_site to remove")
    m.add_argument("--winner", required=True, help="UUID of canonical service_site to keep")
    m.add_argument("--force", action="store_true", help="Allow merge even if loser has contract_rate_line rows")

    args = ap.parse_args()
    conn = psycopg2.connect(dsn())
    conn.autocommit = False
    try:
        if args.cmd == "verify":
            rc = verify(conn)
            raise SystemExit(rc)
        merge(conn, UUID(args.loser), UUID(args.winner), force=args.force)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
