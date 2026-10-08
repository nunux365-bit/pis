"""Meta WhatsApp Cloud API webhook (verify + inbound JIT-hold events)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status

from app.agents.whatsapp_jit_hold.handlers import handle_inbound
from app.agents.whatsapp_jit_hold.meta_parser import (
    apply_meta_status_bindings,
    enrich_order_id,
    iter_meta_inbound_events,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

# Canonical: /api/webhooks/meta
# Alias:     /api/v1/webhook/whatsapp  (Meta App callback URL already set to this)
router = APIRouter(tags=["whatsapp-webhook"])

_WEBHOOK_PATHS = ("/api/webhooks/meta", "/api/v1/webhook/whatsapp")


async def _verify_hub(
    hub_mode: str | None,
    hub_verify_token: str | None,
    hub_challenge: str | None,
) -> Response:
    expected = (settings.whatsapp_meta_webhook_verify_token or "").strip()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook verify token is not configured",
        )
    if hub_mode == "subscribe" and hub_verify_token == expected and hub_challenge is not None:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")


async def _process_meta_events(payload: dict) -> None:
    if not settings.whatsapp_jit_hold_enabled:
        log.debug("meta webhook ignored — WHATSAPP_JIT_HOLD_ENABLED=false")
        return

    # Status webhooks: backfill wamid→order from biz_opaque_callback_data (Meta docs).
    try:
        bound = await apply_meta_status_bindings(payload)
        if bound:
            log.debug("meta status bindings applied count=%s", bound)
    except Exception:
        log.exception("meta status binding failed")

    events = iter_meta_inbound_events(payload)
    for event in events:
        try:
            event = await enrich_order_id(event)
            result = await handle_inbound(event)
            log.debug(
                "meta webhook handled type=%s action=%s order=%s context=%s status=%s",
                event.get("message_type"),
                event.get("action"),
                event.get("order_id"),
                event.get("context_id"),
                result.get("status"),
            )
        except Exception:
            log.exception(
                "meta webhook handler failed action=%s message_id=%s",
                event.get("action"),
                event.get("message_id"),
            )


def _log_delivery_statuses(payload: dict) -> None:
    """Surface delivered/failed so 'accepted but not received' is diagnosable."""
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for st in value.get("statuses") or []:
                status_name = str(st.get("status") or "").lower()
                err = st.get("errors") or []
                if status_name in {"failed", "undelivered"} or err:
                    err0 = err[0] if isinstance(err, list) and err and isinstance(err[0], dict) else {}
                    log.warning(
                        "meta delivery failed wamid=%s status=%s code=%s",
                        st.get("id"),
                        st.get("status"),
                        err0.get("code"),
                    )
                else:
                    log.debug("meta delivery wamid=%s status=%s", st.get("id"), st.get("status"))


async def _receive(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Ack immediately; process button/text replies in background."""
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        try:
            _log_delivery_statuses(payload)
        except Exception:
            log.exception("meta status log failed")
        background_tasks.add_task(_process_meta_events, payload)
    else:
        log.warning("meta webhook POST non-json body")

    return {"success": True}


async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    return await _verify_hub(hub_mode, hub_verify_token, hub_challenge)


async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    return await _receive(request, background_tasks)


for _path in _WEBHOOK_PATHS:
    router.add_api_route(_path, verify_whatsapp_webhook, methods=["GET"])
    router.add_api_route(_path, receive_whatsapp_webhook, methods=["POST"])
