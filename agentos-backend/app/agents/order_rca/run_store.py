"""Redis-backed async run state for Order RCA."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config.settings import settings
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)

RUN_STALE_MESSAGE = "Diagnosis run timed out after 1 hour without completing."
_REUSABLE_STATUSES = frozenset({"queued", "running", "completed"})


def _key(run_id: str) -> str:
    return f"order_rca:run:{run_id}"


def _idem_key(user_id: str, order_id: str) -> str:
    return f"order_rca:idem:{user_id}:{order_id.strip().upper()}"


def _run_ttl_sec() -> int:
    return max(60, int(settings.order_rca_run_ttl_sec))


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def bind_idempotency(user_id: str, order_id: str, run_id: str) -> None:
    """Map (user, order) → run_id for idempotent diagnose; expires with run TTL."""
    await get_redis().set(_idem_key(user_id, order_id), run_id, ex=_run_ttl_sec())


async def clear_idempotency(user_id: str, order_id: str) -> None:
    await get_redis().delete(_idem_key(user_id, order_id))


async def find_reusable_run(user_id: str, order_id: str) -> dict[str, Any] | None:
    """
    Return an existing run for the same user + order when still valid.

    Reuses queued/running/completed runs (until Redis TTL). Failed runs are not reused.
    """
    raw = await get_redis().get(_idem_key(user_id, order_id))
    if not raw:
        return None
    run_id = raw.decode() if isinstance(raw, bytes) else str(raw)
    doc = await get_run(run_id)
    if not doc:
        await clear_idempotency(user_id, order_id)
        return None
    if doc.get("user_id") and str(doc["user_id"]) != str(user_id):
        return None
    if doc.get("status") not in _REUSABLE_STATUSES:
        return None
    return doc


async def create_run(order_id: str, *, user_id: str, chat_id: str | None = None) -> dict[str, Any]:
    run_id = str(uuid4())
    doc = {
        "run_id": run_id,
        "order_id": order_id.strip().upper(),
        "user_id": user_id,
        "status": "queued",
        "progress": {"step": "collect", "label": "Queued", "completed": 0, "total": 6},
        "report": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if chat_id:
        doc["chat_id"] = chat_id.strip()
    await save_run(doc)
    await bind_idempotency(user_id, order_id, run_id)
    return doc


async def save_run(doc: dict[str, Any]) -> None:
    doc["updated_at"] = _now()
    r = get_redis()
    await r.set(_key(doc["run_id"]), json.dumps(doc, default=str), ex=_run_ttl_sec())


def _parse_updated_at(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_is_stale(doc: dict[str, Any]) -> bool:
    if doc.get("status") not in ("queued", "running"):
        return False
    updated = _parse_updated_at(doc.get("updated_at"))
    if not updated:
        return False
    return datetime.now(UTC) - updated > timedelta(seconds=_run_ttl_sec())


async def get_run(run_id: str) -> dict[str, Any] | None:
    raw = await get_redis().get(_key(run_id))
    if not raw:
        return None
    doc = json.loads(raw)
    if _run_is_stale(doc):
        doc = {
            **doc,
            "status": "failed",
            "error": {"code": "run_timeout", "message": RUN_STALE_MESSAGE},
            "report": None,
        }
        await save_run(doc)
        uid, oid = doc.get("user_id"), doc.get("order_id")
        if uid and oid:
            await clear_idempotency(str(uid), str(oid))
        log.warning("order_rca run marked stale failed run_id=%s", run_id)
    return doc


async def patch_run(run_id: str, **fields: Any) -> dict[str, Any] | None:
    doc = await get_run(run_id)
    if not doc:
        return None
    doc.update(fields)
    await save_run(doc)
    uid, oid = doc.get("user_id"), doc.get("order_id")
    if uid and oid:
        status = doc.get("status")
        if status == "completed":
            await bind_idempotency(str(uid), str(oid), run_id)
        elif status == "failed":
            await clear_idempotency(str(uid), str(oid))
    return doc


async def set_progress(run_id: str, completed: int, label: str, step: str = "collect") -> None:
    doc = await get_run(run_id)
    if not doc:
        return
    doc["status"] = "running"
    doc["progress"] = {"step": step, "label": label, "completed": completed, "total": 6}
    await save_run(doc)
