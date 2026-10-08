"""Redis-backed async session state for Optimus conversations."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.agents.optimus import config
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)


def _session_key(conversation_id: str) -> str:
    return f"optimus:session:{conversation_id}"


def _ttl_seconds() -> int:
    return max(3600, config.SESSION_TTL_HOURS * 3600)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def get_session(conversation_id: str) -> dict[str, Any] | None:
    """Retrieve session state from Redis."""
    raw = await get_redis().get(_session_key(conversation_id))
    if not raw:
        return None
    data = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        log.warning("Invalid session JSON for conversation %s", conversation_id)
        return None


async def set_session(conversation_id: str, state: dict[str, Any]) -> None:
    """Store session state to Redis with TTL."""
    state["updated_at"] = _now()
    await get_redis().set(
        _session_key(conversation_id),
        json.dumps(state, default=str),
        ex=_ttl_seconds(),
    )


async def create_session(conversation_id: str, user_id: str, service: str = "smartqna") -> dict[str, Any]:
    """Create a new session with initial state."""
    state = {
        "conversation_id": conversation_id,
        "user_id": str(user_id),
        "service": service,
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    await set_session(conversation_id, state)
    return state


async def add_message(
    conversation_id: str,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    confidence: str | None = None,
    query_type: str | None = None,
    needs_clarification: bool = False,
    suggestions: list[str] | None = None,
) -> dict[str, Any] | None:
    """Add a message to the session and return updated state."""
    session = await get_session(conversation_id)
    if not session:
        return None

    message = {
        "id": str(uuid4()),
        "role": role,
        "content": content,
        "created_at": _now(),
    }
    if citations:
        message["citations"] = citations
    if confidence:
        message["confidence"] = confidence
    if query_type:
        message["query_type"] = query_type
    if needs_clarification:
        message["needs_clarification"] = needs_clarification
    if suggestions:
        message["suggestions"] = suggestions

    session["messages"].append(message)

    # Trim to max turns (keep system messages + last N turns)
    max_messages = config.SESSION_MAX_TURNS * 2  # user + assistant per turn
    if len(session["messages"]) > max_messages:
        session["messages"] = session["messages"][-max_messages:]

    await set_session(conversation_id, session)
    return session


async def get_history(conversation_id: str) -> list[dict[str, Any]]:
    """Get conversation history for context."""
    session = await get_session(conversation_id)
    if not session:
        return []
    return session.get("messages", [])


async def clear_session(conversation_id: str) -> None:
    """Delete session from Redis."""
    await get_redis().delete(_session_key(conversation_id))
