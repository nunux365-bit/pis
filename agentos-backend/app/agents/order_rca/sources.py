"""Fetch order RCA source APIs (live HTTP or local fixtures)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.agents.order_rca import rules
from app.agents.order_rca import soft_allocation
from app.agents.order_rca.constants import FIXTURE_CASE_BY_ORDER_ID, HISTORY_PAGE_SIZE
from app.agents.order_rca.time_utils import ORDER_RCA_API_TZ
from app.config.settings import settings
from app.infra.httpx_clients import get_internal_http_client

log = logging.getLogger(__name__)

_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "order_rca"


def _fixture_case_key(order_id: str) -> str | None:
    return FIXTURE_CASE_BY_ORDER_ID.get(order_id.strip().upper())


def _fixture_root_for_order(order_id: str) -> Path:
    custom = (settings.order_rca_fixture_dir or "").strip()
    base = Path(custom) if custom else _FIXTURE_ROOT
    case = _fixture_case_key(order_id)
    if case:
        return base / "cases" / case
    return base


def _fixture_path(name: str, *, order_id: str | None = None) -> Path:
    root = _fixture_root_for_order(order_id) if order_id else _FIXTURE_ROOT
    custom = (settings.order_rca_fixture_dir or "").strip()
    if custom and not order_id:
        root = Path(custom)
    elif not order_id:
        root = _FIXTURE_ROOT
    return root / name


def _load_fixture(name: str, *, order_id: str | None = None) -> dict[str, Any]:
    p = _fixture_path(name, order_id=order_id)
    if not p.is_file():
        raise FileNotFoundError(f"fixture missing: {p}")
    return json.loads(p.read_text())


def _load_fixture_optional(name: str, *, order_id: str | None = None) -> dict[str, Any] | None:
    p = _fixture_path(name, order_id=order_id)
    if not p.is_file():
        return None
    return json.loads(p.read_text())


async def _fetch_order_analytics(
    client: httpx.AsyncClient,
    base_o: str,
    order_id: str,
) -> dict[str, Any]:
    return await _get_json(
        client,
        f"{base_o}/__onemg-internal__/v4/order_analytics/{order_id}",
        headers=_order_headers(),
    )


async def _fetch_order_analytics_safe(
    client: httpx.AsyncClient,
    base_o: str,
    order_id: str,
) -> dict[str, Any] | None:
    """Best-effort analytics; uses same HTTP retries as other order-service calls."""
    try:
        return await _fetch_order_analytics(client, base_o, order_id)
    except Exception as e:
        log.warning("order_analytics fetch failed order_id=%s: %s", order_id, e)
        return None


def _payment_entity_id(order_id: str, parent_id: str | None) -> str:
    """Split children are billed on the parent PO; use that id when present."""
    pid = (parent_id or "").strip().upper()
    oid = (order_id or "").strip().upper()
    if pid and pid != oid:
        return pid
    return oid


async def _fetch_order_verification_timeline(
    client: httpx.AsyncClient,
    base_p: str,
    order_id: str,
) -> dict[str, Any]:
    return await _get_json(
        client,
        f"{base_p}/v1/order_verification_timeline",
        headers=_order_headers(),
        params={"order_id": order_id},
    )


async def _fetch_payment_details(
    client: httpx.AsyncClient,
    base_a: str,
    order_id: str,
) -> dict[str, Any]:
    return await _get_json(
        client,
        f"{base_a}/__onemg-internal__/v1/payments/payment_transactions",
        headers=_order_headers(),
        params={
            "entity_type": "pharmacy_order",
            "entity_id": order_id,
            "page": 1,
            "per_page": 100,
        },
    )


async def _fetch_validation_details(
    client: httpx.AsyncClient,
    base_p: str,
    order_id: str,
) -> dict[str, Any] | None:
    if not base_p:
        return None
    try:
        out = await _fetch_order_verification_timeline(client, base_p, order_id)
        return out if isinstance(out, dict) else None
    except Exception as e:
        log.warning(
            "validation_details fetch failed order_id=%s: %s", order_id, e)
        return None


async def _fetch_payment_details_safe(
    client: httpx.AsyncClient,
    base_a: str,
    order_id: str,
) -> dict[str, Any] | None:
    if not base_a:
        return None
    try:
        out = await _fetch_payment_details(client, base_a, order_id)
        return out if isinstance(out, dict) else None
    except Exception as e:
        log.warning("payment_details fetch failed order_id=%s: %s", order_id, e)
        return None


async def _fetch_order_history(
    client: httpx.AsyncClient,
    base_o: str,
    order_id: str,
) -> dict[str, Any]:
    """Fetch all history pages (API caps page_size; we use HISTORY_PAGE_SIZE per page)."""
    page_size = HISTORY_PAGE_SIZE
    page_number = 1
    merged: list[Any] = []
    last_payload: dict[str, Any] = {}

    while True:
        payload = await _get_json(
            client,
            f"{base_o}/__onemg-internal__/order/{order_id}/history",
            headers=_order_headers(),
            params={"page_size": page_size, "page_number": page_number},
        )
        if not isinstance(payload, dict):
            break
        last_payload = payload
        chunk = payload.get("history") or []
        if isinstance(chunk, list):
            merged.extend(chunk)
        try:
            total_pages = int(payload.get("total_pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page_number >= total_pages:
            break
        page_number += 1

    if not last_payload:
        return {"history": merged}
    out = {k: v for k, v in last_payload.items() if k != "history"}
    out["history"] = merged
    out["page_size"] = page_size
    out["page_number"] = 1
    out["total_pages"] = 1
    try:
        out["total_count"] = int(last_payload.get("total_count") or len(merged))
    except (TypeError, ValueError):
        out["total_count"] = len(merged)
    return out


def _order_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-SERVICE-NAME": settings.order_rca_service_name,
        "X-SERVICE-VERSION": settings.order_rca_service_version,
    }


_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_EXC = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


def _http_max_attempts() -> int:
    return max(1, 1 + int(settings.order_rca_http_retries))


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kw: Any,
) -> httpx.Response:
    attempts = _http_max_attempts()
    last_err: BaseException | None = None
    for attempt in range(attempts):
        try:
            if method == "GET":
                resp = await client.get(url, **kw)
            else:
                resp = await client.post(url, **kw)
            if resp.status_code in _RETRYABLE_STATUS:
                last_err = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
            resp.raise_for_status()
            return resp
        except _TRANSIENT_EXC as e:
            last_err = e
            if attempt < attempts - 1:
                log.warning("order_rca HTTP %s %s attempt %s/%s: %s", method, url, attempt + 1, attempts, e)
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                last_err = e
                log.warning(
                    "order_rca HTTP %s %s status %s attempt %s/%s",
                    method,
                    url,
                    e.response.status_code,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("order_rca HTTP request failed without response")


async def _get_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    r = await _request_with_retry(client, "GET", url, **kw)
    return r.json()


async def _post_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    r = await _request_with_retry(client, "POST", url, **kw)
    return r.json()


async def _post_explain_allocation(client: httpx.AsyncClient, url: str, **kw: Any) -> dict[str, Any]:
    """POST explain_allocation; 404 → empty data (archived). 5xx still retried via _request_with_retry."""
    try:
        out = await _post_json(client, url, **kw)
        return out if isinstance(out, dict) else {"data": {}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.debug("order_rca explain_allocation 404 (treated as no data) url=%s", url)
            return {"data": {}}
        raise


def _minimal_status_payload() -> dict[str, Any]:
    return {"data": {}, "is_success": True, "status_code": 200}


def _minimal_groot_payload() -> dict[str, Any]:
    return {"data": {}, "is_success": True, "status_code": 200}


def _minimal_clickpost_payload() -> dict[str, Any]:
    return {"data": [], "is_success": True, "status_code": 200}


def _fixture_groot_payload(name: str, *, order_id: str | None = None) -> dict[str, Any]:
    """Load Groot fixture when ``data`` is non-empty; else empty timeline."""
    p = _fixture_path(name, order_id=order_id)
    if not p.is_file():
        return _minimal_groot_payload()
    payload = json.loads(p.read_text())
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data:
        return payload
    return _minimal_groot_payload()


def _fixture_clickpost_payload(name: str, *, order_id: str | None = None) -> dict[str, Any]:
    """Load ClickPost fixture JSON as-is (parse_clickpost_events handles empty/error bodies)."""
    p = _fixture_path(name, order_id=order_id)
    if not p.is_file():
        return _minimal_clickpost_payload()
    payload = json.loads(p.read_text())
    return payload if isinstance(payload, dict) else _minimal_clickpost_payload()


def _bundle_uses_groot_timeline(bundle: dict[str, Any]) -> bool:
    return bool(rules.parse_groot_events(bundle.get("groot")))


def _shipment_tracking_params(order: dict[str, Any], order_id: str) -> tuple[str, str, str] | None:
    ship = order.get("shipment_detail") if isinstance(order.get("shipment_detail"), dict) else {}
    waybill = (ship.get("tracking_number") or "").strip()
    dp = (ship.get("delivery_partners_code") or "").strip()
    if not waybill or not dp:
        return None
    return order_id, waybill, dp


async def _fetch_clickpost_tracking(
    client: httpx.AsyncClient,
    base_p: str,
    order_id: str,
    waybill: str,
    delivery_partners_code: str,
) -> dict[str, Any]:
    url = f"{base_p.rstrip('/')}/__onemg-internal__/v1/3pl_order_tracking/{order_id}"
    params = {
        "waybill": waybill,
        "delivery_partners_code": delivery_partners_code,
        "order_id": order_id,
    }
    try:
        return await _get_json(client, url, headers=_order_headers(), params=params)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            try:
                body = e.response.json()
                if isinstance(body, dict):
                    return body
            except Exception:
                pass
            return {"data": [], "is_success": False, "status_code": 400}
        log.warning("clickpost fetch failed for %s: %s", order_id, e)
        return {"data": [], "is_success": False, "status_code": getattr(e.response, "status_code", None)}
    except Exception as e:
        log.warning("clickpost fetch failed for %s: %s", order_id, e)
        return {"data": [], "is_success": False, "status_code": None}


def _attach_fixture_clickpost(bundle: dict[str, Any], *, order_id: str) -> dict[str, Any]:
    if _bundle_uses_groot_timeline(bundle):
        bundle["clickpost"] = _minimal_clickpost_payload()
        return bundle
    bundle["clickpost"] = _fixture_clickpost_payload("clickpost.json", order_id=order_id)
    return bundle


async def _attach_live_clickpost(
    client: httpx.AsyncClient,
    bundle: dict[str, Any],
    *,
    order_id: str,
    base_p: str,
) -> dict[str, Any]:
    if _bundle_uses_groot_timeline(bundle):
        bundle["clickpost"] = _minimal_clickpost_payload()
        return bundle
    order = bundle.get("order") if isinstance(bundle.get("order"), dict) else {}
    params = _shipment_tracking_params(order, order_id)
    if not base_p or not params:
        bundle["clickpost"] = _minimal_clickpost_payload()
        return bundle
    oid, waybill, dp = params
    bundle["clickpost"] = await _fetch_clickpost_tracking(client, base_p, oid, waybill, dp)
    return bundle


async def _fetch_groot_timeline(
    client: httpx.AsyncClient,
    base_g: str,
    order_id: str,
) -> dict[str, Any]:
    try:
        return await _get_json(
            client,
            f"{base_g}/v1/orders/timelines/{order_id}",
            headers=_order_headers(),
        )
    except Exception as e:
        log.warning("groot fetch failed for %s: %s", order_id, e)
        return _minimal_groot_payload()


async def _fetch_p1_msn_with_client(
    client: httpx.AsyncClient,
    order_id: str,
) -> dict[str, Any]:
    """P1 MSN adherence — shared HTTP client for parallel collect."""
    base = (settings.order_rca_p1_msn_base_url or "").strip().rstrip("/")
    if not base:
        return {}
    oid = order_id.strip().upper()
    try:
        return await _get_json(
            client,
            f"{base}/v1/orders/{oid}/msn-adherence",
            headers=_order_headers(),
        )
    except Exception as e:
        log.warning("P1 MSN fetch failed for %s: %s", oid, e)
        return {}


async def fetch_p1_msn_data(order_id: str) -> dict[str, Any]:
    """P1 planning API — MSN, on-shelf, SKU sub grade per store × order SKU."""
    oid = order_id.strip().upper()
    if settings.order_rca_use_fixtures:
        p = _fixture_path("p1_msn.json")
        if p.is_file():
            return json.loads(p.read_text())
        return {}

    base = (settings.order_rca_p1_msn_base_url or "").strip().rstrip("/")
    if not base:
        return {}

    try:
        client = get_internal_http_client()
        return await _fetch_p1_msn_with_client(client, oid)
    except Exception as e:
        log.warning("P1 MSN fetch failed for %s: %s", oid, e)
        return {}


async def _fetch_soft_allocation(
    client: httpx.AsyncClient,
    base_s: str,
    phone: str,
    *,
    placed_at: datetime | None = None,
) -> dict[str, Any]:
    """Paginate explain_soft_allocation until before place-order window or last page."""
    token = settings.order_rca_sla_auth_token
    if not token or not phone.strip():
        return {}

    headers = {
        "Content-Type": "application/json",
        "Authorization": token,
    }
    per_page = 20
    page = 1
    merged: list[Any] = []
    last_payload: dict[str, Any] = {}
    cutoff = soft_allocation.pagination_cutoff(placed_at) if placed_at else None

    while page <= 50:
        try:
            payload = await _post_json(
                client,
                f"{base_s.rstrip('/')}/v1/analytics/explain_soft_allocation",
                headers=headers,
                json={"phone_number": phone.strip(), "page": page, "per_page": per_page},
            )
        except Exception as e:
            log.warning("soft_allocation fetch failed page=%s: %s", page, e)
            break
        if not isinstance(payload, dict):
            break
        last_payload = payload
        chunk = (payload.get("data") or {}).get("response") if isinstance(payload.get("data"), dict) else []
        if isinstance(chunk, list) and chunk:
            merged.extend(chunk)
            if cutoff is not None:
                oldest = None
                for row in chunk:
                    if not isinstance(row, dict):
                        continue
                    ts = soft_allocation.parse_cart_timestamp(row.get("cart_timestamp"))
                    if ts is not None and (oldest is None or ts < oldest):
                        oldest = ts
                if oldest is not None and oldest.astimezone(ORDER_RCA_API_TZ) < cutoff:
                    break
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        try:
            total_pages = int(meta.get("total_pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            break
        page += 1

    if not last_payload:
        return {"data": {"response": merged}, "is_success": True, "status_code": 200}
    out = dict(last_payload)
    if isinstance(out.get("data"), dict):
        out["data"] = {**out["data"], "response": merged}
    else:
        out["data"] = {"response": merged}
    return out


def _fixture_soft_allocation_payload(order: dict[str, Any]) -> dict[str, Any]:
    p = _fixture_path("soft_allocation.json")
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    phone = soft_allocation.order_phone(order)
    if isinstance(raw, dict) and "data" in raw:
        return raw
    if phone and isinstance(raw, dict) and phone in raw:
        block = raw.get(phone)
        return block if isinstance(block, dict) else {}
    return {}


async def _fetch_soft_allocation_for_order(
    client: httpx.AsyncClient,
    base_s: str,
    order: dict[str, Any],
) -> dict[str, Any]:
    phone = soft_allocation.order_phone(order)
    placed = soft_allocation.order_placed_at(order)
    if not phone:
        return {}
    return await _fetch_soft_allocation(client, base_s, phone, placed_at=placed)


async def _attach_live_soft_allocation(
    client: httpx.AsyncClient,
    bundle: dict[str, Any],
    *,
    base_s: str,
) -> dict[str, Any]:
    order = bundle.get("order") if isinstance(bundle.get("order"), dict) else {}
    bundle["soft_allocation"] = await _fetch_soft_allocation_for_order(client, base_s, order)
    return bundle


def _attach_fixture_family_orders(bundle: dict[str, Any]) -> dict[str, Any]:
    """Fixture-mode family = current PO only (parent stub clones child lines)."""
    if isinstance(bundle.get("family_orders"), list):
        return bundle
    order = bundle.get("order")
    bundle["family_orders"] = [order] if isinstance(order, dict) else []
    return bundle


def _attach_fixture_soft_allocation(bundle: dict[str, Any]) -> dict[str, Any]:
    bundle = _attach_fixture_family_orders(bundle)
    order = bundle.get("order") if isinstance(bundle.get("order"), dict) else {}
    bundle["soft_allocation"] = _fixture_soft_allocation_payload(order)
    return bundle


_FAMILY_SEARCH_PAGE_SIZE = 10
_FAMILY_SEARCH_MAX_PAGES = 5  # 5×10 — enough for split families; hard cap


async def _search_family_order_rows(
    client: httpx.AsyncClient,
    base_o: str,
    root: str,
) -> list[dict[str, Any]]:
    """POST search by parent root; page until exhausted (5×10). Fail-soft → []."""
    url = f"{base_o.rstrip('/')}/__onemg-internal__/search"
    headers = _order_headers()
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= _FAMILY_SEARCH_MAX_PAGES:
        try:
            resp = await _post_json(
                client,
                url,
                headers=headers,
                json={
                    "order_id": root,
                    "page_number": page,
                    "page_size": _FAMILY_SEARCH_PAGE_SIZE,
                    "apply_date_filter_on": "order_creation_time",
                    "queue_name": "search",
                },
            )
        except Exception:
            if page == 1:
                raise
            log.exception(
                "family order search page=%s failed root=%s — using %s rows so far",
                page,
                root,
                len(rows),
            )
            break
        if not isinstance(resp, dict):
            break
        chunk = resp.get("order_details")
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(o for o in chunk if isinstance(o, dict))
        try:
            total_pages = int(resp.get("total_pages") or 0)
        except (TypeError, ValueError):
            total_pages = 0
        if total_pages and page >= total_pages:
            break
        if len(chunk) < _FAMILY_SEARCH_PAGE_SIZE:
            break
        page += 1
    return rows


async def _resolve_family_orders(
    client: httpx.AsyncClient,
    base_o: str,
    *,
    order: dict[str, Any],
    parent_order: dict[str, Any] | None,
    parent_id: str | None,
) -> list[dict[str, Any]]:
    """Parent + all children via search; hydrate GET when search rows lack lines. Fail-soft."""
    oid = str(order.get("order_id") or "").strip().upper()
    root = (parent_id or oid or "").strip().upper()
    by_id: dict[str, dict[str, Any]] = {}
    if oid:
        by_id[oid] = order
    if isinstance(parent_order, dict):
        pid = str(parent_order.get("order_id") or parent_id or "").strip().upper()
        if pid:
            by_id[pid] = parent_order
    if not root:
        return list(by_id.values())

    try:
        rows = await _search_family_order_rows(client, base_o, root)
    except Exception:
        log.exception("family order search failed root=%s — continuing without siblings", root)
        return list(by_id.values())

    missing: list[str] = []
    for row in rows:
        rid = str(row.get("order_id") or "").strip().upper()
        if not rid or rid in by_id:
            continue
        lines = row.get("order_lines")
        if isinstance(lines, list) and lines:
            by_id[rid] = row
        else:
            missing.append(rid)

    if missing:
        base = base_o.rstrip("/")
        headers = _order_headers()

        async def _hydrate(rid: str) -> tuple[str, dict[str, Any] | None]:
            try:
                full = await _get_json(
                    client,
                    f"{base}/__onemg-internal__/orders/{rid}",
                    headers=headers,
                )
                return rid, full if isinstance(full, dict) and full else None
            except Exception:
                log.exception("family order hydrate failed order_id=%s", rid)
                return rid, None

        for rid, full in await asyncio.gather(*[_hydrate(m) for m in missing]):
            if full is not None:
                by_id[rid] = full
    return list(by_id.values())


def _get_neucoins_for_transactions(
    oid: str,
    order_details: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Return only neucoins for the given order_id (exclude neucoins for other sibling/parent orders).
    """
    filtered: list[Any] = []
    if order_details:
        payment_summary = order_details.get("payment_summary")
        if isinstance(payment_summary, dict) and isinstance(payment_summary.get("payments"), list):
            for item in payment_summary["payments"]:
                if not isinstance(item, dict) or item.get("gateway_name") != "NEUCOINS" or oid != str(item.get("order_id") or "").strip().upper():
                    continue
                filtered.append(item)
    return filtered


