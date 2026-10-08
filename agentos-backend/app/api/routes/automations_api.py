"""User automation rules and pattern-learning suggestions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import AutomationRule, AutomationSuggestion, User
from app.db.session import get_db

router = APIRouter()


class RuleOut(BaseModel):
    id: UUID
    name: str
    trigger_description: str
    enabled: bool
    confidence_threshold: int
    execution_count: int

    model_config = {"from_attributes": True}


class RuleCreate(BaseModel):
    name: str = Field(..., max_length=300)
    trigger_description: str = Field(..., max_length=5000)
    confidence_threshold: int = Field(90, ge=0, le=100)
    pattern: dict | None = None


class SuggestionOut(BaseModel):
    id: UUID
    title: str
    pattern_summary: str
    est_savings: str | None
    status: str

    model_config = {"from_attributes": True}


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(AutomationRule)
        .where(AutomationRule.user_id == user.id)
        .order_by(AutomationRule.created_at.desc())
    )
    return list(r.scalars().all())


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rule = AutomationRule(
        user_id=user.id,
        name=body.name.strip(),
        trigger_description=body.trigger_description.strip(),
        confidence_threshold=body.confidence_threshold,
        pattern=body.pattern,
    )
    db.add(rule)
    await db.flush()
    return rule


@router.patch("/rules/{rule_id}/toggle", response_model=RuleOut)
async def toggle_rule(
    rule_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(AutomationRule).where(
            AutomationRule.id == rule_id,
            AutomationRule.user_id == user.id,
        )
    )
    rule = r.scalar_one_or_none()
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    rule.enabled = not rule.enabled
    return rule


@router.get("/suggestions", response_model=list[SuggestionOut])
async def list_suggestions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(AutomationSuggestion)
        .where(AutomationSuggestion.user_id == user.id)
        .order_by(AutomationSuggestion.created_at.desc())
    )
    return list(r.scalars().all())


@router.post("/suggestions/{sid}/enable", response_model=SuggestionOut)
async def enable_suggestion(
    sid: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(AutomationSuggestion).where(
            AutomationSuggestion.id == sid,
            AutomationSuggestion.user_id == user.id,
        )
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    s.status = "enabled"
    return s
