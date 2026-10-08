# app/sbi_mis/db.py
"""Async DB access layer for SBI MIS — mirrors sbi-mis/backend/db.py but uses
AsyncSession + PostgreSQL instead of sqlite3."""

from __future__ import annotations

import json
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.sbi_mis.models import (
    SbiClient, SbiColumnFormat, SbiCurrentRun, SbiJob,
    SbiLookupTable, SbiPfHistorical, SbiRule, SbiRunFile,
    SbiRun, SbiSheetColumn,
)


# ── Path helpers ────────────────────────────────────────────────────────────

def data_dir() -> Path:
    return Path(settings.sbi_mis_data_dir).expanduser().resolve()


def upload_dir() -> Path:
    return data_dir() / "uploads"


def output_dir() -> Path:
    return data_dir() / "outputs"


def path_for_storage(path: str | Path) -> str:
    """Store paths relative to data_dir() when possible — allows moving data directories."""
    p = Path(path).resolve()
    try:
        return p.relative_to(data_dir()).as_posix()
    except ValueError:
        return p.as_posix()


def resolve_stored_path(stored: Optional[str]) -> Optional[Path]:
    if not stored or not str(stored).strip():
        return None
    p = Path(str(stored).strip())
    if p.is_absolute():
        return p if p.exists() else None
    return (data_dir() / p).resolve()


# ── Rules ────────────────────────────────────────────────────────────────────

async def get_all_rules(session: AsyncSession) -> List[Dict[str, Any]]:
    rules_result = await session.execute(
        select(SbiRule).order_by(SbiRule.sheet, SbiRule.column_letter)
    )
    # Merge in number_format from sbi_column_formats so it's included in export JSON
    formats = await get_column_formats(session)
    out = []
    for r in rules_result.scalars():
        d = _rule_to_dict(r)
        d["number_format"] = formats.get(r.sheet, {}).get(r.column_letter)
        out.append(d)
    return out


async def get_rules_for_sheet(session: AsyncSession, sheet: str) -> Dict[str, Dict[str, Any]]:
    """Batch fetch all rules for a sheet in one query — {col_letter: rule_dict}.

    Use this in the preview endpoint instead of calling get_rule() per column,
    which causes an N+1 DB round-trip problem.
    """
    result = await session.execute(
        select(SbiRule).where(SbiRule.sheet == sheet)
    )
    return {r.column_letter: _rule_to_dict(r) for r in result.scalars()}


async def get_rule(session: AsyncSession, sheet: str, col: str) -> Optional[Dict[str, Any]]:
    result = await session.execute(
        select(SbiRule).where(SbiRule.sheet == sheet, SbiRule.column_letter == col)
    )
    r = result.scalar_one_or_none()
    if r is None:
        return None
    d = _rule_to_dict(r)
    # Include number_format from sbi_column_formats so the editor can show the current value
    formats = await get_column_formats(session)
    d["number_format"] = formats.get(sheet, {}).get(col)
    return d


async def update_rule(
    session: AsyncSession,
    sheet: str,
    col: str,
    rule_type: str,
    config: Dict[str, Any],
    status: Optional[str] = None,
) -> Dict[str, Any]:
    stmt = (
        pg_insert(SbiRule)
        .values(sheet=sheet, column_letter=col, rule_type=rule_type, config_json=config,
                status=status or "draft")
        .on_conflict_do_update(
            constraint="uq_sbi_rules_sheet_col",
            set_=dict(rule_type=rule_type, config_json=config,
                      updated_at=datetime.now(timezone.utc),
                      **({} if status is None else {"status": status})),
        )
        .returning(SbiRule)
    )
    result = await session.execute(stmt)
    await session.commit()
    return _rule_to_dict(result.scalar_one())


