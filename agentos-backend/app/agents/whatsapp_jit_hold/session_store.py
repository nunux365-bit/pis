"""Redis-backed conversation session for WhatsApp JIT hold."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from app.agents.whatsapp_jit_hold.phone import mask_phone, normalize_phone_digits
from app.agents.whatsapp_jit_hold.constants import STATE_DONE_HOLD, STATE_DONE_SPLIT
from app.config.settings import settings
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)

# Confirm path: full eligibility bundle + split + tracking + Meta send.
_SESSION_LOCK_TTL_SEC = 900
_SPLIT_PENDING_TTL_SEC = 900
_SENT_PENDING_TTL_SEC = 900
_TERMINAL_STATES = frozenset({STATE_DONE_SPLIT, STATE_DONE_HOLD})
_SPLIT_DONE = "done"
_SPLIT_BLOCKED = "blocked"

# Atomic compare-and-delete so an expired lock cannot be deleted by a stale holder.
_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""

# Merge outbound wamid doc so a status webhook cannot wipe a send-time snapshot.
_BIND_OUTBOUND_LUA = """
local raw = redis.call("GET", KEYS[1])
local oid = ARGV[1]
local cb = ARGV[2]
local snap = ARGV[3]
local ttl = tonumber(ARGV[4])
if raw then
  local ok, doc = pcall(cjson.decode, raw)
  if ok and type(doc) == "table" then
    local existing_oid = tostring(doc["order_id"] or "")
    if existing_oid ~= "" and existing_oid ~= oid then
      oid = existing_oid
    end
    if cb == "" then
      cb = tostring(doc["callback_data"] or "")
    end
    if snap == "" then
      snap = tostring(doc["snapshot"] or "")
    end
  end
