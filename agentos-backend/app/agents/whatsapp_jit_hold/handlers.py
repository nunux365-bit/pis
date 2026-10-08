"""Orchestration — Kafka trigger, Meta webhook replies, template sends."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.whatsapp_jit_hold import meta_client, order_tracker, session_store
from app.agents.whatsapp_jit_hold import order_client as jit_order_client
from app.agents.whatsapp_jit_hold.constants import (
    ACTION_BACK,
    ACTION_CONFIRM,
    ACTION_OPTION_A,
    ACTION_OPTION_B,
    STATE_CHANGING_ACTIONS,
    STATE_DONE_HOLD,
    STATE_DONE_SPLIT,
    STATE_INITIAL_SENT,
    STATE_OPTION_A_PREVIEW,
    STATE_OPTION_B_BACK_SENT,
    STATE_OPTION_B_PREVIEW,
)
from app.agents.whatsapp_jit_hold.models import STATUS_KEPT_ORIGINAL, STATUS_SPLIT_DONE
from app.agents.whatsapp_jit_hold.eligibility import (
    EligibilityResult,
    JitSkuLine,
    OrderNoLongerEligibleError,
    evaluate_eligibility,
    require_eligible,
    retain_skus_for_split,
)
from app.agents.whatsapp_jit_hold.order_client import (
    OrderNotFoundError,
    SplitInProgressError,
    SplitOutcomeUncertainError,
    customer_name,
    customer_phone,
    fetch_eligibility_bundle,
    format_held_orders_status_summary,
    new_child_ids_after_split,
    promised_eta_display,
    resolve_split_tracking_links,
    snapshot_child_ids_before_split,
    split_jit_order,
    utc_now_ts,
    wait_for_split_child_eta,
)
from app.agents.whatsapp_jit_hold.phone import mask_phone, normalize_phone_digits, session_phone
from app.agents.whatsapp_jit_hold.templates import format_sku_lines
from app.config.settings import settings

log = logging.getLogger(__name__)

_SESSION_SAVE_ATTEMPTS = 5


class PreviewStaleError(Exception):
    """CONFIRM snapshot no longer matches the preview the customer saw."""


async def handle_order_trigger(order_id: str, *, nexus_event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate order and send initial WhatsApp if eligible (Kafka or manual API)."""
    oid = order_id.strip().upper()
    if not settings.whatsapp_jit_hold_enabled:
        log.debug("jit_hold skip disabled order_id=%s", oid)
        return {"status": "skipped", "reason": "disabled"}

    if settings.whatsapp_jit_hold_use_fixtures and not settings.whatsapp_jit_hold_test_mode:
        log.error("jit_hold skip fixtures_require_test_mode order_id=%s", oid)
        return {"status": "skipped", "reason": "fixtures_require_test_mode"}
    if settings.whatsapp_jit_hold_split_stub and not settings.whatsapp_jit_hold_test_mode:
        log.error("jit_hold skip split_stub_requires_test_mode order_id=%s", oid)
        return {"status": "skipped", "reason": "split_stub_requires_test_mode"}

    if not await session_store.try_acquire_order_send(oid):
        log.debug("jit_hold skip already_sent order_id=%s", oid)
        return {"status": "skipped", "reason": "already_sent"}

    initial_delivered = False
    send_started = False
    try:
        bundle = await fetch_eligibility_bundle(oid, nexus_event=nexus_event)
        result = evaluate_eligibility(bundle)
        if not result.eligible:
            await _release_sent_retry(oid)
            log.debug(
                "jit_hold ineligible order_id=%s reason=%s held=%s ship=%s nexus=%s",
                oid,
                result.reason,
                len(result.held_skus),
                len(result.ship_skus),
                bool(nexus_event),
            )
            return {"status": "skipped", "reason": result.reason}

        order = bundle["order"]
        cust_phone = customer_phone(order)
        if not cust_phone or len(cust_phone) != 10:
            await _release_sent_retry(oid)
            log.debug("jit_hold ineligible order_id=%s reason=no_phone", oid)
            return {"status": "skipped", "reason": "no_phone"}

        sess_phone = session_phone(cust_phone)
        # Pin 7-day sent lock before Graph POST. If pin fails, do not send —
        # 900s pending would otherwise expire and a later ON_HOLD could duplicate.
        try:
            await session_store.confirm_order_sent(oid)
        except Exception:
            log.exception("jit_hold pin sent lock failed order_id=%s — not sending", oid)
            await _release_sent_retry(oid)
            raise
        send_started = True
        await _send_initial(bundle, result, cust_phone)
        initial_delivered = True
        await order_tracker.record_triggered(oid)
        try:
            await _confirm_sent_retry(oid)
        except Exception:
            log.exception(
                "jit_hold confirm_order_sent failed order_id=%s — 7-day lock may be missing; not releasing",
                oid,
            )
        # Persist the outbound/session phone (test handset in test_mode), never the
        # raw customer number — flipping TEST_MODE off mid-session must not redirect
        # follow-up templates to a real customer.
        try:
            await _save_session_retry(
                sess_phone,
                oid,
                _session_doc(oid, STATE_INITIAL_SENT, order, result.held_skus, result.ship_skus, sess_phone),
            )
        except Exception:
            log.exception(
                "jit_hold session save failed after send order_id=%s — lock kept; inbound may have no_session",
                oid,
            )
        log.info(
            "jit_hold sent initial order_id=%s phone=%s test_mode=%s held=%s ship=%s",
            oid,
            mask_phone(sess_phone),
            settings.whatsapp_jit_hold_test_mode,
            len(result.held_skus),
            len(result.ship_skus),
        )
        return {"status": "sent", "order_id": oid}
    except asyncio.CancelledError:
        # After pin (`send_started`), keep the lock — Meta may have accepted on cancel
        # (`treat_uncertain_as_sent` + `_confirm_sent_best_effort` in meta_client).
        if not send_started:
            await _release_sent_retry(oid)
        raise
    except OrderNotFoundError:
        if not initial_delivered:
            await _release_sent_retry(oid)
        log.debug("jit_hold skip order_not_found order_id=%s", oid)
        return {"status": "skipped", "reason": "order_not_found"}
    except Exception:
        if not initial_delivered:
            await _release_sent_retry(oid)
        else:
            log.exception(
                "jit_hold post-send failure order_id=%s — sent lock retained to prevent duplicate WhatsApp",
                oid,
            )
        raise


