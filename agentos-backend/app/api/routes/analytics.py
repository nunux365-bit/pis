"""Aggregated metrics — scoped by role (self, department, or global)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.services.analytics_summary_core import build_analytics_summary

router = APIRouter()


@router.get("/summary", response_model=dict)
async def analytics_summary(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await build_analytics_summary(db, user)
