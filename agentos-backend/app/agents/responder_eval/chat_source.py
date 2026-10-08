"""External chat source (conversations + messages Postgres) with fixture fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.agents.responder_eval.chat_db import get_chat_eval_async_engine
from app.agents.responder_eval.constants import (
    CHAT_ROLE_AGENT,
    CHAT_ROLE_BOT,
    CHAT_ROLE_USER,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

CLOSED_CONVERSATION_STATUS = "closed"

_CONV_SQL = text(
    """
    SELECT id, status
    FROM conversations
    WHERE id = ANY(CAST(:ids AS bigint[]))
    """
)

_MSG_SQL = text(
    """
    SELECT
        m.conversation_id,
        m.participant_id,
        m.nature,
        m.text,
        m.details,
        m.created_at,
        p.participant_type
    FROM messages m
    LEFT JOIN conversation_participants cp ON cp.id = m.participant_id
    LEFT JOIN participants p ON p.id = cp.participant_id
    WHERE m.conversation_id = ANY(CAST(:ids AS bigint[]))
    ORDER BY m.conversation_id, m.created_at ASC
    """
)

# messages.nature values that are always platform-generated (never customer speech).
BOT_MESSAGE_NATURES = frozenset({"notification", "system_message"})

# Bot/system lines that sometimes arrive with details=NULL in the chat DB.
_NULL_DETAILS_BOT_RE = re.compile(
    r"(?i)(?:"
    r"welcome to tata 1mg"
    r"|virtual health assistant"
    r"|your chat has ended"
    r"|you are no longer responding"
    r"|no longer responding"
    r"|abandonment timer"
    r"|has been in queue"
    r"|connecting to bot"
    r"|connecting\s+you to an agent"
    r"|chatting with\b"
    r"|thank you for choosing tata 1mg"
    r"|you are connected with tata 1mg live chat support"
    r")",
)

# Conversation phases for disambiguating details=NULL rows (customer vs human agent).
_PHASE_BOT = "bot"
_PHASE_LIVE_AGENT = "live_agent"
_PHASE_CLOSING = "closing"

# Customer free-text with details=NULL during bot/queue (not human agent).
_CUSTOMER_NULL_TEXT_RE = re.compile(
    r"(?i)(?:"
    r"^PO\d{10,}\b"
    r"|^(?:pharmacy|lab)\s+orders$"
    r"|\b(?:I didn't|I haven't|my order|why such)\b"
    r")",
)


def _normalize_role_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def looks_like_null_details_bot_text(text: str | None) -> bool:
    """True when null-details rows are known bot/system copy (not customer free text)."""
    normalized = _normalize_role_text(text)
    if not normalized:
        return False
    return _NULL_DETAILS_BOT_RE.search(normalized) is not None


def looks_like_customer_null_text(text: str | None) -> bool:
    """True when details=NULL text is customer speech (order pick, complaint, etc.)."""
    normalized = _normalize_role_text(text)
    if not normalized:
        return False
    return _CUSTOMER_NULL_TEXT_RE.search(normalized) is not None


def _details_event(details: Any) -> str | None:
    if isinstance(details, dict):
        event = details.get("event")
        if event is not None:
            return str(event)
    return None


def _advance_chat_phase(phase: str, details: Any, text: str | None) -> str:
    event = _details_event(details)
    if event == "agent_joined":
        return _PHASE_LIVE_AGENT
    if event == "chat_closed":
        return _PHASE_CLOSING
    if looks_like_null_details_bot_text(text):
        lowered = _normalize_role_text(text).lower()
        if "abandonment timer" in lowered or "connecting to bot" in lowered:
            return _PHASE_CLOSING
    if isinstance(details, dict) and details.get("show_user_input") is False:
        if "connecting to bot" in _normalize_role_text(text).lower():
            return _PHASE_CLOSING
    return phase


@dataclass(frozen=True)
class ChatFetchResult:
    """Outcome of loading chats for eval."""

    chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    not_closed_ids: frozenset[str] = frozenset()
    missing_ids: frozenset[str] = frozenset()
    invalid_ids: frozenset[str] = frozenset()
    empty_closed_ids: frozenset[str] = frozenset()


def reset_chat_db_pool_for_tests() -> None:
    """Test helper — alias for :func:`chat_db.reset_chat_eval_engine_for_tests`."""
    from app.agents.responder_eval.chat_db import reset_chat_eval_engine_for_tests

    reset_chat_eval_engine_for_tests()


def _fixture_chats() -> dict[str, dict[str, Any]]:
    d = settings.responder_eval_chat_fixture_dir.strip()
    if not d:
        return {}
    root = Path(d)
    if not root.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for p in root.glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            cid = str(doc.get("chat_id") or p.stem)
            out[cid] = doc
        except Exception:
            log.warning("skip bad chat fixture %s", p)
    return out


def _normalize_nature(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def is_bot_nature(nature: Any) -> bool:
    """True when messages.nature marks platform-generated bot/system copy."""
    n = _normalize_nature(nature)
    return n in BOT_MESSAGE_NATURES if n else False


def role_from_nature(nature: Any) -> str | None:
    """Map messages.nature to role; None when nature does not decide."""
    if is_bot_nature(nature):
        return CHAT_ROLE_BOT
    return None


def role_from_participant_type(participant_type: Any) -> str | None:
    """Map participants.participant_type to transcript role."""
    pt = str(participant_type or "").strip().lower()
    if pt == "user":
        return CHAT_ROLE_USER
    if pt == "agent":
        return CHAT_ROLE_AGENT
    if pt == "bot":
        return CHAT_ROLE_BOT
    return None


def role_from_details(
    details: Any,
    text: str | None = None,
    *,
    phase: str = _PHASE_BOT,
) -> str:
    """Fallback when participant_type is absent (fixtures, legacy rows)."""
    if details is None:
        if looks_like_null_details_bot_text(text):
            return CHAT_ROLE_BOT
        if phase in (_PHASE_LIVE_AGENT, _PHASE_CLOSING) and not looks_like_customer_null_text(text):
            return CHAT_ROLE_AGENT
        return CHAT_ROLE_USER
    if isinstance(details, str):
        return CHAT_ROLE_USER if not details.strip() else CHAT_ROLE_BOT
    if isinstance(details, dict):
        if not details:
            return CHAT_ROLE_BOT
        if "key" in details:
            return CHAT_ROLE_USER
        return CHAT_ROLE_BOT
    if isinstance(details, (list, tuple, set)):
        return CHAT_ROLE_USER if len(details) == 0 else CHAT_ROLE_BOT
    return CHAT_ROLE_BOT


def resolve_message_role(
    row: dict[str, Any],
    *,
    phase: str = _PHASE_BOT,
) -> str:
    """Resolve eval role with fixed precedence (strongest signal first).

    1. participants.participant_type (user / agent / bot)
    2. messages.nature in {notification, system_message} → bot
    3. messages.details + text heuristics (legacy fallback)
    """
    by_type = role_from_participant_type(row.get("participant_type"))
    if by_type is not None:
        return by_type

    by_nature = role_from_nature(row.get("nature"))
    if by_nature is not None:
        return by_nature

    return role_from_details(row.get("details"), str(row.get("text") or ""), phase=phase)


def _serialize_created_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


def _metadata_from_row(details: Any, participant_type: Any) -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    if isinstance(details, dict):
        meta.update(details)
    elif isinstance(details, str):
        stripped = details.strip()
        if stripped:
            meta["sender"] = stripped
    pt = str(participant_type or "").strip()
    if pt:
        meta["participant_type"] = pt
    return meta or None


def build_chat_doc(chat_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    phase = _PHASE_BOT
    for row in rows:
        text_val = row.get("text")
        if text_val is None:
            continue
        body = str(text_val)
        if not body.strip():
            continue
        details = row.get("details")
        msg: dict[str, Any] = {
            "role": resolve_message_role(row, phase=phase),
            "content": body,
        }
        phase = _advance_chat_phase(phase, details, body)
        created_at = _serialize_created_at(row.get("created_at"))
        if created_at:
            msg["created_at"] = created_at
        metadata = _metadata_from_row(details, row.get("participant_type"))
        if metadata:
            msg["metadata"] = metadata
        messages.append(msg)
    return {"chat_id": chat_id, "messages": messages}


def _parse_conversation_id(chat_id: str) -> int | None:
    """RCA chat_id is stored as text; conversations.id is bigint."""
    try:
        return int(str(chat_id).strip())
    except (TypeError, ValueError):
        return None


def _conversation_id_map(chat_ids: list[str]) -> tuple[dict[int, str], set[str]]:
    """Map numeric chat_id strings to conversations.id integers (deduped)."""
    id_to_chat_id: dict[int, str] = {}
    invalid_ids: set[str] = set()
    for chat_id in chat_ids:
        conv_id = _parse_conversation_id(chat_id)
        if conv_id is None:
            invalid_ids.add(chat_id)
            continue
        id_to_chat_id[conv_id] = chat_id
    return id_to_chat_id, invalid_ids


def _mapping_rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings().all()]


async def _load_conv_rows_from_db(int_ids: list[int]) -> list[dict[str, Any]]:
    engine = get_chat_eval_async_engine()
    async with engine.connect() as conn:
        conv_result = await conn.execute(_CONV_SQL, {"ids": int_ids})
        return _mapping_rows(conv_result)


async def _load_msg_rows_from_db(closed_conv_ids: list[int]) -> list[dict[str, Any]]:
    if not closed_conv_ids:
        return []
    engine = get_chat_eval_async_engine()
    async with engine.connect() as conn:
        msg_result = await conn.execute(_MSG_SQL, {"ids": closed_conv_ids})
        return _mapping_rows(msg_result)


async def _load_chat_rows_from_db(
    int_ids: list[int],
    closed_conv_ids: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load conversations; optionally messages for *closed_conv_ids* on the same connection."""
    engine = get_chat_eval_async_engine()
    async with engine.connect() as conn:
        conv_result = await conn.execute(_CONV_SQL, {"ids": int_ids})
        conv_rows = _mapping_rows(conv_result)
        if not closed_conv_ids:
            return conv_rows, []
        msg_result = await conn.execute(_MSG_SQL, {"ids": closed_conv_ids})
        return conv_rows, _mapping_rows(msg_result)