def order_status_result_should_dedupe(result: dict[str, Any]) -> bool:
    """Sync helper for benign skips other than ``already_sent`` (see async variant)."""
    if result.get("reason") == "disabled":
        return False
    if result.get("status") == "skipped" and result.get("reason") == "already_sent":
        return False
    return result.get("status") in {"sent", "skipped"}


async def order_status_event_should_dedupe(result: dict[str, Any], order_id: str) -> bool:
    """Kafka event dedupe — do not commit ``already_sent`` without delivery evidence."""
    if not order_status_result_should_dedupe(result):
        if result.get("status") == "skipped" and result.get("reason") == "already_sent":
            oid = order_id.strip().upper()
            lock = await session_store.get_order_send_lock_value(oid)
            if lock != "sent":
                return False
            return await session_store.has_active_session_for_order(oid)
        return False
    return True


async def handle_inbound(event: dict[str, Any]) -> dict[str, Any]:
    """Process parsed inbound user action (Meta webhook)."""
    if not settings.whatsapp_jit_hold_enabled:
        log.debug("jit_hold inbound skipped disabled")
        return {"status": "ignored", "reason": "disabled"}
    if settings.whatsapp_jit_hold_use_fixtures and not settings.whatsapp_jit_hold_test_mode:
        log.error("jit_hold inbound skipped fixtures_require_test_mode")
        return {"status": "ignored", "reason": "fixtures_require_test_mode"}
    if settings.whatsapp_jit_hold_split_stub and not settings.whatsapp_jit_hold_test_mode:
        log.error("jit_hold inbound skipped split_stub_requires_test_mode")
        return {"status": "ignored", "reason": "split_stub_requires_test_mode"}

    message_id = event.get("message_id") or ""
    if message_id and await session_store.is_webhook_processed(message_id):
        return {"status": "duplicate", "message_id": message_id}

    raw_phone = event.get("phone") or ""
    phone = session_phone(raw_phone) if raw_phone else ""
    action = event.get("action") or ""
    # Outbound templates set biz_opaque_callback_data=jit_hold:{parent_order_id}:…
    order_hint = event.get("order_id")

    session, oid = await session_store.resolve_session(phone, order_hint) if phone else (None, None)
    if not session or not oid:
        if order_hint:
            log.warning(
                "jit_hold session expired phone=%s order_hint=%s action=%s",
                mask_phone(phone),
                order_hint,
                action,
            )
            return {"status": "ignored", "reason": "session_expired", "order_id": order_hint}
        log.warning("jit_hold no session phone=%s order_hint=%s action=%s", mask_phone(phone), order_hint, action)
        return {"status": "no_session"}

    # State-changing actions must bind to the template's order (wamid / callback).
    # Phone-only fallback can CONFIRM a different live PO after an older session expired.
    if action in STATE_CHANGING_ACTIONS and not order_hint:
        log.warning(
            "jit_hold missing order hint phone=%s action=%s — ignoring to avoid wrong-order split",
            mask_phone(phone),
            action,
        )
        return {"status": "ignored", "reason": "missing_order_hint", "order_id": oid}

    async with session_store.session_lock(phone, oid) as acquired:
        if not acquired:
            log.debug("jit_hold session busy phone=%s order_id=%s action=%s", mask_phone(phone), oid, action)
            return {"status": "busy", "order_id": oid}

        # Re-load under lock in case another worker just finished.
        session = await session_store.get_session(phone, oid)
        if not session:
            return {"status": "no_session"}

        event_oid = str(event.get("order_id") or "").strip().upper()
        if event_oid and event_oid != oid.strip().upper():
            log.warning(
                "jit_hold callback order mismatch phone=%s session=%s event=%s action=%s",
                mask_phone(phone),
                oid,
                event_oid,
                action,
            )
            return {"status": "ignored", "reason": "order_mismatch", "order_id": oid}

        try:
            result = await _apply_inbound_action(
                session, phone, oid, action, message_id, context_id=str(event.get("context_id") or "")
            )
            return result
        except SplitInProgressError:
            log.debug("jit_hold split in progress order_id=%s phone=%s", oid, mask_phone(phone))
            return {"status": "busy", "order_id": oid}
        except OrderNoLongerEligibleError as exc:
            log.warning("jit_hold no longer eligible order_id=%s reason=%s", oid, exc.reason)
            return {"status": "ignored", "reason": exc.reason, "order_id": oid}
        except OrderNotFoundError:
            log.debug("jit_hold inbound order not found order_id=%s", oid)
            if message_id:
                await _mark_webhook_processed_retry(message_id)
            return {"status": "ignored", "reason": "order_not_found", "order_id": oid}
        except Exception:
            log.exception("jit_hold inbound handler failed order_id=%s phone=%s", oid, mask_phone(phone))
            raise


