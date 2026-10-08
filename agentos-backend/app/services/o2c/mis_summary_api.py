"""MIS summary row mutations with HTTP-friendly errors."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from app.agents.o2c_ohc.mis_db import (
    create_contract_rate_line_and_mis_summary_row,
    delete_mis_summary_row,
    insert_mis_summary_row_from_rate_line,
    restore_mis_summary_row,
    set_mis_status,
)


async def delete_mis_summary_row_api(
    *,
    mis_summary_row_id: UUID,
    deleted_by: str | None,
) -> dict[str, Any]:
    try:
        ok = await delete_mis_summary_row(
            mis_summary_row_id=mis_summary_row_id,
            deleted_by=deleted_by,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MIS summary row not found")
    return {"status": "ok", "deleted": True}


async def restore_mis_summary_row_api(
    *, mis_summary_row_id: UUID, restored_by: str
) -> dict[str, Any]:
    try:
        out = await restore_mis_summary_row(
            mis_summary_row_id=mis_summary_row_id,
            restored_by=restored_by or "human",
        )
        return {"status": "ok", **out}
    except ValueError as e:
        msg = str(e)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, msg) from e


async def insert_mis_summary_row_api(
    *,
    mis_run_id: UUID,
    contract_rate_line_id: UUID,
    created_by: str | None,
) -> dict[str, Any]:
    try:
        new_id = await insert_mis_summary_row_from_rate_line(
            mis_run_id=mis_run_id,
            contract_rate_line_id=contract_rate_line_id,
            created_by=created_by,
        )
        return {"status": "ok", "mis_summary_row_id": str(new_id)}
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


async def create_new_mis_summary_row_api(
    *,
    mis_run_id: UUID,
    description: str,
    role_code: str,
    rate_amount: Decimal | None,
    contracted_quantity: Decimal | None,
    billing_model: str,
    visit_per_month: Decimal | None,
    visits_per_week: Decimal | None,
    invoice_admin_pct: Decimal | None,
    invoice_admin_base: str | None,
    created_by: str,
) -> dict[str, Any]:
    try:
        out = await create_contract_rate_line_and_mis_summary_row(
            mis_run_id=mis_run_id,
            description=description,
            role_code=role_code,
            rate_amount=rate_amount,
            contracted_quantity=contracted_quantity,
            billing_model=billing_model,
            visit_per_month=visit_per_month,
            visits_per_week=visits_per_week,
            invoice_admin_pct=invoice_admin_pct,
            invoice_admin_base=invoice_admin_base,
            created_by=created_by,
        )
        return {"status": "ok", **out}
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg) from e
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg) from e


async def set_mis_status_api(
    *,
    mis_run_id: UUID,
    status: str,
    actor: str | None,
    rejection_notes: str | None,
) -> dict[str, Any]:
    try:
        await set_mis_status(
            mis_run_id=mis_run_id,
            status=status,
            actor=actor,
            rejection_notes=rejection_notes,
        )
        return {"status": "ok", "mis_status": status}
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
