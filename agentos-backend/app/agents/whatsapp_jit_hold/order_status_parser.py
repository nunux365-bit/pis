"""Parse order-service nexus status update messages (Kafka)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agents.whatsapp_jit_hold.constants import (
    NEXUS_EVENT_NAME_PACKAGING,
    NEXUS_EVENT_TYPE_NON_ORDER,
    NEXUS_ON_HOLD_DESCRIPTION,
    NEXUS_SUB_EVENT_ON_HOLD,
)

log = logging.getLogger(__name__)

_PO_ID_RE = re.compile(r"^PO\d+$", re.I)


def current_nexus_event(envelope: dict[str, Any]) -> dict[str, Any]:
    ev = envelope.get("event")
    return ev if isinstance(ev, dict) else {}


def is_odin_packaging_on_hold(envelope: dict[str, Any]) -> bool:
    """
    True only when the *current* Kafka ``event`` is ODIN packaging on-hold.

    Do not scan ``events[]`` history — later SKU/ETA publishes still contain
    an old ON_HOLD row.
    """
    ev = current_nexus_event(envelope)
    event_type = str(ev.get("event_type") or "").strip().upper()
    event_name = str(ev.get("event_name") or "").strip().upper()
    sub = str(ev.get("sub_event_name") or "").strip().upper().replace("-", "_")
    desc = str(ev.get("description") or "").strip().lower()
    if event_type != NEXUS_EVENT_TYPE_NON_ORDER:
        return False
    if event_name != NEXUS_EVENT_NAME_PACKAGING:
        return False
    if sub == NEXUS_SUB_EVENT_ON_HOLD:
        return True
    return desc == NEXUS_ON_HOLD_DESCRIPTION


def parse_order_status_triggers(raw: str | bytes | dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract one trigger per PO from a nexus Kafka message.

    Pharma group-order topic uses ``group_order_id`` / ``group_orders`` instead of
    top-level ``order_id``.
    """
    envelope = _load_json(raw)
    if envelope is None:
        return []

    if not is_odin_packaging_on_hold(envelope):
        ev = current_nexus_event(envelope)
        order_id = _extract_order_id(envelope)
        if order_id:
            log.debug(
                "order_status skip not odin_on_hold order_id=%s event_type=%s event_name=%s sub_event=%s",
                order_id,
                ev.get("event_type"),
                ev.get("event_name"),
                ev.get("sub_event_name"),
            )
        return []

    order_ids = _extract_order_ids(envelope)
    if not order_ids:
        log.debug("order_status missing order_id keys=%s", list(envelope.keys())[:12])
        return []

    return [
        {
            "order_id": oid,
            "event_id": _extract_event_id(envelope, oid),
            "raw_envelope": _scoped_nexus_envelope(envelope, oid),
        }
        for oid in order_ids
    ]


def parse_order_status_body(raw: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    """First trigger from :func:`parse_order_status_triggers`, if any."""
    triggers = parse_order_status_triggers(raw)
    return triggers[0] if triggers else None


def unwrap_json_envelope(body_raw: str) -> dict[str, Any] | None:
    """Parse a Kafka / HTTP JSON body to a dict."""
    return _load_json(body_raw)


def _load_json(raw: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        data: Any = raw
    elif isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("order_status invalid json")
            return None
    else:
        return None

    if not isinstance(data, dict):
        return None
    return data


def _extract_event_id(envelope: dict[str, Any], order_id: str | None = None) -> str | None:
    """Dedupe key: order + ON_HOLD triggered_at (Mongo ``id`` is reused across publishes)."""
    ev = current_nexus_event(envelope)
    triggered = ev.get("triggered_at")
    oid = (order_id or _extract_order_id(envelope) or "").strip().upper()
    if oid and triggered not in (None, ""):
        return f"{oid}:ON_HOLD:{triggered}"
    for key in ("id", "event_id", "message_id"):
        val = envelope.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _extract_order_ids(envelope: dict[str, Any]) -> list[str]:
    """
    PO ids to trigger for this Kafka message.

    Pharma ``group_orders`` is authoritative: only explicit child ``order_id``
    values are used (never ``group_order_id`` headers or parent ids embedded in
    child rows). Legacy flat envelopes keep top-level ``order_id`` / ``order_details``.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        oid = _normalize_po_id(raw)
        if oid and oid not in seen:
            seen.add(oid)
            found.append(oid)

    group_orders = envelope.get("group_orders")
    if isinstance(group_orders, list) and group_orders:
        for item in group_orders:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("order_id"))
                add(item.get("orderId"))
        return found

    if isinstance(group_orders, dict) and group_orders:
        for item in group_orders.values():
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("order_id"))
                add(item.get("orderId"))
        return found

    add(envelope.get("order_id"))
    add(envelope.get("orderId"))
    add(envelope.get("group_order_id"))
    add(envelope.get("groupOrderId"))

    order_details = envelope.get("order_details")
    if isinstance(order_details, dict):
        basic = order_details.get("basic_order_details")
        if isinstance(basic, dict):
            add(basic.get("order_id"))
            add(basic.get("group_order_id"))
            add(basic.get("orderId"))

    return found


def _scoped_nexus_envelope(envelope: dict[str, Any], order_id: str) -> dict[str, Any]:
    """Per-child view: lift ``order_details`` so downstream merge can use it."""
    oid = order_id.strip().upper()
    group_orders = envelope.get("group_orders")
    if not isinstance(group_orders, list):
        return envelope

    for item in group_orders:
        if not isinstance(item, dict):
            continue
        item_oid = _normalize_po_id(item.get("order_id") or item.get("orderId"))
        if item_oid != oid:
            continue
        scoped = dict(envelope)
        scoped["order_id"] = oid
        details = item.get("order_details")
        if isinstance(details, dict):
            scoped["order_details"] = details
        return scoped

    return envelope


def _extract_order_id(envelope: dict[str, Any]) -> str | None:
    ids = _extract_order_ids(envelope)
    return ids[0] if ids else None


def _normalize_po_id(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if _PO_ID_RE.match(text):
        return text
    return None
