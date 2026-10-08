"""Meta Cloud API client — template send."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.agents.whatsapp_jit_hold import session_store
from app.agents.whatsapp_jit_hold.phone import mask_phone, normalize_phone_digits, session_phone
from app.agents.whatsapp_jit_hold.templates import TEMPLATE_SPECS, template_body_values
from app.config.settings import settings
from app.infra.httpx_clients import get_meta_http_client

log = logging.getLogger(__name__)


def _access_token() -> str:
    token = (settings.whatsapp_meta_access_token or "").strip()
    if not token:
        raise RuntimeError("WHATSAPP_META_ACCESS_TOKEN is required")
    return token


def _phone_number_id() -> str:
    pid = (settings.whatsapp_meta_phone_number_id or "").strip()
    if not pid:
        raise RuntimeError("WHATSAPP_META_PHONE_NUMBER_ID is required")
    return pid


def _graph_base() -> str:
    version = (settings.whatsapp_meta_graph_version or "v23.0").strip().lstrip("/")
    return f"https://graph.facebook.com/{version}"


def _to_e164_digits(phone: str) -> str:
    """Meta ``to`` field: country+national digits, no plus (India → 91XXXXXXXXXX)."""
    local = normalize_phone_digits(session_phone(phone))
    if len(local) == 10:
        return f"91{local}"
    return local


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }


def _sanitize_body_param(text: str) -> str:
    """Meta rejects newlines/tabs and >4 consecutive spaces in template body params."""
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    cleaned = " | ".join(part.strip() for part in cleaned.split("\n") if part.strip())
    while "     " in cleaned:  # collapse 5+ spaces toward Meta's 4-space cap
        cleaned = cleaned.replace("     ", "    ")
    return cleaned.strip() or "—"


def _template_payload(
    *,
    to: str,
    template_name: str,
    body_values: list[str],
    callback_data: str | None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if body_values:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": _sanitize_body_param(v)} for v in body_values
                ],
            }
        )
    template: dict[str, Any] = {
        "name": template_name,
        "language": {"code": "en"},
    }
    if components:
        template["components"] = components

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": template,
    }
    # Echoed on status webhooks; also stored against wamid for button-reply correlation.
    if callback_data:
        payload["biz_opaque_callback_data"] = callback_data
    return payload


def _initial_send_outcome_uncertain(template_key: str, status_code: int, *, treat_uncertain_as_sent: bool) -> bool:
    """HTTP 5xx/408/429 on the first Kafka initial may have been accepted by Meta."""
    if not treat_uncertain_as_sent or template_key != "initial":
        return False
    return status_code >= 500 or status_code in (408, 429)


async def _confirm_sent_best_effort(order_id: str) -> None:
    """Persist 7-day sent lock. Must not raise Exception — caller already treated Meta as accepted.

    ``CancelledError`` still retries confirm once, then re-raises so the worker
    can stop, with the 7-day lock already written when Redis is up.
    """
    if not order_id:
        return
    try:
        await session_store.confirm_order_sent(order_id)
        return
    except asyncio.CancelledError:
        try:
            await session_store.confirm_order_sent(order_id)
        except Exception:
            log.exception("jit_hold confirm_order_sent failed after Meta accept order_id=%s", order_id)
        raise
    except Exception:
        log.exception("jit_hold confirm_order_sent failed after Meta accept order_id=%s", order_id)
    try:
        await session_store.confirm_order_sent(order_id)
    except Exception:
        log.exception("jit_hold confirm_order_sent retry failed after Meta accept order_id=%s", order_id)


async def send_template(
    phone: str,
    template_key: str,
    context: dict[str, Any],
    *,
    callback_data: str | None = None,
    treat_uncertain_as_sent: bool = False,
) -> dict[str, Any]:
    """Send a pre-approved WhatsApp template via Meta Cloud API."""
    if not settings.whatsapp_jit_hold_enabled:
        raise RuntimeError("WHATSAPP_JIT_HOLD_ENABLED is false — refusing outbound WhatsApp")
    if settings.whatsapp_jit_hold_use_fixtures and not settings.whatsapp_jit_hold_test_mode:
        raise RuntimeError("WHATSAPP_JIT_HOLD_USE_FIXTURES requires TEST_MODE — refusing outbound WhatsApp")
    if settings.whatsapp_jit_hold_split_stub and not settings.whatsapp_jit_hold_test_mode:
        raise RuntimeError("WHATSAPP_JIT_HOLD_SPLIT_STUB requires TEST_MODE — refusing outbound WhatsApp")

    spec = TEMPLATE_SPECS[template_key]
    to = _to_e164_digits(phone)
    local = normalize_phone_digits(session_phone(phone))
    if len(local) != 10:
        raise RuntimeError("refusing WhatsApp send: destination is not a 10-digit Indian mobile")

    order_id = str(context.get("order_id") or "").strip().upper()
    if not order_id and callback_data:
        parts = callback_data.split(":")
        if len(parts) >= 2 and parts[0].lower() == "jit_hold":
            order_id = parts[1].strip().upper()
    if not order_id:
        raise RuntimeError("refusing WhatsApp send: missing order_id")

    body_values = template_body_values(template_key, context)
    required_nonempty = {"customer_name", "held_items", "ship_items"}
    for key, value in zip(spec.body_value_keys, body_values, strict=True):
        if key in required_nonempty and not str(value or "").strip():
            raise RuntimeError(f"refusing WhatsApp send: empty template field {key} for {order_id}")

    payload = _template_payload(
        to=to,
        template_name=spec.name,
        body_values=body_values,
        callback_data=callback_data,
    )

    url = f"{_graph_base()}/{_phone_number_id()}/messages"
    data: dict[str, Any] = {}
    try:
        client = get_meta_http_client()
        resp = await client.post(url, headers=_auth_headers(), json=payload)
        if resp.status_code >= 400:
            log.error(
                "meta send_template failed template=%s order=%s status=%s",
                spec.name,
                order_id,
                resp.status_code,
            )
            if _initial_send_outcome_uncertain(
                template_key, resp.status_code, treat_uncertain_as_sent=treat_uncertain_as_sent
            ):
                log.warning(
                    "meta send_template uncertain template=%s order=%s status=%s — treating as sent to avoid duplicate",
                    spec.name,
                    order_id,
                    resp.status_code,
                )
                await _confirm_sent_best_effort(order_id)
                return {"uncertain": True, "http_status": resp.status_code}
            resp.raise_for_status()
        try:
            parsed = resp.json() if resp.content else {}
            data = parsed if isinstance(parsed, dict) else {"raw": parsed}
        except Exception:
            log.exception(
                "meta send_template json parse failed after HTTP %s template=%s order=%s — treating as sent",
                resp.status_code,
                spec.name,
                order_id,
            )
            data = {"uncertain": True, "http_status": resp.status_code}
    except (httpx.TimeoutException, httpx.NetworkError):
        # Only the Kafka first-send initial holds the sent-lock. BACK resend uses
        # the same template key but must not advance state on timeout.
        if treat_uncertain_as_sent and template_key == "initial":
            log.warning(
                "meta send_template timeout template=%s order=%s to=%s — treating as sent to avoid duplicate",
                spec.name,
                order_id,
                mask_phone(local),
            )
            await _confirm_sent_best_effort(order_id)
            return {"uncertain": True, "timeout": True}
        raise
    except asyncio.CancelledError:
        if treat_uncertain_as_sent and template_key == "initial":
            log.warning(
                "meta send_template cancelled template=%s order=%s — treating as sent to avoid duplicate",
                spec.name,
                order_id,
            )
            await _confirm_sent_best_effort(order_id)
        raise

    wamid = ""
    messages = data.get("messages") or []
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            wamid = str(first.get("id") or "")

    try:
        if wamid and order_id:
            await session_store.bind_outbound_message(
                wamid,
                order_id,
                callback_data=callback_data,
                snapshot=str(context.get("preview_fp") or "") or None,
            )
    except Exception:
        log.exception(
            "jit_hold bind wamid failed after Meta accept template=%s order=%s wamid=%s",
            spec.name,
            order_id,
            wamid or "-",
        )
    finally:
        # Graph already accepted. Confirm even if bind raises CancelledError.
        if treat_uncertain_as_sent:
            await _confirm_sent_best_effort(order_id)

    log.debug(
        "meta sent template=%s to=%s order=%s wamid=%s test_mode=%s",
        spec.name,
        mask_phone(local),
        order_id,
        wamid or "-",
        settings.whatsapp_jit_hold_test_mode,
    )
    return data
