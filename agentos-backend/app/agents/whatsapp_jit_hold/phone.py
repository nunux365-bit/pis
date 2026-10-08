"""Phone normalization and test-mode session routing."""

from __future__ import annotations

import logging

from app.config.settings import settings

log = logging.getLogger(__name__)


def normalize_phone_digits(phone: str) -> str:
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def mask_phone(phone: str) -> str:
    """Log-safe phone: last 4 digits only."""
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


def session_phone(customer_phone: str) -> str:
    """
    Phone used for Redis sessions and all WhatsApp outbound/inbound routing.

    In test mode, **always** redirects to ``WHATSAPP_JIT_HOLD_TEST_PHONE`` so real
    customers never receive WhatsApp (manual trigger, webhook, fixture mode, etc.).
    """
    if settings.whatsapp_jit_hold_test_mode:
        redirect = (settings.whatsapp_jit_hold_test_phone or "").strip()
        if not redirect:
            raise RuntimeError(
                "WHATSAPP_JIT_HOLD_TEST_MODE requires WHATSAPP_JIT_HOLD_TEST_PHONE — "
                "refusing to use customer phone"
            )
        test_digits = normalize_phone_digits(redirect)
        cust_digits = normalize_phone_digits(customer_phone)
        if cust_digits and cust_digits != test_digits:
            log.debug(
                "jit_hold test_mode redirect customer_phone=%s -> test_phone=%s",
                mask_phone(cust_digits),
                mask_phone(test_digits),
            )
        return test_digits
    return normalize_phone_digits(customer_phone)
