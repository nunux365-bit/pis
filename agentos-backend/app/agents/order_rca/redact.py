"""PII redaction helpers (LLM + UI facts)."""

from __future__ import annotations

import re
from typing import Any

_PHONE_RE = re.compile(r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")


def redact_free_text(text: Any) -> str | None:
    """Strip phone/email; preserve operational cancel/retry commentary."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s = _PHONE_RE.sub("[phone redacted]", s)
    s = _EMAIL_RE.sub("[email redacted]", s)
    return s
