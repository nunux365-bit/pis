"""Step type → async handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkflowRun
from app.workflow_engine import steps_builtin

StepHandler = Callable[
    [dict[str, Any], dict[str, Any], AsyncSession, UUID, WorkflowRun],
    Awaitable[dict[str, Any]],
]

REGISTRY: dict[str, StepHandler] = {
    "transform.parse_numbers": steps_builtin.step_parse_numbers,
    "transform.multiply": steps_builtin.step_multiply,
    "tool.google.sheets_values": steps_builtin.step_google_sheets_values,
    "tool.http_json": steps_builtin.step_http_json,
    "hitl.standard": steps_builtin.step_hitl_standard,
}


def get_step_handler(step_type: str) -> StepHandler:
    h = REGISTRY.get(step_type)
    if not h:
        raise KeyError(f"Unknown pipeline step type: {step_type}")
    return h
