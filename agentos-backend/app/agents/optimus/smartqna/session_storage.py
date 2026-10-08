"""Session storage for SmartQnA conversations — PostgreSQL persistence + Redis cache."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.optimus import run_store
from app.db.models import OptimusConversation, OptimusMessage
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)

# Flock conversation timeout (minutes of inactivity before starting new conversation)
FLOCK_CONVERSATION_TIMEOUT_MINUTES = 30


def _parse_uuid(value: str, field_name: str = "id") -> uuid.UUID:
    """Safely parse a UUID string, raising ValueError with descriptive message."""
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError) as e:
        log.warning("Invalid UUID for %s: %s", field_name, value[:50] if value else None)
        raise ValueError(f"Invalid {field_name}: must be a valid UUID") from e


async def create_conversation(
    user_id: str,
    title: str = "New conversation",
    service: str = "smartqna",
    channel: str = "web",
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Create a new conversation.

    Returns conversation dict with id and metadata.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        conversation = OptimusConversation(
            user_id=_parse_uuid(user_id, "user_id"),
            title=title,
            service=service,
            channel=channel,
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)

        conv_id = str(conversation.id)

        # Initialize Redis session for fast access
        await run_store.create_session(conv_id, user_id, service)

        log.debug("Created conversation %s for user %s", conv_id, user_id)

        return {
            "id": conv_id,
            "user_id": user_id,
            "title": title,
            "service": service,
            "channel": channel,
            "created_at": conversation.created_at.isoformat(),
        }

    finally:
        if own_session:
            await session.close()


async def get_or_create_flock_conversation(
    user_id: str,
    flock_chat_id: str,
    timeout_minutes: int = FLOCK_CONVERSATION_TIMEOUT_MINUTES,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Get active Flock conversation or create a new one.

    A conversation is considered "active" if the last message was within
    the timeout period. If no active conversation exists, a new one is created.

    This enables:
    - Continuous conversation within the timeout window
    - New conversation starts after inactivity
    - Unified history visible in both Flock and web UI

    Args:
        user_id: AgentOS user UUID
        flock_chat_id: Flock chat/channel ID (stored in metadata)
        timeout_minutes: Minutes of inactivity before new conversation (default 30)

    Returns:
        Conversation dict with id, is_new flag, etc.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)

        # Find the most recent Flock conversation for this user that's still active
        # We store flock_chat_id in metadata to link conversations to specific Flock chats
        result = await session.scalars(
            select(OptimusConversation)
            .where(
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
                OptimusConversation.channel == "flock",
                OptimusConversation.updated_at >= cutoff,
            )
            .order_by(desc(OptimusConversation.updated_at))
            .limit(10)  # Check recent ones for matching chat_id
        )
        recent_conversations = result.all()

        # Find one matching the specific Flock chat
        for conv in recent_conversations:
            metadata = conv.metadata_ or {}
            if metadata.get("flock_chat_id") == flock_chat_id:
                log.debug(
                    "Found active Flock conversation %s (last updated %s)",
                    conv.id,
                    conv.updated_at,
                )
                return {
                    "id": str(conv.id),
                    "user_id": user_id,
                    "title": conv.title,
                    "service": conv.service,
                    "channel": conv.channel,
                    "flock_chat_id": flock_chat_id,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "is_new": False,
                }

        # No active conversation found - create a new one
        conversation = OptimusConversation(
            user_id=_parse_uuid(user_id, "user_id"),
            title="New conversation",
            service="smartqna",
            channel="flock",
            metadata_={"flock_chat_id": flock_chat_id},
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)

        conv_id = str(conversation.id)

        # Initialize Redis session
        await run_store.create_session(conv_id, user_id, "smartqna")

        return {
            "id": conv_id,
            "user_id": user_id,
            "title": "New conversation",
            "service": "smartqna",
            "channel": "flock",
            "flock_chat_id": flock_chat_id,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
            "is_new": True,
        }

    finally:
        if own_session:
            await session.close()


async def get_conversation(
    conversation_id: str,
    user_id: str,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """
    Get a conversation by ID (validates user ownership).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == _parse_uuid(conversation_id, "conversation_id"),
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
            )
        )

        if not conv:
            return None

        return {
            "id": str(conv.id),
            "user_id": str(conv.user_id),
            "title": conv.title,
            "service": conv.service,
            "channel": conv.channel,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
        }

    finally:
        if own_session:
            await session.close()