def _get_payments_for_transactions(
    oid: str,
    payment_details: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Return only payments for the given order_id (exclude refunds for other sibling/parent orders).
    """
    filtered: list[Any] = []
    if payment_details and isinstance(payment_details.get("data"), list):
        _missing = object()
        for item in payment_details["data"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "REFUND":
                filtered.append(item)
                continue

            raw_meta = item.get("metadata")
            metadata: dict[str, Any] = raw_meta if isinstance(
                raw_meta, dict) else {}

            pharmacy_order_id = metadata.get("pharmacy_order_id", _missing)

            if pharmacy_order_id is not _missing:
                if str(pharmacy_order_id).strip().upper() == oid:
                    filtered.append(item)
            else:
                details = metadata.get("details")
                additional_info = details.get(
                    "additionalInfo") if isinstance(details, dict) else None
                remark = additional_info.get("remark", "") if isinstance(
                    additional_info, dict) else ""
                if str(remark).strip().upper() == oid:
                    filtered.append(item)
    return filtered


async def fetch_bundle(order_id: str) -> dict[str, Any]:
    """Return raw API payloads keyed by logical name (always full collect)."""
    oid = order_id.strip().upper()
    if settings.order_rca_use_fixtures:
        return await _fetch_fixtures(oid)

    base_o = settings.order_rca_order_service_base_url.rstrip("/")
    base_s = settings.order_rca_sla_service_base_url.rstrip("/")
    base_g = (settings.order_rca_groot_base_url or "").rstrip("/")
    base_p = (settings.order_rca_post_order_base_url or "").rstrip("/")
    base_a = (settings.order_rca_admin_service_base_url or "").rstrip("/")
    if not base_o or not base_s:
        raise RuntimeError("order_rca_order_service_base_url and order_rca_sla_service_base_url are required")

    client = get_internal_http_client()
    order = await _get_json(
        client,
        f"{base_o}/__onemg-internal__/orders/{oid}",
        headers=_order_headers(),
    )
    parent_id = (order.get("parent_id") or "").strip().upper() or None
    order_headers = _order_headers()
    sla_headers = {
        "Content-Type": "application/json",
        "Authorization": settings.order_rca_sla_auth_token,
    }

    async def _parent_order() -> dict[str, Any] | None:
        if not parent_id or parent_id == oid:
            return None
        return await _get_json(
            client,
            f"{base_o}/__onemg-internal__/orders/{parent_id}",
            headers=order_headers,
        )

    async def _parent_status() -> dict[str, Any] | None:
        if not parent_id:
            return None
        return await _get_json(
            client,
            f"{base_o}/__onemg-internal__/v4/orders/{parent_id}/status-history",
            headers=order_headers,
        )

    async def _groot() -> dict[str, Any]:
        if not base_g:
            return _minimal_groot_payload()
        return await _fetch_groot_timeline(client, base_g, oid)

    (
        parent_order,
        alloc,
        p1_msn,
        status,
        parent_status,
        history,
        analytics,
        groot,
        soft_allocation_payload,
        payment_details,
        validation_details,
    ) = await asyncio.gather(
        _parent_order(),
        _post_explain_allocation(
            client,
            f"{base_s}/v1/analytics/{oid}/explain_allocation",
            headers=sla_headers,
            json={},
        ),
        _fetch_p1_msn_with_client(client, oid),
        _get_json(
            client,
            f"{base_o}/__onemg-internal__/v4/orders/{oid}/status-history",
            headers=order_headers,
        ),
        _parent_status(),
        _fetch_order_history(client, base_o, oid),
        _fetch_order_analytics_safe(client, base_o, oid),
        _groot(),
        _fetch_soft_allocation_for_order(client, base_s, order),
        _fetch_payment_details_safe(client, base_a, _payment_entity_id(oid, parent_id)),
        _fetch_validation_details(client, base_p, oid),
    )

    order_dict = order if isinstance(order, dict) else {}
    if oid and not str(order_dict.get("order_id") or "").strip():
        order_dict = {**order_dict, "order_id": oid}

    family_orders = await _resolve_family_orders(
        client,
        base_o,
        order=order_dict,
        parent_order=parent_order if isinstance(parent_order, dict) else None,
        parent_id=parent_id,
    )

    oid_payments: list[dict[str, Any]] = _get_payments_for_transactions(
        oid, payment_details)
    oid_neucoins: list[dict[str, Any]] = _get_neucoins_for_transactions(
        oid, order_dict)
    transactions = {
        "payments": oid_payments,
        "neucoins": oid_neucoins,
    }

    out = {
        "order_id": oid,
        "order": order,
        "parent_id": parent_id,
        "parent_order": parent_order,
        "family_orders": family_orders,
        "allocation": alloc,
        "status": status,
        "parent_status": parent_status,
        "history": history,
        "analytics": analytics,
        "groot": groot,
        "p1_msn": p1_msn,
        "soft_allocation": soft_allocation_payload,
        "payment_details": payment_details,
        "validation_details": validation_details,
        "transactions": transactions,
        "collect_mode": "full",
    }
    return await _attach_live_clickpost(client, out, order_id=oid, base_p=base_p)


def _fixture_analytics(order_id: str) -> dict[str, Any] | None:
    oid = order_id.strip().upper()
    case_payload = _load_fixture_optional("analytics.json", order_id=oid)
    if case_payload is not None and "data" in case_payload:
        return case_payload
    root_payload = _load_fixture_optional("analytics.json")
    if isinstance(root_payload, dict) and oid in root_payload:
        block = root_payload.get(oid)
        return block if isinstance(block, dict) else None
    return None


def _attach_fixture_analytics(bundle: dict[str, Any], order_id: str) -> dict[str, Any]:
    analytics = _fixture_analytics(order_id)
    if analytics is not None:
        bundle["analytics"] = analytics
    return bundle


def _attach_fixture_payment_details(bundle: dict[str, Any], order_id: str) -> dict[str, Any]:
    payment = _load_fixture_optional("payment_details.json", order_id=order_id)
    if payment is not None:
        bundle["payment_details"] = payment
    return bundle


def _fixture_parent_order_stub(parent_id: str, allocation: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal parent order shape for split-child fixture runs (no separate order fixture file)."""
    child_order = _load_fixture("order_child.json")
    return _fixture_parent_order_record(parent_id, child_order, allocation)


def _fixture_parent_order_record(
    parent_id: str,
    child_order: dict[str, Any],
    allocation: dict[str, Any],
) -> dict[str, Any] | None:
    """Parent PO stub for fixture mode (allocation + shared address). No child shipment — delivery is on child PO."""
    block = (allocation.get("data") or {}).get(parent_id) or {}
    if not block:
        return None
    addr = child_order.get("delivery_address") if isinstance(child_order.get("delivery_address"), dict) else {}
    return {
        "order_id": parent_id,
        "parent_id": None,
        "status": "40",
        "vendor_id": block.get("allocated_vendor"),
        "delivery_address": dict(addr),
        "shipment_detail": {},
        "eta": {
            "eta_to": 1778748060.0,
            "to_date": "14 May, 2026",
            "text": "May 14th",
            "zone": (child_order.get("eta") or {}).get("zone"),
        },
        "promised_eta": 1778748060,
        "rapid_eligibility_info": child_order.get("rapid_eligibility_info"),
        "created": child_order.get("created"),
        "order_lines": list(child_order.get("order_lines") or []),
    }


def _fixture_p1_msn_payload() -> dict[str, Any]:
    p1_path = _fixture_path("p1_msn.json")
    if p1_path.is_file():
        return json.loads(p1_path.read_text())
    return {}


def _fixture_p1_msn_for_order(order_id: str) -> dict[str, Any]:
    p1_path = _fixture_path("p1_msn.json", order_id=order_id)
    if p1_path.is_file():
        return json.loads(p1_path.read_text())
    return _fixture_p1_msn_payload()


def _fixture_case_bundle(oid: str) -> dict[str, Any]:
    """Named fixture case under tests/fixtures/order_rca/cases/<case>/."""
    child_order = _load_fixture("order.json", order_id=oid)
    child_id = (child_order.get("order_id") or "").strip().upper()
    parent_id = (child_order.get("parent_id") or "").strip().upper() or None
    allocation = _load_fixture("allocation.json", order_id=oid)

    if parent_id and oid == parent_id:
        order = _fixture_parent_order_record(parent_id, child_order, allocation)
        if not order:
            raise ValueError(f"No allocation fixture block for parent order {parent_id}")
        partial = {
            "order_id": oid,
            "order": order,
            "parent_id": None,
            "parent_order": None,
            "allocation": allocation,
        }
    else:
        order_id = child_id or oid
        parent_order = (
            _fixture_parent_order_record(parent_id, child_order, allocation) if parent_id else None
        )
        partial = {
            "order_id": order_id,
            "order": child_order,
            "parent_id": parent_id,
            "parent_order": parent_order,
            "allocation": allocation,
        }
    if rules.is_ideal_bundle(partial):
        log.debug("order_rca fixture IDEAL order_id=%s (full collect)", oid)

    out = {
        **partial,
        "status": _load_fixture("status.json", order_id=oid),
        "parent_status": _load_fixture("status_parent.json", order_id=oid) if parent_id else None,
        "history": _load_fixture("history.json", order_id=oid),
        "groot": _fixture_groot_payload("groot.json", order_id=oid),
        "p1_msn": _fixture_p1_msn_for_order(oid),
        "collect_mode": "full",
    }
    out = _attach_fixture_analytics(out, oid)
    out = _attach_fixture_payment_details(out, oid)
    return _attach_fixture_soft_allocation(_attach_fixture_clickpost(out, order_id=oid))


async def _fetch_fixtures(oid: str) -> dict[str, Any]:
    oid = oid.strip().upper()
    if _fixture_case_key(oid):
        return _fixture_case_bundle(oid)

    child_order = _load_fixture("order_child.json")
    child_id = (child_order.get("order_id") or "").strip().upper()
    parent_id = (child_order.get("parent_id") or "").strip().upper() or None
    allocation = _load_fixture("allocation.json")

    if oid == child_id:
        order = child_order
        parent_order = _fixture_parent_order_record(parent_id, child_order, allocation) if parent_id else None
        partial = {
            "order_id": oid,
            "order": order,
            "parent_id": parent_id,
            "parent_order": parent_order,
            "allocation": allocation,
        }
        if rules.is_ideal_bundle(partial):
            log.debug("order_rca fixture IDEAL child order_id=%s (full collect)", oid)

        out = {
            **partial,
            "status": _load_fixture("status_child.json"),
            "parent_status": _load_fixture("status_parent.json") if parent_id else None,
            "history": _load_fixture("history_child.json"),
            "groot": _fixture_groot_payload("groot_child.json"),
            "p1_msn": _fixture_p1_msn_payload(),
            "collect_mode": "full",
        }
        out = _attach_fixture_analytics(out, oid)
        out = _attach_fixture_payment_details(out, oid)
        return _attach_fixture_soft_allocation(_attach_fixture_clickpost(out, order_id=oid))

    if parent_id and oid == parent_id:
        order = _fixture_parent_order_record(parent_id, child_order, allocation)
        if not order:
            raise ValueError(f"No allocation fixture block for parent order {parent_id}")
        partial = {
            "order_id": oid,
            "order": order,
            "parent_id": None,
            "parent_order": None,
            "allocation": allocation,
        }
        if rules.is_ideal_bundle(partial):
            log.debug("order_rca fixture IDEAL parent order_id=%s (full collect)", oid)

        out = {
            **partial,
            "status": _load_fixture("status_parent.json"),
            "parent_status": None,
            "history": {},
            "groot": _fixture_groot_payload("groot_parent.json"),
            "p1_msn": _fixture_p1_msn_payload(),
            "collect_mode": "full",
        }
        out = _attach_fixture_analytics(out, oid)
        out = _attach_fixture_payment_details(out, oid)
        return _attach_fixture_soft_allocation(_attach_fixture_clickpost(out, order_id=oid))

    known = sorted({child_id, parent_id, *FIXTURE_CASE_BY_ORDER_ID.keys()} - {None})
    allowed = ", ".join(known)
    raise ValueError(
        f"Fixture mode supports: {allowed}. Got {oid}. "
        "Add a case under tests/fixtures/order_rca/cases/<name>/ and register in FIXTURE_CASE_BY_ORDER_ID."
    )
