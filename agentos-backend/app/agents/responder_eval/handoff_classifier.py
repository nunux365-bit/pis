"""Hand-off bucket classifier — isolated from the quality judge."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.responder_eval.handoff_taxonomy import (
    BUCKET_ENUM,
    SUB_BUCKET_ENUM,
    classifier_taxonomy_prompt_block,
    normalize_handoff_classification,
)
from app.agents.responder_eval.judge import prepare_chat_for_judge
from app.agents.responder_eval.segments import SEGMENT_HUMAN_AGENT, segment_turn_indexes
from app.config.settings import settings
from app.infra.openai_async_client import get_shared_openai_client

log = logging.getLogger(__name__)

_HANDOFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "handoff_bucket": {"type": "string", "enum": BUCKET_ENUM},
        "handoff_sub_bucket": {"type": "string", "enum": SUB_BUCKET_ENUM},
    },
    "required": ["handoff_bucket", "handoff_sub_bucket"],
}

_SYSTEM = (
    "You classify why a customer-support chat escalated to a human agent. Output JSON only. "
    + classifier_taxonomy_prompt_block()
)

_MAX_COMPLETION_TOKENS = 2_000
_PARSE_RETRIES = 2


def _has_human_agent(chat: dict[str, Any]) -> bool:
    return bool(segment_turn_indexes(chat).get(SEGMENT_HUMAN_AGENT))


def prepare_chat_for_handoff(chat: dict[str, Any]) -> dict[str, Any] | None:
    """Pre-handoff transcript window for classification."""
    if not _has_human_agent(chat):
        return None
    prepared = prepare_chat_for_judge(chat)
    messages = prepared.get("messages") or []
    human_turns = segment_turn_indexes(chat).get(SEGMENT_HUMAN_AGENT) or []
    first_human = min(human_turns)
    return {**prepared, "messages": messages[:first_human]}


def mock_handoff_classification(chat: dict[str, Any]) -> dict[str, str | None]:
    if not _has_human_agent(chat):
        return {"handoff_bucket": None, "handoff_sub_bucket": None}
    meta = chat.get("metadata") if isinstance(chat.get("metadata"), dict) else {}
    if meta.get("handoff_bucket") and meta.get("handoff_sub_bucket"):
        bucket, sub = normalize_handoff_classification(
            str(meta["handoff_bucket"]),
            str(meta["handoff_sub_bucket"]),
            has_human=True,
        )
        return {"handoff_bucket": bucket, "handoff_sub_bucket": sub}
    return {
        "handoff_bucket": "user_self_service",
        "handoff_sub_bucket": "user_self_service.tracking_status_check",
    }


def _normalized_pair(raw: dict[str, Any]) -> dict[str, str | None]:
    bucket, sub = normalize_handoff_classification(
        raw.get("handoff_bucket"),
        raw.get("handoff_sub_bucket"),
        has_human=True,
    )
    return {"handoff_bucket": bucket, "handoff_sub_bucket": sub}


def _parse_classifier_response(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty classifier response")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("classifier response must be a JSON object")
    return data


async def _call_classifier_llm(window: dict[str, Any]) -> dict[str, str | None]:
    client = get_shared_openai_client()
    model = settings.responder_eval_openai_model or "gpt-5.4-mini"
    user = json.dumps({"chat": window}, default=str)
    last_err: Exception | None = None
    for attempt in range(_PARSE_RETRIES):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "handoff_classification",
                        "strict": True,
                        "schema": _HANDOFF_SCHEMA,
                    },
                },
                max_completion_tokens=_MAX_COMPLETION_TOKENS,
                temperature=0,
            )
            data = _parse_classifier_response(resp.choices[0].message.content or "")
            return _normalized_pair(data)
        except Exception as exc:
            last_err = exc
            if attempt + 1 < _PARSE_RETRIES:
                log.warning(
                    "handoff_classifier invalid attempt=%s/%s; retrying: %s",
                    attempt + 1,
                    _PARSE_RETRIES,
                    exc,
                )
                await asyncio.sleep(0)
                continue
    raise last_err or RuntimeError("handoff classifier failed")


async def run_handoff_classifier(chat: dict[str, Any]) -> dict[str, str | None]:
    """Classify hand-off bucket/sub-bucket. Never raises — grading must not depend on this."""
    if not _has_human_agent(chat):
        return {"handoff_bucket": None, "handoff_sub_bucket": None}
    if settings.responder_eval_mock_judge:
        return mock_handoff_classification(chat)
    window = prepare_chat_for_handoff(chat)
    if not window:
        return {"handoff_bucket": None, "handoff_sub_bucket": None}
    if not (settings.openai_api_key or "").strip():
        log.warning("handoff_classifier skipped: no OPENAI_API_KEY")
        return {"handoff_bucket": None, "handoff_sub_bucket": None}
    try:
        return await _call_classifier_llm(window)
    except Exception:
        log.exception("handoff_classifier failed chat_id=%s", chat.get("chat_id"))
        return {"handoff_bucket": None, "handoff_sub_bucket": None}
