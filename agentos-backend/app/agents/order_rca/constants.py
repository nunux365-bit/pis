"""Order RCA — status labels and progress steps."""

# All UI-facing absolute times are formatted in this zone.
ORDER_RCA_DISPLAY_TZ_NAME = "Asia/Kolkata"
ORDER_RCA_DISPLAY_TZ_LABEL = "IST"

# Delivery SLA breach_kind — canonical for UI, Perfect Order, and LLM wording.
DELIVERY_BREACH_PENDING_WITHIN = "pending_within_sla"
DELIVERY_BREACH_OPEN_PAST = "open_past_promise"
DELIVERY_BREACH_DELIVERED_LATE = "delivered_late"
DELIVERY_BREACH_DELIVERED_ON_TIME = "delivered_on_time"
DELIVERY_BREACH_UNKNOWN_ANCHOR = "unknown_anchor"

# Fields shown as-is or without a single instant — documented for ops / LLM context.
ORDER_RCA_AMBIGUOUS_TIME_FIELDS: tuple[str, ...] = (
    "eta.to_date / order.eta_to top-level label (date only, no time of day)",
    "order.eta.eta_to unix (IST wall clock encoded in unix; not shifted +5:30 on display)",
    "allocation.selected_vendors[].eta (relative strings, e.g. '3 days 9 hours')",
    "rapid / standard service window labels (schedule text, not order event time)",
    "order history comments (free text; only eta_communicated lines are customer IST)",
    "duration_min / late_minutes (elapsed minutes, not a clock time)",
)

# Groot timeline API uses DD-MM-YYYY HH:MM:SS as India wall clock (IST), not UTC.
GROOT_API_NAIVE_TZ_NAME = ORDER_RCA_DISPLAY_TZ_NAME

# Order-service status id → label (ops + LLM). Source: 1mg order status catalog.
STATUS_LABELS: dict[str, str] = {
    "10": "In Cart",
    "15": "Placed",
    "16": "Payment Not Completed",
    "17": "Payment Confirmation Awaited",
    "18": "Payment Failed",
    "19": "Order Reverted",
    "20": "Vendor Stock Allocation Failure",
    "21": "Vendor Stock Verified",
    "25": "To be delivered",
    "30": "Out for delivery",
    "35": "Re-Confirm",
    "40": "Delivered",
    "45": "Deliver Again",
    "50": "Returned",
    "52": "Waiting for Pharmacist Call",
    "55": "Prescription Order",
    "60": "Lost",
    "80": "With Dispatcher",
    "90": "Waiting For Vendor",
    "95": "Cancellation Initiated",
    "99": "Cancelled",
    "100": "Waiting For Rx",
    "105": "Waiting For Doctor Consultation",
    "106": "Rx Queued",
    "110": "Waiting For Digitization",
    "120": "Vendor Stock Allocation",
    "130": "Packaging",
    "131": "Stock Arrangement",
    "132": "Vendor Packaging",
    "133": "Modification From Packaging",
    "140": "Request for Return and Refund",
    "141": "Out for Return and Refund",
    "142": "Returned and Refunded",
    "150": "Waiting for IPD",
    "160": "S1 Accepted And Validation Failed",
    "170": "Waiting for Fraud Detection",
    "180": "Waiting for Online Payment",
    "190": "Waiting For Benefit Application",
}

# Statuses on parent PO through vendor stock allocation (borrowed for split-child ops trace).
PARENT_PRE_PACKAGING = frozenset({"15", "16", "110", "120", "20", "21"})

SPLIT_OPS_BOUNDARY_STATUS = "120"

# Return / refund workflow — no forward fulfilment SLA on these transitions.
RETURN_STATUS_IDS: frozenset[str] = frozenset({"50", "140", "141", "142"})
CANCEL_STATUS_IDS: frozenset[str] = frozenset({"95", "99"})
POST_DELIVERY_STATUS_IDS: frozenset[str] = frozenset({"45"}) | RETURN_STATUS_IDS | CANCEL_STATUS_IDS

