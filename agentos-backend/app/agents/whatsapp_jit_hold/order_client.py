"""Order API client — reuses Order RCA service URLs and headers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.agents.order_rca import rules, sources
from app.agents.order_rca import time_utils as tu
from app.agents.order_rca.rules import build_shipping_summary
from app.agents.whatsapp_jit_hold.constants import fc_type_means_warehouse
from app.config.settings import settings
from app.infra.httpx_clients import get_internal_http_client

log = logging.getLogger(__name__)

_TRACK_URL_BASE = "https://www.1mg.com/track"
_PO_ID_RE = re.compile(r"^PO\d+$", re.I)
_STUB_SPLIT_CHILD_MAP = {
    "PO13326295017145": "PO13326295207344",
    "PO13326295207344": "PO13326295207345",
}
_SEARCH_RETRY_DELAY_SEC = 1.5
_SPLIT_CREATED_BUFFER_SEC = 120
_EPOCH_MS_THRESHOLD = 10**12
_IST = ZoneInfo("Asia/Kolkata")
# Child ORDER_ETA is written asynchronously after allocation. Cap wait so the
# done WhatsApp still sends if ETA never appears.
_ETA_WAIT_TIMEOUT_SEC = 90.0
_ETA_WAIT_INTERVAL_SEC = 2.0


def utc_now_ts() -> int:
    """UTC epoch seconds for post-split ``created`` comparisons."""
    return int(datetime.now(UTC).timestamp())


def normalize_order_created_ts(raw: Any) -> int | None:
    """
    Normalize order-search ``created`` to UTC epoch seconds.

    Handles unix seconds, unix milliseconds, and ISO strings (naive → IST).
    """
    if raw is None:
        return None

    num = tu.parse_num(raw)
    if num is not None and num > 0:
        if num >= _EPOCH_MS_THRESHOLD:
            num /= 1000.0
        return int(num)

    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    return int(dt.astimezone(UTC).timestamp())


class SplitOrderError(RuntimeError):
    """Raised when JIT split API returns a non-success response."""


class SplitInProgressError(RuntimeError):
    """Raised when another worker holds the split lock (retry later)."""


class SplitOutcomeUncertainError(RuntimeError):
    """Split POST timed out or dropped — server may already have applied it."""


class OrderNotFoundError(RuntimeError):
    """GET /orders/{id} returned 404 — Kafka should skip and commit."""

    def __init__(self, order_id: str):
        self.order_id = order_id
        super().__init__(f"order not found {order_id}")


def _reraise_order_http(exc: BaseException, order_id: str) -> None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code == 404:
        raise OrderNotFoundError(order_id) from exc
    raise exc


def split_response_ok(resp: dict[str, Any]) -> bool:
    if not isinstance(resp, dict):
        return False
    # Explicit failure wins even if status_code is 200.
    if resp.get("is_success") is False:
        return False
    if resp.get("is_success") is True:
        return True
    try:
        return int(resp.get("status_code") or 0) == 200
    except (TypeError, ValueError):
        return False


async def _load_jit_hold_fixture_bundle(order_id: str) -> dict[str, Any]:
    """Load order RCA JSON fixtures + JIT-hold overlay (no live order APIs)."""
    from app.agents.whatsapp_jit_hold.fixtures import apply_fixture_overlay

    bundle = await sources._fetch_fixtures(order_id.strip().upper())
    return apply_fixture_overlay(bundle)


def _blank(val: Any) -> bool:
    return val is None or (isinstance(val, str) and not val.strip())


def _merge_nexus_into_order(order: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """Fill phone / vendor / rapid gaps from the Kafka order_details blob."""
    details = envelope.get("order_details")
    if not isinstance(details, dict):
        return order

    user = details.get("user_details") if isinstance(details.get("user_details"), dict) else {}
    phone = user.get("contact_number")
    if phone:
        dest_user = order.get("user") if isinstance(order.get("user"), dict) else {}
        if _blank(dest_user.get("number")):
            dest_user["number"] = phone
        if _blank(dest_user.get("display_number")):
            dest_user["display_number"] = phone
        order["user"] = dest_user
        if _blank(order.get("contact_number")):
            order["contact_number"] = phone

    vendor = details.get("vendor_details") if isinstance(details.get("vendor_details"), dict) else {}
    if vendor:
        # Canonical FC/retail signal is tags.store_type ∈ {WAREHOUSE, RETAIL}.
        # tags.vendor_type is VMO/Non-VMO — never promote it to store_type.
        eligible = frozenset({"WAREHOUSE", "RETAIL"})
        tags = vendor.get("tags") if isinstance(vendor.get("tags"), dict) else {}
        ship = order.get("shipment_detail") if isinstance(order.get("shipment_detail"), dict) else {}
        dest_vendor = ship.get("vendor") if isinstance(ship.get("vendor"), dict) else {}
        dest_tags = dest_vendor.get("tags") if isinstance(dest_vendor.get("tags"), dict) else {}

        raw_store = str(tags.get("store_type") or "").upper().strip()
        if raw_store in eligible:
            dest_tags.setdefault("store_type", raw_store)
            dest_vendor.setdefault("vendor_type", raw_store)
        elif raw_store:
            # Explicit non-eligible (e.g. MARKETPLACE) — preserve for hard deny;
            # never invent WAREHOUSE from fc_type when store_type is present.
            dest_tags.setdefault("store_type", raw_store)
        else:
            # tags.store_type absent: FC label may imply WAREHOUSE.
            # vendor_details.vendor_type is not store kind — never mint store_type from it.
            if fc_type_means_warehouse(tags.get("fc_type")):
                dest_tags.setdefault("store_type", "WAREHOUSE")
                dest_vendor.setdefault("vendor_type", "WAREHOUSE")
        if tags.get("fc_type") and not dest_tags.get("fc_type"):
            dest_tags["fc_type"] = tags.get("fc_type")
        raw_vtag = tags.get("vendor_type")
        if raw_vtag and str(raw_vtag).upper().strip() not in eligible:
            dest_tags.setdefault("vendor_type", raw_vtag)

        dest_vendor.setdefault("id", vendor.get("id"))
        dest_vendor["tags"] = dest_tags
        ship["vendor"] = dest_vendor
        order["shipment_detail"] = ship

    rapid = details.get("rapid_details") if isinstance(details.get("rapid_details"), dict) else {}
    if rapid.get("rapid_eligibility_info") and not order.get("rapid_eligibility_info"):
        order["rapid_eligibility_info"] = rapid["rapid_eligibility_info"]

    return order


async def fetch_eligibility_bundle(
    order_id: str,
    *,
    nexus_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Slim order payload for JIT eligibility, ETA, and conversation steps.

    Kafka ODIN on-hold path: GET ``/orders/{id}`` only (on-hold + vendor hints
    come from the Nexus payload). Conversation re-checks fetch allocation,
    history, and analytics (hold state comes from GET order only).
    """
    oid = order_id.strip().upper()
    if settings.whatsapp_jit_hold_use_fixtures:
        bundle = await _load_jit_hold_fixture_bundle(oid)
        if nexus_event:
            bundle["nexus_event"] = nexus_event
            order = bundle.get("order")
            if isinstance(order, dict):
                bundle["order"] = _merge_nexus_into_order(order, nexus_event)
        return bundle

    if nexus_event is not None:
        order = await fetch_order_details(oid)
        order = _merge_nexus_into_order(order, nexus_event)
        return {
            "order_id": oid,
            "order": order,
            "allocation": {"data": {}},
            "nexus_event": nexus_event,
            "collect_mode": "jit_hold_kafka",
        }

    base_o = settings.order_rca_order_service_base_url.rstrip("/")
    base_s = settings.order_rca_sla_service_base_url.rstrip("/")
    if not base_o or not base_s:
        raise RuntimeError("order_rca_order_service_base_url and order_rca_sla_service_base_url are required")

    client = get_internal_http_client()
    try:
        order = await sources._get_json(
            client,
            f"{base_o}/__onemg-internal__/orders/{oid}",
            headers=sources._order_headers(),
        )
    except httpx.HTTPStatusError as exc:
        _reraise_order_http(exc, oid)
    alloc = await sources._post_explain_allocation(
        client,
        f"{base_s}/v1/analytics/{oid}/explain_allocation",
        headers={
            "Content-Type": "application/json",
            "Authorization": settings.order_rca_sla_auth_token,
        },
        json={},
    )
    history = await sources._fetch_order_history(client, base_o, oid)
    analytics = await sources._fetch_order_analytics_safe(client, base_o, oid)

    return {
        "order_id": oid,
        "order": order,
        "allocation": alloc,
        "history": history,
        "analytics": analytics,
        "collect_mode": "jit_hold_eligibility",
    }


