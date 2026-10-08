"""Async SQLAlchemy engine for optional MySQL compliance_call reads (sidecar DB)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def get_mysql_compliance_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        from app.config.settings import settings

        url = (settings.compliance_call_mysql_url or "").strip()
        if not url:
            raise RuntimeError("COMPLIANCE_CALL_MYSQL_URL is not set")
        if not url.startswith("mysql+asyncmy://"):
            raise RuntimeError(
                "COMPLIANCE_CALL_MYSQL_URL must use async driver mysql+asyncmy:// "
                "(e.g. mysql+asyncmy://user:pass@host:3306/dbname)"
            )
        try:
            import asyncmy  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "MySQL compliance requires the asyncmy package. "
                "Install with: pip install asyncmy==0.2.10 (or pip install -e '.[dev]' from agentos-backend)"
            ) from e
        _engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=2,
            max_overflow=2,
            echo=False,
        )
    return _engine


async def dispose_mysql_compliance_engine() -> None:
    """Dispose MySQL compliance engine if it was created. Safe to call multiple times."""
    global _engine
    eng = _engine
    _engine = None
    if eng is not None:
        await eng.dispose()
