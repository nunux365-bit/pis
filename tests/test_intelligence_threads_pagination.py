"""Unit tests for intelligence thread list cursor encoding."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.email_automation.intelligence_pagination import (
    ThreadCursorError,
    decode_thread_cursor,
    encode_thread_cursor,
)


def test_encode_decode_roundtrip() -> None:
    ca = datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc)
    rid = uuid.uuid4()
    token = encode_thread_cursor(ca, rid)
    ca2, rid2 = decode_thread_cursor(token)
    assert ca2 == ca
    assert rid2 == rid


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ThreadCursorError):
        decode_thread_cursor("not-a-cursor")