async def upsert_rule_if_missing(session: AsyncSession, sheet: str, col: str,
                                  rule_type: str, config: Dict[str, Any],
                                  notes: str = "") -> None:
    """Insert only if (sheet, col) does not exist — used during bootstrap seeding."""
    stmt = (
        pg_insert(SbiRule)
        .values(sheet=sheet, column_letter=col, rule_type=rule_type,
                config_json=config, notes=notes)
        .on_conflict_do_nothing(constraint="uq_sbi_rules_sheet_col")
    )
    await session.execute(stmt)


async def bulk_import_rules(
    session: AsyncSession,
    rules: List[Dict[str, Any]],
) -> int:
    """Upsert a list of exported rule dicts in a single transaction.

    Skips entries missing sheet / column_letter / rule_type.
    Returns the number of rules actually written.
    """
    count = 0
    for r in rules:
        sheet     = r.get("sheet")
        col       = r.get("column_letter")
        rule_type = r.get("rule_type")
        config    = r.get("config_json") or {}
        # config_json may arrive as a JSON string (seeded via format_spec or
        # exported from a production system that stores it as TEXT). Parse it
        # to a dict so JSONB always stores a proper object, not a string literal.
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (ValueError, TypeError):
                config = {}
        status    = r.get("status") or "draft"
        notes     = r.get("notes") or ""
        if not sheet or not col or not rule_type:
            continue
        stmt = (
            pg_insert(SbiRule)
            .values(sheet=sheet, column_letter=col, rule_type=rule_type,
                    config_json=config, status=status, notes=notes)
            .on_conflict_do_update(
                constraint="uq_sbi_rules_sheet_col",
                set_=dict(
                    rule_type=rule_type,
                    config_json=config,
                    status=status,
                    notes=notes,
                    updated_at=datetime.now(timezone.utc),
                ),
            )
        )
        await session.execute(stmt)
        count += 1
        # Also persist number_format if present in the import payload
        number_format = r.get("number_format")
        if number_format:
            nf_stmt = (
                pg_insert(SbiColumnFormat)
                .values(sheet=sheet, column_letter=col, number_format=number_format)
                .on_conflict_do_update(
                    index_elements=["sheet", "column_letter"],
                    set_={"number_format": number_format},
                )
            )
            await session.execute(nf_stmt)
    await session.commit()
    return count


