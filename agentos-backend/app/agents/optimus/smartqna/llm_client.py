"""Native AsyncOpenAI chat helpers for Optimus SmartQnA."""

from __future__ import annotations

from typing import Any

from app.agents.optimus import config
from app.agents.optimus.smartqna.openai_client import get_async_openai_client
from app.agents.optimus.smartqna.usage_logger import (
    extract_usage_from_openai_response,
    schedule_usage_log,
)


def _normalize_messages(messages: list[dict[str, str]] | str) -> list[dict[str, str]]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return messages


async def chat_complete_text(
    *,
    messages: list[dict[str, str]] | str,
    step: str,
    model: str | None = None,
    temperature: float = 0,
    max_tokens: int | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Run chat completion via AsyncOpenAI; log usage; return assistant text."""
    if not config.OPENAI_API_KEY:
        return None

    model_name = model or config.OPENAI_MODEL
    client = get_async_openai_client()
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": _normalize_messages(messages),
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = await client.chat.completions.create(**kwargs)
    usage = extract_usage_from_openai_response(response)
    schedule_usage_log(
        step=step,
        model=model_name,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        conversation_id=conversation_id,
        user_id=user_id,
    )

    if not response.choices:
        return None
    content = response.choices[0].message.content
    return content.strip() if content else None
