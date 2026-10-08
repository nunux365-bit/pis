"""Async agenos transactions for attendance recon resolution and site_alias upsert."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal


async def attendance_recon_resolve_transaction(
    *,
    recon_id: str,
    service_site_id: str,
    alias_code: str | None,
    alias_display: str | None,
    source_system: str,
    resolved_by: str,
    allow_alias_reassign: bool,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """
    Lock recon row, validate site + alias, upsert site_alias, delete open recon.
    Returns (recon_row dict with period dates, site dict, resolved alias string, source_system).
    """
    src = (source_system or "").strip() or "manual"
    who = (resolved_by or "unknown").strip()[:200] or "unknown"

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            rr = await session.execute(
                text("""
                SELECT id::text AS id, client_site_key, billing_period_start, billing_period_end, status
                FROM o2c_attendance_site_recon
                WHERE id = CAST(:rid AS uuid)
                FOR UPDATE
                """),
                {"rid": str(recon_id)},
            )
            recon = rr.mappings().first()
            if not recon:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Recon row not found")
            recon_d = dict(recon)
            if str(recon_d.get("status") or "").strip().lower() != "open":
                raise HTTPException(status.HTTP_409_CONFLICT, "Recon row is not open")

            alias = (alias_code or recon_d.get("client_site_key") or "").strip()
            if not alias:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "alias_code is required")

            sr = await session.execute(
                text("""
                SELECT
                    ss.id::text AS service_site_id,
                    ss.billing_client_id::text AS billing_client_id,
                    COALESCE(ss.site_key, ss.canonical_name, ss.display_name, ss.id::text) AS site_key,
                    COALESCE(ss.display_name, '') AS display_name
                FROM service_site ss
                WHERE ss.id = CAST(:sid AS uuid)
                """),
                {"sid": str(service_site_id)},
            )
            site = sr.mappings().first()
            if not site:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "service_site_id not found")
            site_d = dict(site)

            ar = await session.execute(
                text("""
                SELECT service_site_id::text AS service_site_id
                FROM site_alias
                WHERE source_system = :src AND alias_code = :ac
                """),
                {"src": src[:100], "ac": alias[:500]},
            )
            existing_alias = ar.mappings().first()
            if (
                existing_alias
                and str(existing_alias.get("service_site_id") or "").strip() != str(service_site_id)
                and not allow_alias_reassign
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "alias_code already mapped to another site; pass allow_alias_reassign=true to move it",
                )

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
                    "src": src[:100],
                    "ac": alias[:500],
                    "ad": (alias_display or alias)[:500],
                    "who": who,
                },
            )

            delr = await session.execute(
                text(
                    "DELETE FROM o2c_attendance_site_recon WHERE id = CAST(:rid AS uuid) AND status = 'open'"
                ),
                {"rid": str(recon_id)},
            )
            if (delr.rowcount or 0) != 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "Recon row resolution conflict; refresh and retry"
                )

    return recon_d, site_d, alias, src


async def site_alias_upsert_transaction(
    *,
    service_site_id: str,
    alias_code: str,
    alias_display: str | None,
    source_system: str,
    verified_by: str,
) -> dict[str, Any]:
    """Upsert site_alias; returns service_site row as dict for the response payload."""
    src = (source_system or "").strip() or "manual"
    alias = (alias_code or "").strip()
    who = (verified_by or "unknown").strip()[:200] or "unknown"

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            sr = await session.execute(
                text("""
                SELECT
                    ss.id::text AS service_site_id,
                    ss.billing_client_id::text AS billing_client_id,
                    COALESCE(ss.site_key, ss.canonical_name, ss.display_name, ss.id::text) AS site_key,
                    COALESCE(ss.display_name, '') AS display_name
                FROM service_site ss
                WHERE ss.id = CAST(:sid AS uuid)
                """),
                {"sid": str(service_site_id)},
            )
            site = sr.mappings().first()
            if not site:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "service_site_id not found")
            site_d = dict(site)

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
                    "src": src[:100],
                    "ac": alias[:500],
                    "ad": (alias_display or alias)[:500],
                    "who": who,
                },
            )

    return site_d
