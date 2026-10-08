"""Async LLM usage logging for cost tracking.

Provides non-blocking token usage logging that doesn't impact query latency.
Usage data is stored in the optimus_llm_usage table for cost analysis.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)


async def log_llm_usage(
    step: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Log LLM token usage to database.

    This function is designed to be called with ``spawn()`` for
    fire-and-forget logging that doesn't block the main query flow.

    Args:
        step: The processing step (e.g., 'classification', 'answer_generation', 'reranking')
        model: The model used (e.g., 'gpt-4o', 'gpt-4o-mini', 'text-embedding-3-small')
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens
        total_tokens: Total tokens (prompt + completion)
        conversation_id: Optional conversation UUID
        user_id: Optional user UUID
    """
    try:
        from app.db.models import OptimusLLMUsage
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            usage = OptimusLLMUsage(
                id=uuid.uuid4(),
                conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
                user_id=uuid.UUID(user_id) if user_id else None,
                step=step,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
            session.add(usage)
            await session.commit()
            log.debug(
                "Logged LLM usage: step=%s model=%s tokens=%d",
                step, model, total_tokens
            )
    except Exception as e:
        # Log failures should never crash the main query flow
        log.warning("Failed to log LLM usage: %s", e)


def schedule_usage_log(
    step: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Schedule LLM usage logging as a background task (fire-and-forget).

    This is a convenience wrapper that creates an asyncio task for logging.
    Use this when you want to log usage without awaiting the result.

    Args:
        step: The processing step
        model: The model used
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens
        total_tokens: Total tokens
        conversation_id: Optional conversation UUID string
        user_id: Optional user UUID string
    """
    from app.agents.optimus import config

    if not config.LLM_USAGE_LOGGING_ENABLED:
        return  # Skip logging when disabled

    try:
        from app.infra.task_tracker import spawn

        spawn(
            log_llm_usage(
                step=step,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                conversation_id=conversation_id,
                user_id=user_id,
            ),
            name="smartqna-usage",
        )
    except RuntimeError:
        # No event loop running (e.g., in sync context)
        log.debug("Cannot schedule usage log: no event loop")


def extract_usage_from_openai_response(response: Any) -> dict[str, int]:
    """Extract token usage from a direct OpenAI API response.

    Args:
        response: OpenAI API response object

    Returns:
        Dict with prompt_tokens, completion_tokens, total_tokens
    """
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    if hasattr(response, "usage") and response.usage:
        usage["prompt_tokens"] = response.usage.prompt_tokens or 0
        usage["completion_tokens"] = response.usage.completion_tokens or 0
        usage["total_tokens"] = response.usage.total_tokens or 0

    return usage


def extract_usage_from_embedding_response(response: Any) -> dict[str, int]:
    """Extract token usage from an OpenAI embedding response.

    Embeddings only have prompt tokens (no completion).

    Args:
        response: OpenAI embedding API response

    Returns:
        Dict with prompt_tokens, completion_tokens (0), total_tokens
    """
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    if hasattr(response, "usage") and response.usage:
        usage["prompt_tokens"] = response.usage.prompt_tokens or 0
        usage["total_tokens"] = response.usage.total_tokens or 0

    return usage
