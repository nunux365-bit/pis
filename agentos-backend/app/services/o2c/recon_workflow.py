"""Attendance recon resolve + optional MIS rerun (orchestration)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from app.agents.o2c_ohc.attendance_site_recon import (
    list_open_o2c_attendance_site_recon,
    resolve_o2c_attendance_site_recon,
)
from app.services.o2c.dates_util import coerce_sql_date, prev_month_range
from app.services.o2c.mis_rerun import run_single_site_mis_rerun_async
from app.services.o2c.site_alias_ops import attendance_recon_resolve_transaction


async def list_open_attendance_recon(*, limit: int) -> dict[str, Any]:
    rows = await list_open_o2c_attendance_site_recon(limit=limit)
    return {"count": len(rows), "items": rows}


async def resolve_attendance_recon_simple(
    *,
    recon_id: UUID,
    service_site_id: UUID,
    alias_code: str,
    resolution_notes: str | None,
    resolved_by: str | None,
) -> dict[str, Any]:
    try:
        ok = await resolve_o2c_attendance_site_recon(
            recon_id=recon_id,
            service_site_id=service_site_id,
            alias_code=alias_code,
            resolved_by=resolved_by,
            resolution_notes=resolution_notes,
        )
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Recon row not found")
        return {"status": "ok", "resolved": True}
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


async def resolve_attendance_recon_and_rerun(
    *,
    recon_id: UUID,
    service_site_id: UUID,
    alias_code: str | None,
    alias_display: str | None,
    source_system: str,
    use_recon_period: bool,
    auto_run_previous_month_mis: bool,
    allow_alias_reassign: bool,
    allow_expired_contract_terms: bool,
    resolved_by: str,
) -> dict[str, Any]:
    who = (resolved_by or "unknown").strip()[:200] or "unknown"
    recon_d, site_d, alias, src = await attendance_recon_resolve_transaction(
        recon_id=str(recon_id),
        service_site_id=str(service_site_id),
        alias_code=alias_code,
        alias_display=alias_display,
        source_system=source_system,
        resolved_by=who,
        allow_alias_reassign=allow_alias_reassign,
    )

    if use_recon_period:
        d0 = coerce_sql_date(recon_d["billing_period_start"])
        d1 = coerce_sql_date(recon_d["billing_period_end"])
    else:
        d0, d1 = prev_month_range(date.today())

    rerun_out: dict[str, Any] | None = None
    if auto_run_previous_month_mis:
        rerun_out = await run_single_site_mis_rerun_async(
            alias_code=alias,
            service_site_id=str(service_site_id),
            period_start=d0,
            period_end=d1,
            allow_expired_contract_terms=allow_expired_contract_terms,
        )

    return {
        "status": "ok",
        "resolved": True,
        "recon_id": str(recon_id),
        "alias": {
            "source_system": src,
            "alias_code": alias,
            "alias_display": alias_display or alias,
            "service_site_id": str(service_site_id),
        },
        "site": site_d,
        "mis_rerun": rerun_out,
    }
