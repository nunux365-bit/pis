"""Shared OData V2 helpers for SAP procurement PR/PO payloads and reads."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

# SAP OData ``/Date(0)/`` → UI ``1970-01-01``; posting that back is rejected ("Value 0 is not a valid date").
SAP_EPOCH_FORM_DATE = "1970-01-01"


def odata_norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def storage_location_for_sap(raw: str, *, plant: str = "") -> str:
    """
    SAP expects storage location code only (e.g. ``3021``).

    UI / ``pr_po_reference_values`` use composite ``plant|sloc`` (e.g. ``H001|3021``).
    """
    v = odata_norm(raw)
    if not v or "|" not in v:
        return v
    _plant_part, sloc_part = v.split("|", 1)
    return sloc_part.strip()


def odata_base_root(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def odata_entity_key(value: Any) -> str:
    return odata_norm(value).replace("'", "''")


def odata_deferred_uri(node: Any) -> str | None:
    """Return the ``__deferred.uri`` target when *node* is a deferred navigation property."""
    if not isinstance(node, dict):
        return None
    defr = node.get("__deferred")
    if not isinstance(defr, dict):
        return None
    uri = odata_norm(defr.get("uri"))
    return uri or None


def odata_results_list(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    if isinstance(node, dict):
        inner = node.get("results")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if odata_deferred_uri(node):
            return []
    return []


def odata_entity_properties(entry: dict[str, Any]) -> dict[str, Any]:
    content = entry.get("content")
    if isinstance(content, dict):
        props = content.get("properties")
        if isinstance(props, dict):
            return props
    props = entry.get("properties")
    if isinstance(props, dict):
        return props
    return entry


def odata_text(val: Any) -> str:
    if isinstance(val, dict):
        for key in ("__text", "#text", "value"):
            if key in val and val[key] is not None:
                return odata_norm(val[key])
        return ""
    return odata_norm(val)


def is_sap_deleted_flag(raw: str) -> bool:
    u = odata_norm(raw).upper()
    return u in ("X", "TRUE", "1", "YES")


def form_delivery_date_is_valid(raw: str) -> bool:
    """False for empty, SAP epoch placeholder, or all-zero legacy dates."""
    s = odata_norm(raw)
    if not s or s == SAP_EPOCH_FORM_DATE or s == "0000-00-00":
        return False
    if len(s) >= 8 and s.replace("-", "").isdigit() and s.replace("-", "") in ("0", "00000000"):
        return False
    return True


def default_procurement_delivery_date(*, days_ahead: int = 30) -> str:
    """Future calendar date for PO schedule lines when SAP/PR left delivery unset."""
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def sanitize_form_delivery_date(raw: str, *, fallback: str = "") -> str:
    """Normalize UI delivery; drop SAP epoch placeholders."""
    s = odata_norm(raw)
    if re.search(r"/Date\(", s):
        s = odata_date_to_form(s)
    if form_delivery_date_is_valid(s):
        return s[:10] if len(s) >= 10 and s[4] == "-" else s
    fb = odata_norm(fallback)
    if re.search(r"/Date\(", fb):
        fb = odata_date_to_form(fb)
    return fb if form_delivery_date_is_valid(fb) else ""


def odata_date_to_form(raw: Any) -> str:
    """Convert OData ``/Date(ms)/`` or ISO date to ``YYYY-MM-DD`` for UI forms."""
    s = odata_text(raw) if not isinstance(raw, str) else odata_norm(raw)
    if not s:
        return ""
    m = re.search(r"/Date\((-?\d+)\)/", s)
    if m:
        try:
            ms = int(m.group(1))
            if ms <= 0:
                return ""
            dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) >= 8 and s.isdigit():
        try:
            dt = datetime.strptime(s[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""