async def fetch_order_details(order_id: str) -> dict[str, Any]:
    """Single order record — used when search rows lack tracking or status fields."""
    oid = order_id.strip().upper()
    if settings.whatsapp_jit_hold_use_fixtures:
        bundle = await fetch_eligibility_bundle(oid)
        order = bundle.get("order")
        return order if isinstance(order, dict) else {}

    base_o = settings.order_rca_order_service_base_url.rstrip("/")
    if not base_o:
        raise RuntimeError("order_rca_order_service_base_url is required")

    client = get_internal_http_client()
    try:
        order = await sources._get_json(
            client,
            f"{base_o}/__onemg-internal__/orders/{oid}",
            headers=sources._order_headers(),
        )
    except httpx.HTTPStatusError as exc:
        _reraise_order_http(exc, oid)
    return order if isinstance(order, dict) else {}


async def split_jit_order(order_id: str, retain_skus: list[dict[str, Any]]) -> dict[str, Any]:
    """
    POST JIT split on the parent PO (conversation always starts on parent).

    ``skus_to_be_retained`` stay on the parent in Packaging (available / ship-now
    qty so the current store can pack them). Unfulfilled JIT qty is pushed to a
    new child at vendor-stock-allocation.

    Live split returns only a success string — child PO id is not in the
    response; discover it via family search (``parent_id``).
    """
    retain_qty: dict[str, int] = {}
    for s in retain_skus:
        sku = str(s["sku_id"])
        retain_qty[sku] = retain_qty.get(sku, 0) + int(s.get("qty") or s.get("qty_stuck") or 0)
    retain = [{"sku_id": sku, "quantity": qty} for sku, qty in retain_qty.items() if qty > 0]
    oid = order_id.strip().upper()
    if not retain:
        raise SplitOrderError(f"no JIT SKUs to retain for split on {oid}")

    if settings.whatsapp_jit_hold_split_stub or settings.whatsapp_jit_hold_use_fixtures:
        held_split_oid = _stub_held_split_order_id(oid)
        log.debug(
            "split stub parent_order_id=%s retain=%s held_split_order_id=%s",
            oid,
            retain,
            held_split_oid,
        )
        return {
            "data": {"child_order_id": held_split_oid, "message": "Operation successfully performed"},
            "status_code": 200,
            "is_success": True,
            "error": {},
            "meta": None,
        }

    username = (settings.whatsapp_jit_hold_split_username or "").strip()
    if not username:
        raise RuntimeError("WHATSAPP_JIT_HOLD_SPLIT_USERNAME is required when split stub is disabled")

    base = settings.order_rca_order_service_base_url.rstrip("/")
    url = f"{base}/__onemg-internal__/order/{oid}/pos/jit/split"
    body = {
        "skus_to_be_retained": retain,
        "username": username,
    }
    # One-shot POST — order-RCA retries would double-split if the first attempt
    # already succeeded and the client only saw a timeout.
    client = get_internal_http_client()
    try:
        resp = await client.post(url, headers=sources._order_headers(), json=body)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise SplitOutcomeUncertainError(f"JIT split outcome uncertain for {oid}") from e
    if resp.status_code >= 400:
        log.error("jit split failed order_id=%s status=%s", oid, resp.status_code)
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise SplitOutcomeUncertainError(f"JIT split outcome uncertain for {oid}")
        raise SplitOrderError(f"JIT split failed for {oid}")
    try:
        payload = resp.json() if resp.content else None
    except Exception as e:
        raise SplitOutcomeUncertainError(f"JIT split outcome uncertain for {oid}") from e
    if not isinstance(payload, dict):
        raise SplitOutcomeUncertainError(f"JIT split outcome uncertain for {oid}")
    if not split_response_ok(payload):
        log.error(
            "jit split failed order_id=%s status_code=%s is_success=%s",
            oid,
            payload.get("status_code"),
            payload.get("is_success"),
        )
        raise SplitOrderError(f"JIT split failed for {oid}")
    return payload


