"""YSER PR via SAP ``Z_PURCHASE_REQUISITION_SRV`` (``PRHeaderSet``).

**Update:** incremental flat ``POST`` bodies (Jul 2026 API) — one structural mutation per
request; field edits use per-service update posts with ``PRNumber`` + ``PurDocItemExternalReference``.
**Read:** positive item ``ValuationPrice``, else ``GrossPrice``; qty from ``DistrQuantity``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import httpx

from app.config.settings import settings
from app.procurement.sap_config import SAP_PR_READ_ATTEMPTS, SAP_PR_READ_RETRY_DELAY_S
from app.procurement.sap_odata_deferred import resolve_z_pr_read_body
from app.procurement.sap_read_retry import (
    should_retry_sap_odata_read_error,
    sleep_before_sap_read_retry,
)
from app.procurement.sap_pr_client import (
    _SapRequestError,
    _credentials_or_error,
    _httpx_timeout,
    _request_with_csrf_retry,
    _sap_error_message,
    sap_json_headers,
    sap_post_create_headers,
    sap_pr_configured,
    sap_service_root_url,
)
from app.procurement.sap_odata_utils import is_sap_deleted_flag as _is_sap_deleted_flag
from app.procurement.sap_odata_utils import odata_entity_properties as _odata_entity_properties
from app.procurement.sap_odata_utils import odata_results_list as _odata_results_list
from app.procurement.sap_odata_utils import odata_text as _odata_text
from app.procurement.sap_pr_payload import (
    SapPrItemSnapshot,
    is_placeholder_pr_number,
    normalize_pr_item_number,
)
from app.procurement.sap_pr_z_payload import (
    build_z_yser_item_delete_post_body,
    build_z_yser_pr_payload,
    form_from_z_pr_read,
    merge_yser_update_form_with_sap,
    parse_z_pr_number_from_response,
    verify_yser_read_matches_form,
    yser_effective_line_blocks,
    yser_resolved_form_item_numbers,
    yser_sap_layout_is_grouped,
    z_pr_collection_url,
    z_pr_entity_url,
    z_pr_item_entity_url,
    z_pr_read_url,
)
from app.procurement.sap_pr_z_payload import z_pr_item_collection_url, z_pr_service_root_url

log = logging.getLogger(__name__)

# Pause between incremental Z update POSTs so SAP can settle service-package state.
YSER_Z_UPDATE_POST_DELAY_S = 1.0


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


async def _z_get_pr_body(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Z ``PRHeaderSet`` GET with deferred ``to_Items`` / ``to_Services`` resolution."""
    try:
        url = z_pr_read_url(base, pr_number)
    except ValueError as e:
        return None, str(e)
    csrf_root = sap_service_root_url(base)
    last_err: str | None = None
    for attempt in range(SAP_PR_READ_ATTEMPTS):
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=ticket_id,
            method="GET",
            url=url,
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
                    "SAP Z PR read retry ticket=%s attempt=%s pr=%s err=%s",
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
            return None, "SAP Z GET returned non-JSON body"
        if not isinstance(data, dict):
            return None, "SAP Z GET returned unexpected body"
        try:
            data = await resolve_z_pr_read_body(
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
                    "SAP Z PR deferred resolve retry ticket=%s attempt=%s pr=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    pr_number,
                    last_err,
                )
                await sleep_before_sap_read_retry(SAP_PR_READ_RETRY_DELAY_S)
                continue
            return None, last_err
        return data, None
    return None, last_err or "SAP Z GET failed"


async def get_yser_pr(
    *, pr_number: str, ticket_id: str = "read"
) -> tuple[dict[str, Any] | None, str | None]:
    """Read YSER PR via Z ``PRHeaderSet`` (deferred items/services)."""
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
            return await _z_get_pr_body(
                client, base=base, pr_number=pr_number, ticket_id=ticket_id
            )
    except httpx.TimeoutException:
        return None, "SAP Z GET timed out"
    except httpx.RequestError as e:
        return None, f"SAP Z GET connection error: {e}"


async def create_yser_pr(
    *,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str = "YSER",
) -> tuple[str | None, str | None]:
    try:
        payload = build_z_yser_pr_payload(
            form=form, document_type=document_type, pr_number="", ticket_id=ticket_id
        )
    except ValueError as e:
        return None, str(e)
    return await _z_create_pr(payload=payload, ticket_id=ticket_id)


