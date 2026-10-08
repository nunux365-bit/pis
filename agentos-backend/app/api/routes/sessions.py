"""Agent / LangGraph session inspector — per-user durable threads."""

import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import AgentSession, User, WorkflowRun
from app.db.session import get_db

router = APIRouter()


class AgentSessionCreate(BaseModel):
    workflow_name: str = Field(..., min_length=1, max_length=200)
    thread_id: str | None = Field(None, max_length=80)
    workflow_run_id: UUID | None = None


class AgentSessionPatch(BaseModel):
    workflow_run_id: UUID | None = None


class AgentSessionOut(BaseModel):
    id: UUID
    thread_id: str
    workflow_name: str
    status: str
    progress_pct: int
    last_checkpoint_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, s: AgentSession) -> "AgentSessionOut":
        return cls(
            id=s.id,
            thread_id=s.thread_id,
            workflow_name=s.workflow_name,
            status=s.status,
            progress_pct=s.progress_pct,
            last_checkpoint_at=s.last_checkpoint_at.isoformat() if s.last_checkpoint_at else None,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )


@router.get("", response_model=dict)
async def list_sessions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    q = select(AgentSession)
    if not user.is_admin:
        q = q.where(AgentSession.user_id == user.id)
    q = q.order_by(AgentSession.updated_at.desc()).limit(100)
    result = await db.execute(q)
    rows = result.scalars().all()
    return {
        "sessions": [AgentSessionOut.from_row(s).model_dump() for s in rows],
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_agent_session(
    body: AgentSessionCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    wrid = body.workflow_run_id
    if wrid is not None:
        wr = await db.get(WorkflowRun, wrid)
        if not wr or (
            not user.is_admin and wr.user_id != user.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow_run not found")

    tid = (body.thread_id or f"th-{uuid.uuid4().hex[:12]}")[:80]
    s = AgentSession(
        user_id=user.id,
        thread_id=tid,
        workflow_name=body.workflow_name.strip(),
        status="running",
        progress_pct=0,
        state={},
        workflow_run_id=wrid,
    )
    db.add(s)
    await db.flush()
    return AgentSessionOut.from_row(s).model_dump()


@router.get("/{session_id}", response_model=dict)
async def get_session(
    session_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(AgentSession).where(AgentSession.id == session_id))
    s = r.scalar_one_or_none()
    if not s or (
        not user.is_admin and s.user_id != user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {
        "id": str(s.id),
        "status": s.status,
        "progress_pct": s.progress_pct,
        "snapshots": s.state.get("snapshots", []) if s.state else [],
        "thread_id": s.thread_id,
        "workflow_name": s.workflow_name,
        "workflow_run_id": str(s.workflow_run_id) if s.workflow_run_id else None,
    }


@router.patch("/{session_id}", response_model=dict)
async def patch_agent_session(
    session_id: UUID,
    body: AgentSessionPatch,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(select(AgentSession).where(AgentSession.id == session_id))
    s = r.scalar_one_or_none()
    if not s or (
        not user.is_admin and s.user_id != user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    patch = body.model_dump(exclude_unset=True)
    if "workflow_run_id" in patch:
        wid = patch["workflow_run_id"]
        if wid is not None:
            wr = await db.get(WorkflowRun, wid)
            if not wr or (
                not user.is_admin and wr.user_id != user.id
            ):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow_run not found")
        s.workflow_run_id = wid

    await db.flush()
    return AgentSessionOut.from_row(s).model_dump()
