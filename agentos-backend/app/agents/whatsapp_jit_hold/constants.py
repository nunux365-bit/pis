"""Constants for WhatsApp JIT hold agent."""

from __future__ import annotations

import re
from typing import Any

# Packaging status (order-service).
PACKAGING_STATUS_ID = "130"

# Nexus Kafka current-event filter (ODIN packaging on-hold).
NEXUS_EVENT_TYPE_NON_ORDER = "NON_ORDER_STATUS_UPDATE"
NEXUS_EVENT_NAME_PACKAGING = "PACKAGING"
NEXUS_SUB_EVENT_ON_HOLD = "ON_HOLD"
NEXUS_ON_HOLD_DESCRIPTION = "order on hold at odin"

# 30/60 min rapid services treated as QC — excluded from eligibility.
QC_RAPID_SERVICE_IDS = frozenset({"thirty_minute_delivery", "one_hour_delivery"})

# FC / retail store kinds eligible for this flow.
# Match ONLY these exact values (case-insensitive) on:
#   - order.shipment_detail.vendor.tags.store_type
#   - explain_allocation selected_vendors[].vendor_type
# Do NOT treat tags.vendor_type (VMO / Non-VMO) or unknown types as warehouse.
ELIGIBLE_VENDOR_TYPES = frozenset({"WAREHOUSE", "RETAIL"})
# Secondary FC signal when store_type is absent. Word-boundary ``fulfil`` only;
# never marketplace / non-fulfilment / unfulfilled.
_FC_TYPE_DENY_MARKERS = ("marketplace", "non fulfil", "non fulfill", "unfulfil", "unfulfill")
_FC_TYPE_WAREHOUSE_RE = re.compile(r"\bfulfil", re.I)


def _fc_type_collapsed(raw: Any) -> str:
    return re.sub(r"[\s\-]+", " ", str(raw or "").strip().lower())


def fc_type_is_denied(raw: Any) -> bool:
    """True for marketplace / non-fulfilment labels (hard deny, no allocation fallthrough)."""
    collapsed = _fc_type_collapsed(raw)
    if not collapsed:
        return False
    return any(marker in collapsed for marker in _FC_TYPE_DENY_MARKERS)


def fc_type_means_warehouse(raw: Any) -> bool:
    """True only for FC-style ``fc_type`` (e.g. Fulfilment Center), not marketplace."""
    text = str(raw or "").strip()
    if not text:
        return False
    if fc_type_is_denied(text):
        return False
    return bool(_FC_TYPE_WAREHOUSE_RE.search(text))

# State machine button / action ids (match Meta template button payloads).
ACTION_OPTION_A = "option_a"
ACTION_OPTION_B = "option_b"
ACTION_CONFIRM = "confirm"
ACTION_BACK = "back"

# Inbound actions that advance or mutate the session state machine.
STATE_CHANGING_ACTIONS = frozenset(
    {ACTION_OPTION_A, ACTION_OPTION_B, ACTION_CONFIRM, ACTION_BACK}
)

# Session states.
STATE_INITIAL_SENT = "initial_sent"
STATE_OPTION_A_PREVIEW = "option_a_preview"
STATE_OPTION_B_PREVIEW = "option_b_preview"
STATE_OPTION_B_BACK_SENT = "option_b_back_sent"
STATE_DONE_SPLIT = "done_split"
STATE_DONE_HOLD = "done_hold"
