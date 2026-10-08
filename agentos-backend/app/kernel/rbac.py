"""Permission engine — department-scoped agent/system access."""

# TODO: Enforce RBAC on API and tool calls


def can_agent_access(agent_id: str, resource: str, action: str) -> bool:
    """Stub permission check."""
    return True