end
local payload = cjson.encode({order_id = oid, callback_data = cb, snapshot = snap})
redis.call("SET", KEYS[1], payload, "EX", ttl)
return payload
"""


def _session_ttl() -> int:
    return max(3600, int(settings.whatsapp_jit_hold_session_ttl_sec))


def _sent_ttl() -> int:
    return max(3600, int(settings.whatsapp_jit_hold_sent_ttl_sec))


def _session_key(phone: str, order_id: str) -> str:
    oid = order_id.strip().upper()
    return f"whatsapp_jit_hold:session:{normalize_phone_digits(phone)}:{oid}"


def _phone_index_key(phone: str) -> str:
    return f"whatsapp_jit_hold:phone_orders:{normalize_phone_digits(phone)}"


def _lock_key(phone: str, order_id: str) -> str:
    oid = order_id.strip().upper()
    return f"whatsapp_jit_hold:lock:{normalize_phone_digits(phone)}:{oid}"


def _sent_key(order_id: str) -> str:
    return f"whatsapp_jit_hold:sent:{order_id.strip().upper()}"


def _order_active_key(order_id: str) -> str:
    return f"whatsapp_jit_hold:order_active:{order_id.strip().upper()}"


def _split_key(order_id: str) -> str:
    return f"whatsapp_jit_hold:split:{order_id.strip().upper()}"


def _split_meta_key(order_id: str) -> str:
    return f"whatsapp_jit_hold:split_meta:{order_id.strip().upper()}"


def _dedupe_key(message_id: str) -> str:
    return f"whatsapp_jit_hold:dedupe:{message_id}"


def _outbound_wamid_key(wamid: str) -> str:
    return f"whatsapp_jit_hold:wamid:{wamid}"


def _outbound_snapshot_key(wamid: str) -> str:
    return f"whatsapp_jit_hold:wamid_snap:{wamid}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def bind_outbound_message(
    wamid: str,
    order_id: str,
    *,
    callback_data: str | None = None,
    snapshot: str | None = None,
) -> None:
    """Map Meta outbound message id → parent order for button-reply correlation.

    Merges with any existing bind. Status webhooks omit the Option A preview
    fingerprint and must not wipe a snapshot already stored at send time.
    """
    wid = (wamid or "").strip()
    oid = (order_id or "").strip().upper()
    if not wid or not oid:
        return
    snap_in = (snapshot or "").strip()
    cb_in = (callback_data or "").strip()
    ttl = _session_ttl()
    key = _outbound_wamid_key(wid)
    r = get_redis()
    try:
        await r.eval(_BIND_OUTBOUND_LUA, 1, key, oid, cb_in, snap_in, str(ttl))
    except Exception:
        log.debug("jit_hold bind_outbound lua unavailable — using GET-merge-SET")
        await _bind_outbound_merge_set(wid, oid, cb_in, snap_in, ttl)
    if snap_in:
        await r.set(_outbound_snapshot_key(wid), snap_in, ex=ttl)


async def _bind_outbound_merge_set(
    wid: str,
    oid: str,
    cb_in: str,
    snap_in: str,
    ttl: int,
) -> None:
    existing = await _outbound_message_doc(wid) or {}
    existing_oid = str(existing.get("order_id") or "").strip().upper()
    if existing_oid and existing_oid != oid:
        log.warning(
            "jit_hold wamid order_id kept existing=%s incoming=%s",
            existing_oid,
            oid,
        )
        oid = existing_oid
    snap = snap_in or str(existing.get("snapshot") or "").strip()
    cb = cb_in or str(existing.get("callback_data") or "").strip()
    payload = json.dumps({"order_id": oid, "callback_data": cb, "snapshot": snap})
    await get_redis().set(_outbound_wamid_key(wid), payload, ex=ttl)


async def order_id_for_outbound_message(wamid: str) -> str | None:
    doc = await _outbound_message_doc(wamid)
    if not doc:
        return None
    oid = str(doc.get("order_id") or "").strip().upper()
    return oid or None


async def snapshot_for_outbound_message(wamid: str) -> str | None:
    """Preview fingerprint stored when this outbound template was sent."""
    wid = (wamid or "").strip()
    if not wid:
        return None
    raw = await get_redis().get(_outbound_snapshot_key(wid))
    if raw:
        snap = raw.decode() if isinstance(raw, bytes) else str(raw)
        return snap.strip() or None
    doc = await _outbound_message_doc(wid)
    if not doc:
        return None
    snap = str(doc.get("snapshot") or "").strip()
    return snap or None


async def callback_data_for_outbound_message(wamid: str) -> str | None:
    """``biz_opaque_callback_data`` stored at send time or status-webhook bind."""
    doc = await _outbound_message_doc(wamid)
    if not doc:
        return None
    cb = str(doc.get("callback_data") or "").strip()
    return cb or None


async def _outbound_message_doc(wamid: str) -> dict[str, Any] | None:
    wid = (wamid or "").strip()
    if not wid:
        return None
    raw = await get_redis().get(_outbound_wamid_key(wid))
    if not raw:
        return None
    try:
        doc = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


async def try_acquire_order_send(order_id: str) -> bool:
    """Atomically claim initial-send slot. True = this caller should send."""
    acquired = await get_redis().set(
        _sent_key(order_id),
        "pending",
        ex=_SENT_PENDING_TTL_SEC,
        nx=True,
    )
    return bool(acquired)


async def confirm_order_sent(order_id: str) -> None:
    """Mark initial send as delivered for the 7-day dedupe window."""
    await get_redis().set(_sent_key(order_id), "sent", ex=_sent_ttl())


async def release_order_send(order_id: str) -> None:
    """Allow retry after failed initial send."""
    await get_redis().delete(_sent_key(order_id))


async def get_order_send_lock_value(order_id: str) -> str | None:
    """Current sent-lock value: ``pending``, ``sent``, or None if absent."""
    raw = await get_redis().get(_sent_key(order_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def has_active_session_for_order(order_id: str) -> bool:
    """True when a non-terminal session exists for this parent order."""
    raw = await get_redis().get(_order_active_key(order_id))
    return raw is not None


async def is_split_completed(order_id: str) -> bool:
    return await _split_value(order_id) == _SPLIT_DONE


async def is_split_blocked(order_id: str) -> bool:
    """True when we must not POST split again (uncertain / mark-done failure)."""
    return await _split_value(order_id) == _SPLIT_BLOCKED


async def _split_value(order_id: str) -> str | None:
    raw = await get_redis().get(_split_key(order_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def try_acquire_split(order_id: str) -> bool:
    """Atomically claim split slot. True = this caller should call the split API."""
    acquired = await get_redis().set(
        _split_key(order_id),
        "pending",
        ex=_SPLIT_PENDING_TTL_SEC,
        nx=True,
    )
    return bool(acquired)


async def save_split_context(
    order_id: str,
    *,
    split_after_ts: int,
    known_child_ids: frozenset[str],
) -> None:
    """Persist pre-split child snapshot + split timestamp for tracking resolution retries."""
    payload = json.dumps(
        {
            "split_after_ts": int(split_after_ts),
            "known_child_ids": sorted(known_child_ids),
        }
    )
    await get_redis().set(_split_meta_key(order_id), payload, ex=_sent_ttl())


async def clear_split_context(order_id: str) -> None:
    """Drop pre-split snapshot after a definite failed POST so Option A can retry."""
    await get_redis().delete(_split_meta_key(order_id))


async def get_split_context(order_id: str) -> tuple[int | None, frozenset[str] | None]:
    raw = await get_redis().get(_split_meta_key(order_id))
    if raw is None:
        return None, None
    try:
        doc = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except json.JSONDecodeError:
        log.warning("jit_hold split_meta unreadable order_id=%s — not POSTing again", order_id)
        return None, frozenset()
    if not isinstance(doc, dict):
        log.warning("jit_hold split_meta unreadable order_id=%s — not POSTing again", order_id)
        return None, frozenset()
    ts = doc.get("split_after_ts")
    split_after_ts: int | None = None
    if ts is not None:
        try:
            split_after_ts = int(ts)
        except (TypeError, ValueError):
            log.warning("jit_hold split_meta unreadable order_id=%s — not POSTing again", order_id)
            return None, frozenset()
    ids_raw = doc.get("known_child_ids")
    known: frozenset[str] = frozenset()
    if isinstance(ids_raw, list):
        known = frozenset(str(x).strip().upper() for x in ids_raw if str(x).strip())
    return split_after_ts, known


async def confirm_split_completed(order_id: str) -> None:
    await get_redis().set(_split_key(order_id), _SPLIT_DONE, ex=_sent_ttl())


async def block_split_retry(order_id: str) -> None:
    """7-day block so a later CONFIRM cannot POST split after pending TTL."""
    await get_redis().set(_split_key(order_id), _SPLIT_BLOCKED, ex=_sent_ttl())


async def release_split(order_id: str) -> None:
    """Allow split retry after a failed API call."""
    await get_redis().delete(_split_key(order_id))


async def is_webhook_processed(message_id: str) -> bool:
    if not message_id:
        return False
    raw = await get_redis().get(_dedupe_key(message_id))
    return raw is not None


async def mark_webhook_processed(message_id: str) -> None:
    if not message_id:
        return
    await get_redis().set(_dedupe_key(message_id), "1", ex=86400)


async def mark_webhook_processed_retry(message_id: str, *, attempts: int = 5) -> None:
    if not message_id:
        return
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            await mark_webhook_processed(message_id)
            return
        except Exception as exc:
            last = exc
            log.warning(
                "jit_hold mark_webhook_processed attempt %s/%s failed message_id=%s",
                attempt + 1,
                attempts,
                message_id,
            )
            await asyncio.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


async def get_session(phone: str, order_id: str) -> dict[str, Any] | None:
    raw = await get_redis().get(_session_key(phone, order_id))
    if not raw:
        return None
    try:
        doc = json.loads(raw)
        return doc if isinstance(doc, dict) else None
    except json.JSONDecodeError:
        log.warning("invalid session json phone=%s order_id=%s", mask_phone(phone), order_id)
        return None


async def save_session(phone: str, order_id: str, doc: dict[str, Any]) -> None:
    oid = order_id.strip().upper()
    doc = {**doc, "order_id": oid, "updated_at": _now()}
    r = get_redis()
    await r.set(_session_key(phone, oid), json.dumps(doc), ex=_session_ttl())
    index_key = _phone_index_key(phone)
    if doc.get("state") in _TERMINAL_STATES:
        await r.srem(index_key, oid)
        await r.delete(_order_active_key(oid))
    else:
        await r.sadd(index_key, oid)
        await r.expire(index_key, _session_ttl())
        await r.set(_order_active_key(oid), normalize_phone_digits(phone), ex=_session_ttl())


def _is_active_session(doc: dict[str, Any]) -> bool:
    return doc.get("state") not in _TERMINAL_STATES


async def resolve_session(
    phone: str,
    order_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Load session by phone + order_id, or disambiguate when only phone is known.

    When multiple orders are active for one phone, prefers explicit order_id from
    callback_data; otherwise picks the session with the latest ``updated_at``.
    """
    oid_hint = (order_id or "").strip().upper()
    if oid_hint:
        doc = await get_session(phone, oid_hint)
        return doc, oid_hint if doc else None

    r = get_redis()
    index_key = _phone_index_key(phone)
    members = await r.smembers(index_key)
    if not members:
        return None, None

    order_ids = [m.decode() if isinstance(m, bytes) else str(m) for m in members]
    active: list[tuple[str, dict[str, Any]]] = []
    for oid in order_ids:
        doc = await get_session(phone, oid)
        if not doc:
            await r.srem(index_key, oid)
            continue
        if not _is_active_session(doc):
            await r.srem(index_key, oid)
            continue
        active.append((oid, doc))

    if not active:
        return None, None
    if len(active) == 1:
        return active[0][1], active[0][0]

    best_doc: dict[str, Any] | None = None
    best_oid: str | None = None
    best_ts = ""
    for oid, doc in active:
        ts = str(doc.get("updated_at") or "")
        if ts >= best_ts:
            best_ts = ts
            best_doc = doc
            best_oid = oid

    if best_doc:
        log.debug(
            "jit_hold multiple active sessions phone=%s count=%s picked=%s",
            mask_phone(phone),
            len(active),
            best_oid,
        )
    return best_doc, best_oid


@asynccontextmanager
async def session_lock(
    phone: str,
    order_id: str,
    *,
    ttl_sec: int = _SESSION_LOCK_TTL_SEC,
) -> AsyncIterator[bool]:
    """
    Distributed lock per phone+order session.

    Yields True when acquired, False when another replica is processing the same session.
    """
    key = _lock_key(phone, order_id)
    token = str(uuid.uuid4())
    acquired = await get_redis().set(key, token, ex=ttl_sec, nx=True)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        try:
            await get_redis().eval(_RELEASE_LOCK_LUA, 1, key, token)
        except Exception:
            log.exception(
                "jit_hold session lock release failed phone=%s order_id=%s",
                mask_phone(phone),
                order_id,
            )