async def _z_create_pr(
    *, payload: dict[str, Any], ticket_id: str
) -> tuple[str | None, str | None]:
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    url = z_pr_collection_url(base)

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
                csrf_service_root=sap_service_root_url(base),
            )
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP Z request timed out"
    except httpx.RequestError as e:
        return None, f"SAP Z connection error: {e}"

    if resp.status_code >= 400:
        return None, _sap_error_message(resp)

    parsed = None
    try:
        parsed = parse_z_pr_number_from_response(resp.json())
    except Exception:
        log.exception("SAP Z PR create response parse failed ticket=%s", ticket_id)

    out = parsed or _norm(payload.get("PRNumber"))
    if not out:
        return None, "SAP Z succeeded but PR number missing in response"
    if is_placeholder_pr_number(out):
        return None, f"SAP Z returned placeholder PR number {out!r}"

    log.info("SAP Z YSER create ok ticket=%s pr_number=%s", ticket_id, out)
    return out, None


async def update_yser_pr(
    *,
    sap_id: str,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str = "YSER",
) -> tuple[str | None, str | None]:
    pr_number = _norm(sap_id)
    if not pr_number:
        return None, "Missing SAP PR number for Z update"
    if is_placeholder_pr_number(pr_number):
        return None, f"Cannot update placeholder PR number {pr_number!r}"

    before, pre_read_err = await get_yser_pr(
        pr_number=pr_number, ticket_id=f"{ticket_id}-pre"
    )
    if pre_read_err:
        return None, f"SAP pre-read failed before update: {pre_read_err}"
    if not before:
        return None, "SAP pre-read returned empty body before update"

    from app.procurement.sap_pr_z_payload import count_z_pr_sap_items, yser_max_service_ref_seq_from_z_read
    from app.procurement.sap_pr_z_update import build_yser_pr_update_posts

    sap_item_count = count_z_pr_sap_items(before)
    min_ref_seq = yser_max_service_ref_seq_from_z_read(before)
    sap_form = form_from_z_pr_read(
        before, document_type=document_type, seed_form=form
    )

    try:
        post_bodies = build_yser_pr_update_posts(
            submitted=form,
            sap_form=sap_form,
            pr_number=pr_number,
            sap_item_count=sap_item_count,
            ticket_id=ticket_id,
            min_ref_seq=min_ref_seq,
        )
    except ValueError as e:
        return None, str(e)

    verify_form = merge_yser_update_form_with_sap(form, sap_form=sap_form)
    from app.procurement.sap_pr_z_payload import _ensure_yser_pr_service_refs

    _ensure_yser_pr_service_refs(verify_form, ticket_id=ticket_id, only_without_item=True)

    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    url = z_pr_collection_url(base)

    async def _post_update(
        client: httpx.AsyncClient, post_body: dict[str, Any]
    ) -> httpx.Response:
        return await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=ticket_id,
            method="POST",
            url=url,
            json_body=post_body,
            headers_builder=sap_post_create_headers,
            csrf_service_root=sap_service_root_url(base),
        )

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            # Each POST is applied immediately; a mid-sequence failure leaves prior
            # mutations committed in SAP (no rollback).
            for i, post_body in enumerate(post_bodies):
                if i > 0:
                    await asyncio.sleep(YSER_Z_UPDATE_POST_DELAY_S)
                resp = await _post_update(client, post_body)
                if resp.status_code == 400:
                    await asyncio.sleep(2)
                    resp = await _post_update(client, post_body)
                if resp.status_code >= 400:
                    return None, _sap_error_message(resp)

    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP Z request timed out"
    except httpx.RequestError as e:
        return None, f"SAP Z connection error: {e}"

    after, read_err = await get_yser_pr(pr_number=pr_number, ticket_id=f"{ticket_id}-verify")
    if read_err:
        return None, f"Post-update read failed: {read_err}"
    if after is None:
        return None, "Post-update read returned empty body"
    if not yser_effective_line_blocks(verify_form):
        return None, "SAP verify after update: form has no service lines"

    mismatches = verify_yser_read_matches_form(
        after, form=verify_form, document_type=document_type
    )
    if mismatches:
        return None, "SAP verify after update: " + "; ".join(mismatches[:3])

    log.info("SAP Z YSER update ok ticket=%s pr_number=%s", ticket_id, pr_number)
    return pr_number, None


async def _yser_z_item_snapshots(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    ticket_id: str,
) -> tuple[list[SapPrItemSnapshot], str | None]:
    """PR line snapshots from Z ``GET PRHeaderSet`` (``IsDeleted`` on item row)."""
    z_body, z_err = await _z_get_pr_body(
        client, base=base, pr_number=pr_number, ticket_id=f"{ticket_id}-z-items"
    )
    if z_err or not z_body:
        return [], z_err or "SAP Z read failed"

    root = z_body.get("d") if isinstance(z_body.get("d"), dict) else z_body
    items_node = root.get("to_Items") if isinstance(root, dict) else None
    snapshots: list[SapPrItemSnapshot] = []
    for entry in _odata_results_list(items_node):
        props = _odata_entity_properties(entry)
        item_no = normalize_pr_item_number(_odata_text(props.get("PRItem")))
        if not item_no:
            continue
        services_node = props.get("to_Services")
        if services_node is None and isinstance(entry.get("to_Services"), dict):
            services_node = entry.get("to_Services")
        service_performer = ""
        for svc_entry in _odata_results_list(services_node):
            sp = _odata_entity_properties(svc_entry)
            code = _odata_text(sp.get("Service")) or _odata_text(sp.get("ServiceNumber"))
            if code:
                service_performer = code
                break
        snapshots.append(
            SapPrItemSnapshot(
                item_number=item_no,
                is_deleted=_is_sap_deleted_flag(_odata_text(props.get("IsDeleted"))),
                service_performer=service_performer,
                item_text=_odata_text(props.get("ShortText")),
            )
        )
    return snapshots, None