def customer_name(order: dict[str, Any]) -> str:
    user = order.get("user") if isinstance(order.get("user"), dict) else {}
    props = user.get("properties") if isinstance(user.get("properties"), dict) else {}
    addr = order.get("delivery_address") if isinstance(order.get("delivery_address"), dict) else {}
    for candidate in (
        props.get("name"),
        user.get("name"),
        order.get("customer_name"),
        order.get("patient_name"),
        addr.get("name"),
    ):
        raw = str(candidate or "").strip()
        if raw:
            return raw
    return "Customer"


def customer_phone(order: dict[str, Any]) -> str:
    user = order.get("user") if isinstance(order.get("user"), dict) else {}
    for candidate in (
        user.get("number"),
        user.get("display_number"),
        order.get("contact_number"),
        (order.get("delivery_address") or {}).get("contact_number") if isinstance(order.get("delivery_address"), dict) else None,
    ):
        digits = "".join(c for c in str(candidate or "") if c.isdigit())
        if len(digits) >= 10:
            return digits[-10:]
    return ""


def promised_eta_display(bundle: dict[str, Any]) -> str:
    delivery = rules.extract_order_delivery(bundle)
    current = delivery.get("promised_current") if isinstance(delivery.get("promised_current"), dict) else {}
    display = current.get("display") or delivery.get("promised_delivery") or "—"
    return str(display)


