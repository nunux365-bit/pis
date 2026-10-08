"""PII redaction for RCA run documents and chat text."""

from __future__ import annotations

import copy
from typing import Any

from app.agents.responder_eval.pii_labels import (
    PII_KEY_HINTS,
    hint_for_pii_key,
    person_hint_speaker_for_message,
    speaker_from_role,
)
from app.agents.responder_eval.presidio_engine import redact_free_text
from app.infra.sync_bridge import run_blocking

_PII_KEYS = frozenset(PII_KEY_HINTS.keys())


def redact_string(text: Any, *, speaker: str | None = None) -> str | None:
    if text is None:
        return None
    sp = speaker_from_role(speaker)
    s = redact_free_text(text, speaker=sp)
    return s if s else None


def _redact_value(key: str | None, value: Any) -> Any:
    if key and key.lower() in _PII_KEYS:
        return hint_for_pii_key(key)
    if isinstance(value, str):
        redacted = redact_string(value)
        return redacted if redacted is not None else "[redacted]"
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(None, v) for v in value]
    return value


def redact_dict(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k.lower() in _PII_KEYS:
            out[k] = hint_for_pii_key(k)
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [_redact_value(k, i) for i in v]
        else:
            out[k] = _redact_value(k, v)
    return out


def redact_chat_messages(chat: dict[str, Any]) -> dict[str, Any]:
    """Redact PII in chat message bodies and metadata; role-aware hints ([customer] vs [agent])."""
    out = copy.deepcopy(chat)
    messages = out.get("messages")
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            speaker = person_hint_speaker_for_message(str(msg.get("role") or ""), content)
            msg["content"] = redact_free_text(content, speaker=speaker)
        metadata = msg.get("metadata")
        if isinstance(metadata, dict) and metadata:
            msg["metadata"] = redact_dict(metadata)
    return out


def redact_run_document(doc: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_dict(copy.deepcopy(doc))
    if isinstance(redacted.get("messages"), list):
        return redact_chat_messages(redacted)
    return redacted


async def redact_dict_async(obj: dict[str, Any]) -> dict[str, Any]:
    return await run_blocking(lambda: redact_dict(obj))


async def redact_chat_async(chat: dict[str, Any]) -> dict[str, Any]:
    return await run_blocking(lambda: redact_chat_messages(chat))


async def redact_run_document_async(doc: dict[str, Any]) -> dict[str, Any]:
    return await run_blocking(lambda: redact_run_document(doc))
