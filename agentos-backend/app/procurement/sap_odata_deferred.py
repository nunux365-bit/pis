"""Follow OData V2 ``__deferred`` navigation links when ``$expand`` did not inline children."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

from app.procurement.sap_read_retry import (
    SAP_ODATA_READ_ATTEMPTS as ODATA_DEFERRED_GET_ATTEMPTS,
    SAP_ODATA_READ_RETRY_DELAY_S as ODATA_DEFERRED_RETRY_DELAY_S,
    should_retry_sap_odata_read_error,
    sleep_before_sap_read_retry,
)
from app.procurement.sap_odata_utils import (
    odata_deferred_uri,
    odata_entity_properties,
    odata_norm,
    odata_results_list,
    odata_text,
)

PR_ITEM_ACCT_EXPAND = "to_PurchaseReqnAcctAssgmt"

PO_ITEMS_NAV = "to_PurchaseOrderItem"
PO_SCHEDULE_NAV = "to_ScheduleLine"
PO_ACCT_NAV = "to_PurchaseOrderAccountAssignment"
PO_ACCT_CREATE_NAV = "to_AccountAssignment"
PO_ITEM_SCHEDULE_EXPAND = PO_SCHEDULE_NAV


def append_odata_expand(uri: str, expand: str) -> str:
    """Append ``$expand`` to a deferred URI if not already present."""
    if expand.lower() in uri.lower():
        return uri
    sep = "&" if "?" in uri else "?"
    return f"{uri}{sep}$expand={expand}"


def _std_po_items_read_url(base: str, po_number: str) -> str:
    """Item collection GET when header omits ``to_PurchaseOrderItem`` (QAS post-fix behaviour)."""
    from app.procurement.sap_po_payload import po_entity_url

    return append_odata_expand(
        f"{po_entity_url(base, po_number)}/{PO_ITEMS_NAV}",
        PO_ITEM_SCHEDULE_EXPAND,
    )


def _std_pr_items_read_url(base: str, pr_number: str) -> str:
    """Item collection GET when header omits ``to_PurchaseReqnItem``."""
    from app.procurement.sap_pr_payload import pr_entity_url

    return append_odata_expand(
        f"{pr_entity_url(base, pr_number)}/to_PurchaseReqnItem",
        PR_ITEM_ACCT_EXPAND,
    )


def pr_read_items_need_resolution(items_node: Any) -> bool:
    """True when item lines are missing or only available via deferred nav."""
    if odata_deferred_uri(items_node):
        return True
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        if odata_text(props.get("PurchaseRequisitionItem")):
            return False
    return True


async def _odata_get_json(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    url: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    from app.procurement.sap_pr_client import (
        _SapRequestError,
        _request_with_csrf_retry,
        _sap_error_message,
        sap_json_headers,
    )

    resp = await _request_with_csrf_retry(
        client,
        base=base,
        ticket_id=ticket_id,
        method="GET",
        url=url,
        json_body=None,
        headers_builder=lambda csrf_token: sap_json_headers(csrf_token=csrf_token),
        csrf_service_root=csrf_service_root,
    )
    if resp.status_code >= 400:
        raise _SapRequestError(_sap_error_message(resp))
    data = resp.json()
    if not isinstance(data, dict):
        raise _SapRequestError("SAP GET returned unexpected body")
    return data


async def _odata_get_json_with_retry(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    url: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    """Retry deferred child GETs — fail the whole read if still failing (no partial/stale UI)."""
    from app.procurement.sap_pr_client import _SapRequestError

    last_err: _SapRequestError | None = None
    for attempt in range(ODATA_DEFERRED_GET_ATTEMPTS):
        try:
            return await _odata_get_json(
                client,
                base=base,
                ticket_id=ticket_id,
                url=url,
                csrf_service_root=csrf_service_root,
            )
        except _SapRequestError as e:
            last_err = e
            if (
                attempt + 1 < ODATA_DEFERRED_GET_ATTEMPTS
                and should_retry_sap_odata_read_error(str(e))
            ):
                # QAS often returns transient 404 on deferred child nav (schedule/acct) before
                # the segment is materialized — retry, then succeed or fail the whole read.
                log_fn = log.debug if "404" in str(e) else log.warning
                log_fn(
                    "SAP deferred GET retry ticket=%s attempt=%s url=%s err=%s",
                    ticket_id,
                    attempt + 1,
                    url,
                    e,
                )
                await sleep_before_sap_read_retry(ODATA_DEFERRED_RETRY_DELAY_S)
                continue
            raise
    assert last_err is not None
    raise last_err


async def _resolve_pr_item_acct(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(item_row)
    acct_node = row.get("to_PurchaseReqnAcctAssgmt")
    if acct_node is None:
        props = odata_entity_properties(row)
        acct_node = props.get("to_PurchaseReqnAcctAssgmt")
    if odata_results_list(acct_node):
        return row
    uri = odata_deferred_uri(acct_node)
    if not uri:
        return row
    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-acct",
        url=uri,
        csrf_service_root=csrf_service_root,
    )
    if data is None:
        return row
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    row["to_PurchaseReqnAcctAssgmt"] = {"results": odata_results_list(inner)}
    return row


def _filter_items_for_pr(
    results: list[dict[str, Any]], *, pr_number: str
) -> list[dict[str, Any]]:
    pr_key = odata_norm(pr_number)
    if not pr_key:
        return results
    out: list[dict[str, Any]] = []
    for raw in results:
        props = odata_entity_properties(raw)
        row_pr = odata_text(props.get("PurchaseRequisition"))
        if not row_pr or row_pr == pr_key:
            out.append(raw)
    return out


async def resolve_pr_read_body(
    client: Any,
    *,
    base: str,
    body: dict[str, Any],
    pr_number: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    """Inline deferred PR item and account-assignment navigation on a header GET body."""
    wrapped = "d" in body and isinstance(body.get("d"), dict)
    root = body.get("d") if wrapped else body
    if not isinstance(root, dict):
        return body

    items_node = root.get("to_PurchaseReqnItem")
    if not pr_read_items_need_resolution(items_node):
        enriched: list[dict[str, Any]] = []
        for entry in odata_results_list(items_node):
            props = odata_entity_properties(entry)
            if not odata_text(props.get("PurchaseRequisitionItem")):
                continue
            merged = dict(entry)
            for key, val in props.items():
                if key not in merged or not merged.get(key):
                    merged[key] = val
            enriched.append(
                await _resolve_pr_item_acct(
                    client,
                    base=base,
                    ticket_id=ticket_id,
                    item_row=merged,
                    csrf_service_root=csrf_service_root,
                )
            )
        if enriched:
            root = {**root, "to_PurchaseReqnItem": {"results": enriched}}
        return {"d": root} if wrapped else root

    items_uri = odata_deferred_uri(items_node)
    if not items_uri:
        if odata_results_list(items_node):
            return body
        items_uri = _std_pr_items_read_url(base, pr_number)

    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-items",
        url=append_odata_expand(items_uri, PR_ITEM_ACCT_EXPAND),
        csrf_service_root=csrf_service_root,
    )
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    results = _filter_items_for_pr(odata_results_list(inner), pr_number=pr_number)

    enriched = []
    for raw in results:
        props = odata_entity_properties(raw)
        if not odata_text(props.get("PurchaseRequisitionItem")):
            continue
        merged = dict(raw)
        for key, val in props.items():
            if key not in merged or not merged.get(key):
                merged[key] = val
        enriched.append(
            await _resolve_pr_item_acct(
                client,
                base=base,
                ticket_id=ticket_id,
                item_row=merged,
                csrf_service_root=csrf_service_root,
            )
        )

    root = {**root, "to_PurchaseReqnItem": {"results": enriched}}
    return {"d": root} if wrapped else root


async def _resolve_z_item_services(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(item_row)
    props = odata_entity_properties(row)
    svc_node = row.get("to_Services")
    if svc_node is None:
        svc_node = props.get("to_Services")
    if odata_results_list(svc_node):
        return row
    uri = odata_deferred_uri(svc_node)
    if not uri:
        return row
    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-z-svc",
        url=uri,
        csrf_service_root=csrf_service_root,
    )
    if data is None:
        return row
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    row["to_Services"] = {"results": odata_results_list(inner)}
    return row


async def resolve_z_pr_read_body(
    client: Any,
    *,
    base: str,
    body: dict[str, Any],
    pr_number: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    """Inline deferred ``to_Items`` / ``to_Services`` on Z ``PRHeaderSet`` GET bodies."""
    wrapped = "d" in body and isinstance(body.get("d"), dict)
    root = body.get("d") if wrapped else body
    if not isinstance(root, dict):
        return body

    items_node = root.get("to_Items")
    items_uri = odata_deferred_uri(items_node)
    if items_uri:
        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-z-items",
            url=items_uri,
            csrf_service_root=csrf_service_root,
        )
        inner = data.get("d") if isinstance(data.get("d"), dict) else data
        item_results = odata_results_list(inner)
    elif odata_results_list(items_node):
        item_results = odata_results_list(items_node)
    else:
        from app.procurement.sap_pr_z_payload import z_pr_entity_url

        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-z-items",
            url=f"{z_pr_entity_url(base, pr_number)}/to_Items",
            csrf_service_root=csrf_service_root,
        )
        inner = data.get("d") if isinstance(data.get("d"), dict) else data
        item_results = odata_results_list(inner)

    enriched: list[dict[str, Any]] = []
    for raw in item_results:
        props = odata_entity_properties(raw)
        if not odata_text(props.get("PRItem")):
            continue
        merged = dict(raw)
        for key, val in props.items():
            if key not in merged or not merged.get(key):
                merged[key] = val
        enriched.append(
            await _resolve_z_item_services(
                client,
                base=base,
                ticket_id=ticket_id,
                item_row=merged,
                csrf_service_root=csrf_service_root,
            )
        )

    root = {**root, "to_Items": {"results": enriched}}
    return {"d": root} if wrapped else root


def po_read_items_need_resolution(items_node: Any) -> bool:
    """True when PO item lines are missing or only available via deferred nav."""
    if odata_deferred_uri(items_node):
        return True
    for entry in odata_results_list(items_node):
        props = odata_entity_properties(entry)
        if odata_text(props.get("PurchaseOrderItem")):
            return False
    return True


def _filter_items_for_po(
    results: list[dict[str, Any]], *, po_number: str
) -> list[dict[str, Any]]:
    po_key = odata_norm(po_number)
    if not po_key:
        return results
    out: list[dict[str, Any]] = []
    for raw in results:
        props = odata_entity_properties(raw)
        row_po = odata_text(props.get("PurchaseOrder"))
        if not row_po or row_po == po_key:
            out.append(raw)
    return out


def _merge_po_item_row(raw: dict[str, Any]) -> dict[str, Any]:
    props = odata_entity_properties(raw)
    if not odata_text(props.get("PurchaseOrderItem")):
        return raw
    merged = dict(raw)
    for key, val in props.items():
        if key not in merged or not merged.get(key):
            merged[key] = val
    return merged


async def _resolve_po_item_schedule(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(item_row)
    props = odata_entity_properties(row)
    sched_node = row.get(PO_SCHEDULE_NAV) or props.get(PO_SCHEDULE_NAV)
    if odata_results_list(sched_node):
        return row
    uri = odata_deferred_uri(sched_node)
    if not uri:
        return row
    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-po-sched",
        url=uri,
        csrf_service_root=csrf_service_root,
    )
    if data is None:
        return row
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    row[PO_SCHEDULE_NAV] = {"results": odata_results_list(inner)}
    return row


async def _resolve_po_item_acct(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(item_row)
    props = odata_entity_properties(row)
    for nav in (PO_ACCT_CREATE_NAV, PO_ACCT_NAV):
        acct_node = row.get(nav) or props.get(nav)
        uri = odata_deferred_uri(acct_node)
        if not uri:
            if odata_results_list(acct_node):
                return row
            continue
        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-po-acct",
            url=uri,
            csrf_service_root=csrf_service_root,
        )
        if data is None:
            continue
        inner = data.get("d") if isinstance(data.get("d"), dict) else data
        results = odata_results_list(inner)
        if results:
            row[nav] = {"results": results}
            return row
    return row


async def _resolve_po_item_children(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = await _resolve_po_item_schedule(
        client,
        base=base,
        ticket_id=ticket_id,
        item_row=item_row,
        csrf_service_root=csrf_service_root,
    )
    return await _resolve_po_item_acct(
        client,
        base=base,
        ticket_id=ticket_id,
        item_row=row,
        csrf_service_root=csrf_service_root,
    )


async def resolve_po_read_body(
    client: Any,
    *,
    base: str,
    body: dict[str, Any],
    po_number: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    """Inline deferred PO item, schedule line, and account-assignment navigation."""
    wrapped = "d" in body and isinstance(body.get("d"), dict)
    root = body.get("d") if wrapped else body
    if not isinstance(root, dict):
        return body

    items_node = root.get(PO_ITEMS_NAV)

    async def enrich_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for raw in entries:
            merged = _merge_po_item_row(raw)
            props = odata_entity_properties(merged)
            if not odata_text(props.get("PurchaseOrderItem")):
                continue
            enriched.append(
                await _resolve_po_item_children(
                    client,
                    base=base,
                    ticket_id=ticket_id,
                    item_row=merged,
                    csrf_service_root=csrf_service_root,
                )
            )
        return enriched

    if not po_read_items_need_resolution(items_node):
        enriched = await enrich_entries(odata_results_list(items_node))
        if enriched:
            root = {**root, PO_ITEMS_NAV: {"results": enriched}}
        return {"d": root} if wrapped else root

    items_uri = odata_deferred_uri(items_node)
    if not items_uri:
        if odata_results_list(items_node):
            return body
        items_uri = _std_po_items_read_url(base, po_number)

    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-po-items",
        url=append_odata_expand(items_uri, PO_ITEM_SCHEDULE_EXPAND),
        csrf_service_root=csrf_service_root,
    )
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    results = _filter_items_for_po(odata_results_list(inner), po_number=po_number)
    enriched = await enrich_entries(results)
    root = {**root, PO_ITEMS_NAV: {"results": enriched}}
    return {"d": root} if wrapped else root


PO_SERVICES_NAV = "to_Services"
PO_SERVICE_ACCT_NAV = "to_AccountAssignment"


async def _resolve_z_po_service_acct(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    service_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(service_row)
    props = odata_entity_properties(row)
    acct_node = row.get(PO_SERVICE_ACCT_NAV)
    if acct_node is None:
        acct_node = props.get(PO_SERVICE_ACCT_NAV)
    if odata_results_list(acct_node):
        return row
    uri = odata_deferred_uri(acct_node)
    if not uri:
        return row
    data = await _odata_get_json_with_retry(
        client,
        base=base,
        ticket_id=f"{ticket_id}-z-po-acct",
        url=uri,
        csrf_service_root=csrf_service_root,
    )
    if data is None:
        return row
    inner = data.get("d") if isinstance(data.get("d"), dict) else data
    row[PO_SERVICE_ACCT_NAV] = {"results": odata_results_list(inner)}
    return row


async def _resolve_z_po_item_services(
    client: Any,
    *,
    base: str,
    ticket_id: str,
    item_row: dict[str, Any],
    csrf_service_root: str | None,
) -> dict[str, Any]:
    row = dict(item_row)
    props = odata_entity_properties(row)
    svc_node = row.get(PO_SERVICES_NAV)
    if svc_node is None:
        svc_node = props.get(PO_SERVICES_NAV)
    uri = odata_deferred_uri(svc_node)
    if uri:
        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-z-po-svc",
            url=uri,
            csrf_service_root=csrf_service_root,
        )
        if data is None:
            svc_results = []
        else:
            inner = data.get("d") if isinstance(data.get("d"), dict) else data
            svc_results = odata_results_list(inner)
    else:
        svc_results = odata_results_list(svc_node)

    enriched: list[dict[str, Any]] = []
    for raw in svc_results:
        merged = dict(raw)
        for key, val in odata_entity_properties(raw).items():
            if key not in merged or not merged.get(key):
                merged[key] = val
        enriched.append(
            await _resolve_z_po_service_acct(
                client,
                base=base,
                ticket_id=ticket_id,
                service_row=merged,
                csrf_service_root=csrf_service_root,
            )
        )
    row[PO_SERVICES_NAV] = {"results": enriched}
    return row


async def resolve_z_po_read_body(
    client: Any,
    *,
    base: str,
    body: dict[str, Any],
    po_number: str,
    ticket_id: str,
    csrf_service_root: str | None = None,
) -> dict[str, Any]:
    """Inline deferred ``to_Services`` / service ``to_AccountAssignment`` on Z PO GET."""
    wrapped = "d" in body and isinstance(body.get("d"), dict)
    root = body.get("d") if wrapped else body
    if not isinstance(root, dict):
        return body

    items_node = root.get(PO_ITEMS_NAV)
    items_uri = odata_deferred_uri(items_node)
    z_po_items_expand = f"{PO_SERVICES_NAV}/{PO_SERVICE_ACCT_NAV}"
    if items_uri:
        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-z-po-items",
            url=append_odata_expand(items_uri, z_po_items_expand),
            csrf_service_root=csrf_service_root,
        )
        inner = data.get("d") if isinstance(data.get("d"), dict) else data
        item_results = odata_results_list(inner)
    elif odata_results_list(items_node):
        item_results = odata_results_list(items_node)
    else:
        from app.procurement.sap_po_z_payload import z_po_entity_url

        data = await _odata_get_json_with_retry(
            client,
            base=base,
            ticket_id=f"{ticket_id}-z-po-items",
            url=append_odata_expand(
                f"{z_po_entity_url(base, po_number)}/{PO_ITEMS_NAV}",
                z_po_items_expand,
            ),
            csrf_service_root=csrf_service_root,
        )
        inner = data.get("d") if isinstance(data.get("d"), dict) else data
        item_results = odata_results_list(inner)

    enriched: list[dict[str, Any]] = []
    for raw in item_results:
        props = odata_entity_properties(raw)
        if not odata_text(props.get("PurchaseOrderItem")):
            continue
        merged = dict(raw)
        for key, val in props.items():
            if key not in merged or not merged.get(key):
                merged[key] = val
        enriched.append(
            await _resolve_z_po_item_services(
                client,
                base=base,
                ticket_id=ticket_id,
                item_row=merged,
                csrf_service_root=csrf_service_root,
            )
        )

    root = {**root, PO_ITEMS_NAV: {"results": enriched}}
    return {"d": root} if wrapped else root
