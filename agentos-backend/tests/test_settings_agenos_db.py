"""agenos_database_url_sync must follow DATABASE_URL_SYNC when AGENOS_* is unset."""

from __future__ import annotations

import pytest

from app.config.settings import Settings


def test_agenos_db_follows_database_url_sync_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql://prod:secret@52.66.130.251:9799/agentos")
    monkeypatch.delenv("AGENOS_DATABASE_URL_SYNC", raising=False)
    s = Settings()
    assert s.agenos_database_url_sync == s.database_url_sync
    assert "52.66.130.251" in s.agenos_database_url_sync


def test_agenos_db_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql://main@localhost:5432/agentos")
    monkeypatch.setenv("AGENOS_DATABASE_URL_SYNC", "postgresql://agenos@localhost:5432/agenos_only")
    s = Settings()
    assert s.agenos_database_url_sync == "postgresql://agenos@localhost:5432/agenos_only"
    assert s.database_url_sync != s.agenos_database_url_sync
