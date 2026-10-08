"""
Parse Meta Cloud API webhooks into JIT-hold inbound events.

Validated against Meta docs:

1) Send response
   POST /{phone-number-id}/messages → ``messages[0].id`` = outbound ``wamid``.
   We store Redis ``wamid → order_id`` in ``meta_client.send_template``.

2) Template quick-reply tap (OUR templates)
   Webhook field ``messages``, message ``type`` = ``button`` (NOT interactive).
   Ref: https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/button
   Shape::
     messages[].type = "button"
     messages[].button.payload  ← template button payload (must be option_a / confirm / …)
     messages[].button.text     ← visible title
     messages[].context.id      ← outbound wamid of the template we sent
     messages[].id              ← inbound wamid (dedupe key)

3) Session interactive reply buttons (if ever used)
   ``type`` = ``interactive`` + ``interactive.button_reply.{id,title}``
   + ``context.id`` = outbound wamid
   Ref: interactive reply buttons messages docs.

4) ``biz_opaque_callback_data``
   Echoed on **status** webhooks (sent/delivered/read), NOT on button-tap message
   webhooks. Ref: status messages webhook reference.
   We still send it on outbound for status tracking; order correlation for taps
   uses ``context.id`` → Redis map. Status webhooks can backfill the map.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.whatsapp_jit_hold import session_store
from app.agents.whatsapp_jit_hold.constants import (
    ACTION_BACK,
    ACTION_CONFIRM,
    ACTION_OPTION_A,
    ACTION_OPTION_B,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

_CALLBACK_ORDER_RE = re.compile(r"jit_hold:([A-Z0-9]+):", re.I)


def iter_meta_inbound_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract actionable user replies (button / interactive / text) from a Meta webhook."""
    if not isinstance(payload, dict):
        return []
    if str(payload.get("object") or "") != "whatsapp_business_account":
        return []

    expected_phone_id = (settings.whatsapp_meta_phone_number_id or "").strip()
    events: list[dict[str, Any]] = []

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if str(change.get("field") or "messages") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue

            metadata = value.get("metadata") or {}
            if isinstance(metadata, dict) and expected_phone_id:
                incoming_pid = str(metadata.get("phone_number_id") or "").strip()
                if incoming_pid and incoming_pid != expected_phone_id:
                    log.debug(
                        "meta webhook skip phone_number_id=%s expected=%s",
                        incoming_pid,
                        expected_phone_id,
                    )
                    continue

            contacts = value.get("contacts") or []
            contact_wa = ""
            if isinstance(contacts, list) and contacts:
                c0 = contacts[0]
                if isinstance(c0, dict):
                    contact_wa = str(c0.get("wa_id") or "")

            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                event = _parse_message(msg, contact_wa=contact_wa)
                if event:
                    events.append(event)
                else:
                    log.debug(
                        "meta webhook message ignored type=%s id=%s",
                        msg.get("type"),
                        msg.get("id"),
                    )
    return events


async def apply_meta_status_bindings(payload: dict[str, Any]) -> int:
    """
    Backfill ``wamid → order_id`` from status webhooks' ``biz_opaque_callback_data``.

    Meta returns that field on statuses only (not on button message webhooks).
    """
    if not isinstance(payload, dict):
        return 0
    if str(payload.get("object") or "") != "whatsapp_business_account":
        return 0

    bound = 0
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for st in value.get("statuses") or []:
                if not isinstance(st, dict):
                    continue
                wamid = str(st.get("id") or "").strip()
                opaque = st.get("biz_opaque_callback_data")
                oid = _order_from_callback(opaque)
                if wamid and oid:
                    await session_store.bind_outbound_message(
                        wamid, oid, callback_data=str(opaque or "")
                    )
                    bound += 1
    return bound