def _rule_to_dict(r: SbiRule) -> Dict[str, Any]:
    return {
        "id": r.id, "sheet": r.sheet, "column_letter": r.column_letter,
        "rule_type": r.rule_type, "config_json": r.config_json,
        "notes": r.notes, "status": r.status,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ── Lookup tables ────────────────────────────────────────────────────────────

async def list_lookups(session: AsyncSession) -> List[str]:
    result = await session.execute(select(SbiLookupTable.name).order_by(SbiLookupTable.name))
    return list(result.scalars())


async def get_lookup(session: AsyncSession, name: str) -> List[Dict[str, Any]]:
    result = await session.execute(
        select(SbiLookupTable.data_json).where(SbiLookupTable.name == name)
    )
    row = result.scalar_one_or_none()
    return row if row is not None else []


async def set_lookup(session: AsyncSession, name: str, data: List[Dict[str, Any]]) -> None:
    stmt = (
        pg_insert(SbiLookupTable)
        .values(name=name, data_json=data)
        .on_conflict_do_update(constraint="uq_sbi_lookup_name", set_={"data_json": data})
    )
    await session.execute(stmt)
    await session.commit()


async def upsert_lookup_if_missing(session: AsyncSession, name: str,
                                    data: List[Dict[str, Any]]) -> None:
    stmt = (
        pg_insert(SbiLookupTable)
        .values(name=name, data_json=data)
        .on_conflict_do_nothing(constraint="uq_sbi_lookup_name")
    )
    await session.execute(stmt)


# ── Runs ─────────────────────────────────────────────────────────────────────

def _run_to_dict(r: SbiRun) -> Dict[str, Any]:
    return {
        "month": r.month,
        "raw_file_path": r.raw_file_path,
        "row_count": r.row_count,
        "raw_gmv_mrp": r.raw_gmv_mrp,
        "raw_unique_order_ids": r.raw_unique_order_ids,
        "raw_metrics_from_direct_read": r.raw_metrics_from_direct_read,
        "uploaded_at": r.uploaded_at.isoformat(),
    }


async def list_runs(session: AsyncSession) -> List[Dict[str, Any]]:
    result = await session.execute(select(SbiRun).order_by(SbiRun.month.desc()))
    return [_run_to_dict(r) for r in result.scalars()]


async def get_run(session: AsyncSession, month: str) -> Optional[Dict[str, Any]]:
    r = await session.get(SbiRun, month)
    return _run_to_dict(r) if r else None


async def update_run_recon_metrics(
    session: AsyncSession, month: str, gmv_mrp: float, unique_order_ids: int,
    from_direct_read: bool = False,
) -> None:
    """Persist precomputed raw-file recon metrics.

    Set from_direct_read=True only when the values were computed by reading the
    raw file directly (bypassing load_raw normalisation) — recon uses this flag
    to skip the expensive disk re-read entirely.
    """
    await session.execute(
        sa_update(SbiRun)
        .where(SbiRun.month == month)
        .values(
            raw_gmv_mrp=gmv_mrp,
            raw_unique_order_ids=unique_order_ids,
            raw_metrics_from_direct_read=from_direct_read,
        )
    )
    await session.commit()


async def set_active_month(session: AsyncSession, month: str) -> None:
    """Persist the active month to sbi_current_run without touching sbi_runs.

    Called by activate_run so server restarts restore the last-activated month.
    Does not alter raw_file_path or uploaded_at — only the month pointer.
    """
    # Fetch the current row for its raw_file_path (needed for the NOT NULL column).
    existing = await session.get(SbiCurrentRun, 1)
    raw_path = existing.raw_file_path if existing else None
    rc = existing.row_count if existing else None

    stmt = (
        pg_insert(SbiCurrentRun)
        .values(id=1, raw_file_path=raw_path, month=month, row_count=rc)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"month": month},
        )
    )
    await session.execute(stmt)
    await session.commit()


async def get_current_run(session: AsyncSession) -> Optional[Dict[str, Any]]:
    """Return the singleton current-run record (id=1), or None if never set."""
    r = await session.get(SbiCurrentRun, 1)
    if r is None:
        return None
    return {"raw_file_path": r.raw_file_path, "month": r.month, "row_count": r.row_count}


async def set_current_run(session: AsyncSession, raw_file_path: str,
                           month: str, row_count: int) -> None:
    stored = path_for_storage(raw_file_path)
    # Upsert into sbi_runs
    stmt = (
        pg_insert(SbiRun)
        .values(month=month, raw_file_path=stored, row_count=row_count)
        .on_conflict_do_update(
            index_elements=["month"],
            set_={"raw_file_path": stored, "row_count": row_count,
                  "uploaded_at": datetime.now(timezone.utc)},
        )
    )
    await session.execute(stmt)
    # Upsert singleton current_run
    cr_stmt = (
        pg_insert(SbiCurrentRun)
        .values(id=1, raw_file_path=stored, month=month, row_count=row_count)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"raw_file_path": stored, "month": month, "row_count": row_count,
                  "uploaded_at": datetime.now(timezone.utc)},
        )
    )
    await session.execute(cr_stmt)
    await session.commit()


# ── Run files ─────────────────────────────────────────────────────────────────

async def upsert_run_file(session: AsyncSession, month: str, kind: str,
                           file_path: str, original_filename: Optional[str] = None,
                           row_count: Optional[int] = None) -> None:
    stored = path_for_storage(file_path)
    stmt = (
        pg_insert(SbiRunFile)
        .values(month=month, kind=kind, file_path=stored,
                original_filename=original_filename, row_count=row_count)
        .on_conflict_do_update(
            index_elements=["month", "kind"],
            set_={"file_path": stored, "original_filename": original_filename,
                  "row_count": row_count, "uploaded_at": datetime.now(timezone.utc)},
        )
    )
    await session.execute(stmt)
    await session.commit()