async def _z_mark_items_deleted(
    client: httpx.AsyncClient,
    *,
    base: str,
    pr_number: str,
    item_numbers: list[str],
    ticket_id: str,
) -> str | None:
    """Mark PR item(s) deleted via Z ``POST PRHeaderSet`` with ``IsDeleted=X``."""
    if not item_numbers:
        return None
    try:
        post_body = build_z_yser_item_delete_post_body(
            pr_number=pr_number,
            item_numbers=item_numbers,
        )
    except ValueError as e:
        return str(e)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=z_pr_collection_url(base),
        json_body=post_body,
        headers_builder=sap_post_create_headers,
        csrf_service_root=sap_service_root_url(base),
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


async def apply_yser_z_line_deletes(
    *,
    sap_id: str,
    form: dict[str, Any],
    ticket_id: str,
    document_type: str = "YSER",
) -> str | None:
    """Delete removed service lines via Z ``to_Items[].IsDeleted=X``.

    One SAP item per UI service line on **legacy** layout — removing a line deletes that SAP item.

    **Grouped layout** (multiple services per ``PRItem``): omitting one service from the
    update payload does **not** remove it on SAP QA; delete the whole ``service_group`` item
    instead (remove all lines in that group, or use separate ``service_group`` per line).
    """
    from app.procurement.sap_pr_z_payload import (
        count_z_pr_sap_items,
        yser_assign_update_item_numbers,
    )

    _ = document_type
    pr_number = _norm(sap_id)
    if not pr_number:
        return "Missing SAP PR number for YSER delete"
    if is_placeholder_pr_number(pr_number):
        return f"Cannot delete lines on placeholder PR number {pr_number!r}"

    creds, err = _credentials_or_error()
    if err:
        return err
    base, user, password = creds

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            z_body, z_read_err = await _z_get_pr_body(
                client, base=base, pr_number=pr_number, ticket_id=f"{ticket_id}-del-read"
            )
            if z_read_err or not z_body:
                return z_read_err or "SAP YSER delete: read failed before line delete"

            sap_form = form_from_z_pr_read(
                z_body, document_type=document_type, seed_form=form
            )
            sap_item_count = count_z_pr_sap_items(z_body)
            work = copy.deepcopy(form)
            yser_assign_update_item_numbers(
                work, sap_form=sap_form, sap_item_count=sap_item_count
            )
            sap_blocks = yser_effective_line_blocks(sap_form)
            sub_blocks = yser_effective_line_blocks(work)
            if yser_sap_layout_is_grouped(
                sap_item_count=sap_item_count,
                form_line_count=max(len(sap_blocks), len(sub_blocks)),
            ) and sub_blocks:
                # Grouped PR: service add/remove via incremental planner only.
                # Item ``IsDeleted=X`` leaves orphan services and breaks hydrate.
                return None
            keep_item_nos, resolve_err = yser_resolved_form_item_numbers(
                work, sap_form=sap_form
            )
            if resolve_err:
                return resolve_err

            existing, snap_err = await _yser_z_item_snapshots(
                client, base=base, pr_number=pr_number, ticket_id=ticket_id
            )
            if snap_err:
                return snap_err
            if not existing:
                return "SAP YSER delete: no PR items found"

            to_delete = [
                snap.item_number
                for snap in existing
                if not snap.is_deleted and snap.item_number not in keep_item_nos
            ]
            if not to_delete:
                return None

            err_del = await _z_mark_items_deleted(
                client,
                base=base,
                pr_number=pr_number,
                item_numbers=to_delete,
                ticket_id=ticket_id,
            )
            if err_del:
                return err_del

            log.info(
                "SAP Z YSER item delete ok ticket=%s pr_number=%s items=%s",
                ticket_id,
                pr_number,
                to_delete,
            )
            return None
    except _SapRequestError as e:
        return str(e)
    except httpx.TimeoutException:
        return "SAP YSER delete timed out"
    except httpx.RequestError as e:
        return f"SAP YSER delete connection error: {e}"


# Backward-compatible alias (tests / scripts).
apply_yser_standard_line_deletes = apply_yser_z_line_deletes


