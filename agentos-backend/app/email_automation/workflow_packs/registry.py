"""In-memory registry of workflow packs — one canonical lookup surface."""

from __future__ import annotations

from .base import WorkflowPack
from .payment_reminder import PAYMENT_REMINDER_WEEKLY

REGISTRY: dict[str, WorkflowPack] = {
    PAYMENT_REMINDER_WEEKLY.workflow_type: PAYMENT_REMINDER_WEEKLY,
}


def get_pack(workflow_type: str) -> WorkflowPack:
    try:
        return REGISTRY[workflow_type]
    except KeyError as e:
        raise KeyError(
            f"Unknown workflow_type {workflow_type!r}. Known: {list(REGISTRY)!r}"
        ) from e


def workflow_types() -> list[str]:
    return list(REGISTRY.keys())