def tracking_url(order: dict[str, Any]) -> str:
    summary = build_shipping_summary(order)
    url = (summary.get("tracking_url") or "").strip()
    return url


def _stub_held_split_order_id(parent_oid: str) -> str:
    """Synthetic PO id for the new split child in stub mode (held JIT after retain-available)."""
    parent = parent_oid.strip().upper()
    return _STUB_SPLIT_CHILD_MAP.get(parent, f"{parent}_HELD")


def default_tracking_url(order_id: str) -> str:
    oid = order_id.strip().upper()
    return f"{_TRACK_URL_BASE}/{oid}" if oid else ""


def tracking_url_for_order(order: dict[str, Any] | None, order_id: str) -> str:
    """Shipment tracking URL from order payload, else 1mg track page for that PO."""
    url = tracking_url(order) if isinstance(order, dict) else ""
    return url or default_tracking_url(order_id)


def _collect_po_ids(raw: Any, *, parent_oid: str, found: list[str]) -> None:
    parent = parent_oid.strip().upper()
    if isinstance(raw, str):
        oid = raw.strip().upper()
        if _PO_ID_RE.match(oid) and oid != parent and oid not in found:
            found.append(oid)
        return
    if isinstance(raw, dict):
        oid = str(raw.get("order_id") or raw.get("id") or "").strip().upper()
        if _PO_ID_RE.match(oid) and oid != parent and oid not in found:
            found.append(oid)


