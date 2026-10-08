"""Sync psycopg2 connection to the `agenos` billing database (separate from main AgentOS app DB)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import RealDictCursor

from app.config.settings import settings


@contextmanager
def agenos_connection() -> Generator[PgConnection, None, None]:
    # connect_timeout: fail fast when agenos is down (seconds)
    conn = psycopg2.connect(
        settings.agenos_database_url_sync,
        connect_timeout=max(1, min(settings.agenos_connect_timeout_seconds, 120)),
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def agenos_dict_cursor(conn: PgConnection):
    return conn.cursor(cursor_factory=RealDictCursor)
