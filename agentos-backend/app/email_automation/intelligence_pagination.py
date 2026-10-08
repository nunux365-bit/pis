"""Opaque cursor helpers for ``GET .../intelligence/threads`` keyset pagination.

DEPRECATED: Part of the deprecated Replies tab.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone

__all__ = ["ThreadCursorError", "decode_thread_cursor", "encode_thread_cursor"]


class ThreadCursorError(ValueError):
    """Invalid or corrupt pagination cursor."""


def encode_thread_cursor(classified_at: datetime, row_id: uuid.UUID) -> str:
    """DEPRECATED: URL-safe opaque token for the row after which the next page continues."""

    if classified_at.tzinfo is None:
        classified_at = classified_at.replace(tzinfo=timezone.utc)
    payload = {
        "classified_at": classified_at.isoformat(),
        "id": str(row_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_thread_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """DEPRECATED: Decodes a Keyset pagination cursor."""
    if not (token or "").strip():
        raise ThreadCursorError("empty cursor")
    pad = "=" * (-len(token) % 4)
    try:
        data = base64.urlsafe_b64decode((token + pad).encode())
        obj = json.loads(data.decode())
        ca_raw = obj.get("classified_at")
        id_raw = obj.get("id")
        if not isinstance(ca_raw, str) or not isinstance(id_raw, str):
            raise ThreadCursorError("invalid cursor shape")
        ca = datetime.fromisoformat(ca_raw.replace("Z", "+00:00"))
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        row_id = uuid.UUID(id_raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error) as e:
        raise ThreadCursorError("could not decode cursor") from e
    return ca, row_id