def extract_held_split_order_ids_from_response(
    resp: dict[str, Any] | None,
    parent_oid: str,
) -> list[str]:
    """Best-effort new child PO ids from JIT split API response (usually empty live)."""
    if not isinstance(resp, dict):
        return []
    parent = parent_oid.strip().upper()
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, val in obj.items():
                kl = str(key).lower()
                if kl in {
                    "child_order_id",
                    "new_order_id",
                    "split_order_id",
                    "shipment_order_id",
                }:
                    _collect_po_ids(val, parent_oid=parent, found=found)
                elif kl in {"child_order_ids", "child_orders", "split_order_ids", "split_orders"}:
                    if isinstance(val, list):
                        for item in val:
                            _collect_po_ids(item, parent_oid=parent, found=found)
                    else:
                        _collect_po_ids(val, parent_oid=parent, found=found)
                elif kl == "order_id" and isinstance(val, str):
                    oid = val.strip().upper()
                    if _PO_ID_RE.match(oid) and oid != parent and oid not in found:
                        found.append(oid)
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(resp.get("data"))
    walk(resp)
    return found


def held_split_order_ids_from_order(order: dict[str, Any] | None, parent_oid: str) -> list[str]:
    """Held-line split PO ids embedded on the parent order record."""
    if not isinstance(order, dict):
        return []
    found: list[str] = []
    for key in ("child_order_ids", "child_orders", "split_order_ids", "split_orders", "children"):
        val = order.get(key)
        if isinstance(val, list):
            for item in val:
                _collect_po_ids(item, parent_oid=parent_oid, found=found)
        else:
            _collect_po_ids(val, parent_oid=parent_oid, found=found)
    return found


