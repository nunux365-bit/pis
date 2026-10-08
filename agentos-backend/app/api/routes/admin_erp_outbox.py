"""Admin: inspect post-HITL ERP outbox until SAP connector consumes rows."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import PostHitlOutbox, User
from app.db.session import get_db

router = APIRouter()


class PostHitlOutboxItem(BaseModel):
    id: UUID
    user_id: UUID
    workflow_run_id: UUID | None
    approval_id: UUID | None
    workflow_key: str
    canonical_workflow_key: str
    status: str
    snapshot: dict | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.get("/erp-outbox", response_model=list[PostHitlOutboxItem])
async def list_erp_outbox(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
):
    q = select(PostHitlOutbox).order_by(PostHitlOutbox.created_at.desc()).limit(limit)
    if status_filter:
        q = q.where(PostHitlOutbox.status == status_filter)
    r = await db.execute(q)
    rows = r.scalars().all()
    return [PostHitlOutboxItem.model_validate(x) for x in rows]