async def delete_yser_pr(*, sap_id: str, ticket_id: str) -> tuple[bool, str | None]:
    """Mark all active YSER items deleted via Z ``POST PRHeaderSet`` ``IsDeleted=X``."""
    pr_number = _norm(sap_id)
    if not pr_number:
        return False, "Missing SAP PR number for YSER delete"

    creds, err = _credentials_or_error()
    if err:
        return False, err
    base, user, password = creds

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            existing, snap_err = await _yser_z_item_snapshots(
                client, base=base, pr_number=pr_number, ticket_id=ticket_id
            )
            if snap_err:
                return False, snap_err
            to_delete = [
                snap.item_number for snap in existing if not snap.is_deleted
            ]
            err_del = await _z_mark_items_deleted(
                client,
                base=base,
                pr_number=pr_number,
                item_numbers=to_delete,
                ticket_id=ticket_id,
            )
            if err_del:
                return False, err_del
    except _SapRequestError as e:
        return False, str(e)
    except httpx.TimeoutException:
        return False, "SAP YSER delete timed out"
    except httpx.RequestError as e:
        return False, f"SAP YSER delete connection error: {e}"

    log.info("SAP Z YSER delete all items ok ticket=%s pr_number=%s", ticket_id, pr_number)
    return True, None


async def delete_yser_pr_item(
    *, sap_id: str, pr_item: str, ticket_id: str
) -> tuple[bool, str | None]:
    """Mark one YSER item deleted via Z ``POST PRHeaderSet`` ``IsDeleted=X``."""
    pr_number = _norm(sap_id)
    item_no = normalize_pr_item_number(pr_item)
    if not pr_number or not item_no:
        return False, "PR number and item required for YSER item delete"

    creds, err = _credentials_or_error()
    if err:
        return False, err
    base, user, password = creds

    try:
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            err_del = await _z_mark_items_deleted(
                client,
                base=base,
                pr_number=pr_number,
                item_numbers=[item_no],
                ticket_id=ticket_id,
            )
    except _SapRequestError as e:
        return False, str(e)
    except httpx.TimeoutException:
        return False, "SAP YSER item delete timed out"
    except httpx.RequestError as e:
        return False, f"SAP YSER item delete connection error: {e}"

    if err_del:
        return False, err_del
    return True, None


async def get_yser_pr_legacy_standard(
    *, pr_number: str, ticket_id: str = "read"
) -> tuple[dict[str, Any] | None, str | None]:
    """Deprecated alias — use :func:`get_yser_pr` (Z read). Kept for scripts."""
    return await get_yser_pr(pr_number=pr_number, ticket_id=ticket_id)


async def try_recover_yser_pr(
    client: httpx.AsyncClient, *, base: str, ticket_id: str
) -> tuple[str | None, str | None]:
    """YSER: standard item/header scan, then Z ``ShortText``, then legacy ``HeaderNote``."""
    from app.procurement.sap_pr_client import _paginate_recovery_scan, _try_recover_pr_number

    recovered, _err = await _try_recover_pr_number(
        client, base=base, ticket_id=ticket_id
    )
    if recovered:
        log.info("SAP YSER PR recovered (standard) ticket=%s pr_number=%s", ticket_id, recovered)
        return recovered, None

    pr_no = await _paginate_recovery_scan(
        client,
        base=base,
        ticket_id=ticket_id,
        collection_url=z_pr_item_collection_url(base),
        number_key="PRNumber",
        field_key="Extsourcesystem",
        orderby="PRNumber desc",
        csrf_service_root=z_pr_service_root_url(base),
    )
    if pr_no:
        log.info(
            "SAP Z YSER PR recovered (Extsourcesystem) ticket=%s pr_number=%s",
            ticket_id,
            pr_no,
        )
        return pr_no, None

    pr_no = await _paginate_recovery_scan(
        client,
        base=base,
        ticket_id=ticket_id,
        collection_url=z_pr_item_collection_url(base),
        number_key="PRNumber",
        text_keys=("ShortText",),
        orderby="PRNumber desc",
        csrf_service_root=z_pr_service_root_url(base),
    )
    if pr_no:
        log.info("SAP Z YSER PR recovered (ShortText) ticket=%s pr_number=%s", ticket_id, pr_no)
        return pr_no, None

    pr_no = await _paginate_recovery_scan(
        client,
        base=base,
        ticket_id=ticket_id,
        collection_url=z_pr_item_collection_url(base),
        number_key="PRNumber",
        text_keys=("HeaderNote",),
        orderby="PRNumber desc",
        csrf_service_root=z_pr_service_root_url(base),
    )
    if pr_no:
        log.info("SAP Z YSER PR recovered (legacy HeaderNote) ticket=%s pr_number=%s", ticket_id, pr_no)
        return pr_no, None
    return None, None
