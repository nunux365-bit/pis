"""Library service — unified conversation history across all Optimus services."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OptimusConversation, OptimusMessage
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)


async def get_all_conversations(
    user_id: str,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Get all conversations across all Optimus services for a user.

    Returns paginated list with total count.
    """
    import uuid

    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Get total count
        from sqlalchemy import func

        total = await session.scalar(
            select(func.count())
            .select_from(OptimusConversation)
            .where(OptimusConversation.user_id == uuid.UUID(user_id))
        )

        # Get conversations
        result = await session.scalars(
            select(OptimusConversation)
            .where(OptimusConversation.user_id == uuid.UUID(user_id))
            .order_by(desc(OptimusConversation.updated_at))
            .offset(offset)
            .limit(limit)
        )
        conversations = result.all()

        return {
            "total": total or 0,
            "offset": offset,
            "limit": limit,
            "conversations": [
                {
                    "id": str(c.id),
                    "title": c.title,
                    "service": c.service,
                    "channel": c.channel,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in conversations
            ],
        }

    finally:
        if own_session:
            await session.close()


async def search_conversations(
    user_id: str,
    query: str,
    service: str | None = None,
    limit: int = 20,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """
    Search conversations by title or message content.
    """
    import uuid

    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Search in conversation titles
        title_query = (
            select(OptimusConversation)
            .where(
                OptimusConversation.user_id == uuid.UUID(user_id),
                OptimusConversation.title.ilike(f"%{query}%"),
            )
            .order_by(desc(OptimusConversation.updated_at))
        )

        if service:
            title_query = title_query.where(OptimusConversation.service == service)

        title_query = title_query.limit(limit)

        result = await session.scalars(title_query)
        conversations = result.all()

        # Also search in messages and get unique conversations
        message_query = (
            select(OptimusMessage.conversation_id)
            .join(OptimusConversation)
            .where(
                OptimusConversation.user_id == uuid.UUID(user_id),
                OptimusMessage.content.ilike(f"%{query}%"),
            )
            .distinct()
            .limit(limit)
        )

        if service:
            message_query = message_query.where(OptimusConversation.service == service)

        msg_result = await session.scalars(message_query)
        message_conv_ids = set(msg_result.all())

        # Get those conversations
        if message_conv_ids:
            msg_convs_result = await session.scalars(
                select(OptimusConversation)
                .where(OptimusConversation.id.in_(message_conv_ids))
                .order_by(desc(OptimusConversation.updated_at))
            )
            msg_conversations = msg_convs_result.all()
        else:
            msg_conversations = []

        # Merge and dedupe
        seen_ids = set()
        all_conversations = []

        for c in list(conversations) + list(msg_conversations):
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                all_conversations.append({
                    "id": str(c.id),
                    "title": c.title,
                    "service": c.service,
                    "channel": c.channel,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                })

        return all_conversations[:limit]

    finally:
        if own_session:
            await session.close()


async def get_conversation_summary(
    conversation_id: str,
    user_id: str,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """
    Get a conversation with message count and preview.
    """
    import uuid

    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Get conversation
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == uuid.UUID(conversation_id),
                OptimusConversation.user_id == uuid.UUID(user_id),
            )
        )

        if not conv:
            return None

        # Get message count
        from sqlalchemy import func

        msg_count = await session.scalar(
            select(func.count())
            .select_from(OptimusMessage)
            .where(OptimusMessage.conversation_id == uuid.UUID(conversation_id))
        )

        # Get first user message as preview
        first_msg = await session.scalar(
            select(OptimusMessage)
            .where(
                OptimusMessage.conversation_id == uuid.UUID(conversation_id),
                OptimusMessage.role == "user",
            )
            .order_by(OptimusMessage.created_at)
            .limit(1)
        )

        return {
            "id": str(conv.id),
            "title": conv.title,
            "service": conv.service,
            "channel": conv.channel,
            "message_count": msg_count or 0,
            "preview": first_msg.content[:100] if first_msg else None,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
        }

    finally:
        if own_session:
            await session.close()
