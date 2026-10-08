"""Post-redaction PII verification — hard gate when leaks remain."""

from __future__ import annotations

import re
from typing import Any

from app.agents.responder_eval.pii_labels import HINT_EMAIL, HINT_PHONE, PII_KEY_HINTS
from app.agents.responder_eval.pii_patterns import _EMAIL_RE, _IN_PHONE_RE

_PLACEHOLDER_RE = re.compile(r"^\[[\w\s]+\]$")
_KNOWN_HINTS = frozenset({HINT_EMAIL, HINT_PHONE, "[customer]", "[agent]", "[address]", "[id]"})


def _looks_like_raw_pii_value(value: str) -> bool:
    text = (value or "").strip()
    if not text or text in _KNOWN_HINTS:
        return False
    if _PLACEHOLDER_RE.match(text):
        return False
    if _EMAIL_RE.search(text):
        return True
    if _IN_PHONE_RE.search(text):
        return True
    return False


def _scan_strings(obj: Any, *, incidents: set[str]) -> None:
    if isinstance(obj, str):
        if _EMAIL_RE.search(obj):
            incidents.add("email")
        if _IN_PHONE_RE.search(obj):
            incidents.add("phone")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in PII_KEY_HINTS and isinstance(value, str) and _looks_like_raw_pii_value(value):
                incidents.add(f"field:{key_l}")
            _scan_strings(value, incidents=incidents)
        return
    if isinstance(obj, list):
        for item in obj:
            _scan_strings(item, incidents=incidents)


def verify_no_pii_leak(chat: dict[str, Any], artifact: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scan redacted chat + ground truth for residual PII patterns."""
    incidents: set[str] = set()
    _scan_strings(chat, incidents=incidents)
    if artifact:
        _scan_strings(artifact, incidents=incidents)
    types = sorted(incidents)
    return {"pii_incident": bool(types), "pii_incident_types": types}