async def _apply_inbound_action(
    session: dict[str, Any],
    phone: str,
    oid: str,
    action: str,
    message_id: str,
    *,
    context_id: str = "",
) -> dict[str, Any]:
    state = session.get("state") or ""

    if state in {STATE_DONE_SPLIT, STATE_DONE_HOLD}:
        if message_id:
            await _mark_webhook_processed_retry(message_id)
        return {"status": "terminal", "state": state, "order_id": oid}

    if state in {STATE_INITIAL_SENT, STATE_OPTION_B_BACK_SENT}:
        if action == ACTION_OPTION_A:
            await _prepare_option_a_preview(session, phone)
            await _persist_session_then(
                phone,
                oid,
                session,
                STATE_OPTION_A_PREVIEW,
                lambda: _fire_option_a_preview(session, phone),
            )
            await _mark_webhook_processed_retry(message_id)
            return {"status": "ok", "order_id": oid, "state": session.get("state")}
        elif action == ACTION_OPTION_B:
            await _prepare_option_b_preview(session, phone)
            await _persist_session_then(
                phone,
                oid,
                session,
                STATE_OPTION_B_PREVIEW,
                lambda: _fire_option_b_preview(session, phone),
            )
            await _mark_webhook_processed_retry(message_id)
            return {"status": "ok", "order_id": oid, "state": session.get("state")}
        elif action == ACTION_CONFIRM and context_id:
            # Session save after a preview send may have failed; recover from the card.
            kind = await _card_kind_for_context(context_id)
            if kind == "option_a":
                return await _confirm_option_a_and_save(
                    session, phone, oid, message_id, context_id=context_id
                )
            elif kind == "option_b":
                await _persist_session_then(
                    phone,
                    oid,
                    session,
                    STATE_DONE_HOLD,
                    lambda: _send_option_b_done(session, phone),
                )
                await _mark_webhook_processed_retry(message_id)
                return {"status": "ok", "order_id": oid, "state": session.get("state")}
            else:
                return {"status": "ignored", "state": state, "action": action, "order_id": oid}
        else:
            return {"status": "ignored", "state": state, "action": action, "order_id": oid}

    elif state == STATE_OPTION_A_PREVIEW:
        if action == ACTION_CONFIRM:
            return await _confirm_option_a_and_save(
                session, phone, oid, message_id, context_id=context_id
            )
        elif action == ACTION_BACK:
            await _prepare_resend_initial(session, phone)
            await _persist_session_then(
                phone,
                oid,
                session,
                STATE_INITIAL_SENT,
                lambda: _fire_resend_initial(session, phone),
            )
            await _mark_webhook_processed_retry(message_id)
            return {"status": "ok", "order_id": oid, "state": session.get("state")}
        else:
            return {"status": "ignored", "state": state, "action": action, "order_id": oid}

    elif state == STATE_OPTION_B_PREVIEW:
        if action == ACTION_CONFIRM:
            if await _is_leftover_option_a_confirm(context_id):
                log.warning(
                    "jit_hold option A confirm ignored while session is option B order_id=%s",
                    oid,
                )
                await _prepare_option_b_preview(session, phone)
                await _persist_session_then(
                    phone,
                    oid,
                    session,
                    STATE_OPTION_B_PREVIEW,
                    lambda: _fire_option_b_preview(session, phone),
                )
                await _mark_webhook_processed_retry(message_id)
                return {"status": "ok", "order_id": oid, "state": session.get("state")}
            await _persist_session_then(
                phone,
                oid,
                session,
                STATE_DONE_HOLD,
                lambda: _send_option_b_done(session, phone),
            )
            await _mark_webhook_processed_retry(message_id)
            return {"status": "ok", "order_id": oid, "state": session.get("state")}
        elif action == ACTION_BACK:
            await _prepare_option_b_back(session, phone)
            await _persist_session_then(
                phone,
                oid,
                session,
                STATE_OPTION_B_BACK_SENT,
                lambda: _fire_option_b_back(session, phone),
            )
            await _mark_webhook_processed_retry(message_id)
            return {"status": "ok", "order_id": oid, "state": session.get("state")}
        else:
            return {"status": "ignored", "state": state, "action": action, "order_id": oid}

    else:
        return {"status": "unknown_state", "state": state, "order_id": oid}


