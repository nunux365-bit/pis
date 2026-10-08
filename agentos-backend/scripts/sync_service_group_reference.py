#!/usr/bin/env python3
"""
Build ``service_group`` reference rows + ``extra.service_group`` on services (local / any env).

Mirrors the material_group pattern:
  * ``domain = service_group`` rows in ``pr_po_reference_values`` (code + label for header picker).
  * Each ``domain = service`` row gets ``extra.service_group`` (same code as ``Material Group``) for filtering.

Sources distinct codes from existing service rows' ``extra->>'Material Group'``.
Labels are taken from ``domain = material_group`` reference rows (same **Material Group** sheet
as the master import) when present; otherwise the code is used as the label.

Run from ``agentos-backend/``::

  python scripts/sync_service_group_reference.py
  python scripts/sync_service_group_reference.py --dry-run

Requires ``DATABASE_URL`` (or ``DATABASE_URL_SYNC``) like other backend scripts.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _truncate_label(s: str, max_len: int = 500) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _truncate_code(code: str, *, max_len: int = 256) -> str:
    c = (code or "").strip()
    if len(c) <= max_len:
        return c
    return c[: max_len - 1] + "…"


async def _upsert_service_group_rows(rows: list[dict[str, Any]]) -> tuple[int, int]:
    from sqlalchemy import select

    from app.db.models import PrPoReferenceValue
    from app.db.session import AsyncSessionLocal

    ins, upd = 0, 0
    async with AsyncSessionLocal() as session:
        for row in rows:
            atk = row.get("applies_to_kind") or ""
            q = select(PrPoReferenceValue).where(
                PrPoReferenceValue.domain == row["domain"],
                PrPoReferenceValue.document_type == row["document_type"],
                PrPoReferenceValue.code == row["code"],
                PrPoReferenceValue.applies_to_kind == atk,
            )
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing is None:
                session.add(PrPoReferenceValue(**row))
                ins += 1
            else:
                existing.label = row["label"]
                existing.sort_order = row["sort_order"]
                existing.extra = row.get("extra")
                upd += 1
        await session.commit()
    return ins, upd


async def _distinct_service_group_codes() -> list[str]:
    from sqlalchemy import func, select

    from app.db.models import PrPoReferenceValue
    from app.db.session import AsyncSessionLocal

    col = PrPoReferenceValue.extra["Material Group"].as_string()
    async with AsyncSessionLocal() as session:
        q = (
            select(func.distinct(func.trim(col)))
            .where(PrPoReferenceValue.domain == "service")
            .where(col.isnot(None))
            .where(func.trim(col) != "")
        )
        rows = (await session.execute(q)).all()
    out = [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]
    out.sort()
    return out


async def _material_group_labels(session: "AsyncSession", codes: list[str]) -> dict[str, str]:
    """Map material group code → display label from ``domain = material_group`` reference rows."""
    if not codes:
        return {}
    from sqlalchemy import select

    from app.db.models import PrPoReferenceValue

    q = select(PrPoReferenceValue.code, PrPoReferenceValue.label).where(
        PrPoReferenceValue.domain == "material_group",
        PrPoReferenceValue.document_type == "",
        PrPoReferenceValue.applies_to_kind == "",
        PrPoReferenceValue.code.in_(codes),
    )
    rows = (await session.execute(q)).all()
    return {str(c).strip(): (lbl or "").strip() or str(c).strip() for c, lbl in rows}


async def _patch_service_extra_service_group(*, dry_run: bool) -> int:
    """Set extra.service_group from extra.\"Material Group\" on each service row."""
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    sql = text(
        """
        UPDATE pr_po_reference_values
        SET extra = extra || jsonb_build_object(
            'service_group',
            to_jsonb(trim(both from extra->>'Material Group'))
        )
        WHERE domain = 'service'
          AND extra ? 'Material Group'
          AND trim(both from extra->>'Material Group') <> ''
          AND (
            NOT (extra ? 'service_group')
            OR trim(both from COALESCE(extra->>'service_group', '')) = ''
            OR trim(both from COALESCE(extra->>'service_group', ''))
               IS DISTINCT FROM trim(both from extra->>'Material Group')
          )
        """
    )
    async with AsyncSessionLocal() as session:
        if dry_run:
            r = await session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM pr_po_reference_values
                    WHERE domain = 'service'
                      AND extra ? 'Material Group'
                      AND trim(both from extra->>'Material Group') <> ''
                    """
                )
            )
            n = int(r.scalar() or 0)
            return n
        r = await session.execute(sql)
        await session.commit()
        return int(r.rowcount or 0)


async def main_async(*, dry_run: bool) -> int:
    codes = await _distinct_service_group_codes()
    if not codes:
        print("No distinct extra.'Material Group' values on service rows; nothing to do.", file=sys.stderr)
        return 0

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        labels = await _material_group_labels(session, codes)

    rows: list[dict[str, Any]] = []
    for i, code in enumerate(codes):
        c = _truncate_code(code, max_len=256)
        name = (labels.get(code) or "").strip() or c
        rows.append(
            {
                "domain": "service_group",
                "document_type": "",
                "applies_to_kind": "",
                "code": c,
                "label": _truncate_label(name),
                "sort_order": i,
                "extra": None,
            }
        )

    if dry_run:
        eligible = await _patch_service_extra_service_group(dry_run=True)
        print(
            f"dry-run: would upsert {len(rows)} service_group ref row(s); "
            f"{eligible} service row(s) have Material Group and would get extra.service_group"
        )
        return 0

    ins, upd = await _upsert_service_group_rows(rows)
    touched = await _patch_service_extra_service_group(dry_run=False)
    print(f"service_group reference: inserted {ins}, updated {upd}; service rows patched (extra.service_group): {touched}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print counts only; no DB writes")
    args = p.parse_args()
    return asyncio.run(main_async(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
