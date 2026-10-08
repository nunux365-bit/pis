"""Live SAP S/4 PR — OData V2.

- **GET (YUNB/YAST):** ``API_PURCHASEREQ_PROCESS_SRV`` via :func:`get_pr` (deferred acct in :mod:`sap_odata_deferred`).
- **GET (YSER):** ``Z_PURCHASE_REQUISITION_SRV`` via :func:`~app.procurement.sap_pr_z_client.get_yser_pr`.
- **YSER create/update:** ``Z_PURCHASE_REQUISITION_SRV`` (``PRHeaderSet`` collection POST).
- **YUNB / YAST create & update:** standard ``API_PURCHASEREQ_PROCESS_SRV``.

The FastAPI service awaits :func:`create_pr` / :func:`update_pr` / :func:`get_pr`.
Sync wrappers exist for CLI scripts only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Coroutine
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx

from app.config.settings import settings
from app.procurement.sap_pr_payload import (
    build_pr_acct_delete_patch,
    build_pr_item_delete_patch,
    build_pr_payload,
    build_pr_resubmit_plan,
    is_placeholder_pr_number,
    legacy_sap_ticket_ref_tag,
    parse_pr_items_from_read,
    parse_pr_number_from_response,
    pick_recovery_document_by_field,
    pick_recovery_document_by_tag,
    pr_acct_assgmt_entity_url,
    pr_collection_url,
    pr_entity_url,
    pr_acct_collection_post_url,
    pr_item_collection_post_url,
    pr_item_entity_url,
    pr_read_full_url,
    normalize_pr_item_number,
    sap_ticket_ref_tag,
    verify_pr_read_against_form,
    sap_service_root_url,
)

log = logging.getLogger(__name__)

from app.procurement.sap_config import SAP_PR_READ_ATTEMPTS, SAP_PR_READ_RETRY_DELAY_S
from app.procurement.sap_odata_utils import odata_norm
from app.procurement.sap_read_retry import (
    should_retry_sap_odata_read_error,
    sleep_before_sap_read_retry,
)

T = TypeVar("T")

_HTTP_STATUS_MESSAGES: dict[int, str] = {
    400: "Bad Request — method not supported or invalid payload",
    401: "Unauthorized — check SAP credentials",
    403: "Forbidden — check CSRF token or service authorization",
    405: "Method not allowed",
    406: "Not acceptable — missing or invalid parameters",
    500: "SAP internal server error",
}

_CSRF_FETCH_SENTINEL = "fetch"


def sap_pr_configured() -> bool:
    base = (settings.procurement_sap_base_url or "").strip()
    user = (settings.procurement_sap_username or "").strip()
    password = settings.procurement_sap_password or ""
    return bool(base and user and password)


def _sap_client() -> str:
    return str(settings.procurement_sap_client or "200").strip()


def _sap_language() -> str:
    return str(settings.procurement_sap_language or "EN").strip().upper()


def sap_gateway_query_params() -> dict[str, str]:
    return {"sap-client": _sap_client(), "sap-language": _sap_language()}


def _initial_sap_usercontext_cookie() -> str:
    return f"sap-usercontext=sap-client={_sap_client()}"


def sap_csrf_fetch_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "sap-client": _sap_client(),
        "sap-language": _sap_language(),
        "X-CSRF-Token": "Fetch",
        "Cookie": _initial_sap_usercontext_cookie(),
    }


def sap_json_headers(*, csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "sap-client": _sap_client(),
        "sap-language": _sap_language(),
        "X-CSRF-Token": csrf_token,
    }


def sap_post_create_headers(*, csrf_token: str) -> dict[str, str]:
    return {
        **sap_json_headers(csrf_token=csrf_token),
        "Content-Type": "application/json",
    }


def sap_post_update_headers(*, csrf_token: str, pr_number: str) -> dict[str, str]:
    """Z YSER update: plain POST with ``PRNumber`` in HTTP headers (key also in entity URL)."""
    pr_key = str(pr_number or "").strip()
    if not pr_key:
        raise ValueError("PR number is required for Z update POST headers")
    return {
        **sap_post_create_headers(csrf_token=csrf_token),
        "PRNumber": pr_key,
    }


def sap_patch_headers(*, csrf_token: str) -> dict[str, str]:
    return {
        **sap_json_headers(csrf_token=csrf_token),
        "Content-Type": "application/json",
    }


def _acct_patches_are_qty_only(acct_patches: list[tuple[str, dict[str, Any]]]) -> bool:
    allowed = {"Quantity", "PurchaseOrderQuantityUnit", "BaseUnit"}
    return bool(acct_patches) and all(
        isinstance(body, dict) and set(body.keys()) <= allowed for _, body in acct_patches
    )


def _sap_qty_equal_loose(left: str, right: str) -> bool:
    a = (left or "").strip()
    b = (right or "").strip()
    if not a or not b:
        return a == b
    if a == b:
        return True
    try:
        return float(a.replace(",", ".")) == float(b.replace(",", "."))
    except ValueError:
        return False


# SAP item key -> snapshot attribute (qty-loose / date special-cased).
_PR_ITEM_FIELD_SNAP: dict[str, str] = {
    "Material": "material",
    "ServicePerformer": "service_performer",
    "PurchaseRequisitionItemText": "item_text",
    "Plant": "plant",
    "StorageLocation": "storage_location",
    "PurchaseRequisitionPrice": "unit_price",
    "MaterialGroup": "material_group",
    "BaseUnit": "base_unit",
    "PurchasingGroup": "purchasing_group",
    "PurchasingOrganization": "purchasing_organization",
    "CompanyCode": "company_code",
    "FixedSupplier": "fixed_supplier",
    "DeliveryDate": "delivery_date",
}

# Keys always resent on item body that are structural / not form deltas.
_PR_ITEM_COMPARE_IGNORE: frozenset[str] = frozenset(
    {
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
        "PurchaseRequisitionType",
        "ProductType",
        "AccountAssignmentCategory",
        "PurchasingDocumentItemCategory",
        "MultipleAcctAssgmtDistribution",
        "PurReqnExternalSystemId",
        "RequestedQuantity",
    }
)

_PR_ITEM_QTY_COMPARE_KEYS: frozenset[str] = frozenset(
    {"PurchaseRequisitionPrice", "RequestedQuantity"}
)


def _norm_item_patch_val(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip()


def _item_patch_fields_differ(
    snapshot: Any,
    patch_body: dict[str, Any],
    *,
    field_map: dict[str, str],
    ignore_keys: frozenset[str],
    qty_keys: frozenset[str],
    date_keys: frozenset[str],
) -> bool:
    """True when any outbound item field differs from SAP (or cannot be compared).

    Unknown non-empty keys force a PATCH so we never drop fields that used to be
    sent on the full-item update path.
    """
    from app.procurement.sap_odata_utils import odata_date_to_form, sanitize_form_delivery_date

    for key, val in patch_body.items():
        if key in ignore_keys or str(key).startswith("to_"):
            continue
        desired = _norm_item_patch_val(val)
        if key not in field_map:
            if desired:
                return True
            continue
        snap_val = _norm_item_patch_val(getattr(snapshot, field_map[key], ""))
        if key in date_keys:
            if not desired:
                continue
            desired_dd = sanitize_form_delivery_date(
                odata_date_to_form(val) or desired
            )
            snap_dd = sanitize_form_delivery_date(snap_val)
            if desired_dd and snap_dd and desired_dd != snap_dd:
                return True
            if desired_dd and not snap_dd:
                return True
            continue
        if key in qty_keys:
            if desired and (not snap_val or not _sap_qty_equal_loose(desired, snap_val)):
                return True
            continue
        if desired and desired != snap_val:
            return True
        if not desired and snap_val:
            # Explicit clear / empty — still a change when SAP has a value.
            continue
    return False


def _pr_item_rest_needs_patch(snapshot: Any, rest: dict[str, Any]) -> bool:
    """True when non-qty item fields in ``rest`` differ from the SAP snapshot."""
    return _item_patch_fields_differ(
        snapshot,
        rest,
        field_map=_PR_ITEM_FIELD_SNAP,
        ignore_keys=_PR_ITEM_COMPARE_IGNORE,
        qty_keys=_PR_ITEM_QTY_COMPARE_KEYS,
        date_keys=frozenset({"DeliveryDate"}),
    )


async def sap_odata_batch_patch(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    csrf_service_root: str,
    patches: list[tuple[str, dict[str, Any]]],
) -> str | None:
    """Apply entity PATCHes in one OData ``$batch`` changeset (atomic).

    Needed for multi-account-assignment qty changes (and item qty + acct qty together)
    so interim sums never violate SAP's sum(acct qty) == item qty rule.
    """
    if not patches:
        return None
    csrf, csrf_err = await _fetch_csrf(
        client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_service_root
    )
    if csrf_err or not csrf:
        return csrf_err or "SAP CSRF fetch failed"

    boundary = f"batch_{uuid.uuid4().hex}"
    changeset = f"changeset_{uuid.uuid4().hex}"
    parts: list[str] = [
        f"--{boundary}\r\nContent-Type: multipart/mixed; boundary={changeset}\r\n\r\n"
    ]
    for idx, (url, body) in enumerate(patches, start=1):
        path = urlparse(url).path
        parts.append(
            f"--{changeset}\r\n"
            f"Content-Type: application/http\r\n"
            f"Content-Transfer-Encoding: binary\r\n"
            f"Content-ID: {idx}\r\n\r\n"
            f"PATCH {path} HTTP/1.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Accept: application/json\r\n\r\n"
            f"{json.dumps(body)}\r\n"
        )
    parts.append(f"--{changeset}--\r\n--{boundary}--\r\n")
    batch_url = f"{csrf_service_root.rstrip('/')}/$batch"
    resp = await client.post(
        batch_url,
        content="".join(parts).encode(),
        headers={
            "Content-Type": f"multipart/mixed;boundary={boundary}",
            "Accept": "multipart/mixed",
            "x-csrf-token": csrf,
        },
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    if re.search(r"HTTP/1\.1 [45]\d{2}\b", resp.text or ""):
        return "SAP $batch changeset failed: " + (resp.text or "")[:400]
    return None


def _credentials_or_error() -> tuple[tuple[str, str, str] | None, str | None]:
    base = (settings.procurement_sap_base_url or "").strip()
    user = (settings.procurement_sap_username or "").strip()
    password = settings.procurement_sap_password or ""
    if not base:
        return None, "SAP base URL not configured"
    if not user or not password:
        return None, "SAP credentials not configured (username/password)"
    return (base, user, password), None


def _httpx_timeout() -> httpx.Timeout:
    return httpx.Timeout(float(settings.procurement_sap_timeout_seconds or 120.0))


def _sap_error_message(resp: httpx.Response) -> str:
    code = resp.status_code
    base = _HTTP_STATUS_MESSAGES.get(code, f"SAP HTTP {code}")
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "").strip()
        if text:
            return f"{base}: {text[:500]}"
        return base
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, dict):
                val = msg.get("value")
                if val:
                    base = f"{base}: {val}"
            elif isinstance(msg, str) and msg.strip():
                base = f"{base}: {msg.strip()}"
            inner = err.get("innererror")
            if isinstance(inner, dict):
                details = inner.get("errordetails")
                if isinstance(details, list):
                    extra = [
                        str(d.get("message", "")).strip()
                        for d in details
                        if isinstance(d, dict) and str(d.get("message", "")).strip()
                    ]
                    if extra:
                        base = f"{base} ({'; '.join(extra[:3])})"
    text = (resp.text or "").strip()
    return f"{base}: {text[:500]}" if text and ":" not in base[10:] else base


def _is_csrf_validation_failure(resp: httpx.Response) -> bool:
    if resp.status_code != 403:
        return False
    try:
        data = resp.json()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if not isinstance(err, dict):
        return False
    msg = err.get("message")
    text = ""
    if isinstance(msg, dict):
        text = str(msg.get("value") or "")
    elif isinstance(msg, str):
        text = msg
    low = text.lower()
    return "csrf" in low or "token validation" in low


def _extract_csrf_token(resp: httpx.Response) -> str | None:
    raw = (resp.headers.get("x-csrf-token") or resp.headers.get("X-CSRF-Token") or "").strip()
    if not raw or raw.lower() == _CSRF_FETCH_SENTINEL:
        return None
    return raw


async def _fetch_csrf(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> tuple[str | None, str | None]:
    url = csrf_service_root or sap_service_root_url(base)
    params = sap_gateway_query_params()
    try:
        resp = await client.get(url, params=params, headers=sap_csrf_fetch_headers())
    except httpx.TimeoutException:
        log.warning("SAP CSRF fetch timeout ticket=%s url=%s", ticket_id, url)
        return None, "SAP CSRF fetch timed out"
    except httpx.RequestError as e:
        log.warning("SAP CSRF fetch network error ticket=%s: %s", ticket_id, e)
        return None, f"SAP CSRF fetch connection error: {e}"

    if resp.status_code >= 400:
        return None, _sap_error_message(resp)

    csrf = _extract_csrf_token(resp)
    if not csrf:
        return None, "SAP CSRF fetch did not return x-csrf-token"

    log.info("SAP CSRF ok ticket=%s csrf_len=%s", ticket_id, len(csrf))
    return csrf, None


async def _fetch_csrf_refresh(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> tuple[str | None, str | None]:
    log.info("SAP CSRF refresh ticket=%s", ticket_id)
    return await _fetch_csrf(
        client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_service_root
    )


async def _request_with_csrf_retry(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    method: str,
    url: str,
    json_body: dict[str, Any] | None,
    headers_builder,
    csrf_service_root: str | None = None,
) -> httpx.Response:
    csrf, err = await _fetch_csrf(
        client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_service_root
    )
    if err:
        raise _SapRequestError(err)

    hdrs = headers_builder(csrf_token=csrf)
    async def _do() -> httpx.Response:
        if method == "POST":
            return await client.post(
                url, params=sap_gateway_query_params(), json=json_body, headers=hdrs
            )
        if method == "PATCH":
            return await client.patch(
                url, params=sap_gateway_query_params(), json=json_body, headers=hdrs
            )
        if method == "DELETE":
            return await client.delete(url, params=sap_gateway_query_params(), headers=hdrs)
        return await client.get(url, params=sap_gateway_query_params(), headers=hdrs)

    resp = await _do()
    if _is_csrf_validation_failure(resp):
        csrf, err = await _fetch_csrf_refresh(
            client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_service_root
        )
        if err:
            raise _SapRequestError(err)
        hdrs = headers_builder(csrf_token=csrf)
        resp = await _do()
    return resp


class _SapRequestError(Exception):
    pass


async def _sap_create_pr(
    *, payload: dict[str, Any], ticket_id: str
) -> tuple[str | None, str | None]:
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    url = pr_collection_url(base)

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            resp = await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=ticket_id,
                method="POST",
                url=url,
                json_body=payload,
                headers_builder=sap_post_create_headers,
            )
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP request timed out"
    except httpx.RequestError as e:
        return None, f"SAP connection error: {e}"

    if resp.status_code >= 400:
        return None, _sap_error_message(resp)

    parsed = None
    try:
        parsed = parse_pr_number_from_response(resp.json())
    except Exception:
        log.exception("SAP PR create response parse failed ticket=%s", ticket_id)

    out = parsed or _norm(payload.get("PurchaseRequisition"))
    if not out:
        return None, "SAP succeeded but PR number missing in response"
    if is_placeholder_pr_number(out):
        return None, f"SAP returned placeholder PR number {out!r} — check item master data / validation"

    log.info("SAP PR create ok ticket=%s pr_number=%s", ticket_id, out)
    return out, None


async def _sap_get_pr_body(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    from app.procurement.sap_odata_deferred import resolve_pr_read_body

    read_url = pr_read_full_url(base, pr_number)
    csrf_root = sap_service_root_url(base)
    last_err: str | None = None
    for attempt in range(SAP_PR_READ_ATTEMPTS):
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=ticket_id,
            method="GET",
            url=read_url,
            json_body=None,
            headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
            csrf_service_root=csrf_root,
        )
        if resp.status_code >= 400:
            last_err = _sap_error_message(resp)
            if (
                attempt + 1 < SAP_PR_READ_ATTEMPTS
                and should_retry_sap_odata_read_error(last_err, status_code=resp.status_code)
            ):
                log.warning(
                    "SAP PR read retry ticket=%s attempt=%s pr=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    pr_number,
                    last_err,
                )
                await sleep_before_sap_read_retry(SAP_PR_READ_RETRY_DELAY_S)
                continue
            return None, last_err
        try:
            data = resp.json()
        except Exception:
            return None, "SAP GET returned non-JSON body"
        if not isinstance(data, dict):
            return None, "SAP GET returned unexpected body"
        try:
            data = await resolve_pr_read_body(
                client,
                base=base,
                body=data,
                pr_number=pr_number,
                ticket_id=ticket_id,
                csrf_service_root=csrf_root,
            )
        except _SapRequestError as e:
            last_err = str(e)
            if (
                attempt + 1 < SAP_PR_READ_ATTEMPTS
                and should_retry_sap_odata_read_error(last_err)
            ):
                log.warning(
                    "SAP PR deferred resolve retry ticket=%s attempt=%s pr=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    pr_number,
                    last_err,
                )
                await sleep_before_sap_read_retry(SAP_PR_READ_RETRY_DELAY_S)
                continue
            return None, last_err
        return data, None
    return None, last_err or "SAP GET failed"


async def _sap_mark_item_deleted(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    item_number: str,
    ticket_id: str,
) -> str | None:
    """Mark line deleted: HTTP DELETE if supported, else PATCH ``IsDeleted=X`` (PR API)."""
    item_url = pr_item_entity_url(base, pr_number=pr_number, item_number=item_number)
    del_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="DELETE",
        url=item_url,
        json_body=None,
        headers_builder=sap_patch_headers,
    )
    if del_resp.status_code in (200, 204):
        return None
    if del_resp.status_code not in (400, 403, 404, 405):
        return _sap_error_message(del_resp)

    patch_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="PATCH",
        url=item_url,
        json_body=build_pr_item_delete_patch(),
        headers_builder=sap_patch_headers,
    )
    if patch_resp.status_code >= 400:
        return _sap_error_message(patch_resp)
    return None


async def _sap_create_pr_item(
    client: httpx.AsyncClient,
    *,
    base: str,
    item_body: dict[str, Any],
    ticket_id: str,
) -> str | None:
    url = pr_item_collection_post_url(base)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=url,
        json_body=item_body,
        headers_builder=sap_post_create_headers,
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


async def _sap_mark_acct_deleted(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    item_number: str,
    acct_assgmt_number: str,
    ticket_id: str,
) -> str | None:
    """Remove account assignment: DELETE if supported, else PATCH ``IsDeleted=X``."""
    acct_url = pr_acct_assgmt_entity_url(
        base,
        pr_number=pr_number,
        item_number=item_number,
        acct_assgmt_number=acct_assgmt_number,
    )
    del_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="DELETE",
        url=acct_url,
        json_body=None,
        headers_builder=sap_patch_headers,
    )
    if del_resp.status_code in (200, 204):
        return None
    if del_resp.status_code not in (400, 403, 404, 405):
        return _sap_error_message(del_resp)

    patch_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="PATCH",
        url=acct_url,
        json_body=build_pr_acct_delete_patch(),
        headers_builder=sap_patch_headers,
    )
    if patch_resp.status_code >= 400:
        return _sap_error_message(patch_resp)
    return None


async def _sap_create_pr_acct_assgmt(
    client: httpx.AsyncClient,
    *,
    base: str,
    acct_body: dict[str, Any],
    ticket_id: str,
) -> str | None:
    url = pr_acct_collection_post_url(base)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=url,
        json_body=acct_body,
        headers_builder=sap_post_create_headers,
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


_RECOVERY_PAGE_SIZE = 100
_RECOVERY_MAX_PAGES = 30
# When SAP ignores ``$top``/``$skip`` and returns the full collection in one response.
_RECOVERY_BULK_DUMP_MIN_ROWS = _RECOVERY_PAGE_SIZE + 1


def _recovery_first_doc_no(results: list[Any], *, number_key: str) -> str:
    from app.procurement.sap_pr_payload import _odata_result_props

    for row in results:
        if not isinstance(row, dict):
            continue
        props = _odata_result_props(row)
        val = odata_norm(props.get(number_key))
        if val:
            return val
    return ""


async def _fetch_recovery_results_page(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    collection_url: str,
    number_key: str,
    field_key: str,
    text_keys: tuple[str, ...],
    orderby: str,
    page: int,
    csrf_service_root: str | None = None,
) -> list[dict[str, Any]] | None:
    select_parts = [number_key]
    if field_key:
        select_parts.append(field_key)
    else:
        select_parts.extend(text_keys)
    select = ",".join(dict.fromkeys(select_parts))
    skip = page * _RECOVERY_PAGE_SIZE
    url = (
        f"{collection_url}?$orderby={orderby}"
        f"&$top={_RECOVERY_PAGE_SIZE}&$skip={skip}&$select={select}"
    )
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-recover-p{page}",
        method="GET",
        url=url,
        json_body=None,
        headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
        csrf_service_root=csrf_service_root,
    )
    if resp.status_code >= 400:
        log.warning(
            "SAP recovery page failed ticket=%s page=%s status=%s",
            ticket_id,
            page,
            resp.status_code,
        )
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    root_node = data.get("d") if isinstance(data.get("d"), dict) else data
    results = root_node.get("results") if isinstance(root_node, dict) else None
    if not isinstance(results, list) or not results:
        return None
    return [x for x in results if isinstance(x, dict)]


async def _paginate_recovery_scan(
    client: httpx.AsyncClient,
    *,
    base: str,
    ticket_id: str,
    collection_url: str,
    number_key: str,
    text_keys: tuple[str, ...] = (),
    field_key: str = "",
    orderby: str,
    csrf_service_root: str | None = None,
) -> str | None:
    """Scan OData pages; verify tag client-side.

    Stops after the first page when SAP ignores ``$top``/``$skip`` and returns the
  full collection (QAS gateway behaviour), or when a subsequent page repeats page 0.
    """
    prev_first_doc = ""
    for page in range(_RECOVERY_MAX_PAGES):
        results = await _fetch_recovery_results_page(
            client,
            base=base,
            ticket_id=ticket_id,
            collection_url=collection_url,
            number_key=number_key,
            field_key=field_key,
            text_keys=text_keys,
            orderby=orderby,
            page=page,
            csrf_service_root=csrf_service_root,
        )
        if not results:
            return None
        if field_key:
            doc_no = pick_recovery_document_by_field(
                results,
                number_key=number_key,
                field_key=field_key,
                ticket_id=ticket_id,
            )
        else:
            doc_no = pick_recovery_document_by_tag(
                results,
                ticket_id=ticket_id,
                number_key=number_key,
                text_keys=text_keys,
            )
        if doc_no:
            return doc_no
        first_doc = _recovery_first_doc_no(results, number_key=number_key)
        if page > 0 and first_doc and first_doc == prev_first_doc:
            log.info(
                "SAP recovery: duplicate page %s ticket=%s (skip ignored); stop scan",
                page,
                ticket_id,
            )
            return None
        if first_doc:
            prev_first_doc = first_doc
        if len(results) >= _RECOVERY_BULK_DUMP_MIN_ROWS:
            log.info(
                "SAP recovery: bulk collection (%s rows) on page %s ticket=%s; stop paging",
                len(results),
                page,
                ticket_id,
            )
            return None
        if len(results) < _RECOVERY_PAGE_SIZE:
            return None
    log.warning(
        "SAP recovery: no tag match in %s pages ticket=%s",
        _RECOVERY_MAX_PAGES,
        ticket_id,
    )
    return None


async def _try_recover_pr_number(
    client: httpx.AsyncClient, *, base: str, ticket_id: str
) -> tuple[str | None, str | None]:
    """Item collection: external id then legacy item text (one GET when SAP bulk-dumps)."""
    from app.procurement.sap_pr_payload import pr_collection_url, pr_item_collection_url

    item_url = pr_item_collection_url(base)
    orderby = "PurchaseRequisition desc"
    results = await _fetch_recovery_results_page(
        client,
        base=base,
        ticket_id=f"{ticket_id}-recover-items",
        collection_url=item_url,
        number_key="PurchaseRequisition",
        field_key="PurReqnExternalSystemId",
        text_keys=("PurchaseRequisitionItemText",),
        orderby=orderby,
        page=0,
    )
    if results:
        pr_no = pick_recovery_document_by_field(
            results,
            number_key="PurchaseRequisition",
            field_key="PurReqnExternalSystemId",
            ticket_id=ticket_id,
        )
        if pr_no:
            log.info(
                "SAP PR recovered via PurReqnExternalSystemId ticket=%s pr_number=%s",
                ticket_id,
                pr_no,
            )
            return pr_no, None
        pr_no = pick_recovery_document_by_tag(
            results,
            ticket_id=ticket_id,
            number_key="PurchaseRequisition",
            text_keys=("PurchaseRequisitionItemText",),
        )
        if pr_no:
            log.info(
                "SAP PR recovered via item text ticket=%s pr_number=%s",
                ticket_id,
                pr_no,
            )
            return pr_no, None

    pr_no = await _paginate_recovery_scan(
        client,
        base=base,
        ticket_id=ticket_id,
        collection_url=pr_collection_url(base),
        number_key="PurchaseRequisition",
        text_keys=("PurReqnDescription",),
        orderby=orderby,
    )
    if pr_no:
        log.info(
            "SAP PR recovered via header description ticket=%s pr_number=%s",
            ticket_id,
            pr_no,
        )
        return pr_no, None
    return None, None


async def _sap_update_pr(
    *,
    pr_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
) -> tuple[str | None, str | None]:
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            before, err = await _sap_get_pr_body(
                client, base=base, pr_number=pr_number, ticket_id=ticket_id
            )
            if err:
                return None, err
            assert before is not None
            _, existing_items = parse_pr_items_from_read(before)
            plan = build_pr_resubmit_plan(
                form=form,
                document_type=document_type,
                pr_number=pr_number,
                existing_items=existing_items,
                ticket_id=ticket_id,
            )

            create_item_nos = {
                _norm(x.get("PurchaseRequisitionItem")) for x in plan.items_to_create
            }
            create_item_nos.discard("")

            # Item lines before header PATCH — write line-1 trace before clearing legacy header tag.
            for item_body in plan.items_to_create:
                err_post = await _sap_create_pr_item(
                    client, base=base, item_body=item_body, ticket_id=ticket_id
                )
                if err_post:
                    item_no = _norm(item_body.get("PurchaseRequisitionItem"))
                    return None, f"Create item {item_no}: {err_post}"

            for item_no, body, acct_patches, acct_creates, acct_deletes in plan.item_patches:
                if item_no in plan.items_to_mark_deleted or item_no in create_item_nos:
                    continue
                item_url = pr_item_entity_url(base, pr_number=pr_number, item_number=item_no)
                snapshot = next(
                    (
                        s
                        for s in existing_items
                        if normalize_pr_item_number(s.item_number) == item_no
                        and not s.is_deleted
                    ),
                    None,
                )
                dt = (document_type or "").upper()
                desired_rq = _norm(body.get("RequestedQuantity"))
                rq_changed = bool(
                    snapshot
                    and desired_rq
                    and snapshot.requested_quantity
                    and not _sap_qty_equal_loose(desired_rq, snapshot.requested_quantity)
                )
                # YAST multi-asset: item qty + acct qtys must change atomically.
                use_qty_batch = (
                    dt == "YAST"
                    and _acct_patches_are_qty_only(acct_patches)
                    and not acct_creates
                    and not acct_deletes
                    and (
                        len(acct_patches) > 1
                        or (rq_changed and bool(acct_patches))
                    )
                )
                if use_qty_batch:
                    batch_ops: list[tuple[str, dict[str, Any]]] = []
                    if rq_changed and desired_rq:
                        batch_ops.append((item_url, {"RequestedQuantity": desired_rq}))
                    for acct_no, acct_body in acct_patches:
                        batch_ops.append(
                            (
                                pr_acct_assgmt_entity_url(
                                    base,
                                    pr_number=pr_number,
                                    item_number=item_no,
                                    acct_assgmt_number=acct_no,
                                ),
                                acct_body,
                            )
                        )
                    batch_err = await sap_odata_batch_patch(
                        client,
                        base=base,
                        ticket_id=f"{ticket_id}-acct-batch-{item_no}",
                        csrf_service_root=sap_service_root_url(base),
                        patches=batch_ops,
                    )
                    if batch_err:
                        return None, batch_err
                    # Other item fields (material / text / group / …) when they
                    # differ — omit RQ (already applied in $batch). Prefer
                    # full rest PATCH when snapshot missing so we never drop
                    # fields the pre-batch path always sent.
                    rest = {
                        k: v
                        for k, v in body.items()
                        if k
                        not in (
                            "PurchaseRequisition",
                            "PurchaseRequisitionItem",
                            "RequestedQuantity",
                            "to_PurchaseReqnAcctAssgmt",
                        )
                        and not str(k).startswith("to_")
                    }
                    if rest and (
                        snapshot is None or _pr_item_rest_needs_patch(snapshot, rest)
                    ):
                        resp = await _request_with_csrf_retry(
                            client,
                            base=base,
                            ticket_id=ticket_id,
                            method="PATCH",
                            url=item_url,
                            json_body=rest,
                            headers_builder=sap_patch_headers,
                        )
                        if resp.status_code >= 400:
                            return None, _sap_error_message(resp)
                    continue

                resp = await _request_with_csrf_retry(
                    client,
                    base=base,
                    ticket_id=ticket_id,
                    method="PATCH",
                    url=item_url,
                    json_body=body,
                    headers_builder=sap_patch_headers,
                )
                if resp.status_code >= 400:
                    return None, _sap_error_message(resp)
                for acct_no, acct_body in acct_patches:
                    acct_url = pr_acct_assgmt_entity_url(
                        base,
                        pr_number=pr_number,
                        item_number=item_no,
                        acct_assgmt_number=acct_no,
                    )
                    resp = await _request_with_csrf_retry(
                        client,
                        base=base,
                        ticket_id=ticket_id,
                        method="PATCH",
                        url=acct_url,
                        json_body=acct_body,
                        headers_builder=sap_patch_headers,
                    )
                    if resp.status_code >= 400:
                        return None, _sap_error_message(resp)
                for acct_body in acct_creates:
                    err_acct = await _sap_create_pr_acct_assgmt(
                        client, base=base, acct_body=acct_body, ticket_id=ticket_id
                    )
                    if err_acct:
                        seq = _norm(acct_body.get("PurchaseReqnAcctAssgmtNumber"))
                        return None, f"Create acct {item_no}/{seq}: {err_acct}"
                for acct_no in acct_deletes:
                    err_del_acct = await _sap_mark_acct_deleted(
                        client,
                        base=base,
                        pr_number=pr_number,
                        item_number=item_no,
                        acct_assgmt_number=acct_no,
                        ticket_id=ticket_id,
                    )
                    if err_del_acct:
                        return None, f"Delete acct {item_no}/{acct_no}: {err_del_acct}"

            header_url = pr_entity_url(base, pr_number)
            resp = await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=ticket_id,
                method="PATCH",
                url=header_url,
                json_body=plan.header_patch,
                headers_builder=sap_patch_headers,
            )
            if resp.status_code >= 400:
                return None, _sap_error_message(resp)

            for item_no in plan.items_to_mark_deleted:
                err_del = await _sap_mark_item_deleted(
                    client,
                    base=base,
                    pr_number=pr_number,
                    item_number=item_no,
                    ticket_id=ticket_id,
                )
                if err_del:
                    return None, f"Delete item {item_no}: {err_del}"

            after, err = await _sap_get_pr_body(
                client, base=base, pr_number=pr_number, ticket_id=f"{ticket_id}-verify"
            )
            if err:
                return None, f"Post-update read failed: {err}"
            assert after is not None
            mismatches = verify_pr_read_against_form(
                after, form=form, document_type=document_type, ticket_id=ticket_id
            )
            if mismatches:
                return None, "SAP verify after resubmit: " + "; ".join(mismatches[:5])
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP request timed out"
    except httpx.RequestError as e:
        return None, f"SAP connection error: {e}"

    log.info(
        "SAP PR update ok ticket=%s pr_number=%s patch=%s delete=%s create=%s",
        ticket_id,
        pr_number,
        len(plan.item_patches),
        len(plan.items_to_mark_deleted),
        len(plan.items_to_create),
    )
    return pr_number, None


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def _run_sync(coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Use async create_pr/update_pr/get_pr from an async context, not *_sync")


async def try_recover_pr(
    *, ticket_id: str, document_type: str | None = None
) -> tuple[str | None, str | None]:
    from app.procurement import sap_pr_z_client

    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    dt = (document_type or "").upper()
    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            if dt == "YSER":
                return await sap_pr_z_client.try_recover_yser_pr(
                    client, base=base, ticket_id=ticket_id
                )
            recovered, rec_err = await _try_recover_pr_number(
                client, base=base, ticket_id=ticket_id
            )
            if recovered:
                return recovered, None
            return None, rec_err
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP recovery search timed out"
    except httpx.RequestError as e:
        return None, f"SAP recovery connection error: {e}"


async def create_pr(
    *, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    if (document_type or "").upper() == "YSER":
        from app.procurement.sap_pr_z_client import create_yser_pr

        return await create_yser_pr(
            ticket_id=ticket_id, form=form, document_type=document_type
        )
    try:
        payload = build_pr_payload(
            form=form, document_type=document_type, pr_number="", ticket_id=ticket_id
        )
    except ValueError as e:
        return None, str(e)
    return await _sap_create_pr(payload=payload, ticket_id=ticket_id)


async def _yser_skip_z_update_after_deletes(
    *,
    pr_number: str,
    form: dict[str, Any],
    ticket_id: str,
    document_type: str,
) -> tuple[bool, str | None]:
    """True when the whole Z item was marked deleted (no service lines left in form)."""
    from app.procurement.sap_pr_z_payload import yser_effective_line_blocks

    _ = (pr_number, ticket_id, document_type)
    skip = not yser_effective_line_blocks(form)
    return skip, None


async def update_pr(
    *, sap_id: str, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    pr_number = _norm(sap_id)
    if not pr_number:
        return None, "Missing SAP PR number for update"
    if is_placeholder_pr_number(pr_number):
        return None, f"Cannot update placeholder PR number {pr_number!r}"
    if (document_type or "").upper() == "YSER":
        from app.procurement.sap_pr_z_client import (
            apply_yser_z_line_deletes,
            update_yser_pr,
        )

        del_err = await apply_yser_z_line_deletes(
            sap_id=pr_number,
            form=form,
            ticket_id=ticket_id,
            document_type=document_type,
        )
        if del_err:
            return None, del_err
        # Line-delete-only resubmit: Z item ``IsDeleted=X`` is enough (skip Z field POST).
        skip_z, skip_err = await _yser_skip_z_update_after_deletes(
            pr_number=pr_number,
            form=form,
            ticket_id=ticket_id,
            document_type=document_type,
        )
        if skip_err:
            return None, skip_err
        if skip_z:
            return pr_number, None
        return await update_yser_pr(
            sap_id=pr_number,
            ticket_id=ticket_id,
            form=form,
            document_type=document_type,
        )
    try:
        build_pr_payload(form=form, document_type=document_type, pr_number=pr_number)
    except ValueError as e:
        return None, str(e)
    return await _sap_update_pr(
        pr_number=pr_number,
        form=form,
        document_type=document_type,
        ticket_id=ticket_id,
    )


async def _get_pr_standard(
    *, pr_number: str, ticket_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    pr_number = _norm(pr_number)
    if not pr_number:
        return None, "PR number is required"
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            data, err = await _sap_get_pr_body(
                client, base=base, pr_number=pr_number, ticket_id=ticket_id
            )
            if err:
                return None, err
            return data, None
    except httpx.TimeoutException:
        return None, "SAP GET timed out"
    except httpx.RequestError as e:
        return None, f"SAP GET connection error: {e}"


async def get_pr(
    *,
    pr_number: str,
    ticket_id: str = "read",
    document_type: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """GET PR — YSER via Z ``PRHeaderSet``; YUNB/YAST via standard OData."""
    if (document_type or "").upper() == "YSER":
        from app.procurement.sap_pr_z_client import get_yser_pr

        return await get_yser_pr(pr_number=pr_number, ticket_id=ticket_id)
    return await _get_pr_standard(pr_number=pr_number, ticket_id=ticket_id)


def create_pr_sync(
    *, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    return _run_sync(create_pr(ticket_id=ticket_id, form=form, document_type=document_type))


def update_pr_sync(
    *, sap_id: str, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    return _run_sync(
        update_pr(sap_id=sap_id, ticket_id=ticket_id, form=form, document_type=document_type)
    )


def get_pr_sync(*, pr_number: str, ticket_id: str = "read") -> tuple[dict[str, Any] | None, str | None]:
    return _run_sync(get_pr(pr_number=pr_number, ticket_id=ticket_id))