async def _persist_session_then(
    phone: str,
    oid: str,
    session: dict[str, Any],
    new_state: str,
    send_fn: Callable[[], Awaitable[None]],
) -> None:
    """Save optimistic session state before Meta send; revert if send fails."""
    prior = session.get("state")
    session["state"] = new_state
    transient = {k: session.pop(k) for k in list(session) if str(k).startswith("_")}
    try:
        await _save_session_retry(phone, oid, session)
    except Exception:
        session["state"] = prior
        session.update(transient)
        raise
    session.update(transient)
    try:
        await send_fn()
    except Exception:
        session["state"] = prior
        session.update({k: session.pop(k) for k in list(session) if str(k).startswith("_")})
        try:
            await _save_session_retry(phone, oid, session)
        except Exception:
            log.exception("jit_hold revert session state failed order_id=%s", oid)
        raise


async def _confirm_option_a_and_save(
    session: dict[str, Any],
    phone: str,
    oid: str,
    message_id: str,
    *,
    context_id: str = "",
) -> dict[str, Any]:
    """Run split + option_a_done, then persist terminal/preview state before returning."""
    try:
        await _confirm_split(session, phone, context_id=context_id)
        session["state"] = STATE_DONE_SPLIT
    except PreviewStaleError:
        session["state"] = STATE_OPTION_A_PREVIEW
    await _save_session_retry(phone, oid, session)
    await _mark_webhook_processed_retry(message_id)
    if session.get("state") == STATE_DONE_SPLIT:
        log.info("jit_hold split completed order_id=%s phone=%s", oid, mask_phone(phone))
    return {"status": "ok", "order_id": oid, "state": session.get("state")}


async def _mark_webhook_processed_retry(message_id: str) -> None:
    if not message_id:
        return
    try:
        await session_store.mark_webhook_processed_retry(message_id, attempts=_SESSION_SAVE_ATTEMPTS)
    except Exception:
        log.exception("jit_hold mark_webhook_processed failed message_id=%s", message_id)


