"""Async SQLAlchemy engine for responder-eval chat Postgres (conversations + messages).

Uses ``settings.responder_eval_chat_db_url_sync`` (``postgresql://`` or ``postgresql+asyncpg://``)
coerced to ``postgresql+asyncpg://`` — no separate env var or ORM models.
"""

from __future__ import annotations

import threading

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config.settings import settings

_engine_lock = threading.Lock()
_engine: AsyncEngine | None = None
_engine_url: str | None = None


def chat_eval_database_url_async() -> str:
    u = (settings.responder_eval_chat_db_url_sync or "").strip()
    if not u:
        raise RuntimeError("responder_eval_chat_db_url_sync is not configured")
    if u.startswith("postgresql+asyncpg://"):
        return u
    if u.startswith("postgresql://"):
        return "postgresql+asyncpg://" + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return "postgresql+asyncpg://" + u[len("postgres://") :]
    if u.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + u.split("postgresql+psycopg2://", 1)[1]
    raise ValueError(
        f"Unsupported responder eval chat DB URL scheme (need postgresql://…): {u[:48]}…"
    )


def get_chat_eval_async_engine() -> AsyncEngine:
    global _engine, _engine_url
    url = chat_eval_database_url_async()
    with _engine_lock:
        if _engine is None or _engine_url != url:
            if _engine is not None:
                # Sync dispose from lazy re-init (tests / URL change); shutdown uses async dispose.
                _engine.sync_engine.dispose()
            _engine = create_async_engine(
                url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=0,
                connect_args={"timeout": 15},
            )
            _engine_url = url
        return _engine


async def dispose_chat_eval_async_engine() -> None:
    global _engine, _engine_url
    with _engine_lock:
        eng = _engine
        _engine = None
        _engine_url = None
    if eng is not None:
        await eng.dispose()


def reset_chat_eval_engine_for_tests() -> None:
    """Test helper — close and clear the lazy chat eval engine."""
    global _engine, _engine_url
    with _engine_lock:
        if _engine is not None:
            _engine.sync_engine.dispose()
        _engine = None
        _engine_url = None
