"""Process-wide AsyncOpenAI + httpx pool for platform LLM hot paths (eval, RCA).

One client per worker avoids per-request TCP/TLS churn. Call ``close_shared_openai_client``
on app shutdown. Init is lock-guarded; close clears globals before awaiting so we never
hold the lock across I/O.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import httpx
from openai import AsyncOpenAI

from app.config.settings import settings

log = logging.getLogger(__name__)

# Covers Order RCA synthesis (120s) and responder-eval judge.
_TIMEOUT_SEC = 120.0

_lock = threading.Lock()
_http_client: httpx.AsyncClient | None = None
_openai_client: AsyncOpenAI | None = None


def _schedule_http_close(http: httpx.AsyncClient) -> None:
    """Best-effort close from sync code (failed init / defensive recreate)."""
    if http.is_closed:
        return

    async def _close() -> None:
        try:
            await http.aclose()
        except Exception:
            log.debug("shared openai httpx deferred close failed", exc_info=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        log.warning("shared openai httpx orphaned (no running loop to close)")
        return
    from app.infra.task_tracker import spawn

    spawn(_close(), name="openai-http-close")


def get_shared_openai_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client (lazy init, connection-pooled httpx)."""
    global _http_client, _openai_client
    with _lock:
        if (
            _openai_client is not None
            and _http_client is not None
            and not _http_client.is_closed
        ):
            return _openai_client

        # Defensive recreate: prior instance closed or partial init left stale refs.
        stale_http = _http_client
        _http_client = None
        _openai_client = None

        key = (settings.openai_api_key or "").strip()
        if not key:
            if stale_http is not None:
                _schedule_http_close(stale_http)
            raise RuntimeError("openai_api_key is not configured")

        http = httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT_SEC, connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            ),
        )
        try:
            client = AsyncOpenAI(
                api_key=key,
                timeout=_TIMEOUT_SEC,
                http_client=http,
            )
        except Exception:
            _schedule_http_close(http)
            if stale_http is not None:
                _schedule_http_close(stale_http)
            raise

        if stale_http is not None:
            log.warning("shared openai client recreated; closing prior httpx pool")
            _schedule_http_close(stale_http)

        _http_client = http
        _openai_client = client
        return client


async def close_shared_openai_client() -> None:
    """Close shared OpenAI/httpx clients. Safe to call multiple times."""
    global _http_client, _openai_client
    with _lock:
        client = _openai_client
        http = _http_client
        _openai_client = None
        _http_client = None

    # Clear globals before awaiting so concurrent get_* never races with close I/O.
    if client is not None:
        try:
            await client.close()
        except Exception:
            log.debug("shared openai client close failed", exc_info=True)
    # SDK may or may not close a caller-supplied httpx client; always ensure pool is down.
    if http is not None and not http.is_closed:
        try:
            await http.aclose()
        except Exception:
            log.debug("shared openai httpx close failed", exc_info=True)
