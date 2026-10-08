from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import hashlib
import secrets
from jose import JWTError, jwt

from app.config.settings import settings


def create_access_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    *,
    expires_delta: timedelta | None = None,
) -> str:
    now = datetime.now(UTC)
    expire = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )


def verify_access_token(token: str) -> UUID:
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise JWTError("wrong token type")
        sub = payload.get("sub")
        if not sub:
            raise JWTError("missing sub")
        return UUID(sub)
    except (JWTError, ValueError) as e:
        raise ValueError("invalid token") from e


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_google_oauth_state(user_id: UUID) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": "google_oauth",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_google_oauth_state(token: str) -> UUID:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "google_oauth":
            raise ValueError("wrong type")
        sub = payload.get("sub")
        if not sub:
            raise ValueError("missing sub")
        return UUID(sub)
    except (JWTError, ValueError, TypeError) as e:
        raise ValueError("invalid oauth state") from e


def create_google_sso_state(next_path: str) -> str:
    """Opaque state for Google web login (OIDC); bound to post-login redirect path."""
    now = datetime.now(UTC)
    safe = (next_path or "/chat")[:512]
    payload: dict[str, Any] = {
        "type": "google_sso",
        "next": safe,
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_google_sso_state(token: str) -> str:
    """Return sanitized in-app path after login (leading slash, no open redirects)."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "google_sso":
            raise ValueError("wrong type")
        nxt = payload.get("next")
        if not isinstance(nxt, str) or not nxt.startswith("/"):
            return "/chat"
        if nxt.startswith("//") or "://" in nxt or "@" in nxt.split("/", 1)[0]:
            return "/chat"
        return nxt
    except (JWTError, ValueError, TypeError) as e:
        raise ValueError("invalid google sso state") from e