# Transition labels for ops timeline (from_status_id, to_status_id) -> display name.
STATUS_TRANSITION_LABELS: dict[tuple[str, str], str] = {
    ("15", "16"): "Payment completed",
    ("16", "110"): "Rx validation",
    ("16", "100"): "Rx validation",
    ("100", "110"): "Digitization queue",
    ("110", "120"): "Allocation",
    ("120", "130"): "Packaging prep",
    ("130", "25"): "Packaging",
    ("25", "30"): "Dispatch",
    ("30", "40"): "Last mile",
    ("40", "45"): "Reschedule delivery",
    ("45", "25"): "Back to dispatch queue",
    ("45", "30"): "Reschedule — out for delivery",
    ("100", "140"): "Return requested",
    ("40", "140"): "Return requested",
    ("140", "141"): "Return pickup",
    ("141", "142"): "Refund completed",
    ("40", "50"): "Returned",
    ("40", "95"): "Cancellation started",
    ("95", "99"): "Cancelled",
}

# Ops trace: omit 120→130 (merged into Packaging 130→25 in the UI).
OPS_SKIP_TRANSITION_IDS: frozenset[tuple[str, str]] = frozenset({("120", "130")})

OPS_RETURN_NOTE = (
    "Order was delivered; return/refund milestones follow. "
    "Long gaps after Delivered (e.g. to Request for Return) are post-delivery workflow, "
    "not last-mile delivery SLA."
)

# Default SLA cutoffs in minutes (no DB / order_sla).
OPS_SLA_EARLY_MINUTES = 5
OPS_SLA_PACKAGING_ACTIVE_MINUTES = 20

OPS_EARLY_PHASE_LABELS: frozenset[str] = frozenset(
    {"Payment completed", "Rx validation", "Digitization queue", "Allocation"}
)

# Ranked physical-store panels (P1/P3/P4): nearest all-types, then extra warehouses.
TOP_PHYSICAL_STORES = 5
TOP_WAREHOUSE_STORES = 3

# Perfect Order scorecard — MRP increase vs cart (order-level, increases only).
PERFECT_ORDER_MRP_INCREASE_LIMIT = 150.0
# Pushback = child PO returns to allocation after post-packaging fulfilment (status 20 or 120).
PUSHBACK_ALLOCATION_STATUS_IDS: frozenset[str] = frozenset({"20", "120"})
# Fulfilment status ids at or after packaging (must appear before 20/120 counts as pushback).
POST_ALLOCATION_STATUS_IDS: frozenset[str] = frozenset(
    {"130", "131", "132", "133", "25", "30", "35", "40", "45"}
)
PERFECT_ORDER_PRICE_HINT = (
    "MRP increases vs cart (Σ line Δ×qty, increases only) must be ≤ ₹150"
)

# Tooltip / glossary for ops UI and LLM context.
DELIVED_STOCK_HINT = "Stock in the depletion pipeline (Delived), not on live shelf yet."

# Fulfilment segment labels (status history transitions — not pick/pack terminology).
SEGMENT_PACKAGING_TO_TBD = "Packaging → To be delivered"
SEGMENT_TBD_TO_OFD = "To be delivered → Out for delivery"
SEGMENT_OFD_TO_DELIVERED = "Out for delivery → Delivered"

# Human-readable rejection / table copy (not “capacity”).
SERVICE_UNAVAILABLE_SIGNAL = "No active delivery service windows"
# Kept for docs/LLM; UI uses SERVICE_UNAVAILABLE_SIGNAL only (no duplicate long line).
SERVICE_UNAVAILABLE_DETAIL = "No rapid or standard delivery slot for this pincode"

PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("collect", "Resolving order"),
    ("collect", "Loading order & shipment"),
    ("collect", "Loading allocation"),
    ("collect", "Loading operations trace"),
    ("build_facts", "Building facts"),
    ("synthesize", "Generating RCA"),
)

# Fixture cases: order_id → subdirectory under tests/fixtures/order_rca/cases/
FIXTURE_CASE_BY_ORDER_ID: dict[str, str] = {
    "PO11526254696421": "return_refund",
    "PO13426186269279": "split_mounjaro",
    "PO13026639774401": "split_mounjaro",
    "PO16326573780658": "rapid_delivered_late",
    "PO16326622291558": "service_changed_rapid",
    "PO16126626766912": "one_hour_groot_3p_wait",
    "PO15826548510041": "clickpost_sfx",
    "PO17026573780658": "inventory_mixed_skus",
}

# Order-service ``order_lines[].sku.priority`` — sampling / freebie lines excluded from RCA analysis.
SAMPLING_SKU_PRIORITY = 30

HISTORY_PAGE_SIZE = 50

# Redis run owner for unauthenticated internal diagnose/poll routes.
ORDER_RCA_INTERNAL_USER_ID = "__internal__"
