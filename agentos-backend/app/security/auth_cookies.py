"""HttpOnly auth cookies — browser sessions without tokens in URLs or JS-readable storage."""

from __future__ import annotations

from typing import Literal

from starlette.responses import Response

from app.config.settings import settings

# Names must match ``get_current_user`` (``access_token`` cookie) and refresh handling.
COOKIE_ACCESS = "access_token"
COOKIE_REFRESH = "refresh_token"


def _samesite() -> Literal["lax", "strict", "none"]:
    v = (settings.auth_cookie_samesite or "lax").strip().lower()
    if v in ("lax", "strict", "none"):
        return v  # type: ignore[return-value]
    return "lax"


def attach_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    """Set standard HttpOnly cookies for SPA + API (BFF-style)."""
    secure = bool(settings.auth_cookie_secure)
    same = _samesite()
    if same == "none" and not secure:
        secure = True
    max_access = max(60, int(settings.access_token_expire_minutes) * 60)
    max_refresh = max(3600, int(settings.refresh_token_expire_days) * 86400)
    response.set_cookie(
        COOKIE_ACCESS,
        access_token,
        max_age=max_access,
        httponly=True,
        secure=secure,
        samesite=same,
        path="/",
    )
    response.set_cookie(
        COOKIE_REFRESH,
        refresh_token,
        max_age=max_refresh,
        httponly=True,
        secure=secure,
        samesite=same,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    secure = bool(settings.auth_cookie_secure)
    same = _samesite()
    if same == "none" and not secure:
        secure = True
    response.delete_cookie(COOKIE_ACCESS, path="/", secure=secure, samesite=same, httponly=True)
    response.delete_cookie(COOKIE_REFRESH, path="/", secure=secure, samesite=same, httponly=True)