async def enrich_order_id(event: dict[str, Any]) -> dict[str, Any]:
    """Resolve parent order via Redis using ``context.id`` (outbound wamid)."""
    if event.get("order_id"):
        return event
    context_id = str(event.get("context_id") or "").strip()
    if not context_id:
        log.warning(
            "meta inbound missing context.id action=%s message_id=%s — phone session fallback only",
            event.get("action"),
            event.get("message_id"),
        )
        return event
    oid = await session_store.order_id_for_outbound_message(context_id)
    if oid:
        return {**event, "order_id": oid}
    log.warning(
        "meta inbound unknown context.id=%s action=%s — phone session fallback",
        context_id,
        event.get("action"),
    )
    return event


def _parse_message(message: dict[str, Any], *, contact_wa: str) -> dict[str, Any] | None:
    phone = _phone_from_message(message, contact_wa)
    if not phone:
        return None

    action, action_label = _extract_action(message)
    if not action:
        return None

    message_id = str(message.get("id") or "")
    context = message.get("context") if isinstance(message.get("context"), dict) else {}
    context_id = str((context or {}).get("id") or "")

    # Button-tap message webhooks do NOT include biz_opaque_callback_data (Meta docs).
    # Keep a defensive read in case Meta adds it later.
    order_id = _order_from_callback(
        message.get("biz_opaque_callback_data")
        or (context or {}).get("biz_opaque_callback_data")
    )

    return {
        "event_type": "meta_message",
        "phone": phone,
        "order_id": order_id,
        "action": action,
        "action_label": action_label,
        "message_id": message_id,
        "context_id": context_id,
        "message_type": str(message.get("type") or ""),
        "raw_envelope": message,
    }


def _phone_from_message(message: dict[str, Any], contact_wa: str) -> str:
    for raw in (message.get("from"), contact_wa):
        digits = "".join(c for c in str(raw or "") if c.isdigit())
        if len(digits) >= 10:
            return digits[-10:]
    return ""


def _order_from_callback(raw: Any) -> str | None:
    if not raw:
        return None
    m = _CALLBACK_ORDER_RE.search(str(raw))
    if m:
        return m.group(1).strip().upper()
    return None


def _extract_action(message: dict[str, Any]) -> tuple[str | None, str | None]:
    msg_type = str(message.get("type") or "").lower()

    # Template quick-reply (primary path for JIT templates) — Meta "button" webhook.
    if msg_type == "button":
        button = message.get("button") or {}
        if isinstance(button, dict):
            payload = str(button.get("payload") or "").strip()
            text = str(button.get("text") or "").strip()
            # Prefer payload (developer-defined); fall back to visible title.
            normalized = _normalize_action(payload) or _normalize_action(text)
            return normalized, text or payload
        return None, None

    # Interactive reply buttons (session messages, not template QR).
    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        if not isinstance(interactive, dict):
            return None, None
        itype = str(interactive.get("type") or "").lower()
        if itype == "button_reply":
            br = interactive.get("button_reply") or {}
            if isinstance(br, dict):
                action_id = str(br.get("id") or "").strip()
                title = str(br.get("title") or "").strip()
                return _normalize_action(action_id) or _normalize_action(title), title or action_id
        if itype == "list_reply":
            lr = interactive.get("list_reply") or {}
            if isinstance(lr, dict):
                action_id = str(lr.get("id") or "").strip()
                title = str(lr.get("title") or "").strip()
                return _normalize_action(action_id) or _normalize_action(title), title or action_id
        return None, None

    # Free-text is not a button. Do not map yes/a/b/proceed onto CONFIRM/Option A.
    return None, None


def _normalize_action(raw: str) -> str | None:
    s = (raw or "").strip().lower().replace(" ", "_")
    if s in {ACTION_OPTION_A, "option_a", "optiona"}:
        return ACTION_OPTION_A
    if s in {ACTION_OPTION_B, "option_b", "optionb"}:
        return ACTION_OPTION_B
    if s in {ACTION_CONFIRM, "confirm"}:
        return ACTION_CONFIRM
    if s in {ACTION_BACK, "back", "go_back"}:
        return ACTION_BACK
    return None
