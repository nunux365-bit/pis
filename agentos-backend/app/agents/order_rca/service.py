"""Order RCA service — start run + background graph."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.order_rca import run_store
from app.config.settings import settings
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)


async def ensure_redis() -> None:
    await get_redis().ping()


async def start_diagnosis(
    order_id: str,
    *,
    user_id: str,
    reuse_existing: bool = True,
    chat_id: str | None = None,
) -> dict[str, Any]:
    if not settings.order_rca_enabled:
        raise RuntimeError("Order RCA is disabled")
    if not settings.order_rca_use_fixtures:
        if not (settings.order_rca_order_service_base_url and settings.order_rca_sla_service_base_url):
            raise RuntimeError("Order RCA external API URLs are not configured")
        if not (settings.order_rca_sla_auth_token or "").strip():
            raise RuntimeError("order_rca_sla_auth_token is required for live mode")
    await ensure_redis()
    existing = await run_store.find_reusable_run(user_id, order_id) if reuse_existing else None
    if existing:
        cid = (chat_id or "").strip()
        if cid and str(existing.get("chat_id") or "").strip() != cid:
            patched = await run_store.patch_run(existing["run_id"], chat_id=cid)
            if patched:
                existing = patched
        return {
            "run_id": existing["run_id"],
            "order_id": existing["order_id"],
            "status": existing["status"],
            "_reused": True,
        }
    doc = await run_store.create_run(order_id, user_id=user_id, chat_id=chat_id)
    return {
        "run_id": doc["run_id"],
        "order_id": doc["order_id"],
        "status": doc["status"],
        "_reused": False,
    }


async def get_run(run_id: str, *, user_id: str) -> dict[str, Any] | None:
    doc = await run_store.get_run(run_id)
    if not doc:
        return None
    if doc.get("user_id") and str(doc["user_id"]) != str(user_id):
        return None
    return doc