async def _save_session_retry(phone: str, oid: str, doc: dict[str, Any]) -> None:
    last: BaseException | None = None
    for attempt in range(_SESSION_SAVE_ATTEMPTS):
        try:
            await session_store.save_session(phone, oid, doc)
            return
        except Exception as exc:
            last = exc
            log.warning("jit_hold save_session attempt %s/%s failed order_id=%s", attempt + 1, _SESSION_SAVE_ATTEMPTS, oid)
            await asyncio.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


async def _confirm_sent_retry(oid: str) -> None:
    last: BaseException | None = None
    for attempt in range(_SESSION_SAVE_ATTEMPTS):
        try:
            await session_store.confirm_order_sent(oid)
            return
        except Exception as exc:
            last = exc
            log.warning(
                "jit_hold confirm_order_sent attempt %s/%s failed order_id=%s",
                attempt + 1,
                _SESSION_SAVE_ATTEMPTS,
                oid,
            )
            await asyncio.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


async def _release_sent_retry(oid: str) -> None:
    last: BaseException | None = None
    for attempt in range(_SESSION_SAVE_ATTEMPTS):
        try:
            await session_store.release_order_send(oid)
            return
        except Exception as exc:
            last = exc
            log.warning(
                "jit_hold release_order_send attempt %s/%s failed order_id=%s",
                attempt + 1,
                _SESSION_SAVE_ATTEMPTS,
                oid,
            )
            await asyncio.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


def _outbound_template_kind(callback_data: str) -> str:
    parts = str(callback_data or "").strip().split(":")
    if len(parts) >= 3 and parts[0].lower() == "jit_hold":
        return parts[-1].strip().lower()
    return ""


async def _card_kind_for_context(context_id: str) -> str:
    """Template kind for an outbound wamid (snapshot implies Option A preview)."""
    if not context_id:
        return ""
    if await session_store.snapshot_for_outbound_message(context_id):
        return "option_a"
    cb = await session_store.callback_data_for_outbound_message(context_id) or ""
    return _outbound_template_kind(cb)


async def _is_leftover_option_a_confirm(context_id: str) -> bool:
    """True when CONFIRM is from an Option A card (or untrusted) while session is Option B."""
    if not context_id:
        return False
    card_fp = await session_store.snapshot_for_outbound_message(context_id)
    if card_fp:
        return True
    cb = await session_store.callback_data_for_outbound_message(context_id) or ""
    kind = _outbound_template_kind(cb)
    if kind == "option_a":
        return True
    if kind == "option_b":
        return False
    return True


def _preview_fp_str(held: list[Any], ship: list[dict[str, Any]]) -> str:
    return repr((_held_fingerprint(held), _ship_fingerprint(ship)))


def _eta_for_template(bundle: dict[str, Any]) -> str:
    raw = str(promised_eta_display(bundle) or "").strip()
    if not raw or raw in {"—", "-", "–"}:
        return "as per your order confirmation"
    return raw


async def _updated_eta_after_split(
    oid: str,
    *,
    split_response: dict[str, Any] | None,
    split_after_ts: int | None,
    known_child_ids: frozenset[str] | None,
) -> str:
    """Ship-now ETA from the parent (available items stay at the same store)."""
    try:
        await wait_for_split_child_eta(
            oid,
            split_response=split_response,
            split_after_ts=split_after_ts,
            known_child_ids_before=known_child_ids,
        )
    except Exception:
        log.exception("jit_hold child wait failed order_id=%s", oid)
    try:
        return _eta_for_template(await fetch_eligibility_bundle(oid))
    except Exception:
        log.exception("jit_hold parent eta fallback failed order_id=%s", oid)
        return "as per your order confirmation"


_TRACKING_PENDING = "Tracking will be shared shortly"