async def list_conversations(
    user_id: str,
    service: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """
    List conversations for a user.

    Args:
        user_id: User UUID
        service: Optional service filter (e.g., "smartqna")
        channel: Optional channel filter (e.g., "web", "flock")
        limit: Max conversations to return
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        query = (
            select(OptimusConversation)
            .where(OptimusConversation.user_id == _parse_uuid(user_id, "user_id"))
            .order_by(desc(OptimusConversation.updated_at))
            .limit(limit)
        )

        if service:
            query = query.where(OptimusConversation.service == service)

        if channel:
            query = query.where(OptimusConversation.channel == channel)

        result = await session.scalars(query)
        conversations = result.all()

        return [
            {
                "id": str(c.id),
                "title": c.title,
                "service": c.service,
                "channel": c.channel,
                "metadata": dict(c.metadata_) if c.metadata_ else None,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in conversations
        ]

    finally:
        if own_session:
            await session.close()


async def delete_conversation(
    conversation_id: str,
    user_id: str,
    session: AsyncSession | None = None,
) -> bool:
    """
    Delete a conversation (validates user ownership).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == _parse_uuid(conversation_id, "conversation_id"),
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
            )
        )

        if not conv:
            return False

        await session.delete(conv)
        await session.commit()

        # Clear Redis session
        await run_store.clear_session(conversation_id)

        log.debug("Deleted conversation %s", conversation_id)
        return True

    finally:
        if own_session:
            await session.close()


async def add_message(
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    confidence: str | None = None,
    query_type: str | None = None,
    needs_clarification: bool = False,
    suggestions: list[str] | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """
    Add a message to a conversation.

    Stores in both PostgreSQL (durable) and Redis (cache).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Verify conversation exists and belongs to user
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == _parse_uuid(conversation_id, "conversation_id"),
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
            )
        )

        if not conv:
            return None

        # Create message
        message = OptimusMessage(
            conversation_id=_parse_uuid(conversation_id, "conversation_id"),
            role=role,
            content=content,
            citations=citations,
            confidence=confidence,
        )
        session.add(message)

        # Update conversation timestamp
        conv.updated_at = datetime.now(UTC)

        # Auto-generate title from first user message
        if role == "user" and conv.title == "New conversation":
            conv.title = content[:50] + "..." if len(content) > 50 else content

        await session.commit()
        await session.refresh(message)

        # Update Redis cache
        await run_store.add_message(
            conversation_id,
            role,
            content,
            citations=citations,
            confidence=confidence,
            query_type=query_type,
            needs_clarification=needs_clarification,
            suggestions=suggestions,
        )

        return {
            "id": str(message.id),
            "role": role,
            "content": content,
            "citations": citations,
            "confidence": confidence,
            "query_type": query_type,
            "needs_clarification": needs_clarification,
            "suggestions": suggestions,
            "created_at": message.created_at.isoformat(),
        }

    finally:
        if own_session:
            await session.close()


async def get_messages(
    conversation_id: str,
    user_id: str,
    limit: int = 100,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """
    Get messages for a conversation.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Verify conversation belongs to user
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == _parse_uuid(conversation_id, "conversation_id"),
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
            )
        )

        if not conv:
            return []

        # Get messages
        result = await session.scalars(
            select(OptimusMessage)
            .where(OptimusMessage.conversation_id == _parse_uuid(conversation_id, "conversation_id"))
            .order_by(OptimusMessage.created_at)
            .limit(limit)
        )
        messages = result.all()

        return [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "confidence": m.confidence,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]

    finally:
        if own_session:
            await session.close()


async def get_conversation_history(
    conversation_id: str,
    include_metadata: bool = True,
) -> list[dict[str, Any]]:
    """
    Get conversation history from Redis cache (for LLM context).

    Args:
        conversation_id: The conversation ID
        include_metadata: If True, include citations, confidence, query_type etc.
                         If False, only return role and content (for LLM context building)

    Returns list of message dicts with role, content, and optionally citations/metadata.
    """
    messages = await run_store.get_history(conversation_id)

    if not include_metadata:
        return [{"role": m["role"], "content": m["content"]} for m in messages]

    # Include full message data for follow-up detection (source queries, clarification, etc.)
    return [
        {
            "role": m["role"],
            "content": m["content"],
            "citations": m.get("citations"),
            "confidence": m.get("confidence"),
            "query_type": m.get("query_type"),
            "needs_clarification": m.get("needs_clarification", False),
            "suggestions": m.get("suggestions"),
        }
        for m in messages
    ]


async def reset_conversation(
    conversation_id: str,
    user_id: str,
    session: AsyncSession | None = None,
) -> bool:
    """
    Clear all messages in a conversation (keep the conversation shell).
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Verify conversation belongs to user
        conv = await session.scalar(
            select(OptimusConversation).where(
                OptimusConversation.id == _parse_uuid(conversation_id, "conversation_id"),
                OptimusConversation.user_id == _parse_uuid(user_id, "user_id"),
            )
        )

        if not conv:
            return False

        # Delete all messages
        from sqlalchemy import delete

        await session.execute(
            delete(OptimusMessage).where(
                OptimusMessage.conversation_id == _parse_uuid(conversation_id, "conversation_id")
            )
        )

        # Reset conversation metadata
        conv.title = "New conversation"
        conv.updated_at = datetime.now(UTC)

        await session.commit()

        # Clear and recreate Redis session
        await run_store.clear_session(conversation_id)
        await run_store.create_session(conversation_id, user_id, conv.service)

        log.debug("Reset conversation %s", conversation_id)
        return True

    finally:
        if own_session:
            await session.close()
