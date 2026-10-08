"""Normalize free-form judge policy rules into closed dashboard buckets."""

from __future__ import annotations

import re

# Closed set used in TEXT[] as ``policy:<bucket>``.
POLICY_BUCKETS = frozenset(
    {
        "order_selection",
        "explain_absence",
        "unsupported_status_or_eta",
        "unsupported_refund",
        "unsupported_delivery",
        "unsupported_cancellation",
        "tone",
        "other",
    }
)

_EXACT = {
    "order_selection_before_action": "order_selection",
    "explain_absence_dont_repeat": "explain_absence",
}

_SPACE_RE = re.compile(r"[\s/\-]+")


def normalize_policy_rule(rule: str) -> str:
    """Map a free-form policy rule string to a closed bucket id."""
    raw = str(rule or "").strip()
    if not raw:
        return "other"
    key = _SPACE_RE.sub("_", raw.lower())
    key = key.replace("__", "_").strip("_")
    if key in _EXACT:
        return _EXACT[key]
    if key in POLICY_BUCKETS:
        return key

    if "tone" in key or "professional" in key or "unprofessional" in key:
        return "tone"
    if "cancel" in key and "refund" not in key:
        return "unsupported_cancellation"
    if "refund" in key:
        return "unsupported_refund"
    if "cancel" in key:
        return "unsupported_cancellation"
    if "deliver" in key or "shipment" in key or "shipped" in key or "dispatch" in key:
        return "unsupported_delivery"
    if (
        "status" in key
        or "timing" in key
        or "eta" in key
        or "promised" in key
        or "chronolog" in key
    ):
        return "unsupported_status_or_eta"
    return "other"
