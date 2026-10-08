"""Kernel audit hook — use `app.services.audit.write_audit` from FastAPI routes and services."""

import logging
from typing import Any

log = logging.getLogger(__name__)


def log_action(
    agent: str,
    action: str,
    user_id: str | None = None,
    payload: dict[str, Any] | None = None,
    mlflow_run_id: str | None = None,
) -> None:
    """
    Deprecated placeholder. Persist decisions via `app.services.audit.write_audit`
    inside async request handlers (PostgreSQL `audit_logs`).
    """
    log.debug(
        "kernel.audit.log_action skipped (use services.audit.write_audit): agent=%s action=%s",
        agent,
        action,
    )
