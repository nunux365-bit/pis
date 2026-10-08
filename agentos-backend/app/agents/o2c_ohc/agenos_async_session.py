"""Async SQLAlchemy engine + session for the agenos billing DB (asyncpg).

Use from FastAPI routes and other async code with ``await`` and ``AgenosAsyncSessionLocal``.

Sync call sites (LangGraph worker thread, scripts) should use ``run_agenos_async(...)`` to run
one-shot coroutines when no event loop is running — not from inside ``async def`` handlers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings

T = TypeVar("T")


def agenos_database_url_async() -> str:
    u = (settings.agenos_database_url_sync or "").strip()
    if not u:
        raise ValueError("agenos_database_url_sync is empty")
    if u.startswith("sqlite+aiosqlite://"):
        return u
    if u.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + u[len("sqlite://") :]
    if u.startswith("postgresql+asyncpg://"):
        return u
    if u.startswith("postgresql://"):
        return "postgresql+asyncpg://" + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return "postgresql+asyncpg://" + u[len("postgres://") :]
    if u.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + u.split("postgresql+psycopg2://", 1)[1]
    raise ValueError(f"Unsupported agenos database URL scheme (need postgresql://…): {u[:48]}…")


_ct = max(1, min(int(settings.agenos_connect_timeout_seconds), 120))

# NullPool: safe when ``run_agenos_async`` uses ``asyncio.run()`` per call (new loop each time);
# pooled asyncpg connections must not be reused across closed event loops.
agenos_async_engine = create_async_engine(
    agenos_database_url_async(),
    poolclass=NullPool,
    pool_pre_ping=True,
    connect_args={"timeout": _ct},
)

AgenosAsyncSessionLocal = async_sessionmaker(
    agenos_async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def dispose_agenos_async_engine() -> None:
    await agenos_async_engine.dispose()


def run_agenos_async(coro: Coroutine[Any, Any, T]) -> T:
    """
    Run an agenos coroutine from synchronous code when **no** event loop is running.

    Intended for LangGraph / ``run_in_executor`` worker threads and similar. Do **not** call from
    async FastAPI handlers — ``await`` the mis_db coroutine instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "run_agenos_async() must not be used under a running event loop; await the coroutine instead."
    )