async def get_run_file(session: AsyncSession, month: str, kind: str) -> Optional[Dict[str, Any]]:
    r = await session.get(SbiRunFile, (month, kind))
    if r is None:
        return None
    return {"month": r.month, "kind": r.kind, "file_path": r.file_path,
            "original_filename": r.original_filename, "row_count": r.row_count,
            "uploaded_at": r.uploaded_at.isoformat()}


async def all_run_files(session: AsyncSession) -> List[Dict[str, Any]]:
    result = await session.execute(
        select(SbiRunFile).order_by(SbiRunFile.month.desc(), SbiRunFile.kind)
    )
    return [{"month": r.month, "kind": r.kind, "file_path": r.file_path,
             "original_filename": r.original_filename, "row_count": r.row_count,
             "uploaded_at": r.uploaded_at.isoformat()} for r in result.scalars()]


# ── Column formats ────────────────────────────────────────────────────────────

async def get_column_formats(session: AsyncSession) -> Dict[str, Dict[str, str]]:
    result = await session.execute(select(SbiColumnFormat))
    out: Dict[str, Dict[str, str]] = {}
    for r in result.scalars():
        out.setdefault(r.sheet, {})[r.column_letter] = r.number_format
    return out


async def set_column_format(session: AsyncSession, sheet: str, col: str,
                             number_format: Optional[str]) -> None:
    if not number_format:
        await session.execute(
            delete(SbiColumnFormat).where(
                SbiColumnFormat.sheet == sheet, SbiColumnFormat.column_letter == col
            )
        )
    else:
        stmt = (
            pg_insert(SbiColumnFormat)
            .values(sheet=sheet, column_letter=col, number_format=number_format)
            .on_conflict_do_update(
                index_elements=["sheet", "column_letter"],
                set_={"number_format": number_format},
            )
        )
        await session.execute(stmt)
    await session.commit()


# ── Clients ───────────────────────────────────────────────────────────────────

async def list_clients(session: AsyncSession) -> List[Dict[str, Any]]:
    result = await session.execute(select(SbiClient).order_by(SbiClient.id))
    return [{"id": r.id, "code": r.code, "display_name": r.display_name,
             "active": r.active} for r in result.scalars()]


# ── PF historicals ────────────────────────────────────────────────────────────

