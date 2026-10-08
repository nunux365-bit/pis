"""Approvals — row-level security: assignee, creator, or admin."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.db.models import Approval, ApprovalStatus, Notification, User
from app.db.session import get_db
from app.services.audit import write_audit
from app.services.post_hitl_outbox import enqueue_post_hitl_after_approval
from app.services.workflow_hitl import sync_workflow_run_from_approval_decision

router = APIRouter()


class ApprovalCreate(BaseModel):
    assignee_email: EmailStr
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    amount: Decimal | None = None
    currency: str = "INR"
    confidence: int | None = Field(None, ge=0, le=100)
    risk: str = Field("low", pattern="^(low|medium|high)$")
    agent_name: str = Field("AgentOS", max_length=120)
    payload: dict | None = None


class ApprovalOut(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: str
    amount: str | None
    currency: str
    confidence: int | None
    risk: str
    agent_name: str
    assignee_user_id: UUID
    created_by_user_id: UUID | None
    created_at: datetime
    decided_at: datetime | None
    workflow_key: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(cls, a: Approval) -> "ApprovalOut":
        return cls(
            id=a.id,
            title=a.title,
            description=a.description,
            status=a.status,
            amount=str(a.amount) if a.amount is not None else None,
            currency=a.currency,
            confidence=a.confidence,
            risk=a.risk,
            agent_name=a.agent_name,
            assignee_user_id=a.assignee_user_id,
            created_by_user_id=a.created_by_user_id,
            created_at=a.created_at,
            decided_at=a.decided_at,
            workflow_key=(
                str((a.payload or {}).get("workflow_key")).strip()
                if isinstance(a.payload, dict) and (a.payload or {}).get("workflow_key")
                else None
            ),
        )


def _can_see_approval(user: User, a: Approval) -> bool:
    if user.is_admin:
        return True
    return a.assignee_user_id == user.id or a.created_by_user_id == user.id


def _can_decide(user: User, a: Approval) -> bool:
    if user.is_admin:
        return True
    return a.assignee_user_id == user.id


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(None, alias="status"),
):
    q = select(Approval)
    if not user.is_admin:
        q = q.where(
            or_(
                Approval.assignee_user_id == user.id,
                Approval.created_by_user_id == user.id,
            )
        )
    if status_filter:
        q = q.where(Approval.status == status_filter)
    q = q.order_by(Approval.created_at.desc())
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ApprovalOut.from_row(a) for a in rows]


@router.get("/pending", response_model=dict)
async def list_pending_compact(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    q = (
        select(Approval)
        .where(
            Approval.status == ApprovalStatus.PENDING.value,
        )
        .order_by(Approval.created_at.desc())
    )
    if not user.is_admin:
        q = q.where(
            or_(
                Approval.assignee_user_id == user.id,
                Approval.created_by_user_id == user.id,
            )
        )
    result = await db.execute(q)
    rows = result.scalars().all()
    return {
        "items": [ApprovalOut.from_row(a) for a in rows],
        "count": len(rows),
    }


@router.post("", response_model=ApprovalOut, status_code=status.HTTP_201_CREATED)
async def create_approval(
    body: ApprovalCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    email = body.assignee_email.lower().strip()
    ur = await db.execute(select(User).where(User.email == email))
    assignee = ur.scalar_one_or_none()
    if not assignee:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignee user not found")
    if not user.is_admin and assignee.id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You may only create approvals assigned to yourself",
        )
    a = Approval(
        assignee_user_id=assignee.id,
        created_by_user_id=user.id,
        title=body.title.strip(),
        description=body.description,
        amount=body.amount,
        currency=body.currency,
        confidence=body.confidence,
        risk=body.risk,
        agent_name=body.agent_name,
        payload=body.payload,
        status=ApprovalStatus.PENDING.value,
    )
    db.add(a)
    await db.flush()
    db.add(
        Notification(
            user_id=assignee.id,
            category="approval",
            title=f"Approval required: {a.title}",
            body=a.description,
            link="/o2c/ohc-mis",
        )
    )
    await write_audit(
        db,
        actor_user_id=user.id,
        action="approval.create",
        resource_type="approval",
        resource_id=str(a.id),
    )
    return ApprovalOut.from_row(a)


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve_item(
    approval_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(Approval).where(Approval.id == approval_id))
    a = r.scalar_one_or_none()
    if not a or not _can_see_approval(user, a) or not _can_decide(user, a):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if a.status != ApprovalStatus.PENDING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Approval is not pending")
    a.status = ApprovalStatus.APPROVED.value
    a.decided_at = datetime.now(UTC)
    await write_audit(
        db,
        actor_user_id=user.id,
        action="approval.approve",
        resource_type="approval",
        resource_id=str(a.id),
    )
    await sync_workflow_run_from_approval_decision(db, a)
    await enqueue_post_hitl_after_approval(db, a)
    return ApprovalOut.from_row(a)


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
async def reject_item(
    approval_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(Approval).where(Approval.id == approval_id))
    a = r.scalar_one_or_none()
    if not a or not _can_see_approval(user, a) or not _can_decide(user, a):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if a.status != ApprovalStatus.PENDING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Approval is not pending")
    a.status = ApprovalStatus.REJECTED.value
    a.decided_at = datetime.now(UTC)
    await write_audit(
        db,
        actor_user_id=user.id,
        action="approval.reject",
        resource_type="approval",
        resource_id=str(a.id),
    )
    await sync_workflow_run_from_approval_decision(db, a)
    return ApprovalOut.from_row(a)


@router.delete("/{approval_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_approval_admin(
    approval_id: UUID,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(Approval).where(Approval.id == approval_id))
    a = r.scalar_one_or_none()
    if not a:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await db.execute(delete(Approval).where(Approval.id == approval_id))
