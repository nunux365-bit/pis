"""Flock API client for calling Flock endpoints.

PERFORMANCE: Uses a shared httpx.AsyncClient with connection pooling
to avoid creating new connections for each API call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.agents.optimus import config

log = logging.getLogger(__name__)

FLOCK_API_BASE = "https://api.flock.com/v1"

# Shared HTTP client with connection pooling
# Reuses TCP connections across requests for better performance
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            ),
        )
    return _http_client


async def close_http_client() -> None:
    """Close the shared HTTP client. Call on app shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def get_user_info(user_token: str, user_id: str | None = None) -> dict[str, Any] | None:
    """
    Get user info from Flock API.

    https://docs.flock.com/display/flockos/users.getInfo

    When called with a userToken (from app.install), returns the token owner's profile.
    This is more reliable than using bot token because workspace privacy settings
    rarely restrict a user from seeing their own profile.

    Args:
        user_token: User's Flock token (from app.install event's userToken field)
        user_id: Optional user ID (defaults to token owner)

    Returns:
        User info dict with id, email, firstName, lastName, etc.
        Returns None if request fails.
    """
    try:
        client = _get_http_client()
        # Use query params for GET-style request (Flock API pattern)
        params = {"token": user_token}
        if user_id:
            params["userId"] = user_id

        response = await client.get(
            f"{FLOCK_API_BASE}/users.getInfo",
            params=params,
            timeout=5.0,  # Shorter timeout for user info
        )

        if response.status_code != 200:
            log.warning("Flock users.getInfo failed: %s %s", response.status_code, response.text)
            return None

        data = response.json()

        # Flock v1 API returns { status: "ok", user: {...} } for users.getInfo
        # OR it might return the user directly without wrapper
        if data.get("status") == "ok":
            user = data.get("user", {})
            return {
                "id": user.get("id", ""),
                "email": user.get("email", ""),
                "firstName": user.get("firstName", ""),
                "lastName": user.get("lastName", ""),
            }

        # Some Flock API responses return user data directly (no "status" wrapper)
        if data.get("id") and data.get("email"):
            return {
                "id": data.get("id", ""),
                "email": data.get("email", ""),
                "firstName": data.get("firstName", ""),
                "lastName": data.get("lastName", ""),
            }

        log.warning("Flock users.getInfo non-ok: error=%s, full_response=%s", data.get("error"), data)
        return None

    except Exception as e:
        log.error("Error calling Flock users.getInfo: %s", e)
        return None


async def send_message(
    to: str,
    text: str,
    bot_token: str | None = None,
    flockml: str | None = None,
) -> bool:
    """
    Send a message to a Flock user or channel.

    https://docs.flock.com/display/flockos/chat.sendMessage

    Args:
        to: User ID or channel ID to send to
        text: Plain text (used for push notification fallback)
        bot_token: Bot token (uses config if not provided)
        flockml: Optional FlockML formatted message

    Returns:
        True if sent successfully
    """
    token = bot_token or config.FLOCK_BOT_TOKEN
    if not token:
        log.error("No Flock bot token configured")
        return False

    try:
        client = _get_http_client()
        # Use form-encoded body (handles long messages safely)
        data = {
            "token": token,
            "to": to,
            "text": text,
        }
        if flockml:
            data["flockml"] = flockml

        response = await client.post(
            f"{FLOCK_API_BASE}/chat.sendMessage",
            data=data,  # form-encoded, not JSON
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            log.warning("Flock chat.sendMessage failed: %s %s", response.status_code, response.text)
            return False

        result = response.json()
        if result.get("uid"):
            return True
        else:
            log.warning("Flock sendMessage unexpected response")
            return False

    except Exception as e:
        log.error("Error sending Flock message: %s", e)
        return False
