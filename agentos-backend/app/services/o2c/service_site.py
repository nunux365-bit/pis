"""Manual service_site creation with recon cleanup, optional clone + MIS rerun."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.site_rate_line_clone import clone_site_scoped_rate_lines
from app.services.o2c.dates_util import prev_month_range
from app.services.o2c.mis_rerun import run_single_site_mis_rerun_async


def _parse_iso_date(value: str | None, field_label: str) -> date:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{field_label} must be YYYY-MM-DD",
        ) from e


async def create_service_site_with_recon(
    *,
    site_key: str,
    display_name: str,
    billing_client_id: UUID,
    recon_id: UUID,
    alias_code: str,
    verified_by: str,
    clone_rate_lines_from_site_id: UUID | None,
    replace_existing_site_rate_lines: bool,
    require_same_terms_for_period: bool,
    clone_terms_period_start: str | None,
    clone_terms_period_end: str | None,
    auto_run_previous_month_mis: bool,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any]:
    who = (verified_by or "unknown").strip()[:200]
    sk = (site_key or "").strip()
    display = (display_name or sk).strip()
    alias = (alias_code or "").strip()
    if not sk:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "site_key is required")
    if not alias:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "alias_code is required — use the attendance site name from an open item in the recon queue",
        )

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            bc = await session.execute(
                text("SELECT 1 FROM billing_client WHERE id = CAST(:id AS uuid)"),
                {"id": str(billing_client_id)},
            )
            if bc.mappings().first() is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "billing_client_id not found")

            ins = await session.execute(
                text("""
                INSERT INTO service_site (
                    id, billing_client_id, site_key, display_name, canonical_name, is_active, created_at
                ) VALUES (gen_random_uuid(), CAST(:bc AS uuid), :sk, :disp, :canon, TRUE, now())
                RETURNING id::text AS id
                """),
                {
                    "bc": str(billing_client_id),
                    "sk": sk[:500],
                    "disp": display[:500],
                    "canon": sk[:500],
                },
            )
            new_site = ins.mappings().first()
            if not new_site:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR, "service_site insert failed"
                )
            new_site_id = str(new_site["id"])

            await session.execute(
                text("""
                INSERT INTO site_alias (
                    id, service_site_id, source_system, alias_code, alias_display, verified_by, verified_at
                ) VALUES (gen_random_uuid(), CAST(:sid AS uuid), 'manual', :ac, :ad, :who, now())
                ON CONFLICT (source_system, alias_code) DO UPDATE SET
                    service_site_id = EXCLUDED.service_site_id,
                    alias_display = EXCLUDED.alias_display,
                    verified_by = EXCLUDED.verified_by,
                    verified_at = EXCLUDED.verified_at
                """),
                {"sid": new_site_id, "ac": alias[:500], "ad": display[:500], "who": who},
            )

            rr = await session.execute(
                text("""
                SELECT client_site_key, status
                FROM o2c_attendance_site_recon
                WHERE id = CAST(:rid AS uuid)
                FOR UPDATE
                """),
                {"rid": str(recon_id)},
            )
            rrow = rr.mappings().first()
            if not rrow:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "recon_id not found")
            rrow_d = dict(rrow)
            if str(rrow_d.get("status") or "").strip().lower() != "open":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Recon row is not open; refresh and retry",
                )
            rkey = (rrow_d.get("client_site_key") or "").strip()
            if rkey != alias:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "alias_code must exactly match the selected recon row's attendance name",
                )
            delr = await session.execute(
                text(
                    "DELETE FROM o2c_attendance_site_recon WHERE id = CAST(:rid AS uuid) AND status = 'open'"
                ),
                {"rid": str(recon_id)},
            )
            if (delr.rowcount or 0) != 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Could not clear recon row; refresh and retry",
                )

    clone_out: dict[str, Any] | None = None
    if clone_rate_lines_from_site_id is not None:
        ps: date | None = None
        pe: date | None = None
        if require_same_terms_for_period:
            if not clone_terms_period_start or not clone_terms_period_end:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "clone_terms_period_start and clone_terms_period_end are required when "
                    "require_same_terms_for_period is true",
                )
            ps = _parse_iso_date(clone_terms_period_start, "clone_terms_period_start")
            pe = _parse_iso_date(clone_terms_period_end, "clone_terms_period_end")
        clone_out = await clone_site_scoped_rate_lines(
            source_service_site_id=str(clone_rate_lines_from_site_id),
            target_service_site_id=new_site_id,
            replace_existing=replace_existing_site_rate_lines,
            require_same_terms_for_period=require_same_terms_for_period,
            period_start=ps,
            period_end=pe,
        )

    rerun_out: dict[str, Any] | None = None
    if auto_run_previous_month_mis and alias:
        d0, d1 = prev_month_range(date.today())
        rerun_out = await run_single_site_mis_rerun_async(
            alias_code=alias,
            service_site_id=new_site_id,
            period_start=d0,
            period_end=d1,
            allow_expired_contract_terms=allow_expired_contract_terms,
        )

    return {
        "status": "ok",
        "service_site_id": new_site_id,
        "site_key": sk,
        "display_name": display,
        "alias_code": alias,
        "recon_id": str(recon_id),
        "rate_line_clone": clone_out,
        "mis_rerun": rerun_out,
    }


async def clone_site_rate_lines_for_api(
    *,
    source_service_site_id: UUID,
    target_service_site_id: UUID,
    replace_existing: bool,
    require_same_terms_for_period: bool,
    period_start: str | None,
    period_end: str | None,
) -> dict[str, Any]:
    ps: date | None = None
    pe: date | None = None
    if require_same_terms_for_period:
        if not period_start or not period_end:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "period_start and period_end are required when require_same_terms_for_period is true",
            )
        ps = _parse_iso_date(period_start, "period_start")
        pe = _parse_iso_date(period_end, "period_end")
    return await clone_site_scoped_rate_lines(
        source_service_site_id=str(source_service_site_id),
        target_service_site_id=str(target_service_site_id),
        replace_existing=replace_existing,
        require_same_terms_for_period=require_same_terms_for_period,
        period_start=ps,
        period_end=pe,
    )
