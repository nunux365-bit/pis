#!/usr/bin/env python3
"""
Upsert rows into ``pr_po_reference_values`` from a CSV export (e.g. from the PR/PO master sheet).

Expected columns (header row, case-insensitive):
  domain, document_type, code, label
Optional:
  sort_order (integer; default 0)
  applies_to_kind (PR | PO | empty = both; required for duplicate purchasing_doc_type rows)
  extra (JSON object string; stored in JSONB)

``document_type`` may be empty for values that apply to all PR/PO document types (YSER/YUNB/YAST).

Usage (from ``agentos-backend/``)::

  python scripts/import_pr_po_reference_csv.py path/to/reference.csv
  python scripts/import_pr_po_reference_csv.py --dry-run path/to/reference.csv
  python scripts/import_pr_po_reference_csv.py --list-domains

Requires database URL in env (same as the app; see ``app.config.settings``).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _collect_reference_domains() -> list[str]:
    from app.procurement.field_schema import FIELDS_BY_DOCUMENT_TYPE

    domains: set[str] = set()
    for specs in FIELDS_BY_DOCUMENT_TYPE.values():
        for spec in specs:
            if spec.reference_domain:
                domains.add(spec.reference_domain)
    return sorted(domains)


def _norm_header(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "_")


def _parse_row(raw: dict[str, str], line_no: int) -> dict[str, Any]:
    norm = {_norm_header(k): (v or "").strip() for k, v in raw.items() if k}
    domain = norm.get("domain", "")
    doc_type = norm.get("document_type", norm.get("doc_type", norm.get("documenttype", "")))
    code = norm.get("code", "")
    label = norm.get("label", norm.get("description", ""))
    so_raw = norm.get("sort_order", norm.get("sort", "0"))
    atk = norm.get("applies_to_kind", norm.get("kind", "")).strip().upper()
    if atk not in ("", "PR", "PO"):
        raise ValueError(f"line {line_no}: applies_to_kind must be '', PR, or PO")
    extra_raw = norm.get("extra", "")

    if not domain:
        raise ValueError(f"line {line_no}: missing domain")
    if not label:
        raise ValueError(f"line {line_no}: missing label")
    if not code and domain != "item_category":
        raise ValueError(f"line {line_no}: missing code")

    try:
        sort_order = int(so_raw) if so_raw else 0
    except ValueError as e:
        raise ValueError(f"line {line_no}: invalid sort_order: {so_raw!r}") from e

    extra: dict[str, Any] | None = None
    if extra_raw:
        try:
            parsed = json.loads(extra_raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {line_no}: extra is not valid JSON") from e
        if not isinstance(parsed, dict):
            raise ValueError(f"line {line_no}: extra must be a JSON object")
        extra = parsed

    return {
        "domain": domain,
        "document_type": doc_type,
        "applies_to_kind": atk,
        "code": code,
        "label": label,
        "sort_order": sort_order,
        "extra": extra,
    }


async def _upsert_rows(rows: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, int]:
    from sqlalchemy import select

    from app.db.models import PrPoReferenceValue
    from app.db.session import AsyncSessionLocal

    inserted = 0
    updated = 0
    async with AsyncSessionLocal() as session:
        for row in rows:
            q = select(PrPoReferenceValue).where(
                PrPoReferenceValue.domain == row["domain"],
                PrPoReferenceValue.document_type == row["document_type"],
                PrPoReferenceValue.code == row["code"],
                PrPoReferenceValue.applies_to_kind == row.get("applies_to_kind", ""),
            )
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing is None:
                if not dry_run:
                    session.add(PrPoReferenceValue(**row))
                inserted += 1
            else:
                if not dry_run:
                    existing.label = row["label"]
                    existing.sort_order = row["sort_order"]
                    existing.extra = row["extra"]
                    existing.applies_to_kind = row.get("applies_to_kind", "") or ""
                updated += 1
        if not dry_run:
            await session.commit()
    return inserted, updated


def main() -> int:
    p = argparse.ArgumentParser(description="Import pr_po_reference_values from CSV.")
    p.add_argument("csv_path", nargs="?", type=Path, help="UTF-8 CSV path")
    p.add_argument("--dry-run", action="store_true", help="Parse and count upserts; do not write DB.")
    p.add_argument(
        "--list-domains",
        action="store_true",
        help="Print reference_domain keys used by procurement field_schema and exit.",
    )
    args = p.parse_args()

    if args.list_domains:
        print("reference_domain values used by procurement selects (align sheet tabs / domain column):")
        for d in _collect_reference_domains():
            print(f"  {d}")
        return 0

    if not args.csv_path:
        p.error("csv_path is required unless using --list-domains")

    path = args.csv_path.expanduser()
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("error: CSV has no header row", file=sys.stderr)
            return 2
        for i, raw in enumerate(reader, start=2):
            if not any((v or "").strip() for v in raw.values()):
                continue
            try:
                rows.append(_parse_row(raw, i))
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2

    if not rows:
        print("error: no data rows after header", file=sys.stderr)
        return 2

    known = set(_collect_reference_domains())
    unknown_domains = sorted({r["domain"] for r in rows if r["domain"] not in known})
    if unknown_domains:
        print("warning: domain values not present in field_schema selects:", ", ".join(unknown_domains))
        print("  (They will still be stored if you proceed; use --list-domains for expected names.)")

    inserted, updated = asyncio.run(_upsert_rows(rows, dry_run=args.dry_run))
    if args.dry_run:
        print(
            f"dry-run: would insert {inserted}, would update {updated} "
            f"row(s) from {len(rows)} CSV row(s). Re-run without --dry-run to apply."
        )
    else:
        print(f"committed: inserted {inserted}, updated {updated} row(s) from {len(rows)} CSV row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
