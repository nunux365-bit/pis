"""Shared AsyncOpenAI client singleton for Optimus SmartQnA."""

from __future__ import annotations

import logging
import threading

import httpx
from openai import AsyncOpenAI

from app.agents.optimus import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_async_openai_client: AsyncOpenAI | None = None
_httpx_client: httpx.AsyncClient | None = None


def _get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.EMBEDDING_TIMEOUT_SECONDS, connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            ),
        )
    return _httpx_client


def get_async_openai_client() -> AsyncOpenAI:
    """Async OpenAI client with timeout, retry, and connection pooling."""
    global _async_openai_client
    with _lock:
        if _async_openai_client is not None and _httpx_client is not None and not _httpx_client.is_closed:
            return _async_openai_client
        _async_openai_client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=config.EMBEDDING_TIMEOUT_SECONDS,
            max_retries=config.EMBEDDING_MAX_RETRIES,
            http_client=_get_httpx_client(),
        )
        return _async_openai_client


async def close_async_openai_client() -> None:
    """Close SmartQnA OpenAI/httpx singletons. Safe to call multiple times."""
    global _async_openai_client, _httpx_client
    with _lock:
        client = _async_openai_client
        http = _httpx_client
        _async_openai_client = None
        _httpx_client = None
    if client is not None:
        try:
            await client.close()
        except Exception:
            log.debug("SmartQnA openai client close failed", exc_info=True)
    if http is not None and not http.is_closed:
        try:
            await http.aclose()
        except Exception:
            log.debug("SmartQnA httpx close failed", exc_info=True)
