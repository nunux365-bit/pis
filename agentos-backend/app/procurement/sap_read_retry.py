"""Shared SAP OData read retry policy (QAS flakes: deferred 404, mapping errors, 5xx)."""

from __future__ import annotations

import asyncio
import re

# Keep in sync with deferred child GET retries in sap_odata_deferred.
SAP_ODATA_READ_ATTEMPTS = 3
SAP_ODATA_READ_RETRY_DELAY_S = 0.75

_NON_RETRYABLE_MARKERS = (
    "unauthorized",
    "forbidden",
    "bad request",
    "method not allowed",
    "not acceptable",
    "credentials not configured",
    "base url not configured",
    "vendor (supplier) is required",
    "no po line items",
    "no pr line items",
)

_RETRYABLE_MARKERS = (
    "invalid or no mapping",
    "resource not found for the segment",
    "internal server error",
    "timed out",
    "timeout",
    "connection error",
    "csrf token validation failed",
    "temporarily unavailable",
    "gateway timeout",
    "service unavailable",
)

_RETRYABLE_HTTP_STATUS = frozenset({404, 408, 429, 500, 502, 503, 504})
_NON_RETRYABLE_HTTP_STATUS = frozenset({401, 403, 405, 406})


def should_retry_sap_odata_read_error(
    message: str | None,
    *,
    status_code: int | None = None,
) -> bool:
    """True for transient QAS OData read failures; false for auth/validation errors."""
    if status_code is not None:
        if status_code in _NON_RETRYABLE_HTTP_STATUS:
            return False
        if status_code in _RETRYABLE_HTTP_STATUS:
            return True

    m = (message or "").lower()
    if not m:
        return False
    if any(marker in m for marker in _NON_RETRYABLE_MARKERS):
        return False
    if any(marker in m for marker in _RETRYABLE_MARKERS):
        return True

    match = re.search(r"sap http (\d{3})", m)
    if match:
        code = int(match.group(1))
        if code in _NON_RETRYABLE_HTTP_STATUS:
            return False
        if code in _RETRYABLE_HTTP_STATUS:
            return True

    return False


async def sleep_before_sap_read_retry(delay_s: float = SAP_ODATA_READ_RETRY_DELAY_S) -> None:
    await asyncio.sleep(delay_s)
