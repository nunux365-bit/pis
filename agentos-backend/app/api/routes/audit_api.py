"""Audit trail — admin global; dept_head department actors; employee self only."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import AuditLog, User
from app.db.session import get_db
from app.security.rbac import scope_audit_log

router = APIRouter()


class AuditOut(BaseModel):
    id: UUID
    action: str
    resource_type: str
    resource_id: str | None
    actor_user_id: UUID | None
    details: dict | None
    created_at: str

    @classmethod
    def from_row(cls, a: AuditLog) -> "AuditOut":
        return cls(
            id=a.id,
            action=a.action,
            resource_type=a.resource_type,
            resource_id=a.resource_id,
            actor_user_id=a.actor_user_id,
            details=a.details,
            created_at=a.created_at.isoformat(),
        )


@router.get("", response_model=dict)
async def list_audit(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(100, ge=1, le=500),
):
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    q = scope_audit_log(q, user)
    rows = (await db.execute(q)).scalars().all()
    return {"entries": [AuditOut.from_row(a).model_dump() for a in rows]}
