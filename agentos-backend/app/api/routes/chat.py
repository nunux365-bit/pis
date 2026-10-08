"""Chat — sessions and messages persisted per user; optional WebSocket."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_current_user_detached
from app.config.settings import settings
from app.db.models import ChatMessage, ChatSession, User
from app.infra.rate_limit import check_rate_limit
from app.db.session import AsyncSessionLocal, get_db
from app.schemas.chat import (
    ChatMessageCreate,
    ChatMessageOut,
    ChatSessionCreate,
    ChatSessionOut,
)
from app.security.tokens import verify_access_token
from app.services.chat_agent import (
    generate_assistant_reply_from_context,
    load_assistant_reply_context,
)
from app.services.audit import write_audit

log = logging.getLogger(__name__)

router = APIRouter()


class LegacyChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)
    session_id: UUID | None = None


def _msg_out(m: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=m.id,
        role=m.role,
        content=m.content,
        meta=m.metadata_,
        created_at=m.created_at,
    )


async def _get_session_for_user(
    db: AsyncSession, user: User, session_id: UUID
) -> ChatSession:
    r = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user.id,
        )
    )
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return s


async def _persist_user_and_reply(
    user: User, session_id: UUID, content: str
) -> dict:
    """Session A: user message + approval context. LLM with no PG. Session B: assistant."""
    from datetime import UTC, datetime

    text = content.strip()
    async with AsyncSessionLocal() as db:
        s = await _get_session_for_user(db, user, session_id)
        um = ChatMessage(
            session_id=s.id,
            user_id=user.id,
            role="user",
            content=text,
        )
        db.add(um)
        await db.flush()
        ctx = await load_assistant_reply_context(db, user, text)
        await db.commit()
        user_payload = _msg_out(um).model_dump(mode="json")
        sid = s.id

    reply_text, meta = await generate_assistant_reply_from_context(ctx)

    async with AsyncSessionLocal() as db:
        s = await db.get(ChatSession, sid)
        if not s:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
        am = ChatMessage(
            session_id=s.id,
            user_id=user.id,
            role="assistant",
            content=reply_text,
            metadata_=meta,
        )
        db.add(am)
        s.updated_at = datetime.now(UTC)
        await write_audit(
            db,
            actor_user_id=user.id,
            action="chat.message",
            resource_type="chat_session",
            resource_id=str(s.id),
        )
        await db.commit()
        assistant_payload = _msg_out(am).model_dump(mode="json")

    return {
        "user_message": user_payload,
        "assistant_message": assistant_payload,
    }


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
    )
    return list(r.scalars().all())


@router.post("/sessions", response_model=ChatSessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: ChatSessionCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    title = (body.title or "New conversation").strip()[:500]
    s = ChatSession(user_id=user.id, title=title)
    db.add(s)
    await db.flush()
    await write_audit(
        db,
        actor_user_id=user.id,
        action="chat.session_create",
        resource_type="chat_session",
        resource_id=str(s.id),
    )
    return s


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    session_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _get_session_for_user(db, user, session_id)
    r = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    rows = r.scalars().all()
    return [
        ChatMessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            meta=m.metadata_,
            created_at=m.created_at,
        )
        for m in rows
    ]


@router.post("/sessions/{session_id}/messages", response_model=dict)
async def post_message(
    session_id: UUID,
    body: ChatMessageCreate,
    user: Annotated[User, Depends(get_current_user_detached)],
):
    return await _persist_user_and_reply(user, session_id, body.content)


@router.post("/message", response_model=dict)
async def legacy_message(
    body: LegacyChatBody,
    user: Annotated[User, Depends(get_current_user_detached)],
):
    """Backward-compatible single endpoint — uses latest session or creates one."""
    async with AsyncSessionLocal() as db:
        if body.session_id:
            sid = body.session_id
            await _get_session_for_user(db, user, sid)
        else:
            r = await db.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .limit(1)
            )
            s = r.scalar_one_or_none()
            if not s:
                s = ChatSession(user_id=user.id, title="Conversation")
                db.add(s)
                await db.flush()
            sid = s.id
        await db.commit()
    return await _persist_user_and_reply(user, sid, body.message)

@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _get_session_for_user(db, user, session_id)
    await db.execute(
        delete(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user.id,
        )
    )
    return None


@router.websocket("/ws")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        user_id = verify_access_token(token)
    except ValueError:
        await websocket.close(code=4401)
        return
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
            user = r.scalar_one_or_none()
            if not user:
                await websocket.close(code=4401)
                return
            db.expunge(user)
        while True:
            try:
                raw = await websocket.receive_json()
            except Exception:
                log.debug("websocket receive failed", exc_info=True)
                break
            try:
                session_id = UUID(str(raw.get("session_id", "")))
            except ValueError:
                await websocket.send_json({"error": "invalid session_id"})
                continue
            content = (raw.get("content") or "").strip()
            if not content:
                await websocket.send_json({"error": "empty content"})
                continue
            if not await check_rate_limit(
                f"api:{user.id}",
                settings.rate_limit_api_per_user,
                60,
            ):
                await websocket.send_json({"error": "rate_limited"})
                continue
            try:
                payload = await _persist_user_and_reply(user, session_id, content)
                await websocket.send_json({"type": "reply", **payload})
            except HTTPException as he:
                await websocket.send_json(
                    {"error": "session not found" if he.status_code == 404 else "message_failed"}
                )
            except Exception:
                log.exception("websocket chat message failed")
                try:
                    await websocket.send_json({"error": "message_failed"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
