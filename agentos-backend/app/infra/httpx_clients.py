"""Shared httpx clients for outbound HTTP (order/SLA internals + Meta Graph).

Auth and host-specific headers stay **per request** — never set as client defaults.
Call ``close_shared_http_clients`` on app shutdown.
"""

from __future__ import annotations

import logging
import threading

import httpx

from app.config.settings import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_internal_client: httpx.AsyncClient | None = None
_meta_client: httpx.AsyncClient | None = None

_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0,
)


def get_internal_http_client() -> httpx.AsyncClient:
    """Shared pool for 1mg order/SLA/P1/Groot/admin-style APIs (RCA + WhatsApp JIT)."""
    global _internal_client
    with _lock:
        if _internal_client is not None and not _internal_client.is_closed:
            return _internal_client
        timeout_sec = float(settings.order_rca_http_timeout_sec or 30)
        _internal_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec, connect=min(10.0, timeout_sec)),
            limits=_LIMITS,
        )
        return _internal_client


def get_meta_http_client() -> httpx.AsyncClient:
    """Shared pool for Meta WhatsApp Cloud API only (separate from internal APIs)."""
    global _meta_client
    with _lock:
        if _meta_client is not None and not _meta_client.is_closed:
            return _meta_client
        _meta_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=_LIMITS,
        )
        return _meta_client


async def close_shared_http_clients() -> None:
    """Close internal + Meta pools. Safe to call multiple times."""
    global _internal_client, _meta_client
    with _lock:
        internal = _internal_client
        meta = _meta_client
        _internal_client = None
        _meta_client = None

    for name, client in (("internal", internal), ("meta", meta)):
        if client is None:
            continue
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            log.debug("shared %s httpx close failed", name, exc_info=True)
