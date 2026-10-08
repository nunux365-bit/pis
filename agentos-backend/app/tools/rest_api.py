"""Async outbound HTTP for agents and workflows — SSRF-aware."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config.settings import settings


class RestApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
    }
)


def _host_blocked(hostname: str) -> bool:
    h = (hostname or "").strip().lower().rstrip(".")
    if not h or h in _METADATA_HOSTS:
        return True
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or (ip.version == 4 and ip == ipaddress.IPv4Address("0.0.0.0"))
        )
    except ValueError:
        pass
    # IPv6 literals in brackets already stripped by urlparse hostname
    return False


def _allowlist_allows(hostname: str) -> bool:
    allow = settings.external_http_allowlist
    if not allow:
        return True
    h = hostname.lower().rstrip(".")
    for entry in allow:
        e = entry.lower().strip().rstrip(".")
        if h == e or h.endswith(f".{e}"):
            return True
    return False


def assert_url_allowed(url: str) -> None:
    """Raise RestApiError if URL must not be fetched (SSRF / policy)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise RestApiError("Only http/https URLs are allowed")
    if not parsed.hostname:
        raise RestApiError("URL missing hostname")
    if _host_blocked(parsed.hostname):
        raise RestApiError("Hostname is not allowed (blocked network or metadata)")
    if not _allowlist_allows(parsed.hostname):
        raise RestApiError("Hostname not in external_http_allowlist")


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


async def async_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """
    Perform an async HTTP request. Returns (status_code, response_headers_subset, body_bytes).
    """
    m = method.upper().strip()
    if m not in _METHODS:
        raise RestApiError(f"Unsupported method: {method}")
    assert_url_allowed(url)
    max_bytes = 5 * 1024 * 1024
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        resp = await client.request(
            m,
            url,
            headers=headers or None,
            json=json_body,
            params=params or None,
        )
        body = resp.content
        if len(body) > max_bytes:
            raise RestApiError("Response body exceeds size limit")
        hout = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "content-length")}
        return resp.status_code, hout, body


def json_safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Drop hop-by-hop / auth headers mistakenly passed from clients."""
    if not headers:
        return {}
    block = frozenset(
        k.lower()
        for k in (
            "host",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        )
    )
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in block or lk.startswith("x-forwarded"):
            continue
        if re.match(r"^[\x20-\x7e]+$", k) and isinstance(v, str) and len(v) < 8192:
            out[k] = v
    return out
