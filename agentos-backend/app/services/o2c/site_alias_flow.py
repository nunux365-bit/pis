"""Site alias upsert + optional previous-month MIS rerun."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from app.services.o2c.dates_util import prev_month_range
from app.services.o2c.mis_rerun import run_single_site_mis_rerun_async
from app.services.o2c.site_alias_ops import site_alias_upsert_transaction


async def site_alias_upsert_with_optional_mis_rerun(
    *,
    service_site_id: UUID,
    alias_code: str,
    alias_display: str | None,
    source_system: str,
    verified_by: str,
    auto_run_previous_month_mis: bool,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any]:
    alias = (alias_code or "").strip()
    if not alias:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "alias_code is required")
    src = (source_system or "").strip() or "manual"

    site_d = await site_alias_upsert_transaction(
        service_site_id=str(service_site_id),
        alias_code=alias,
        alias_display=alias_display,
        source_system=src,
        verified_by=(verified_by or "unknown")[:200],
    )

    rerun_out: dict[str, Any] | None = None
    if auto_run_previous_month_mis:
        d0, d1 = prev_month_range(date.today())
        rerun_out = await run_single_site_mis_rerun_async(
            alias_code=alias,
            service_site_id=str(service_site_id),
            period_start=d0,
            period_end=d1,
            allow_expired_contract_terms=allow_expired_contract_terms,
        )

    return {
        "status": "ok",
        "alias": {
            "source_system": src,
            "alias_code": alias,
            "alias_display": alias_display or alias,
            "service_site_id": str(service_site_id),
        },
        "site": site_d,
        "mis_rerun_previous_month": rerun_out,
    }
