"""Regex PII patterns for responder eval (India-aware, preserves order IDs)."""

from __future__ import annotations

import re

from app.agents.responder_eval.pii_labels import (
    HINT_ADDRESS,
    HINT_EMAIL,
    HINT_PHONE,
    Speaker,
    person_hint,
)

# +91 / 91 / 0-prefix / plain 10-digit Indian mobile (6–9 start).
_IN_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+?91[\s-]?)?"
    r"0?"
    r"[6-9]\d{9}"
    r"(?!\d)",
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Operational IDs — preserved (not redacted).
_ORDER_ID_RE = re.compile(r"\bPO\d{10,}\b", re.I)

_ORDER_PLACEHOLDER_PREFIX = "__ORDER_ID_"

# "Hi Rahul" / "Hello Priya Sharma" — spaCy sm often misses short Indian names.
_GREETING_NAME_RE = re.compile(
    r"(?i)\b(hi|hello|dear|namaste)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)"
)
_SELF_INTRO_NAME_RE = re.compile(
    r"(?i)\b(i am|i'm|my name is)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)"
)


def _redact_greeting_names(text: str, speaker: Speaker) -> str:
    hint = person_hint(speaker)
    return _GREETING_NAME_RE.sub(rf"\1 {hint}", text)


def _redact_self_intro_names(text: str) -> str:
    hint = person_hint("agent")
    return _SELF_INTRO_NAME_RE.sub(rf"\1 {hint}", text)


def mask_order_ids(text: str) -> tuple[str, dict[str, str]]:
    """Replace PO… tokens with placeholders so Presidio cannot partially redact them."""
    placeholders: dict[str, str] = {}

    def _repl(match: re.Match[str]) -> str:
        key = f"{_ORDER_PLACEHOLDER_PREFIX}{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    return _ORDER_ID_RE.sub(_repl, text), placeholders


def unmask_order_ids(text: str, placeholders: dict[str, str]) -> str:
    out = text
    for key, value in placeholders.items():
        out = out.replace(key, value)
    return out


def apply_regex_redactions(text: str, *, speaker: Speaker = "customer") -> str:
    """Deterministic regex pass; safe without Presidio installed."""
    if not text:
        return text
    masked, placeholders = mask_order_ids(text)
    out = _EMAIL_RE.sub(HINT_EMAIL, masked)
    out = _IN_PHONE_RE.sub(HINT_PHONE, out)
    out = unmask_order_ids(out, placeholders)
    out = _redact_self_intro_names(out)
    return _redact_greeting_names(out, speaker)


def order_ids_in_text(text: str) -> list[str]:
    return _ORDER_ID_RE.findall(text or "")