def _assemble_chat_fetch_result(
    *,
    chat_ids: list[str],
    id_to_chat_id: dict[int, str],
    invalid_ids: set[str],
    conv_rows: list[dict[str, Any]],
    msg_rows: list[dict[str, Any]],
) -> ChatFetchResult:
    by_id = {
        id_to_chat_id[int(row["id"])]: str(row.get("status") or "")
        for row in conv_rows
        if int(row["id"]) in id_to_chat_id
    }
    found_ids = set(by_id)
    missing_ids = {cid for cid in chat_ids if cid not in found_ids and cid not in invalid_ids}
    not_closed_ids = {
        cid
        for cid in chat_ids
        if cid in found_ids and by_id[cid].lower() != CLOSED_CONVERSATION_STATUS
    }
    closed_conv_ids = [
        conv_id
        for conv_id, cid in id_to_chat_id.items()
        if cid in found_ids and cid not in not_closed_ids
    ]

    if not closed_conv_ids:
        return ChatFetchResult(
            not_closed_ids=frozenset(not_closed_ids),
            missing_ids=frozenset(missing_ids),
            invalid_ids=frozenset(invalid_ids),
        )

    closed_chat_ids = [id_to_chat_id[i] for i in closed_conv_ids]
    grouped: dict[str, list[dict[str, Any]]] = {cid: [] for cid in closed_chat_ids}
    for row in msg_rows:
        conv_id = int(row["conversation_id"])
        cid = id_to_chat_id.get(conv_id)
        if cid is None:
            continue
        grouped.setdefault(cid, []).append(row)

    chats: dict[str, dict[str, Any]] = {}
    empty_closed_ids: set[str] = set()
    for cid in closed_chat_ids:
        doc = build_chat_doc(cid, grouped.get(cid, []))
        if not doc["messages"]:
            empty_closed_ids.add(cid)
        else:
            chats[cid] = doc

    return ChatFetchResult(
        chats=chats,
        not_closed_ids=frozenset(not_closed_ids),
        missing_ids=frozenset(missing_ids),
        invalid_ids=frozenset(invalid_ids),
        empty_closed_ids=frozenset(empty_closed_ids),
    )


