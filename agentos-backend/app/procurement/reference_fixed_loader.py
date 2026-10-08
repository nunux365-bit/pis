"""Load non-SAP procurement reference master from JSON + optional SQL supplements."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.reference_domains import FIXED_MASTER_DOMAINS

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_JSON_PATH = _DATA_DIR / "reference_fixed_master.json"
DEFAULT_SQL_PATH = _DATA_DIR / "reference_supplements.sql"


def fixed_master_json_path(path: Path | None = None) -> Path:
    return path or DEFAULT_JSON_PATH


def fixed_master_sql_path(path: Path | None = None) -> Path:
    return path or DEFAULT_SQL_PATH


def load_fixed_master_rows(*, json_path: Path | None = None) -> list[dict[str, Any]]:
    """Parse ``reference_fixed_master.json`` into upsert row dicts."""
    path = fixed_master_json_path(json_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected top-level 'rows' array")
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {i} is not an object")
        domain = str(row.get("domain") or "").strip()
        if domain not in FIXED_MASTER_DOMAINS:
            raise ValueError(f"{path}: row {i} domain {domain!r} not in FIXED_MASTER_DOMAINS")
        code = str(row.get("code") or "").strip()
        if domain != "item_category" and not code:
            raise ValueError(f"{path}: row {i} missing code for domain {domain!r}")
        out.append(
            {
                "domain": domain,
                "document_type": str(row.get("document_type") or "").strip(),
                "applies_to_kind": str(row.get("applies_to_kind") or "").strip(),
                "code": code,
                "label": str(row.get("label") or code or "Standard").strip(),
                "sort_order": int(row.get("sort_order") or i),
                "extra": row.get("extra"),
            }
        )
    return _dedupe_rows(out)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per DB unique key; later rows win."""
    by_key: OrderedDict[tuple[str, str, str, str], dict[str, Any]] = OrderedDict()
    for r in rows:
        if "and so on" in str(r.get("code") or "").lower():
            continue
        k = (r["domain"], r["document_type"], r["code"], r.get("applies_to_kind") or "")
        by_key[k] = r
    return list(by_key.values())


async def upsert_fixed_master_rows(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Upsert fixed-master rows. SAP sync must not overwrite these domains."""
    rows = _dedupe_rows(rows)
    inserted = 0
    updated = 0
    for row in rows:
        dt = row.get("document_type") or ""
        atk = row.get("applies_to_kind") or ""
        q = select(PrPoReferenceValue).where(
            PrPoReferenceValue.domain == row["domain"],
            func.coalesce(PrPoReferenceValue.document_type, "") == dt,
            PrPoReferenceValue.code == row["code"],
            func.coalesce(PrPoReferenceValue.applies_to_kind, "") == atk,
        )
        existing = (await session.execute(q)).scalar_one_or_none()
        if existing is None:
            if not dry_run:
                session.add(PrPoReferenceValue(**row))
                await session.commit()
            inserted += 1
        else:
            if not dry_run:
                existing.label = row["label"]
                existing.sort_order = row["sort_order"]
                if row.get("extra") is not None:
                    existing.extra = row.get("extra")
                existing.document_type = dt
                existing.applies_to_kind = atk
                await session.commit()
            updated += 1
    return inserted, updated


async def apply_fixed_master_sql(
    session: AsyncSession,
    *,
    sql_path: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Run idempotent SQL supplements (label tweaks, org extras). Returns statement count."""
    path = fixed_master_sql_path(sql_path)
    if not path.is_file():
        log.info("fixed_master_sql: skip (not found): %s", path)
        return 0
    body = path.read_text(encoding="utf-8")
    sql_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        sql_lines.append(stripped)
    statements = [s.strip() for s in " ".join(sql_lines).split(";") if s.strip()]
    if dry_run:
        return len(statements)
    for stmt in statements:
        await session.execute(text(stmt))
    await session.commit()
    return len(statements)


async def load_fixed_master(
    *,
    json_path: Path | None = None,
    sql_path: Path | None = None,
    dry_run: bool = False,
    skip_sql: bool = False,
) -> dict[str, Any]:
    """Load JSON fixed master + SQL supplements into ``pr_po_reference_values``."""
    from app.db.session import AsyncSessionLocal

    rows = load_fixed_master_rows(json_path=json_path)
    async with AsyncSessionLocal() as session:
        ins, upd = await upsert_fixed_master_rows(session, rows, dry_run=dry_run)
        sql_count = 0
        if not skip_sql:
            sql_count = await apply_fixed_master_sql(session, sql_path=sql_path, dry_run=dry_run)
    return {
        "json_rows": len(rows),
        "inserted": ins,
        "updated": upd,
        "sql_statements": sql_count,
        "dry_run": dry_run,
    }
