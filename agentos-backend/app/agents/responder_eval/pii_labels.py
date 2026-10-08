"""Redaction placeholder hints for judge context (no raw PII)."""

from __future__ import annotations

import re
from typing import Literal

Speaker = Literal["customer", "agent", "unknown"]

HINT_CUSTOMER = "[customer]"
HINT_AGENT = "[agent]"
HINT_EMAIL = "[email]"
HINT_PHONE = "[phone]"
HINT_ADDRESS = "[address]"
HINT_ID = "[id]"

# JSON field → placeholder (structured PII outside chat turns).
PII_KEY_HINTS: dict[str, str] = {
    "email": HINT_EMAIL,
    "phone": HINT_PHONE,
    "contact_number": HINT_PHONE,
    "fe_phone_number": HINT_PHONE,
    "patient_name": HINT_CUSTOMER,
    "customer_name": HINT_CUSTOMER,
    "doctor_name": HINT_AGENT,
    "name": HINT_CUSTOMER,
    "user": HINT_CUSTOMER,
    "username": HINT_CUSTOMER,
    "street1": HINT_ADDRESS,
    "street2": HINT_ADDRESS,
    "street3": HINT_ADDRESS,
    "delivery_address": HINT_ADDRESS,
    "address": HINT_ADDRESS,
    "shipping_address": HINT_ADDRESS,
    "billing_address": HINT_ADDRESS,
    "landmark": HINT_ADDRESS,
    "pincode": HINT_ADDRESS,
    "tata_customer_hash": HINT_ID,
    "visitor_id": HINT_ID,
    "transaction_id": HINT_ID,
    "gateway_aggregator_txn_id": HINT_ID,
    "tcp_number": HINT_ID,
    # payment_transactions / payment_details (admin-service)
    "payment_id": HINT_ID,
    "txn_id": HINT_ID,
    "refund_id": HINT_ID,
    "unique_id": HINT_ID,
    "last_four_digits": HINT_ID,
    "instrument_text": HINT_ID,
    "payment_method": HINT_ID,
    "card_issuer": HINT_ID,
    "nb_bank_name": HINT_ID,
    "payment_instrument_summary": HINT_ID,
}


def speaker_from_role(role: str | None) -> Speaker:
    r = (role or "").strip().lower()
    if r in ("assistant", "bot", "agent"):
        return "agent"
    if r == "user":
        return "customer"
    return "unknown"


_AGENT_SELF_INTRO_RE = re.compile(r"\b(i am|i'm|my name is)\s+", re.I)


def person_hint_speaker_for_message(role: str | None, content: str = "") -> Speaker:
    """Who a redacted PERSON span refers to — not necessarily who sent the message."""
    r = (role or "").strip().lower()
    if r == "user":
        return "customer"
    if r in ("assistant", "bot", "agent"):
        if _AGENT_SELF_INTRO_RE.search(content or ""):
            return "agent"
        # Bot replies usually mention the customer or product facts, not the agent's name.
        return "customer"
    return "unknown"


def person_hint(speaker: Speaker) -> str:
    if speaker == "agent":
        return HINT_AGENT
    return HINT_CUSTOMER


def hint_for_pii_key(key: str | None) -> str:
    if not key:
        return HINT_ID
    return PII_KEY_HINTS.get(key.lower(), HINT_ID)
