"""Tests for responder eval chat DB async engine URL coercion."""

from __future__ import annotations

import pytest

from app.agents.responder_eval import chat_db
from app.config.settings import settings


@pytest.fixture(autouse=True)
def _reset_engine():
    chat_db.reset_chat_eval_engine_for_tests()
    yield
    chat_db.reset_chat_eval_engine_for_tests()


@pytest.mark.parametrize(
    ("sync_url", "expected"),
    [
        (
            "postgresql://localhost:5432/chats",
            "postgresql+asyncpg://localhost:5432/chats",
        ),
        (
            "postgres://localhost:5432/chats",
            "postgresql+asyncpg://localhost:5432/chats",
        ),
        (
            "postgresql+psycopg2://localhost:5432/chats",
            "postgresql+asyncpg://localhost:5432/chats",
        ),
        (
            "postgresql+asyncpg://localhost:5432/chats",
            "postgresql+asyncpg://localhost:5432/chats",
        ),
    ],
)
def test_chat_eval_database_url_async_scheme_coercion(monkeypatch, sync_url, expected):
    monkeypatch.setattr(settings, "responder_eval_chat_db_url_sync", sync_url)
    assert chat_db.chat_eval_database_url_async() == expected


def test_chat_eval_database_url_async_empty_raises(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_chat_db_url_sync", "")
    with pytest.raises(RuntimeError, match="not configured"):
        chat_db.chat_eval_database_url_async()


def test_chat_eval_database_url_async_unsupported_scheme(monkeypatch):
    monkeypatch.setattr(settings, "responder_eval_chat_db_url_sync", "mysql://x")
    with pytest.raises(ValueError, match="Unsupported"):
        chat_db.chat_eval_database_url_async()
