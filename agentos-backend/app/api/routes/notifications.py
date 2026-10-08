"""User-scoped notifications."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Notification, User
from app.db.session import get_db

router = APIRouter()


class NotificationOut(BaseModel):
    id: UUID
    category: str
    title: str
    body: str | None
    read: bool
    link: str | None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(cls, n: Notification) -> "NotificationOut":
        return cls(
            id=n.id,
            category=n.category,
            title=n.title,
            body=n.body,
            read=n.read,
            link=n.link,
            created_at=n.created_at.isoformat(),
        )


@router.get("", response_model=dict)
async def list_notifications(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    unread_only: bool = Query(False),
):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.read.is_(False))
    q = q.order_by(Notification.created_at.desc()).limit(200)
    result = await db.execute(q)
    rows = result.scalars().all()
    return {"notifications": [NotificationOut.from_row(n).model_dump() for n in rows]}


@router.post("/mark-all-read")
async def mark_all_read(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read.is_(False))
        .values(read=True)
    )
    return {"ok": True}


@router.post("/{notification_id}/read")
async def mark_one_read(
    notification_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user.id,
        )
    )
    n = r.scalar_one_or_none()
    if not n:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    n.read = True
    return {"ok": True}
