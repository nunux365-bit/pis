"""Command bar search — scoped full-text style (ILIKE) across user-visible rows."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Approval, ChatMessage, ChatSession, Notification, User
from app.db.session import get_db

router = APIRouter()


class SearchQuery(BaseModel):
    q: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(20, ge=1, le=50)


@router.post("", response_model=dict)
async def search(
    body: SearchQuery,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    q = f"%{body.q.strip()}%"
    hits: list[dict] = []
    lim = body.limit

    # Nav shortcuts (always)
    nav = [
        {"type": "nav", "label": "Chat", "href": "/chat"},
        {"type": "nav", "label": "Order to cash (O2C)", "href": "/o2c/ohc-mis"},
        {"type": "nav", "label": "Source to pay (S2P)", "href": "/s2p/po"},
        {"type": "nav", "label": "Dashboard", "href": "/dashboard"},
        {"type": "nav", "label": "Email Agent", "href": "/admin/email-agent"},
        {"type": "nav", "label": "Sessions", "href": "/sessions"},
    ]
    ql = body.q.strip().lower()
    for n in nav:
        if ql in n["label"].lower():
            hits.append(n)

    # Approvals visible to user
    aq = select(Approval).where(Approval.title.ilike(q))
    if not user.is_admin:
        aq = aq.where(
            or_(
                Approval.assignee_user_id == user.id,
                Approval.created_by_user_id == user.id,
            )
        )
    aq = aq.order_by(Approval.created_at.desc()).limit(lim)
    for a in (await db.execute(aq)).scalars().all():
        hits.append(
            {
                "type": "approval",
                "label": a.title,
                "href": "/o2c/ohc-mis",
                "id": str(a.id),
            }
        )

    # Chat sessions
    sq = (
        select(ChatSession)
        .where(ChatSession.user_id == user.id, ChatSession.title.ilike(q))
        .limit(lim)
    )
    for s in (await db.execute(sq)).scalars().all():
        hits.append(
            {
                "type": "chat_session",
                "label": s.title,
                "href": f"/chat?session={s.id}",
                "id": str(s.id),
            }
        )

    # Messages (content) — join session owner
    mq = (
        select(ChatMessage, ChatSession)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .where(
            ChatSession.user_id == user.id,
            ChatMessage.content.ilike(q),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(lim)
    )
    for msg, sess in (await db.execute(mq)).all():
        snippet = msg.content[:120] + ("…" if len(msg.content) > 120 else "")
        hits.append(
            {
                "type": "chat_message",
                "label": snippet,
                "href": f"/chat?session={sess.id}",
                "id": str(msg.id),
            }
        )

    # Notifications
    nq = (
        select(Notification)
        .where(Notification.user_id == user.id, Notification.title.ilike(q))
        .limit(lim)
    )
    for n in (await db.execute(nq)).scalars().all():
        hits.append(
            {
                "type": "notification",
                "label": n.title,
                "href": n.link or "/o2c/ohc-mis",
                "id": str(n.id),
            }
        )

    # Dedupe by label+href, cap
    seen: set[tuple[str, str]] = set()
    out = []
    for h in hits:
        key = (h.get("label", ""), h.get("href", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= lim:
            break

    return {"query": body.q, "hits": out}