def _ship_fingerprint(ship: list[dict[str, Any]]) -> tuple[tuple[str, int], ...]:
    rows: list[tuple[str, int]] = []
    for s in ship or []:
        sku = str(s.get("sku_id") or "")
        try:
            qty = int(s.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        rows.append((sku, qty))
    return tuple(sorted(rows))


def _held_fingerprint(held: list[Any]) -> tuple[tuple[str, int], ...]:
    rows: list[tuple[str, int]] = []
    for h in held or []:
        if isinstance(h, JitSkuLine):
            rows.append((h.sku_id, int(h.qty_stuck)))
            continue
        sku = str(h.get("sku_id") or "")
        try:
            qty = int(h.get("qty_stuck") or h.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        rows.append((sku, qty))
    return tuple(sorted(rows))


def _session_doc(
    order_id: str,
    state: str,
    order: dict[str, Any],
    held: list[JitSkuLine],
    ship: list[dict[str, Any]],
    cust_phone: str | None = None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "state": state,
        "customer_name": customer_name(order),
        "customer_phone": normalize_phone_digits(cust_phone or customer_phone(order)),
        "held_skus": [
            {
                "sku_id": h.sku_id,
                "name": h.name,
                "qty_ordered": h.qty_ordered,
                "qty_stuck": h.qty_stuck,
            }
            for h in held
        ],
        "ship_skus": ship,
    }


async def _send_initial(bundle: dict[str, Any], result: EligibilityResult, cust_phone: str) -> None:
    order = bundle["order"]
    oid = str(bundle.get("order_id") or "").strip().upper()
    held_lines = [{"name": h.name, "qty": h.qty_stuck} for h in result.held_skus]
    ctx = {
        "order_id": oid,
        "customer_name": customer_name(order),
        "held_items": format_sku_lines(held_lines, held=True),
    }
    await meta_client.send_template(
        cust_phone,
        "initial",
        ctx,
        callback_data=f"jit_hold:{oid}:initial",
        treat_uncertain_as_sent=True,
    )


async def _prepare_option_a_preview(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    bundle = await fetch_eligibility_bundle(oid)
    result = require_eligible(bundle)
    session["ship_skus"] = result.ship_skus
    session["held_skus"] = [
        {
            "sku_id": h.sku_id,
            "name": h.name,
            "qty_ordered": h.qty_ordered,
            "qty_stuck": h.qty_stuck,
        }
        for h in result.held_skus
    ]
    held = _held_lines_for_template(session.get("held_skus") or [])
    ship = session.get("ship_skus") or []
    session["preview_fp"] = _preview_fp_str(session.get("held_skus") or [], ship)
    session["_option_a_ctx"] = {
        "order_id": oid,
        "ship_items": format_sku_lines(ship, held=False),
        "held_items": format_sku_lines(held, held=True),
        "preview_fp": session["preview_fp"],
    }


async def _fire_option_a_preview(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    ctx = session.pop("_option_a_ctx", None)
    if not ctx:
        await _prepare_option_a_preview(session, phone)
        ctx = session.pop("_option_a_ctx", {})
    cust = session.get("customer_phone") or phone
    await meta_client.send_template(cust, "option_a", ctx, callback_data=f"jit_hold:{oid}:option_a")


async def _send_option_a_preview(session: dict[str, Any], phone: str) -> None:
    await _prepare_option_a_preview(session, phone)
    await _fire_option_a_preview(session, phone)


async def _prepare_option_b_preview(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    bundle = await fetch_eligibility_bundle(oid)
    require_eligible(bundle)
    session["_option_b_ctx"] = {
        "order_id": oid,
        "revised_eta": _eta_for_template(bundle),
    }


async def _fire_option_b_preview(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    ctx = session.pop("_option_b_ctx", None)
    if not ctx:
        await _prepare_option_b_preview(session, phone)
        ctx = session.pop("_option_b_ctx", {})
    cust = session.get("customer_phone") or phone
    await meta_client.send_template(cust, "option_b", ctx, callback_data=f"jit_hold:{oid}:option_b")


async def _send_option_b_preview(session: dict[str, Any], phone: str) -> None:
    await _prepare_option_b_preview(session, phone)
    await _fire_option_b_preview(session, phone)


async def _prepare_option_b_back(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    bundle = await fetch_eligibility_bundle(oid)
    require_eligible(bundle)
    session["_option_b_back_ctx"] = {
        "order_id": oid,
        "revised_eta": _eta_for_template(bundle),
    }


async def _fire_option_b_back(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    ctx = session.pop("_option_b_back_ctx", None)
    if not ctx:
        await _prepare_option_b_back(session, phone)
        ctx = session.pop("_option_b_back_ctx", {})
    cust = session.get("customer_phone") or phone
    await meta_client.send_template(cust, "option_b_back", ctx, callback_data=f"jit_hold:{oid}:option_b_back")


async def _send_option_b_back(session: dict[str, Any], phone: str) -> None:
    await _prepare_option_b_back(session, phone)
    await _fire_option_b_back(session, phone)


async def _prepare_resend_initial(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    bundle = await fetch_eligibility_bundle(oid)
    result = require_eligible(bundle)
    session["held_skus"] = [
        {
            "sku_id": h.sku_id,
            "name": h.name,
            "qty_ordered": h.qty_ordered,
            "qty_stuck": h.qty_stuck,
        }
        for h in result.held_skus
    ]
    session["ship_skus"] = result.ship_skus
    held = _held_lines_for_template(session.get("held_skus") or [])
    session["_initial_resend_ctx"] = {
        "order_id": oid,
        "customer_name": session.get("customer_name") or customer_name(bundle["order"]),
        "held_items": format_sku_lines(held, held=True),
    }


async def _fire_resend_initial(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    ctx = session.pop("_initial_resend_ctx", None)
    if not ctx:
        await _prepare_resend_initial(session, phone)
        ctx = session.pop("_initial_resend_ctx", {})
    cust = session.get("customer_phone") or phone
    await meta_client.send_template(cust, "initial", ctx, callback_data=f"jit_hold:{oid}:initial_retry")


async def _resend_initial(session: dict[str, Any], phone: str) -> None:
    await _prepare_resend_initial(session, phone)
    await _fire_resend_initial(session, phone)


async def _confirm_split(session: dict[str, Any], phone: str, *, context_id: str = "") -> None:
    oid = session["order_id"]
    cust = session.get("customer_phone") or phone
    split_response: dict[str, Any] | None = None
    split_after_ts, known_child_ids = await session_store.get_split_context(oid)

    if context_id:
        card_fp = await session_store.snapshot_for_outbound_message(context_id)
        sess_fp = str(session.get("preview_fp") or "") or _preview_fp_str(
            session.get("held_skus") or [], session.get("ship_skus") or []
        )
        if not card_fp or card_fp != sess_fp:
            log.warning(
                "jit_hold option A card fingerprint missing or stale order_id=%s has_card_fp=%s",
                oid,
                bool(card_fp),
            )
            await _send_option_a_preview(session, phone)
            raise PreviewStaleError(oid)

    async def _new_children(known: frozenset[str]) -> frozenset[str] | None:
        try:
            return await new_child_ids_after_split(
                oid,
                known_child_ids=known,
                split_after_ts=split_after_ts,
            )
        except Exception:
            log.exception("jit_hold child snapshot unavailable order_id=%s", oid)
            return None

    async def _wait_for_new_children(known: frozenset[str]) -> frozenset[str] | None:
        stub = settings.whatsapp_jit_hold_split_stub or settings.whatsapp_jit_hold_use_fixtures
        started = time.monotonic()
        last: frozenset[str] | None = frozenset()
        while True:
            last = await _new_children(known)
            if last:
                return last
            if stub or (time.monotonic() - started) >= jit_order_client._ETA_WAIT_TIMEOUT_SEC:
                return last
            await asyncio.sleep(jit_order_client._ETA_WAIT_INTERVAL_SEC)

    if await session_store.is_split_completed(oid):
        pass
    elif await session_store.is_split_blocked(oid):
        if known_child_ids is None:
            raise SplitInProgressError(f"split retry blocked for {oid}")
        fresh = await _new_children(known_child_ids)
        if fresh:
            await session_store.confirm_split_completed(oid)
        else:
            raise SplitInProgressError(f"split retry blocked for {oid}")
    else:
        bundle = await fetch_eligibility_bundle(oid)
        result = require_eligible(bundle)
        if _held_fingerprint(result.held_skus) != _held_fingerprint(session.get("held_skus") or []) or _ship_fingerprint(
            result.ship_skus
        ) != _ship_fingerprint(session.get("ship_skus") or []):
            session["ship_skus"] = result.ship_skus
            session["held_skus"] = [
                {
                    "sku_id": h.sku_id,
                    "name": h.name,
                    "qty_ordered": h.qty_ordered,
                    "qty_stuck": h.qty_stuck,
                }
                for h in result.held_skus
            ]
            log.warning("jit_hold confirm snapshot stale order_id=%s — resending option A preview", oid)
            await _send_option_a_preview(session, phone)
            raise PreviewStaleError(oid)

        ship_skus = result.ship_skus
        session["ship_skus"] = ship_skus

        if known_child_ids is not None:
            fresh = await _new_children(known_child_ids)
            if fresh is None:
                raise SplitInProgressError(f"split child snapshot unavailable for {oid}")
            if fresh:
                # Empty id snapshot is safe when split_after_ts scopes children by created time.
                if not known_child_ids and split_after_ts is None:
                    raise SplitInProgressError(f"split in progress for {oid}")
                await session_store.confirm_split_completed(oid)
            else:
                # Snapshot exists but no new child yet. Do not 7-day-block:
                # crash-before-POST never called split; a later tap can still
                # wait for children. Never POST again (anti-double-split).
                raise SplitInProgressError(f"split in progress for {oid}")
        else:
            try:
                known_child_ids = await snapshot_child_ids_before_split(oid)
            except Exception:
                log.exception("jit_hold pre-split snapshot failed order_id=%s — not POSTing", oid)
                raise SplitInProgressError(f"split child snapshot unavailable for {oid}")
            split_after_ts = utc_now_ts()
            if not await session_store.try_acquire_split(oid):
                raise SplitInProgressError(f"split in progress for {oid}")
            try:
                await session_store.save_split_context(
                    oid,
                    split_after_ts=split_after_ts,
                    known_child_ids=known_child_ids,
                )
            except Exception:
                await session_store.release_split(oid)
                raise
            split_applied = False
            try:
                retain_skus = retain_skus_for_split(result)
                split_response = await split_jit_order(oid, retain_skus)
                split_applied = True
                await session_store.confirm_split_completed(oid)
            except SplitOutcomeUncertainError:
                log.exception(
                    "jit_hold split outcome uncertain order_id=%s — not retrying split blindly",
                    oid,
                )
                fresh = await _wait_for_new_children(known_child_ids)
                if fresh:
                    await session_store.confirm_split_completed(oid)
                elif fresh is None:
                    raise SplitInProgressError(f"split outcome uncertain for {oid}")
                else:
                    await session_store.block_split_retry(oid)
                    raise SplitInProgressError(f"split outcome uncertain for {oid}")
            except Exception:
                if split_applied:
                    log.exception(
                        "jit_hold split mark-done failed order_id=%s — not releasing lock (API already succeeded)",
                        oid,
                    )
                    try:
                        await session_store.confirm_split_completed(oid)
                    except Exception:
                        log.exception("jit_hold split mark-done retry failed order_id=%s", oid)
                        await session_store.block_split_retry(oid)
                    raise SplitInProgressError(f"split completed but lock persist failed for {oid}")
                if not await session_store.is_split_completed(oid):
                    await session_store.release_split(oid)
                    try:
                        await session_store.clear_split_context(oid)
                    except Exception:
                        log.exception("jit_hold clear_split_context failed order_id=%s", oid)
                raise

    updated_eta = await _updated_eta_after_split(
        oid,
        split_response=split_response,
        split_after_ts=split_after_ts,
        known_child_ids=known_child_ids,
    )
    ship_track, held_track, held_orders = await resolve_split_tracking_links(
        oid,
        split_response=split_response,
        split_after_ts=split_after_ts,
        known_child_ids_before=known_child_ids,
    )
    ctx = {
        "order_id": oid,
        "updated_eta": updated_eta,
        "ship_now_tracking_link": ship_track or _TRACKING_PENDING,
        "held_orders_status": format_held_orders_status_summary(held_orders),
        "held_order_tracking_link": held_track or _TRACKING_PENDING,
    }
    await meta_client.send_template(cust, "option_a_done", ctx, callback_data=f"jit_hold:{oid}:done_split")
    await order_tracker.record_terminal(oid, STATUS_SPLIT_DONE)


async def _send_option_b_done(session: dict[str, Any], phone: str) -> None:
    oid = session["order_id"]
    bundle = await fetch_eligibility_bundle(oid)
    require_eligible(bundle)
    cust = session.get("customer_phone") or phone
    await meta_client.send_template(
        cust,
        "option_b_done",
        {"order_id": oid},
        callback_data=f"jit_hold:{oid}:done_hold",
    )
    await order_tracker.record_terminal(oid, STATUS_KEPT_ORIGINAL)
    log.info("jit_hold hold confirmed order_id=%s phone=%s", oid, mask_phone(phone))


def _held_lines_for_template(held: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": h.get("name"),
            "qty": h.get("qty_stuck") or h.get("qty") or h.get("qty_ordered"),
        }
        for h in held
    ]
