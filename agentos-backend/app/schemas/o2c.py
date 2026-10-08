"""Pydantic models shared by O2C contract review routes and services."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, Field


class ContractReviewStatusBody(BaseModel):
    contract_terms_version_id: UUID
    status: str = Field(..., min_length=1, max_length=50)
    period_start: str | None = Field(None, min_length=10, max_length=10)
    period_end: str | None = Field(None, min_length=10, max_length=10)


class ContractReviewHeaderPatchBody(BaseModel):
    effective_from: str | None = Field(None, min_length=10, max_length=10)
    effective_to: str | None = Field(None, min_length=10, max_length=10)
    clear_effective_to: bool = False


class ContractRateLinePatchItem(BaseModel):
    id: UUID
    billing_model: str | None = Field(None, max_length=100)
    role_code: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)
    rate_amount: Decimal | None = None
    rate_unit: str | None = Field(None, max_length=100)
    contracted_quantity: Decimal | None = None
    attendance_required: bool | None = None
    minimum_units_per_period: Decimal | None = None
    unfilled_penalty_pct: Decimal | None = None
    ot_multiplier: Decimal | None = None
    service_charge_type: str | None = Field(None, max_length=50)
    service_charge_value: Decimal | None = None
    actuals_markup_pct: Decimal | None = None
    schedule_type: str | None = Field(None, max_length=50)
    schedule_config: dict[str, Any] | None = None
    billing_rules: dict[str, Any] | None = None
    billing_rule_text: str | None = None
    currency: str | None = Field(None, max_length=20)
    effective_from: str | None = Field(None, min_length=10, max_length=10)
    effective_to: str | None = Field(None, min_length=10, max_length=10)
    is_active: bool | None = None


class ContractRateLineBulkPatchBody(BaseModel):
    items: list[ContractRateLinePatchItem]


def validate_contract_rate_line_patch(item: ContractRateLinePatchItem) -> None:
    if item.rate_amount is not None and item.rate_amount < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"rate_amount must be >= 0 for line {item.id}")
    if item.contracted_quantity is not None and item.contracted_quantity < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"contracted_quantity must be >= 0 for line {item.id}"
        )
    if item.minimum_units_per_period is not None and item.minimum_units_per_period < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"minimum_units_per_period must be >= 0 for line {item.id}",
        )
    if item.unfilled_penalty_pct is not None and item.unfilled_penalty_pct < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unfilled_penalty_pct must be >= 0 for line {item.id}"
        )
    if item.actuals_markup_pct is not None and item.actuals_markup_pct < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"actuals_markup_pct must be >= 0 for line {item.id}"
        )
    if item.ot_multiplier is not None and item.ot_multiplier < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"ot_multiplier must be >= 0 for line {item.id}")
    if item.service_charge_value is not None and item.service_charge_value < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"service_charge_value must be >= 0 for line {item.id}"
        )
    if item.billing_model is not None:
        bm = item.billing_model.strip().lower()
        if bm and bm not in {"fixed_monthly", "rate_attendance", "as_per_actuals", "per_visit", "per_head"}:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unsupported billing_model '{item.billing_model}' for line {item.id}",
            )
