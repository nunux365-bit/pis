"""Shared order-status event processing (Kafka)."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.whatsapp_jit_hold.handlers import (
    handle_order_trigger,
    order_status_event_should_dedupe,
)
from app.agents.whatsapp_jit_hold.order_status_parser import (
    parse_order_status_triggers,
    unwrap_json_envelope,
)
from app.agents.whatsapp_jit_hold import session_store

log = logging.getLogger(__name__)


async def process_order_status_raw(raw: str | bytes | dict[str, Any]) -> None:
    """
    Parse nexus order-status payload, dedupe by event_id, trigger eligibility flow.
    """
    envelope: dict[str, Any] | None
    if isinstance(raw, dict):
        envelope = raw
    elif isinstance(raw, (str, bytes)):
        envelope = unwrap_json_envelope(raw if isinstance(raw, str) else raw.decode("utf-8"))
    else:
        envelope = None

    if envelope is None:
        log.debug("order_status non-json body — skipping")
        return

    events = parse_order_status_triggers(envelope)
    if not events:
        log.debug("order_status skip not_on_hold_or_invalid")
        return

    for event in events:
        await _process_packaging_on_hold_event(event)


async def _process_packaging_on_hold_event(event: dict[str, Any]) -> None:
    order_id = str(event.get("order_id") or "")
    event_id = event.get("event_id") or ""
    triggered_at = _packaging_on_hold_triggered_at(event)

    log.info(
        "order_status packaging_on_hold received order_id=%s event_id=%s triggered_at=%s",
        order_id,
        event_id or "-",
        triggered_at or "-",
    )

    if event_id and await session_store.is_webhook_processed(f"order_status:{event_id}"):
        log.info(
            "order_status packaging_on_hold duplicate order_id=%s event_id=%s",
            order_id,
            event_id,
        )
        return

    result = await handle_order_trigger(
        order_id,
        nexus_event=event.get("raw_envelope") if isinstance(event.get("raw_envelope"), dict) else None,
    )
    dedupe = await order_status_event_should_dedupe(result, order_id)
    log.info(
        "order_status packaging_on_hold processed order_id=%s result=%s reason=%s dedupe=%s",
        order_id,
        result.get("status"),
        result.get("reason"),
        dedupe,
    )

    if event_id and dedupe:
        await session_store.mark_webhook_processed(f"order_status:{event_id}")


def _packaging_on_hold_triggered_at(event: dict[str, Any]) -> str:
    raw = event.get("raw_envelope")
    if not isinstance(raw, dict):
        return ""
    ev = raw.get("event")
    if not isinstance(ev, dict):
        return ""
    return str(ev.get("triggered_at") or "").strip()


def normalize_kafka_bootstrap_servers(raw: str) -> list[str]:
    """Comma-separated broker list for aiokafka (host or host:port)."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]