def parse_order_search_response(resp: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize ``order_details`` from POST /__onemg-internal__/search."""
    if not isinstance(resp, dict):
        return []
    raw = resp.get("order_details")
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    return []


def child_orders_from_search(orders: list[dict[str, Any]], parent_oid: str) -> list[dict[str, Any]]:
    """
    Child PO rows for a split family from order-service search.

    Search by parent ``order_id`` returns parent + children; children have
    ``parent_id`` set to the parent PO (see PO13326295017145 → PO13326295207344).
    """
    parent = parent_oid.strip().upper()
    children: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = str(order.get("order_id") or "").strip().upper()
        if not oid or oid == parent:
            continue
        pid = str(order.get("parent_id") or "").strip().upper()
        if pid == parent:
            children.append(order)
    children.sort(
        key=lambda o: normalize_order_created_ts(o.get("created")) or 0,
        reverse=True,
    )
    return children


def parent_order_from_search(orders: list[dict[str, Any]], parent_oid: str) -> dict[str, Any] | None:
    parent = parent_oid.strip().upper()
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("order_id") or "").strip().upper() == parent:
            return order
    return None


def _order_index_by_id(orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = str(order.get("order_id") or "").strip().upper()
        if oid:
            out[oid] = order
    return out


def known_child_ids_from_search(orders: list[dict[str, Any]], parent_oid: str) -> frozenset[str]:
    return frozenset(
        str(c.get("order_id") or "").strip().upper()
        for c in child_orders_from_search(orders, parent_oid)
        if str(c.get("order_id") or "").strip()
    )


def new_child_orders_after_split(
    orders: list[dict[str, Any]],
    parent_oid: str,
    *,
    known_child_ids: frozenset[str] | None = None,
    split_after_ts: int | None = None,
) -> list[dict[str, Any]]:
    """
    Child PO rows created by the current JIT split (not pre-existing split children).

    Uses a pre-split child id snapshot when available, else ``created`` timestamp cutoff.
    """
    children = child_orders_from_search(orders, parent_oid)
    if not children:
        return []

    known = known_child_ids or frozenset()
    cutoff = (split_after_ts - _SPLIT_CREATED_BUFFER_SEC) if split_after_ts is not None else None
    fresh: list[dict[str, Any]] = []
    for child in children:
        oid = str(child.get("order_id") or "").strip().upper()
        if not oid:
            continue
        if oid in known:
            continue
        if cutoff is not None:
            created_ts = normalize_order_created_ts(child.get("created"))
            if created_ts is None or created_ts < cutoff:
                continue
        fresh.append(child)

    if fresh:
        return fresh
    if known or cutoff is not None:
        return []
    return children[:1]


def _has_shipment_tracking(order: dict[str, Any] | None) -> bool:
    if not isinstance(order, dict):
        return False
    ship = order.get("shipment_detail")
    if not isinstance(ship, dict):
        return False
    return bool(str(ship.get("tracking_url") or "").strip())


async def tracking_url_with_fallback(
    order: dict[str, Any] | None,
    order_id: str,
) -> str:
    """Prefer tracking URL embedded on a search/order row; fetch order-details only if missing."""
    oid = order_id.strip().upper()
    if _has_shipment_tracking(order):
        return tracking_url_for_order(order, oid)

    try:
        fetched = await fetch_order_details(oid)
    except OrderNotFoundError:
        log.debug("jit_hold tracking GET 404 order_id=%s — using default track URL", oid)
        return default_tracking_url(oid)
    return tracking_url_for_order(fetched if isinstance(fetched, dict) else None, oid)


async def format_tracking_links_for_orders(orders: list[dict[str, Any]]) -> str:
    """One or more held-child tracking links for the done template."""
    lines: list[str] = []
    multi = len(orders) > 1
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = str(order.get("order_id") or "").strip().upper()
        if not oid:
            continue
        url = await tracking_url_with_fallback(order, oid)
        if not url:
            continue
        lines.append(f"{oid}: {url}" if multi else url)
    return "\n".join(lines)


async def search_orders_by_parent(parent_oid: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """
    List related POs (parent + children) via order-service internal search.

    Paginates 5×10 (same helper as order RCA family collect).
    Requires ``queue_name=search`` and service headers (same as order RCA).
    Tracking lookups use ``strict=False`` (empty on failure). Split snapshot /
    new-child detection uses ``strict=True`` so a failed search is not stored
    as “no children”.
    """
    parent = parent_oid.strip().upper()
    if settings.whatsapp_jit_hold_split_stub and settings.whatsapp_jit_hold_use_fixtures:
        return []

    base = settings.order_rca_order_service_base_url.rstrip("/")
    if not base:
        if strict:
            raise RuntimeError(f"order search unavailable for {parent}: missing order-service base url")
        return []

    try:
        client = get_internal_http_client()
        orders = await sources._search_family_order_rows(client, base, parent)
    except Exception:
        log.exception("order search failed parent_order_id=%s", parent)
        if strict:
            raise
        return []

    log.debug(
        "order search parent=%s rows=%s children=%s",
        parent,
        len(orders),
        len(child_orders_from_search(orders, parent)),
    )
    return orders


async def _search_orders_after_split(
    parent_oid: str,
    *,
    split_after_ts: int | None,
    known_child_ids: frozenset[str] | None,
) -> list[dict[str, Any]]:
    """Search once; retry briefly when we expect a new child row right after split."""
    rows = await search_orders_by_parent(parent_oid)
    if split_after_ts is None:
        return rows

    if new_child_orders_after_split(
        rows,
        parent_oid,
        known_child_ids=known_child_ids,
        split_after_ts=split_after_ts,
    ):
        return rows

    await asyncio.sleep(_SEARCH_RETRY_DELAY_SEC)
    return await search_orders_by_parent(parent_oid)


def _held_orders_from_split_response(
    search_rows: list[dict[str, Any]],
    parent_oid: str,
    split_response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    held_ids = extract_held_split_order_ids_from_response(split_response, parent_oid)
    if not held_ids:
        return []
    by_id = _order_index_by_id(search_rows)
    return [by_id[oid] for oid in held_ids if oid in by_id]


def format_held_orders_status_summary(orders: list[dict[str, Any]]) -> str:
    """Held PO ids with status/sub-status for the split-done template (new children)."""
    lines: list[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = str(order.get("order_id") or "").strip().upper()
        if not oid:
            continue
        status = str(order.get("status") or order.get("status_id") or "—").strip()
        sub = str(order.get("sub_status") or "").strip()
        line = f"{oid}: {status}"
        if sub:
            line = f"{line} / {sub}"
        lines.append(line)
    return "\n".join(lines) if lines else "Held order details will be shared shortly"


async def _resolve_held_orders(
    search_rows: list[dict[str, Any]],
    parent: str,
    *,
    split_response: dict[str, Any] | None,
    known_child_ids_before: frozenset[str] | None,
    split_after_ts: int | None,
    parent_order: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    held_orders = _held_orders_from_split_response(search_rows, parent, split_response)
    if not held_orders:
        held_orders = new_child_orders_after_split(
            search_rows,
            parent,
            known_child_ids=known_child_ids_before,
            split_after_ts=split_after_ts,
        )

    if not held_orders and isinstance(parent_order, dict):
        embedded_ids = held_split_order_ids_from_order(parent_order, parent)
        by_id = _order_index_by_id(search_rows)
        held_orders = [by_id[oid] for oid in embedded_ids if oid in by_id]
        for oid in embedded_ids:
            if oid in by_id:
                continue
            try:
                order = await fetch_order_details(oid)
            except OrderNotFoundError:
                log.debug("jit_hold held child GET 404 order_id=%s — skipping", oid)
                continue
            if order:
                held_orders.append(order)

    if not held_orders:
        for oid in extract_held_split_order_ids_from_response(split_response, parent):
            try:
                order = await fetch_order_details(oid)
            except OrderNotFoundError:
                log.debug("jit_hold held child GET 404 order_id=%s — skipping", oid)
                continue
            if order:
                held_orders.append(order)

    return held_orders


async def resolve_split_tracking_links(
    parent_oid: str,
    *,
    split_response: dict[str, Any] | None = None,
    split_after_ts: int | None = None,
    known_child_ids_before: frozenset[str] | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """
    Return (ship_now_tracking_url, held_order_tracking_url, held_order_rows) after JIT split.

    Available items stay on the parent at the current store (ship-now). New
    children are held JIT at vendor-stock-allocation. Search is the primary
    source; GET only when a row lacks tracking.
    """
    parent = parent_oid.strip().upper()
    search_rows = await _search_orders_after_split(
        parent,
        split_after_ts=split_after_ts,
        known_child_ids=known_child_ids_before,
    )

    parent_order = parent_order_from_search(search_rows, parent)
    child_orders = await _resolve_held_orders(
        search_rows,
        parent,
        split_response=split_response,
        known_child_ids_before=known_child_ids_before,
        split_after_ts=split_after_ts,
        parent_order=parent_order,
    )

    ship_now_url = await tracking_url_with_fallback(parent_order, parent)
    held_url = await format_tracking_links_for_orders(child_orders)
    return ship_now_url, held_url, child_orders


def _positive_epoch(raw: Any) -> bool:
    try:
        return float(raw) > 0
    except (TypeError, ValueError):
        return False


def child_order_has_allocation_eta(order: dict[str, Any] | None) -> bool:
    """True when GET child has post-allocation ETA (not the group's first promise).

    GET ``/orders/{id}`` always fills ``eta.to_date`` (epoch 0 → ``1 Jan, 1970``)
    even at vendor-stock-allocation with no ORDER_ETA. ``promised_eta`` is the
    group's first promise. Only ``eta.eta_to > 0`` or ORDER_ETA metadata counts.
    """
    if not isinstance(order, dict):
        return False
    eta = order.get("eta") if isinstance(order.get("eta"), dict) else {}
    if _positive_epoch(eta.get("eta_to")):
        return True
    raw = order.get("order_eta")
    if isinstance(raw, str) and "," in raw:
        to_part = raw.split(",", 1)[1].strip()
        if _positive_epoch(to_part):
            return True
    return False


def allocation_eta_display(order: dict[str, Any] | None) -> str | None:
    """Customer-facing allocation ETA from the child GET, if present."""
    if not child_order_has_allocation_eta(order) or not isinstance(order, dict):
        return None
    eta = order.get("eta") if isinstance(order.get("eta"), dict) else {}
    for raw in (eta.get("to_date"), order.get("eta_to")):
        text = str(raw or "").strip()
        if text and "1970" not in text:
            return text
    return None


async def _discover_split_child_ids(
    parent_oid: str,
    *,
    split_response: dict[str, Any] | None,
    split_after_ts: int | None,
    known_child_ids_before: frozenset[str] | None,
) -> list[str]:
    found = extract_held_split_order_ids_from_response(split_response, parent_oid)
    if found:
        return found
    known = known_child_ids_before if known_child_ids_before is not None else frozenset()
    try:
        fresh = await new_child_ids_after_split(
            parent_oid,
            known_child_ids=known,
            split_after_ts=split_after_ts,
        )
    except Exception:
        log.exception("jit_hold child discovery failed parent_order_id=%s", parent_oid)
        return []
    return sorted(oid for oid in fresh if oid)


async def wait_for_split_child_eta(
    parent_oid: str,
    *,
    split_response: dict[str, Any] | None = None,
    split_after_ts: int | None = None,
    known_child_ids_before: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """
    Poll GET on the new child until it exists (held JIT at VSA).

    Split does not return a child id. Returns the last fetched child or None.
    Stub/fixture and timeout skip further polls. Does not wait for allocation ETA.
    """
    parent = parent_oid.strip().upper()
    stub = settings.whatsapp_jit_hold_split_stub or settings.whatsapp_jit_hold_use_fixtures
    started = time.monotonic()
    last: dict[str, Any] | None = None
    child_ids: list[str] = []
    while True:
        if not child_ids:
            child_ids = await _discover_split_child_ids(
                parent,
                split_response=split_response,
                split_after_ts=split_after_ts,
                known_child_ids_before=known_child_ids_before,
            )
        for oid in child_ids:
            try:
                order = await fetch_order_details(oid)
            except OrderNotFoundError:
                log.debug("jit_hold child GET 404 order_id=%s — still waiting", oid)
                continue
            except Exception:
                log.exception("jit_hold child GET failed order_id=%s", oid)
                continue
            if not isinstance(order, dict):
                continue
            last = order
            # Held child is at VSA — do not wait for allocation ETA before Done.
            return order
        if stub or (time.monotonic() - started) >= _ETA_WAIT_TIMEOUT_SEC:
            return last
        await asyncio.sleep(_ETA_WAIT_INTERVAL_SEC)


async def snapshot_child_ids_before_split(parent_oid: str) -> frozenset[str]:
    """Child PO ids already linked to parent before JIT split (for diffing search results).

    Raises if family search fails or returns no rows — callers must not persist
    an empty set as “no children” when the snapshot was unavailable.
    """
    rows = await _family_rows_for_split_snapshot(parent_oid)
    return known_child_ids_from_search(rows, parent_oid)


async def new_child_ids_after_split(
    parent_oid: str,
    *,
    known_child_ids: frozenset[str],
    split_after_ts: int | None,
) -> frozenset[str]:
    """Child PO ids created by this split (id snapshot + created-after cutoff)."""
    rows = await _family_rows_for_split_snapshot(parent_oid)
    fresh = new_child_orders_after_split(
        rows,
        parent_oid,
        known_child_ids=known_child_ids,
        split_after_ts=split_after_ts,
    )
    if split_after_ts is None and not known_child_ids:
        # Unscoped children[:1] fallback is for tracking display only.
        return frozenset()
    return frozenset(
        str(c.get("order_id") or "").strip().upper()
        for c in fresh
        if str(c.get("order_id") or "").strip()
    )


async def _family_rows_for_split_snapshot(parent_oid: str) -> list[dict[str, Any]]:
    rows = await search_orders_by_parent(parent_oid, strict=True)
    if rows:
        return rows
    if settings.whatsapp_jit_hold_use_fixtures:
        return []
    raise RuntimeError(f"order search returned no rows for {parent_oid}")
