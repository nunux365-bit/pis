"""Long-lived Deepgram / OpenAI SDK clients for one compliance workflow run."""

from __future__ import annotations

import logging
from deepgram import AsyncDeepgramClient
from openai import AsyncOpenAI

from app.config.settings import settings

log = logging.getLogger(__name__)


def create_compliance_deepgram_client(*, timeout_sec: float = 600.0) -> AsyncDeepgramClient:
    key = (settings.compliance_deepgram_api_key or "").strip()
    if not key:
        raise RuntimeError("COMPLIANCE_DEEPGRAM_API_KEY is not set")
    return AsyncDeepgramClient(api_key=key, timeout=timeout_sec)


def create_compliance_openai_client(*, timeout_sec: float = 600.0) -> AsyncOpenAI:
    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return AsyncOpenAI(api_key=key, timeout=timeout_sec)


async def close_openai_client_safely(client: AsyncOpenAI | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        log.debug("openai client close failed", exc_info=True)


async def close_deepgram_client_safely(client: AsyncDeepgramClient | None) -> None:
    """Best-effort: Fern SDK keeps an internal httpx.AsyncClient."""
    if client is None:
        return
    try:
        cw = getattr(client, "_client_wrapper", None)
        hc = getattr(cw, "httpx_client", None) if cw else None
        inner = getattr(hc, "httpx_client", None) if hc else None
        if inner is not None:
            await inner.aclose()
    except Exception:
        log.debug("deepgram client close failed", exc_info=True)
