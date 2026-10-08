"""Bounded retries for compliance_call external APIs (OpenAI, Deepgram SDK / httpx)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

from app.config.settings import settings

log = logging.getLogger(__name__)

_T = TypeVar("_T")


def sleep_backoff(attempt: int) -> None:
    base = float(settings.compliance_retry_base_seconds or 0.75)
    cap = float(settings.compliance_retry_max_sleep_seconds or 60.0)
    delay = min(cap, max(0.05, base) * (2**attempt))
    time.sleep(delay)


async def async_sleep_backoff(attempt: int) -> None:
    base = float(settings.compliance_retry_base_seconds or 0.75)
    cap = float(settings.compliance_retry_max_sleep_seconds or 60.0)
    delay = min(cap, max(0.05, base) * (2**attempt))
    await asyncio.sleep(delay)


def retryable_openai_error(exc: BaseException) -> bool:
    """429, 5xx, and connection/timeout from the OpenAI SDK."""
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(exc, APIError):
        code = getattr(exc, "status_code", None)
        return code is None or code >= 500 or code == 429
    return False


def retryable_http_status(status_code: int) -> bool:
    """Deepgram / generic REST: retry rate limits and server/transient errors."""
    if status_code == 429:
        return True
    if status_code == 408:
        return True
    if 500 <= status_code <= 599:
        return True
    return False


def retryable_httpx_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.RequestError) and not isinstance(exc, httpx.HTTPStatusError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return retryable_http_status(exc.response.status_code)
    return False


def retryable_deepgram_error(exc: BaseException) -> bool:
    """Deepgram SDK ``ApiError`` plus httpx-compatible errors."""
    try:
        from deepgram.core.api_error import ApiError as DeepgramApiError
    except ImportError:
        return retryable_httpx_error(exc)
    if isinstance(exc, DeepgramApiError):
        code = getattr(exc, "status_code", None)
        if code is None:
            return True
        return retryable_http_status(int(code))
    return retryable_httpx_error(exc)


def max_retry_attempts() -> int:
    return max(1, int(settings.compliance_retry_attempts or 5))


def vendor_client_refresh_recommended(exc: BaseException, *, vendor: str) -> bool:
    """
    After bounded SDK retries, optionally discard and recreate the vendor HTTP client.

    Use only for transport / pool / ambiguous API failures — not for 401 (bad key) or
    ordinary rate limits (429) where a fresh client will not help.
    """
    v = (vendor or "").strip().lower()
    if v == "openai":
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
        if isinstance(exc, APIError):
            code = getattr(exc, "status_code", None)
            if code in (401, 403):
                return False
            if code == 429:
                return False
            if code is not None and 500 <= int(code) <= 599:
                return True
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            sc = exc.response.status_code
            if sc in (401, 403, 429):
                return False
            if 500 <= sc <= 599:
                return True
            return False
        return retryable_httpx_error(exc) and not isinstance(exc, httpx.HTTPStatusError)

    if v == "deepgram":
        try:
            from deepgram.core.api_error import ApiError as DeepgramApiError

            if isinstance(exc, DeepgramApiError):
                code = getattr(exc, "status_code", None)
                if code is None:
                    return True
                ic = int(code)
                if ic in (401, 403, 429):
                    return False
                return retryable_http_status(ic)
        except ImportError:
            pass
        if isinstance(exc, httpx.HTTPStatusError):
            sc = exc.response.status_code
            if sc in (401, 403, 429):
                return False
            return retryable_http_status(sc)
        return retryable_httpx_error(exc) and not isinstance(exc, httpx.HTTPStatusError)

    return False


def call_with_retry(
    op_name: str,
    fn: Callable[[], _T],
    *,
    is_retryable: Callable[[BaseException], bool],
) -> _T:
    """Run sync ``fn`` up to ``compliance_retry_attempts`` times with exponential backoff."""
    attempts = max_retry_attempts()
    for attempt in range(attempts):
        try:
            return fn()
        except BaseException as e:
            if attempt + 1 >= attempts or not is_retryable(e):
                raise
            log.warning(
                "%s failed (attempt %d/%d): %s — backing off",
                op_name,
                attempt + 1,
                attempts,
                e,
            )
            sleep_backoff(attempt)


async def async_call_with_retry(
    op_name: str,
    fn: Callable[[], Awaitable[_T]],
    *,
    is_retryable: Callable[[BaseException], bool],
) -> _T:
    """Run async ``fn`` up to ``compliance_retry_attempts`` times with exponential backoff."""
    attempts = max_retry_attempts()
    for attempt in range(attempts):
        try:
            return await fn()
        except BaseException as e:
            if attempt + 1 >= attempts or not is_retryable(e):
                raise
            log.warning(
                "%s failed (attempt %d/%d): %s — backing off",
                op_name,
                attempt + 1,
                attempts,
                e,
            )
            await async_sleep_backoff(attempt)
