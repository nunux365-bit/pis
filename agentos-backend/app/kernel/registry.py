"""Agent registry — source of truth is PostgreSQL `catalog_agents` (see `GET /api/agents`).

Ring-0 code without an async DB session cannot list rows; use the HTTP catalog or
inject `AsyncSession` in your caller.
"""


def list_registered_agents() -> list[dict]:
    """Backward-compatible no-op for sync callers without DB. Prefer `GET /api/agents`."""
    return []