async def pf_historicals_list(session: AsyncSession,
                               month_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    q = select(SbiPfHistorical).order_by(SbiPfHistorical.month.desc(), SbiPfHistorical.pf)
    if month_filter:
        q = q.where(SbiPfHistorical.month == month_filter)
    result = await session.execute(q)
    return [{"pf": r.pf, "month": r.month, "pharma_total": r.pharma_total,
             "ahc_total": r.ahc_total, "pf_type": r.pf_type,
             "wallet_limit": r.wallet_limit} for r in result.scalars()]


async def pf_historicals_months(session: AsyncSession) -> List[str]:
    result = await session.execute(
        select(SbiPfHistorical.month).distinct().order_by(SbiPfHistorical.month)
    )
    return list(result.scalars())


async def pf_historicals_month_stats(session: AsyncSession) -> Dict[str, Dict[str, Any]]:
    """One GROUP BY query returning per-month aggregates.

    Replaces the Python-level loop over all rows that was O(N) in memory.
    Returns {month: {pf_count, pharma_total, ahc_total, wallets_filled}}.
    """
    from sqlalchemy import case as sa_case
    result = await session.execute(
        select(
            SbiPfHistorical.month,
            func.count().label("pf_count"),
            func.coalesce(func.sum(SbiPfHistorical.pharma_total), 0).label("pharma_total"),
            func.coalesce(func.sum(SbiPfHistorical.ahc_total), 0).label("ahc_total"),
            func.sum(
                sa_case((SbiPfHistorical.wallet_limit > 0, 1), else_=0)
            ).label("wallets_filled"),
        ).group_by(SbiPfHistorical.month).order_by(SbiPfHistorical.month)
    )
    return {
        row.month: {
            "pf_count": int(row.pf_count),
            "pharma_total": float(row.pharma_total or 0),
            "ahc_total": float(row.ahc_total or 0),
            "wallets_filled": int(row.wallets_filled or 0),
        }
        for row in result
    }


async def pf_historicals_delete_month(session: AsyncSession, month: str) -> int:
    result = await session.execute(
        delete(SbiPfHistorical).where(SbiPfHistorical.month == month)
    )
    await session.commit()
    return result.rowcount


async def bulk_upsert_pf_historicals(session: AsyncSession,
                                      records: List[Dict[str, Any]]) -> None:
    for r in records:
        stmt = (
            pg_insert(SbiPfHistorical)
            .values(pf=str(r["pf"]), month=r["month"],
                    pharma_total=r.get("pharma_total"), ahc_total=r.get("ahc_total"),
                    pf_type=r.get("pf_type"), wallet_limit=r.get("wallet_limit"))
            .on_conflict_do_update(
                index_elements=["pf", "month"],
                set_={"pharma_total": r.get("pharma_total"),
                      "ahc_total": r.get("ahc_total"),
                      "pf_type": r.get("pf_type"),
                      "wallet_limit": r.get("wallet_limit")},
            )
        )
        await session.execute(stmt)
    await session.commit()


# ── Sheet columns ─────────────────────────────────────────────────────────────

async def sheet_columns_for(session: AsyncSession, sheet: str) -> List[Dict[str, Any]]:
    result = await session.execute(
        select(SbiSheetColumn)
        .where(SbiSheetColumn.sheet == sheet)
        .order_by(SbiSheetColumn.position)
    )
    return [_sc_to_dict(r) for r in result.scalars()]


async def add_sheet_column(session: AsyncSession, sheet: str, header: str,
                            col_letter: Optional[str] = None,
                            number_format: Optional[str] = None,
                            notes: Optional[str] = None) -> Dict[str, Any]:
    cols = await sheet_columns_for(session, sheet)
    existing = {c["col_letter"] for c in cols}
    if col_letter:
        col_letter = col_letter.upper()
        if col_letter in existing:
            raise ValueError(f"Column {col_letter} already exists on sheet {sheet}")
    else:
        col_letter = _next_col_letter(existing)
    position = (max((c["position"] for c in cols), default=0) + 1)
    col = SbiSheetColumn(sheet=sheet, position=position, col_letter=col_letter,
                         header=header, number_format=number_format, notes=notes)
    session.add(col)
    await session.commit()
    await session.refresh(col)
    return _sc_to_dict(col)


async def update_sheet_column(session: AsyncSession, sheet: str, col_letter: str,
                               header: Optional[str] = None,
                               number_format: Optional[str] = None) -> Optional[Dict[str, Any]]:
    result = await session.execute(
        select(SbiSheetColumn).where(
            SbiSheetColumn.sheet == sheet, SbiSheetColumn.col_letter == col_letter
        )
    )
    col = result.scalar_one_or_none()
    if col is None:
        return None
    if header is not None:
        col.header = header
    if number_format is not None:
        col.number_format = number_format
    await session.commit()
    await session.refresh(col)
    return _sc_to_dict(col)


async def delete_sheet_column(session: AsyncSession, sheet: str, col_letter: str) -> bool:
    result = await session.execute(
        select(SbiSheetColumn).where(
            SbiSheetColumn.sheet == sheet, SbiSheetColumn.col_letter == col_letter
        )
    )
    col = result.scalar_one_or_none()
    if col is None:
        return False
    await session.delete(col)
    # Also delete associated rule and format
    await session.execute(
        delete(SbiRule).where(SbiRule.sheet == sheet, SbiRule.column_letter == col_letter)
    )
    await session.execute(
        delete(SbiColumnFormat).where(
            SbiColumnFormat.sheet == sheet, SbiColumnFormat.column_letter == col_letter
        )
    )
    # Renumber positions
    remaining = await sheet_columns_for(session, sheet)
    for i, c in enumerate(remaining, start=1):
        await session.execute(
            sa_update(SbiSheetColumn)
            .where(SbiSheetColumn.id == c["id"])
            .values(position=i)
        )
    await session.commit()
    return True


def _sc_to_dict(r: SbiSheetColumn) -> Dict[str, Any]:
    return {"id": r.id, "sheet": r.sheet, "position": r.position,
            "col_letter": r.col_letter, "header": r.header,
            "number_format": r.number_format, "is_system": r.is_system, "notes": r.notes}


def _next_col_letter(existing: set[str]) -> str:
    for a in string.ascii_uppercase:
        if a not in existing:
            return a
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            if (a + b) not in existing:
                return a + b
    raise RuntimeError("No free column letter")


# ── Sync bridges (called from thread pool / pipeline / seeding scripts) ──────
#
# All functions below use AsyncEngine.sync_engine — same connection pool, no
# extra config. Safe to call from run_in_executor threads or standalone scripts.

def _sync_session():
    """Return a synchronous SQLAlchemy Session.

    Uses a psycopg2-backed engine rather than asyncpg's greenlet-based
    sync emulation.  The asyncpg sync_engine requires SQLAlchemy's greenlet
    context to be active (true in production run_in_executor threads once
    the async pool is warm), but fails cold (e.g. standalone scripts).
    psycopg2 works unconditionally from any thread or coroutine context.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SyncSession

    # Convert postgresql+asyncpg:// → postgresql:// (psycopg2 default dialect)
    raw_url = str(settings.database_url)
    sync_url = raw_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+asyncpg:", "postgresql:"
    )

    # Module-level cache — one engine for the process lifetime.
    if _sync_session._engine is None:  # type: ignore[attr-defined]
        _sync_session._engine = create_engine(  # type: ignore[attr-defined]
            sync_url, pool_pre_ping=True, pool_size=3, max_overflow=5
        )
    return SyncSession(_sync_session._engine)  # type: ignore[attr-defined]


_sync_session._engine = None  # type: ignore[attr-defined]


def month_minus(month: str, n: int) -> str:
    """Return the YYYY-MM string that is n months before *month*."""
    y, m = map(int, month.split("-"))
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def get_all_rules_sync() -> List[Dict[str, Any]]:
    """Sync version of get_all_rules — used by pipeline.py in thread pool."""
    with _sync_session() as s:
        result = s.execute(
            select(SbiRule).order_by(SbiRule.sheet, SbiRule.column_letter)
        )
        return [_rule_to_dict(r) for r in result.scalars()]


def list_lookups_sync() -> List[str]:
    """Sync version of list_lookups — used by pipeline.py in thread pool."""
    with _sync_session() as s:
        result = s.execute(select(SbiLookupTable.name).order_by(SbiLookupTable.name))
        return list(result.scalars())


def get_lookup_sync(name: str) -> List[Dict[str, Any]]:
    """Sync version of get_lookup — used by pipeline.py in thread pool."""
    with _sync_session() as s:
        result = s.execute(
            select(SbiLookupTable.data_json).where(SbiLookupTable.name == name)
        )
        row = result.scalar_one_or_none()
        return row if row is not None else []


def get_column_formats_sync() -> Dict[str, Dict[str, str]]:
    """Sync version of get_column_formats — used by export.py in thread pool."""
    with _sync_session() as s:
        result = s.execute(select(SbiColumnFormat))
        out: Dict[str, Dict[str, str]] = {}
        for r in result.scalars():
            out.setdefault(r.sheet, {})[r.column_letter] = r.number_format
        return out


def get_run_file_sync(month: str, kind: str) -> Optional[Dict[str, Any]]:
    """Sync version of get_run_file — used by pipeline.py in thread pool."""
    with _sync_session() as s:
        r = s.get(SbiRunFile, (month, kind))
        if r is None:
            return None
        return {"month": r.month, "kind": r.kind, "file_path": r.file_path,
                "original_filename": r.original_filename, "row_count": r.row_count,
                "uploaded_at": r.uploaded_at.isoformat()}


def bulk_upsert_pf_historicals_sync(records: List[Dict[str, Any]]) -> None:
    """Sync version of bulk_upsert_pf_historicals — used by pipeline.py in thread pool."""
    with _sync_session() as s:
        for r in records:
            stmt = (
                pg_insert(SbiPfHistorical)
                .values(pf=str(r["pf"]), month=r["month"],
                        pharma_total=r.get("pharma_total"), ahc_total=r.get("ahc_total"),
                        pf_type=r.get("pf_type"), wallet_limit=r.get("wallet_limit"))
                .on_conflict_do_update(
                    index_elements=["pf", "month"],
                    set_={"pharma_total": r.get("pharma_total"),
                          "ahc_total": r.get("ahc_total"),
                          "pf_type": r.get("pf_type"),
                          "wallet_limit": r.get("wallet_limit")},
                )
            )
            s.execute(stmt)
        s.commit()


def pf_historicals_for_months_sync(months: List[str]) -> Dict[str, Dict[str, Any]]:
    """Return {pf: {month: {pharma_total, ahc_total, pf_type, wallet_limit}}} for the
    given months. Sync version for use by pipeline.py in thread pool."""
    with _sync_session() as s:
        result = s.execute(
            select(SbiPfHistorical).where(SbiPfHistorical.month.in_(months))
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in result.scalars():
            out.setdefault(str(r.pf), {})[r.month] = {
                "pharma_total": r.pharma_total,
                "ahc_total": r.ahc_total,
                "pf_type": r.pf_type,
                "wallet_limit": r.wallet_limit,
            }
        return out


def get_latest_wallet_limits_sync() -> Dict[str, float]:
    """Return {pf: wallet_limit} — the most recent non-null wallet_limit per PF.

    Used as a fallback for PF Summary column C when no wallet_checker is uploaded.
    Queries pf_historicals ordered by month DESC so the first hit per PF is the
    most recent entry.
    """
    with _sync_session() as s:
        result = s.execute(
            select(SbiPfHistorical.pf, SbiPfHistorical.wallet_limit)
            .where(SbiPfHistorical.wallet_limit.isnot(None))
            .order_by(SbiPfHistorical.month.desc())
        )
        out: Dict[str, float] = {}
        for pf, wl in result:
            pf_str = str(pf)
            if pf_str not in out:
                out[pf_str] = float(wl)
        return out


# ── Sync bridge (for format_spec.effective_columns called from thread pool) ──

def sheet_columns_for_sync(sheet: str) -> List[Dict[str, Any]]:
    """Synchronous version of sheet_columns_for.

    Used by format_spec.effective_columns(), which is called from:
      - pipeline.py running inside run_in_executor (thread pool, no event loop)
      - export.py running inside run_in_executor
      - the router preview endpoint (sync context inside async handler)

    Uses AsyncEngine.sync_engine so no second connection pool is created.
    """
    from sqlalchemy.orm import Session as SyncSession
    from app.db.session import engine as _async_engine

    with SyncSession(_async_engine.sync_engine) as session:
        result = session.execute(
            select(SbiSheetColumn)
            .where(SbiSheetColumn.sheet == sheet)
            .order_by(SbiSheetColumn.position)
        )
        return [_sc_to_dict(r) for r in result.scalars()]