async def _fetch_from_db(chat_ids: list[str]) -> ChatFetchResult:
    id_to_chat_id, invalid_ids = _conversation_id_map(chat_ids)
    if not id_to_chat_id:
        return ChatFetchResult(invalid_ids=frozenset(invalid_ids))

    int_ids = list(id_to_chat_id.keys())
    conv_rows = await _load_conv_rows_from_db(int_ids)
    by_id = {
        id_to_chat_id[int(row["id"])]: str(row.get("status") or "")
        for row in conv_rows
        if int(row["id"]) in id_to_chat_id
    }
    closed_conv_ids = [
        conv_id
        for conv_id, cid in id_to_chat_id.items()
        if cid in by_id and by_id[cid].lower() == CLOSED_CONVERSATION_STATUS
    ]
    msg_rows = await _load_msg_rows_from_db(closed_conv_ids)

    return _assemble_chat_fetch_result(
        chat_ids=chat_ids,
        id_to_chat_id=id_to_chat_id,
        invalid_ids=invalid_ids,
        conv_rows=conv_rows,
        msg_rows=msg_rows,
    )


def _fetch_chats_from_fixtures(chat_ids: list[str]) -> ChatFetchResult:
    fixtures = _fixture_chats()
    chats = {cid: fixtures[cid] for cid in chat_ids if cid in fixtures}
    missing_ids = {cid for cid in chat_ids if cid not in fixtures}
    return ChatFetchResult(chats=chats, missing_ids=frozenset(missing_ids))


def fetch_chats(chat_ids: list[str]) -> ChatFetchResult:
    if not chat_ids:
        return ChatFetchResult()
    if settings.responder_eval_use_chat_fixtures:
        return _fetch_chats_from_fixtures(chat_ids)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_fetch_from_db(chat_ids))
    raise RuntimeError("fetch_chats() cannot be called from a running event loop; use fetch_chats_async()")


async def fetch_chats_async(chat_ids: list[str]) -> ChatFetchResult:
    if not chat_ids:
        return ChatFetchResult()
    if settings.responder_eval_use_chat_fixtures:
        return _fetch_chats_from_fixtures(chat_ids)
    return await _fetch_from_db(chat_ids)
