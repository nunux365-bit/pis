"""Startup validation for responder eval configuration."""

from __future__ import annotations

import logging

from app.config.settings import settings

log = logging.getLogger(__name__)


def validate_responder_eval_settings() -> None:
    """Fail fast when responder eval is enabled but misconfigured."""
    if not settings.responder_eval_enabled:
        return

    errors: list[str] = []
    if not settings.responder_eval_use_chat_fixtures:
        if not (settings.responder_eval_chat_db_url_sync or "").strip():
            errors.append("RESPONDER_EVAL_CHAT_DB_URL_SYNC is required when chat fixtures are disabled")
    if not settings.responder_eval_mock_judge and not (settings.openai_api_key or "").strip():
        errors.append(
            "OPENAI_API_KEY is required for the eval judge "
            "(or set RESPONDER_EVAL_MOCK_JUDGE=true)"
        )

    if errors:
        msg = "Responder eval misconfigured: " + "; ".join(errors)
        log.error(msg)
        raise RuntimeError(msg)

    log.info(
        "responder_eval enabled (fixtures=%s, mock_judge=%s)",
        settings.responder_eval_use_chat_fixtures,
        settings.responder_eval_mock_judge,
    )
