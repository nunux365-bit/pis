"""
Persist recon rows when attendance sites do not get an MIS draft (unresolved site, no contract, etc.).

All DB access is **async** (``AgenosAsyncSessionLocal``). Sync call sites (MIS drafts, LangGraph)
use thin wrappers that delegate to ``run_agenos_async``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Any, Protocol
from uuid import UUID

import asyncpg
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async

log = logging.getLogger(__name__)


class _O2cSiteSkipLike(Protocol):
    client_site_key: str
    reason_code: str
    detail: str
    attendance_row_count: int
    llm_match_attempted: bool


SOURCE_MANUAL = "manual"


def _is_undefined_table(exc: BaseException) -> bool:
    if isinstance(exc, ProgrammingError):
        orig = getattr(exc, "orig", None) or getattr(exc, "__cause__", None)
        if isinstance(orig, asyncpg.exceptions.UndefinedTableError):
            return True
        if "undefined_table" in str(exc).lower():
            return True
    return False


async def persist_o2c_attendance_site_recon_async(
    skips: Sequence[_O2cSiteSkipLike],
    *,
    period_start: date,
    period_end: date,
) -> int:
    if not skips:
        return 0
    n = 0
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                for s in skips:
                    r = await session.execute(
                        text("""
                        INSERT INTO o2c_attendance_site_recon (
                            client_site_key, billing_period_start, billing_period_end,
                            reason_code, detail, attendance_row_count, llm_match_attempted,
                            status, first_seen_at, last_seen_at
                        ) VALUES (
                            :csk, :d0, :d1, :rc, :det, :arc, :llm, 'open', now(), now()
                        )
                        ON CONFLICT (client_site_key, billing_period_start, billing_period_end, reason_code)
                        DO UPDATE SET
                            detail = EXCLUDED.detail,
                            attendance_row_count = EXCLUDED.attendance_row_count,
                            llm_match_attempted = o2c_attendance_site_recon.llm_match_attempted
                                OR EXCLUDED.llm_match_attempted,
                            last_seen_at = now()
                        WHERE o2c_attendance_site_recon.status = 'open'
                        """),
                        {
                            "csk": s.client_site_key,
                            "d0": period_start,
                            "d1": period_end,
                            "rc": s.reason_code,
                            "det": (s.detail or "")[:10000],
                            "arc": s.attendance_row_count,
                            "llm": s.llm_match_attempted,
                        },
                    )
                    n += r.rowcount or 0
        return n
    except ProgrammingError as e:
        if _is_undefined_table(e):
            log.warning(
                "o2c_attendance_site_recon table missing; skip DB recon persist. "
                "Apply alembic 008_o2c_recon or docs/agenos/DDL_PATCH_O2C.sql."
            )
            return 0
        raise


def persist_o2c_attendance_site_recon(
    skips: Sequence[_O2cSiteSkipLike],
    *,
    period_start: date,
    period_end: date,
) -> int:
    """Sync entry for MIS drafts / graph (no running event loop)."""
    return run_agenos_async(
        persist_o2c_attendance_site_recon_async(skips, period_start=period_start, period_end=period_end)
    )


async def clear_open_o2c_attendance_site_recon_for_period_key_async(
    *,
    client_site_key: str,
    period_start: date,
    period_end: date,
) -> int:
    k = (client_site_key or "").strip()
    if not k:
        return 0
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(
                    text("""
                    DELETE FROM o2c_attendance_site_recon
                    WHERE client_site_key = :csk
                      AND billing_period_start = :d0
                      AND billing_period_end = :d1
                      AND status = 'open'
                    """),
                    {"csk": k, "d0": period_start, "d1": period_end},
                )
                return r.rowcount or 0
    except ProgrammingError as e:
        if _is_undefined_table(e):
            log.warning(
                "o2c_attendance_site_recon table missing; skip recon clear. "
                "Apply alembic 008_o2c_recon or docs/agenos/DDL_PATCH_O2C.sql."
            )
            return 0
        raise


def clear_open_o2c_attendance_site_recon_for_period_key(
    *,
    client_site_key: str,
    period_start: date,
    period_end: date,
) -> int:
    return run_agenos_async(
        clear_open_o2c_attendance_site_recon_for_period_key_async(
            client_site_key=client_site_key,
            period_start=period_start,
            period_end=period_end,
        )
    )


async def list_open_o2c_attendance_site_recon_async(*, limit: int = 500) -> list[dict[str, Any]]:
    n = max(1, min(int(limit), 5000))
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(
                    text("""
                    SELECT id, client_site_key, billing_period_start, billing_period_end,
                           reason_code, detail, attendance_row_count, llm_match_attempted,
                           status, first_seen_at, last_seen_at
                    FROM o2c_attendance_site_recon
                    WHERE status = 'open'
                    ORDER BY last_seen_at DESC
                    LIMIT :lim
                    """),
                    {"lim": n},
                )
                return [dict(row) for row in r.mappings().all()]
    except ProgrammingError as e:
        if _is_undefined_table(e):
            return []
        raise


async def resolve_o2c_attendance_site_recon_async(
    *,
    recon_id: UUID,
    service_site_id: UUID,
    alias_code: str,
    resolved_by: str,
    resolution_notes: str | None = None,
) -> bool:
    alias = (alias_code or "").strip()
    if not alias:
        raise ValueError("alias_code is required")
    who = (resolved_by or "").strip()[:200] or "unknown"
    notes = (resolution_notes or "").strip()[:2000] or None
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r0 = await session.execute(
                    text("SELECT 1 FROM service_site WHERE id = CAST(:sid AS uuid)"),
                    {"sid": str(service_site_id)},
                )
                if r0.mappings().first() is None:
                    raise ValueError("service_site_id not found")
                await session.execute(
                    text("""
                    INSERT INTO site_alias (
                        id, service_site_id, source_system, alias_code, alias_display, verified_by, verified_at
                    ) VALUES (gen_random_uuid(), CAST(:sid AS uuid), :src, :ac, :ad, :who, now())
                    ON CONFLICT (source_system, alias_code) DO UPDATE SET
                        service_site_id = EXCLUDED.service_site_id,
                        alias_display = COALESCE(EXCLUDED.alias_display, site_alias.alias_display),
                        verified_by = EXCLUDED.verified_by,
                        verified_at = EXCLUDED.verified_at
                    """),
                    {
                        "sid": str(service_site_id),
                        "src": SOURCE_MANUAL,
                        "ac": alias[:500],
                        "ad": alias[:500],
                        "who": who,
                    },
                )
                r2 = await session.execute(
                    text("""
                    UPDATE o2c_attendance_site_recon
                    SET status = 'resolved',
                        resolved_service_site_id = CAST(:sid AS uuid),
                        resolved_at = now(),
                        resolved_by = :who,
                        resolution_notes = :notes
                    WHERE id = CAST(:rid AS uuid)
                    """),
                    {
                        "sid": str(service_site_id),
                        "who": who,
                        "notes": notes,
                        "rid": str(recon_id),
                    },
                )
                updated = r2.rowcount or 0
                await session.execute(
                    text("DELETE FROM o2c_attendance_site_recon WHERE id = CAST(:rid AS uuid)"),
                    {"rid": str(recon_id)},
                )
                return updated > 0
    except ProgrammingError as e:
        if _is_undefined_table(e):
            raise ValueError("o2c_attendance_site_recon table missing") from e
        raise


async def list_open_o2c_attendance_site_recon(*, limit: int = 500) -> list[dict[str, Any]]:
    """Async API (FastAPI routes)."""
    return await list_open_o2c_attendance_site_recon_async(limit=limit)


async def resolve_o2c_attendance_site_recon(
    *,
    recon_id: UUID,
    service_site_id: UUID,
    alias_code: str,
    resolved_by: str,
    resolution_notes: str | None = None,
) -> bool:
    """Async API (FastAPI routes)."""
    return await resolve_o2c_attendance_site_recon_async(
        recon_id=recon_id,
        service_site_id=service_site_id,
        alias_code=alias_code,
        resolved_by=resolved_by,
        resolution_notes=resolution_notes,
    )
