"""Session manager — snapshots, fork, rollback (LangGraph RedisSaver + APIs)."""

# TODO: Wire RedisSaver checkpoint IDs to REST


def list_active_sessions(user_id: str | None = None) -> list[dict]:
    """Stub active sessions for session inspector."""
    return []
