"""Internal routes for WhatsApp JIT hold (no dashboard)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.agents.whatsapp_jit_hold.handlers import handle_order_trigger
from app.agents.whatsapp_jit_hold.templates import template_catalog
from app.config.settings import settings

router = APIRouter(prefix="/__internal__/whatsapp-jit-hold", tags=["whatsapp-jit-hold-internal"])


def _require_enabled() -> None:
    if not settings.whatsapp_jit_hold_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="WhatsApp JIT hold is disabled")


@router.get("/templates")
async def list_templates():
    """Meta template catalog for dashboard upload."""
    _require_enabled()
    return {"templates": template_catalog()}


@router.post("/trigger/{order_id}")
async def trigger_order(order_id: str):
    """Manually trigger eligibility + initial message (dev / test)."""
    _require_enabled()
    result = await handle_order_trigger(order_id.strip().upper())
    return result
