"""Live SAP S/4 PO — OData V2.

- **YUNB/YAST:** ``API_PURCHASEORDER_PROCESS_SRV`` — create/update via PATCH + item POST.
- **YSER:** ``ZAPI_PURCHASEORDER_PROCESS_SRV`` — create/update via collection ``POST`` with
  ``to_PurchaseOrderItem`` / ``to_Services`` / service ``to_AccountAssignment``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import httpx

from app.config.settings import settings
from app.procurement.sap_po_payload import (
    PO_HEADER_COLLECTION,
    PO_ITEM_COLLECTION,
    SapPoItemSnapshot,
    build_po_acct_delete_patch,
    build_po_item_delete_patch,
    build_po_payload,
    build_po_resubmit_plan,
    iter_po_acct_post_bodies,
    po_item_patch_body,
    legacy_sap_ticket_ref_tag,
    parse_po_items_from_read,
    parse_po_number_from_response,
    normalize_po_item_number,
    po_acct_assgmt_entity_url,
    po_acct_collection_post_url,
    po_collection_url,
    po_entity_url,
    po_item_collection_post_url,
    po_item_entity_url,
    po_read_full_url,
    po_schedule_line_entity_url,
    sap_po_service_root_url,
    sap_ticket_ref_tag,
    verify_po_read_against_form,
)
from app.procurement.sap_odata_utils import odata_norm
from app.procurement.sap_pr_payload import (
    pick_recovery_document_by_field,
    pick_recovery_document_by_tag,
    recovery_tags_for_ticket,
    sap_ticket_correspnc_external_marker,
)
from app.procurement.sap_odata_deferred import resolve_po_read_body, resolve_z_po_read_body
from app.procurement.sap_po_z_payload import (
    any_po_text_in_form,
    build_z_po_texts_post_body,
    build_z_yser_po_post_body,
    build_z_yser_po_resubmit_plan,
    parse_z_po_items_from_read,
    po_texts_differ,
    PO_TEXT_SAP_FIELDS,
    verify_z_po_read_against_form,
    z_po_collection_url,
    z_po_header_read_url,
    z_po_read_url,
    z_po_service_root_url,
)
from app.procurement.sap_odata_utils import odata_base_root as _base_root
from app.procurement.sap_pr_client import (
    _SapRequestError,
    _acct_patches_are_qty_only,
    _credentials_or_error,
    _httpx_timeout,
    _item_patch_fields_differ,
    _norm,
    _request_with_csrf_retry,
    _sap_error_message,
    _sap_qty_equal_loose,
    sap_json_headers,
    sap_odata_batch_patch,
    sap_patch_headers,
    sap_post_create_headers,
    sap_pr_configured,
)

log = logging.getLogger(__name__)

from app.procurement.sap_config import (
    SAP_PO_READ_ATTEMPTS,
    SAP_PO_READ_RETRY_DELAY_S,
)
from app.procurement.sap_read_retry import (
    should_retry_sap_odata_read_error,
    sleep_before_sap_read_retry,
)


def sap_po_configured() -> bool:
    return sap_pr_configured()


def _po_csrf_root(base: str, *, document_type: str = "") -> str:
    if (document_type or "").upper() == "YSER":
        return z_po_service_root_url(base)
    return sap_po_service_root_url(base)


def _is_yser_po(document_type: str) -> bool:
    return (document_type or "").upper() == "YSER"


def _po_form_block_for_item(form: dict[str, Any], item_no: str) -> dict[str, Any]:
    want = normalize_po_item_number(item_no)
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    for i, raw in enumerate(lines):
        if not isinstance(raw, dict):
            continue
        key = normalize_po_item_number(
            raw.get("purchase_order_item")
            or raw.get("PurchaseOrderItem")
            or raw.get("purchase_requisition_item")
            or ""
        )
        if not key:
            key = normalize_po_item_number(str((i + 1) * 10))
        if key == want:
            return raw
    if len(lines) == 1 and isinstance(lines[0], dict) and want in ("10", "0010", "00010"):
        return lines[0]
    return {}


async def _patch_po_schedule_delivery_if_needed(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    item_no: str,
    snapshot: SapPoItemSnapshot | None,
    block: dict[str, Any],
    ticket_id: str,
    document_type: str,
) -> str | None:
    """PATCH schedule-line delivery date when the form date differs from SAP."""
    from app.procurement.sap_odata_utils import (
        default_procurement_delivery_date,
        sanitize_form_delivery_date,
    )
    from app.procurement.sap_pr_payload import _sap_date

    raw_dd = _norm(block.get("delivery_date"))
    if not raw_dd:
        return None
    form_dd = sanitize_form_delivery_date(
        raw_dd, fallback=default_procurement_delivery_date()
    )
    if not form_dd:
        return None
    sap_dd = sanitize_form_delivery_date(_norm(snapshot.delivery_date) if snapshot else "")
    if sap_dd and form_dd == sap_dd:
        return None
    delivery = _sap_date(form_dd)
    if not delivery:
        return None
    sl = (snapshot.schedule_line if snapshot else "") or "1"
    url = po_schedule_line_entity_url(
        base,
        po_number=po_number,
        item_number=item_no,
        schedule_line=sl,
    )
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="PATCH",
        url=url,
        json_body={"ScheduleLineDeliveryDate": delivery},
        headers_builder=sap_patch_headers,
        csrf_service_root=_po_csrf_root(base, document_type=document_type),
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


def _po_item_patch_is_noop(
    snapshot: SapPoItemSnapshot | None,
    patch_body: dict[str, Any],
    *,
    acct_patches: list[tuple[str, dict[str, Any]]],
    acct_creates: list[dict[str, Any]],
    acct_deletes: list[str],
) -> bool:
    """Skip item PATCH when only multi/single MFA qty redistribute is needed.

    QAS rejects re-sending Plant/Material/… on many POs (missing partner PI / requester)
    even when account-assignment qty updates alone would succeed — but if plant, price,
    text, material group, tax, or any other outbound item field actually changed, we must
    PATCH (same fields we used to send on the full-item update path).
    """
    if not patch_body:
        return True
    if snapshot is None:
        return False
    if acct_creates or acct_deletes:
        return False
    if not _acct_patches_are_qty_only(acct_patches):
        return False
    return not _po_item_non_qty_fields_changed(snapshot, patch_body)


# SAP PO item key -> snapshot attribute.
_PO_ITEM_FIELD_SNAP: dict[str, str] = {
    "Material": "material",
    "ServicePerformer": "service_performer",
    "PurchaseOrderItemText": "item_text",
    "Plant": "plant",
    "StorageLocation": "storage_location",
    "NetPriceAmount": "net_price",
    "TaxCode": "tax_code",
    "TaxJurisdiction": "tax_jurisdiction",
    "MaterialGroup": "material_group",
    "PurchaseOrderQuantityUnit": "order_unit",
    "OrderQuantity": "order_quantity",
}

# Structural keys always resent; safe to ignore when deciding noop vs PATCH.
_PO_ITEM_COMPARE_IGNORE: frozenset[str] = frozenset(
    {
        "PurchaseOrder",
        "PurchaseOrderItem",
        "PurchaseOrderItemCategory",
        "AccountAssignmentCategory",
        "MultipleAcctAssgmtDistribution",
        "OrderPriceUnit",
        "NetPriceQuantity",
        "ProductType",
        "PurchasingInfoRecord",
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
    }
)

_PO_ITEM_QTY_COMPARE_KEYS: frozenset[str] = frozenset(
    {"NetPriceAmount", "OrderQuantity"}
)


def _po_item_non_qty_fields_changed(
    snapshot: SapPoItemSnapshot, patch_body: dict[str, Any]
) -> bool:
    """Detect material / text / plant / group / price / tax / unit / unknown-field changes."""
    return _item_patch_fields_differ(
        snapshot,
        patch_body,
        field_map=_PO_ITEM_FIELD_SNAP,
        ignore_keys=_PO_ITEM_COMPARE_IGNORE,
        qty_keys=_PO_ITEM_QTY_COMPARE_KEYS,
        date_keys=frozenset(),
    )


def _merge_z_po_texts_into_body(body: dict[str, Any], z_root: dict[str, Any]) -> None:
    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return
    for form_key, sap_key in (
        ("po_remarks", "Remarks"),
        ("po_deadlines", "Deadlines"),
        ("po_terms_of_delivery", "TermsOfDelivery"),
    ):
        if sap_key in z_root:
            root[sap_key] = z_root.get(sap_key)


async def _sap_fetch_z_po_header_root(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    ticket_id: str,
) -> dict[str, Any] | None:
    for attempt in range(SAP_PO_READ_ATTEMPTS):
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=ticket_id,
            method="GET",
            url=z_po_header_read_url(base, po_number),
            json_body=None,
            headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
            csrf_service_root=z_po_service_root_url(base),
        )
        if resp.status_code >= 400:
            err = _sap_error_message(resp)
            if (
                attempt + 1 < SAP_PO_READ_ATTEMPTS
                and should_retry_sap_odata_read_error(err, status_code=resp.status_code)
            ):
                log.warning(
                    "SAP Z PO header text GET retry ticket=%s attempt=%s po=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    po_number,
                    err,
                )
                await sleep_before_sap_read_retry(SAP_PO_READ_RETRY_DELAY_S)
                continue
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        root = data.get("d") if isinstance(data.get("d"), dict) else data
        return root if isinstance(root, dict) else None
    return None


async def _sap_post_po_texts(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    form: dict[str, Any],
    ticket_id: str,
    sap_text_root: dict[str, Any] | None = None,
) -> str | None:
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    if sap_text_root is not None:
        if not po_texts_differ(header, sap_text_root):
            return None
    elif not any_po_text_in_form(header):
        return None
    try:
        payload = build_z_po_texts_post_body(po_number=po_number, header=header)
    except ValueError as e:
        return str(e)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=z_po_collection_url(base),
        json_body=payload,
        headers_builder=sap_post_create_headers,
        csrf_service_root=z_po_service_root_url(base),
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


async def _reconcile_yser_po_services_after_create(
    *,
    po_number: str,
    form: dict[str, Any],
    ticket_id: str,
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
) -> str | None:
    """SAP Z PO create often persists only one service per item; add the rest via update."""
    from app.procurement.sap_po_z_payload import (
        form_from_z_po_read,
        prepare_yser_po_create_reconcile_forms,
    )
    from app.procurement.sap_po_z_update import yser_po_create_reconcile_needed

    creds, err = _credentials_or_error()
    if err:
        return err
    base, user, password = creds

    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        body, read_err = await _sap_get_po_body(
            client,
            base=base,
            po_number=po_number,
            ticket_id=f"{ticket_id}-reconcile-read",
            document_type="YSER",
        )
        if read_err:
            return f"Post-create read failed: {read_err}"
        assert body is not None
        sap_form = form_from_z_po_read(body, seed_form=form)
        sub_prep, sap_prep = prepare_yser_po_create_reconcile_forms(
            form, sap_form=sap_form
        )
        if not yser_po_create_reconcile_needed(sap_form=sap_prep, submitted=sub_prep):
            return None

    log.info(
        "SAP YSER PO create reconcile ticket=%s po_number=%s — adding missing services",
        ticket_id,
        po_number,
    )
    _, update_err = await _sap_z_update_yser_po_once(
        po_number=po_number,
        form=form,
        ticket_id=f"{ticket_id}-reconcile",
        parent_pr_number=parent_pr_number,
        creator_email=creator_email,
        create_reconcile=True,
    )
    return update_err


async def _sap_create_po(
    *,
    payload: dict[str, Any],
    ticket_id: str,
    form: dict[str, Any] | None = None,
    document_type: str = "",
    parent_pr_number: str | None = None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    dt = (document_type or "").upper()
    url = z_po_collection_url(base) if dt == "YSER" else po_collection_url(base)
    csrf_root = _po_csrf_root(base, document_type=dt)

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
                csrf_service_root=csrf_root,
            )
            if resp.status_code >= 400:
                return None, _sap_error_message(resp)

            try:
                parsed = parse_po_number_from_response(resp.json())
            except Exception:
                log.exception("SAP PO create response parse failed ticket=%s", ticket_id)
                parsed = None

            out = parsed or _norm(payload.get("PurchaseOrder"))
            if not out:
                return None, "SAP succeeded but PO number missing in response"
            log.info("SAP PO create ok ticket=%s po_number=%s", ticket_id, out)

            if form and (document_type or "").strip() and dt != "YSER":
                for acct_body in iter_po_acct_post_bodies(
                    form=form,
                    document_type=document_type,
                    po_number=out,
                ):
                    acct_err = await _sap_create_po_acct_assgmt(
                        client,
                        base=base,
                        acct_body=acct_body,
                        ticket_id=ticket_id,
                    )
                    if acct_err:
                        return (
                            out,
                            f"PO {out} created; account assignment failed: {acct_err}",
                        )
                texts_err = await _sap_post_po_texts(
                    client,
                    base=base,
                    po_number=out,
                    form=form,
                    ticket_id=f"{ticket_id}-texts",
                )
                if texts_err:
                    return out, f"PO {out} created; long text update failed: {texts_err}"
            if form and dt == "YSER":
                reconcile_err = await _reconcile_yser_po_services_after_create(
                    po_number=out,
                    form=form,
                    ticket_id=ticket_id,
                    parent_pr_number=parent_pr_number,
                    creator_email=creator_email,
                )
                if reconcile_err:
                    return out, f"PO {out} created; service reconcile failed: {reconcile_err}"
            return out, None
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP request timed out"
    except httpx.RequestError as e:
        return None, f"SAP connection error: {e}"


async def _sap_get_po_body(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    ticket_id: str,
    document_type: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    dt = (document_type or "").upper()
    read_url = (
        z_po_read_url(base, po_number) if dt == "YSER" else po_read_full_url(base, po_number)
    )
    csrf_root = _po_csrf_root(base, document_type=dt)
    last_err: str | None = None
    for attempt in range(SAP_PO_READ_ATTEMPTS):
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
                attempt + 1 < SAP_PO_READ_ATTEMPTS
                and should_retry_sap_odata_read_error(last_err, status_code=resp.status_code)
            ):
                log.warning(
                    "SAP PO read retry ticket=%s attempt=%s po=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    po_number,
                    last_err,
                )
                await sleep_before_sap_read_retry(SAP_PO_READ_RETRY_DELAY_S)
                continue
            return None, last_err
        try:
            data = resp.json()
        except Exception:
            return None, "SAP GET returned non-JSON body"
        if not isinstance(data, dict):
            return None, "SAP GET returned unexpected body"
        if dt != "YSER":
            z_root = await _sap_fetch_z_po_header_root(
                client,
                base=base,
                po_number=po_number,
                ticket_id=f"{ticket_id}-ztext",
            )
            if z_root:
                _merge_z_po_texts_into_body(data, z_root)
        try:
            if dt == "YSER":
                data = await resolve_z_po_read_body(
                    client,
                    base=base,
                    body=data,
                    po_number=po_number,
                    ticket_id=ticket_id,
                    csrf_service_root=csrf_root,
                )
            else:
                data = await resolve_po_read_body(
                    client,
                    base=base,
                    body=data,
                    po_number=po_number,
                    ticket_id=ticket_id,
                    csrf_service_root=csrf_root,
                )
        except _SapRequestError as e:
            last_err = str(e)
            if (
                attempt + 1 < SAP_PO_READ_ATTEMPTS
                and should_retry_sap_odata_read_error(last_err)
            ):
                log.warning(
                    "SAP PO deferred resolve retry ticket=%s attempt=%s po=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    po_number,
                    last_err,
                )
                await sleep_before_sap_read_retry(SAP_PO_READ_RETRY_DELAY_S)
                continue
            return None, last_err
        return data, None
    return None, last_err or "SAP GET failed"


async def _sap_patch_po_header(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    header_patch: dict[str, Any],
    ticket_id: str,
    document_type: str,
) -> str | None:
    """PATCH PO header — CSRF from service root; per-field fallback when bundle is rejected."""
    if not header_patch:
        return None
    url = po_entity_url(base, po_number)
    csrf_root = _po_csrf_root(base, document_type=document_type)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="PATCH",
        url=url,
        json_body=header_patch,
        headers_builder=sap_patch_headers,
        csrf_service_root=csrf_root,
    )
    if resp.status_code < 400:
        return None
    bundle_err = _sap_error_message(resp)
    field_errs: list[str] = []
    for key, val in header_patch.items():
        one = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-hdr-{key}",
            method="PATCH",
            url=url,
            json_body={key: val},
            headers_builder=sap_patch_headers,
            csrf_service_root=csrf_root,
        )
        if one.status_code >= 400:
            field_errs.append(f"{key}: {_sap_error_message(one)}")
    if not field_errs:
        return None
    if len(field_errs) == 1 and len(header_patch) == 1:
        return field_errs[0]
    return bundle_err + ("; " + "; ".join(field_errs) if field_errs else "")


async def _sap_mark_po_item_deleted(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    item_number: str,
    ticket_id: str,
) -> str | None:
    item_url = po_item_entity_url(base, po_number=po_number, item_number=item_number)
    del_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="DELETE",
        url=item_url,
        json_body=None,
        headers_builder=sap_patch_headers,
        csrf_service_root=_po_csrf_root(base),
    )
    if del_resp.status_code in (200, 204):
        return None
    # QAS often returns 500 on DELETE; PATCH ``PurchasingDocumentDeletionCode`` works.
    if del_resp.status_code not in (400, 403, 404, 405, 500):
        return _sap_error_message(del_resp)

    patch_resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="PATCH",
        url=item_url,
        json_body=build_po_item_delete_patch(),
        headers_builder=sap_patch_headers,
        csrf_service_root=_po_csrf_root(base),
    )
    if patch_resp.status_code >= 400:
        return _sap_error_message(patch_resp)
    return None


async def _sap_create_po_item(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    item_body: dict[str, Any],
    ticket_id: str,
) -> str | None:
    url = po_item_collection_post_url(base, po_number)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=url,
        json_body=item_body,
        headers_builder=sap_post_create_headers,
        csrf_service_root=_po_csrf_root(base),
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


async def _sap_mark_po_acct_deleted(
    client: httpx.AsyncClient,
    *,
    base: str,
    po_number: str,
    item_number: str,
    acct_assgmt_number: str,
    ticket_id: str,
) -> str | None:
    acct_url = po_acct_assgmt_entity_url(
        base,
        po_number=po_number,
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
        csrf_service_root=_po_csrf_root(base),
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
        json_body=build_po_acct_delete_patch(),
        headers_builder=sap_patch_headers,
        csrf_service_root=_po_csrf_root(base),
    )
    if patch_resp.status_code >= 400:
        return _sap_error_message(patch_resp)
    return None


async def _sap_create_po_acct_assgmt(
    client: httpx.AsyncClient,
    *,
    base: str,
    acct_body: dict[str, Any],
    ticket_id: str,
) -> str | None:
    url = po_acct_collection_post_url(base)
    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="POST",
        url=url,
        json_body=acct_body,
        headers_builder=sap_post_create_headers,
        csrf_service_root=_po_csrf_root(base),
    )
    if resp.status_code >= 400:
        return _sap_error_message(resp)
    return None


_PO_RECOVERY_SCAN_TOP = 100


async def _try_recover_po_number(
    client: httpx.AsyncClient, *, base: str, ticket_id: str
) -> tuple[str | None, str | None]:
    compact = sap_ticket_correspnc_external_marker(ticket_id)
    csrf_root = _po_csrf_root(base)
    if compact:
        esc = compact.replace("'", "''")
        header_url = (
            f"{_base_root(base)}{PO_HEADER_COLLECTION}"
            f"?$filter=CorrespncExternalReference eq '{esc}'"
            f"&$top=5&$select=PurchaseOrder,CorrespncExternalReference"
        )
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-recover-hdr",
            method="GET",
            url=header_url,
            json_body=None,
            headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
            csrf_service_root=csrf_root,
        )
        if resp.status_code < 400:
            try:
                data = resp.json()
                root_node = data.get("d") if isinstance(data.get("d"), dict) else data
                results = root_node.get("results") if isinstance(root_node, dict) else None
                if isinstance(results, list):
                    po_no = pick_recovery_document_by_field(
                        results,
                        number_key="PurchaseOrder",
                        field_key="CorrespncExternalReference",
                        ticket_id=ticket_id,
                    )
                    if po_no:
                        log.info(
                            "SAP PO recovered via CorrespncExternalReference ticket=%s po_number=%s",
                            ticket_id,
                            po_no,
                        )
                        return po_no, None
            except Exception:
                pass

        esc = compact.replace("'", "''")
        item_url = (
            f"{_base_root(base)}{PO_ITEM_COLLECTION}"
            f"?$filter=substringof('{esc}',PurchaseOrderItemText)"
            f"&$top={_PO_RECOVERY_SCAN_TOP}"
            f"&$select=PurchaseOrder,PurchaseOrderItemText"
        )
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-recover-item",
            method="GET",
            url=item_url,
            json_body=None,
            headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
            csrf_service_root=csrf_root,
        )
        if resp.status_code < 400:
            try:
                data = resp.json()
                root_node = data.get("d") if isinstance(data.get("d"), dict) else data
                results = root_node.get("results") if isinstance(root_node, dict) else None
                if isinstance(results, list) and results:
                    po_no = pick_recovery_document_by_tag(
                        results,
                        ticket_id=ticket_id,
                        number_key="PurchaseOrder",
                        text_keys=("PurchaseOrderItemText",),
                    )
                    if po_no:
                        log.info(
                            "SAP PO recovered via item text ticket=%s po_number=%s",
                            ticket_id,
                            po_no,
                        )
                        return po_no, None
            except Exception:
                pass

    return None, None


async def _try_recover_yser_po_by_ext_system(
    client: httpx.AsyncClient, *, base: str, ticket_id: str
) -> str | None:
    """YSER PO: standard list + Z GET ``Extsourcesystem`` (Z has no filterable entity set)."""
    from app.procurement.sap_po_z_payload import z_po_header_read_url
    from app.procurement.sap_pr_payload import sap_ticket_ext_system_marker

    marker = sap_ticket_ext_system_marker(ticket_id).upper()
    if not marker:
        return None
    csrf_root = _po_csrf_root(base, document_type="YSER")
    for page in range(30):
        skip = page * 100
        url = (
            f"{_base_root(base)}{PO_HEADER_COLLECTION}"
            f"?$filter=PurchaseOrderType eq 'YSER'"
            f"&$orderby=PurchaseOrder desc&$top=100&$skip={skip}"
            f"&$select=PurchaseOrder"
        )
        resp = await _request_with_csrf_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-recover-yser-p{page}",
            method="GET",
            url=url,
            json_body=None,
            headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
            csrf_service_root=csrf_root,
        )
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        root_node = data.get("d") if isinstance(data.get("d"), dict) else data
        results = root_node.get("results") if isinstance(root_node, dict) else None
        if not isinstance(results, list) or not results:
            return None
        for row in results:
            if not isinstance(row, dict):
                continue
            po_no = _norm(row.get("PurchaseOrder"))
            if not po_no:
                continue
            z_resp = await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=f"{ticket_id}-recover-yser-{po_no}",
                method="GET",
                url=f"{z_po_header_read_url(base, po_no)}&$select=Extsourcesystem",
                json_body=None,
                headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
                csrf_service_root=csrf_root,
            )
            if z_resp.status_code >= 400:
                continue
            try:
                z_root = z_resp.json().get("d") if isinstance(z_resp.json().get("d"), dict) else z_resp.json()
            except Exception:
                continue
            if isinstance(z_root, dict) and _norm(z_root.get("Extsourcesystem")).upper() == marker:
                return po_no
        if len(results) < 100:
            return None
    return None


async def _sap_z_update_yser_po_once(
    *,
    po_number: str,
    form: dict[str, Any],
    ticket_id: str,
    parent_pr_number: str | None,
    creator_email: str | None = None,
    create_reconcile: bool = False,
) -> tuple[str | None, str | None]:
    from app.procurement.sap_po_z_payload import (
        count_z_po_sap_items,
        form_from_z_po_read,
        merge_yser_po_update_form_with_sap,
        yser_po_max_service_ref_seq_from_form,
    )
    from app.procurement.sap_po_z_update import build_yser_po_update_posts
    from app.procurement.sap_pr_z_client import YSER_Z_UPDATE_POST_DELAY_S

    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    csrf_root = _po_csrf_root(base, document_type="YSER")

    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        before, err = await _sap_get_po_body(
            client,
            base=base,
            po_number=po_number,
            ticket_id=ticket_id,
            document_type="YSER",
        )
        if err:
            return None, err
        assert before is not None

        sap_form = form_from_z_po_read(before, seed_form=form)
        sap_item_count = count_z_po_sap_items(before)
        _, existing_items = parse_z_po_items_from_read(before)
        min_ref_seq = max(
            yser_po_max_service_ref_seq_from_form(sap_form),
            yser_po_max_service_ref_seq_from_form(form),
        )

        try:
            post_bodies, verify_form = build_yser_po_update_posts(
                submitted=form,
                sap_form=sap_form,
                po_number=po_number,
                sap_item_count=sap_item_count,
                ticket_id=ticket_id,
                parent_pr_number=parent_pr_number,
                existing_items=existing_items,
                min_ref_seq=min_ref_seq,
                create_reconcile=create_reconcile,
            )
        except ValueError as e:
            if "No YSER PO line changes" not in str(e):
                return None, str(e)
            post_bodies = []
            verify_form = merge_yser_po_update_form_with_sap(
                copy.deepcopy(form), sap_form=sap_form
            )

        if not post_bodies:
            if create_reconcile:
                log.info(
                    "SAP YSER PO create reconcile noop ticket=%s po_number=%s",
                    ticket_id,
                    po_number,
                )
                return po_number, None
            before_root = before.get("d") if isinstance(before.get("d"), dict) else before
            existing_sales_person = ""
            existing_creator_mail_id = ""
            existing_correspnc_internal_reference = ""
            existing_po_texts: dict[str, str] = {}
            if isinstance(before_root, dict):
                existing_sales_person = odata_norm(before_root.get("SalesPerson"))
                existing_creator_mail_id = odata_norm(before_root.get("CreatorMailId"))
                existing_correspnc_internal_reference = odata_norm(
                    before_root.get("CorrespncInternalReference")
                )
                existing_po_texts = {
                    sap_key: odata_norm(before_root.get(sap_key))
                    for _, sap_key in PO_TEXT_SAP_FIELDS
                }
            plan = build_z_yser_po_resubmit_plan(
                form=form,
                po_number=po_number,
                existing_items=existing_items,
                ticket_id=ticket_id,
                parent_pr_number=parent_pr_number,
                existing_sales_person=existing_sales_person,
                existing_po_texts=existing_po_texts,
                existing_creator_mail_id=existing_creator_mail_id,
                existing_correspnc_internal_reference=existing_correspnc_internal_reference,
                creator_email=creator_email,
            )
            post_bodies = [plan.post_body]
            verify_form = merge_yser_po_update_form_with_sap(
                copy.deepcopy(form), sap_form=sap_form
            )

        from app.procurement.sap_po_z_payload import _ensure_yser_po_service_refs

        _ensure_yser_po_service_refs(verify_form, ticket_id=ticket_id, min_ref_seq=min_ref_seq)

        url = z_po_collection_url(base)

        async def _post_update(post_body: dict[str, Any]) -> httpx.Response:
            return await _request_with_csrf_retry(
                client,
                base=base,
                ticket_id=ticket_id,
                method="POST",
                url=url,
                json_body=post_body,
                headers_builder=sap_post_create_headers,
                csrf_service_root=csrf_root,
            )

        for i, post_body in enumerate(post_bodies):
            if i > 0:
                await asyncio.sleep(YSER_Z_UPDATE_POST_DELAY_S)
            resp = await _post_update(post_body)
            if resp.status_code == 400:
                await asyncio.sleep(2)
                resp = await _post_update(post_body)
            if resp.status_code >= 400:
                return None, _sap_error_message(resp)

        from app.procurement.sap_po_z_payload import reconcile_yser_po_verify_form_with_sap_read

        mismatches: list[str] = []
        for attempt in range(SAP_PO_READ_ATTEMPTS):
            if attempt > 0:
                await sleep_before_sap_read_retry(SAP_PO_READ_RETRY_DELAY_S)
            after, err = await _sap_get_po_body(
                client,
                base=base,
                po_number=po_number,
                ticket_id=f"{ticket_id}-verify",
                document_type="YSER",
            )
            if err:
                return None, f"Post-update read failed: {err}"
            assert after is not None
            reconciled = reconcile_yser_po_verify_form_with_sap_read(verify_form, after)
            mismatches = verify_z_po_read_against_form(
                after,
                form=reconciled,
                ticket_id=ticket_id,
                parent_pr_number=parent_pr_number,
                creator_email=creator_email,
            )
            if not mismatches:
                break

        if mismatches:
            return None, "SAP verify after YSER PO resubmit: " + "; ".join(mismatches[:5])

        log.info("SAP YSER PO update ok ticket=%s po_number=%s", ticket_id, po_number)
        return po_number, None


async def _sap_update_po_once(
    *,
    po_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
    parent_pr_number: str | None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    if _is_yser_po(document_type):
        return await _sap_z_update_yser_po_once(
            po_number=po_number,
            form=form,
            ticket_id=ticket_id,
            parent_pr_number=parent_pr_number,
            creator_email=creator_email,
        )

    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds

    async with httpx.AsyncClient(
        timeout=_httpx_timeout(),
        verify=bool(settings.procurement_sap_verify_ssl),
        auth=(user, password),
    ) as client:
        before, err = await _sap_get_po_body(
            client,
            base=base,
            po_number=po_number,
            ticket_id=ticket_id,
            document_type=document_type,
        )
        if err:
            return None, err
        assert before is not None
        before_root = before.get("d") if isinstance(before.get("d"), dict) else before
        _, existing_items = parse_po_items_from_read(before)
        plan = build_po_resubmit_plan(
            form=form,
            document_type=document_type,
            po_number=po_number,
            existing_items=existing_items,
            ticket_id=ticket_id,
            parent_pr_number=parent_pr_number,
            creator_email=creator_email,
            existing_sap_header=before_root if isinstance(before_root, dict) else None,
        )

        hdr_err = await _sap_patch_po_header(
            client,
            base=base,
            po_number=po_number,
            header_patch=plan.header_patch,
            ticket_id=ticket_id,
            document_type=document_type,
        )
        if hdr_err:
            return None, hdr_err

        create_item_nos = {
            normalize_po_item_number(x.get("PurchaseOrderItem"))
            for x in plan.items_to_create
        }
        create_item_nos.discard("")

        for item_body in plan.items_to_create:
            body = {**item_body, "PurchaseOrder": po_number}
            err_post = await _sap_create_po_item(
                client,
                base=base,
                po_number=po_number,
                item_body=body,
                ticket_id=ticket_id,
            )
            if err_post:
                item_no = _norm(body.get("PurchaseOrderItem"))
                return None, f"Create PO item {item_no}: {err_post}"

        for item_no, body, acct_patches, acct_creates, acct_deletes in plan.item_patches:
            if item_no in plan.items_to_mark_deleted or item_no in create_item_nos:
                continue
            item_url = po_item_entity_url(
                base, po_number=po_number, item_number=item_no
            )
            patch_body = po_item_patch_body(body)
            snapshot = next(
                (
                    s
                    for s in existing_items
                    if normalize_po_item_number(s.item_number) == item_no and not s.is_deleted
                ),
                None,
            )
            dt = (document_type or "").upper()
            desired_oq = _norm(patch_body.get("OrderQuantity")) if patch_body else ""
            oq_changed = bool(
                snapshot
                and desired_oq
                and snapshot.order_quantity
                and not _sap_qty_equal_loose(desired_oq, snapshot.order_quantity)
            )
            # YAST: item OrderQuantity + MFA qtys must move together (increase-total).
            use_qty_batch = (
                dt == "YAST"
                and _acct_patches_are_qty_only(acct_patches)
                and not acct_creates
                and not acct_deletes
                and (len(acct_patches) > 1 or (oq_changed and bool(acct_patches)))
            )
            if use_qty_batch:
                batch_ops: list[tuple[str, dict[str, Any]]] = []
                if oq_changed and desired_oq:
                    # Item OrderQuantity PATCH is rejected ("cannot be processed");
                    # qty lives on schedule line (create deep-insert already sets both).
                    sl = (snapshot.schedule_line if snapshot else "") or "1"
                    batch_ops.append(
                        (
                            po_schedule_line_entity_url(
                                base,
                                po_number=po_number,
                                item_number=item_no,
                                schedule_line=sl,
                            ),
                            {"ScheduleLineOrderQuantity": desired_oq},
                        )
                    )
                for acct_no, acct_body in acct_patches:
                    batch_ops.append(
                        (
                            po_acct_assgmt_entity_url(
                                base,
                                po_number=po_number,
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
                    csrf_service_root=_po_csrf_root(base, document_type=document_type),
                    patches=batch_ops,
                )
                if batch_err:
                    return None, batch_err
                # Qty moved via $batch; still PATCH other item fields when they
                # changed (material / text / plant / price / group / tax / …).
                # Never re-send OrderQuantity — schedule line owns it.
                rest = {
                    k: v
                    for k, v in (patch_body or {}).items()
                    if k != "OrderQuantity"
                }
                if rest and (
                    snapshot is None
                    or _po_item_non_qty_fields_changed(snapshot, rest)
                ):
                    resp = await _request_with_csrf_retry(
                        client,
                        base=base,
                        ticket_id=ticket_id,
                        method="PATCH",
                        url=item_url,
                        json_body=rest,
                        headers_builder=sap_patch_headers,
                        csrf_service_root=_po_csrf_root(
                            base, document_type=document_type
                        ),
                    )
                    if resp.status_code >= 400:
                        return None, _sap_error_message(resp)
                dd_err = await _patch_po_schedule_delivery_if_needed(
                    client,
                    base=base,
                    po_number=po_number,
                    item_no=item_no,
                    snapshot=snapshot,
                    block=_po_form_block_for_item(form, item_no),
                    ticket_id=ticket_id,
                    document_type=document_type,
                )
                if dd_err:
                    return None, dd_err
                continue

            skip_item = _po_item_patch_is_noop(
                snapshot,
                patch_body,
                acct_patches=acct_patches,
                acct_creates=acct_creates,
                acct_deletes=acct_deletes,
            )
            if patch_body and not skip_item:
                resp = await _request_with_csrf_retry(
                    client,
                    base=base,
                    ticket_id=ticket_id,
                    method="PATCH",
                    url=item_url,
                    json_body=patch_body,
                    headers_builder=sap_patch_headers,
                    csrf_service_root=_po_csrf_root(base, document_type=document_type),
                )
                if resp.status_code >= 400:
                    return None, _sap_error_message(resp)

            dd_err = await _patch_po_schedule_delivery_if_needed(
                client,
                base=base,
                po_number=po_number,
                item_no=item_no,
                snapshot=snapshot,
                block=_po_form_block_for_item(form, item_no),
                ticket_id=ticket_id,
                document_type=document_type,
            )
            if dd_err:
                return None, dd_err

            for acct_no, acct_body in acct_patches:
                acct_url = po_acct_assgmt_entity_url(
                    base,
                    po_number=po_number,
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
                    csrf_service_root=_po_csrf_root(base, document_type=document_type),
                )
                if resp.status_code >= 400:
                    return None, _sap_error_message(resp)
            for acct_body in acct_creates:
                err_acct = await _sap_create_po_acct_assgmt(
                    client, base=base, acct_body=acct_body, ticket_id=ticket_id
                )
                if err_acct:
                    seq = _norm(acct_body.get("AccountAssignmentNumber"))
                    return None, f"Create PO acct {item_no}/{seq}: {err_acct}"
            for acct_no in acct_deletes:
                err_del_acct = await _sap_mark_po_acct_deleted(
                    client,
                    base=base,
                    po_number=po_number,
                    item_number=item_no,
                    acct_assgmt_number=acct_no,
                    ticket_id=ticket_id,
                )
                if err_del_acct:
                    return None, f"Delete PO acct {item_no}/{acct_no}: {err_del_acct}"

        for item_no in plan.items_to_mark_deleted:
            err_del = await _sap_mark_po_item_deleted(
                client,
                base=base,
                po_number=po_number,
                item_number=item_no,
                ticket_id=ticket_id,
            )
            if err_del:
                return None, f"Delete PO item {item_no}: {err_del}"

        before_root = before.get("d") if isinstance(before.get("d"), dict) else before
        sap_text_root: dict[str, Any] | None = None
        if isinstance(before_root, dict):
            sap_text_root = {
                sap_key: odata_norm(before_root.get(sap_key))
                for _, sap_key in PO_TEXT_SAP_FIELDS
            }
        texts_err = await _sap_post_po_texts(
            client,
            base=base,
            po_number=po_number,
            form=form,
            ticket_id=f"{ticket_id}-texts",
            sap_text_root=sap_text_root,
        )
        if texts_err:
            return None, f"PO long text update failed: {texts_err}"

        after, err = await _sap_get_po_body(
            client,
            base=base,
            po_number=po_number,
            ticket_id=f"{ticket_id}-verify",
            document_type=document_type,
        )
        if err:
            return None, f"Post-update read failed: {err}"
        assert after is not None
        mismatches = verify_po_read_against_form(
            after,
            form=form,
            document_type=document_type,
            ticket_id=ticket_id,
            parent_pr_number=parent_pr_number,
            creator_email=creator_email,
        )
        if mismatches:
            return None, "SAP verify after PO resubmit: " + "; ".join(mismatches[:5])

        log.info(
            "SAP PO update ok ticket=%s po_number=%s patch=%s delete=%s create=%s",
            ticket_id,
            po_number,
            len(plan.item_patches),
            len(plan.items_to_mark_deleted),
            len(plan.items_to_create),
        )
        return po_number, None


async def _sap_update_po(
    *,
    po_number: str,
    form: dict[str, Any],
    document_type: str,
    ticket_id: str,
    parent_pr_number: str | None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    try:
        return await _sap_update_po_once(
            po_number=po_number,
            form=form,
            document_type=document_type,
            ticket_id=ticket_id,
            parent_pr_number=parent_pr_number,
            creator_email=creator_email,
        )
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP request timed out"
    except httpx.RequestError as e:
        return None, f"SAP connection error: {e}"


async def try_recover_po(
    *, ticket_id: str, document_type: str | None = None
) -> tuple[str | None, str | None]:
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
            recovered, rec_err = await _try_recover_po_number(
                client, base=base, ticket_id=ticket_id
            )
            if recovered:
                return recovered, None
            if dt == "YSER":
                yser_po = await _try_recover_yser_po_by_ext_system(
                    client, base=base, ticket_id=ticket_id
                )
                if yser_po:
                    log.info(
                        "SAP YSER PO recovered via Extsourcesystem ticket=%s po_number=%s",
                        ticket_id,
                        yser_po,
                    )
                    return yser_po, None
            return None, rec_err
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP recovery search timed out"
    except httpx.RequestError as e:
        return None, f"SAP recovery connection error: {e}"


async def create_po(
    *,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str,
    parent_sap_id: str | None = None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    try:
        if _is_yser_po(document_type):
            payload = build_z_yser_po_post_body(
                form=form,
                po_number="",
                ticket_id=ticket_id,
                parent_pr_number=parent_sap_id,
                creator_email=creator_email,
            )
        else:
            payload = build_po_payload(
                form=form,
                document_type=document_type,
                po_number="",
                ticket_id=ticket_id,
                parent_pr_number=parent_sap_id,
                kind="PO",
                creator_email=creator_email,
            )
    except ValueError as e:
        return None, str(e)
    return await _sap_create_po(
        payload=payload,
        ticket_id=ticket_id,
        form=form,
        document_type=document_type,
        parent_pr_number=parent_sap_id,
        creator_email=creator_email,
    )


async def update_po(
    *,
    sap_id: str,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str,
    parent_sap_id: str | None = None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    po_number = _norm(sap_id)
    if not po_number:
        return None, "Missing SAP PO number for update"
    return await _sap_update_po(
        po_number=po_number,
        form=form,
        document_type=document_type,
        ticket_id=ticket_id,
        parent_pr_number=parent_sap_id,
        creator_email=creator_email,
    )


async def get_po(
    *,
    po_number: str,
    ticket_id: str = "read",
    document_type: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    po_number = _norm(po_number)
    if not po_number:
        return None, "PO number is required"
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
            return await _sap_get_po_body(
                client,
                base=base,
                po_number=po_number,
                ticket_id=ticket_id,
                document_type=dt,
            )
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP GET timed out"
    except httpx.RequestError as e:
        return None, f"SAP GET connection error: {e}"
