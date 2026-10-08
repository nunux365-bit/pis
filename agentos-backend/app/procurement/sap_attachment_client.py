"""SAP ``Z_PURCHASE_REQUISITION_SRV`` / ``AttachmentSet`` (PR and PO attachments)."""

from __future__ import annotations

import logging
from typing import Any
import httpx

from app.procurement.sap_odata_utils import (
    odata_base_root,
    odata_entity_key,
    odata_results_list,
    odata_text,
)
from app.procurement.sap_pr_client import (
    _SapRequestError,
    _credentials_or_error,
    _fetch_csrf,
    _fetch_csrf_refresh,
    _httpx_timeout,
    _is_csrf_validation_failure,
    _sap_error_message,
    sap_gateway_query_params,
    sap_json_headers,
    sap_pr_configured,
)
from app.procurement.sap_pr_z_payload import Z_PR_SERVICE_PATH

log = logging.getLogger(__name__)

Z_ATTACHMENT_COLLECTION = "/sap/opu/odata/sap/Z_PURCHASE_REQUISITION_SRV/AttachmentSet"


def sap_attachment_configured() -> bool:
    return sap_pr_configured()


def z_attachment_collection_url(base_url: str) -> str:
    return f"{odata_base_root(base_url)}{Z_ATTACHMENT_COLLECTION}"


def z_attachment_entity_url(base_url: str, document_id: str) -> str:
    key = odata_entity_key(document_id)
    if not key:
        raise ValueError("document_id is required")
    return f"{z_attachment_collection_url(base_url)}('{key}')"


def attachment_filter_document_id(*, kind: str, sap_id: str) -> str:
    """Filter value for list GET — ``PR-1040000077`` / ``PO-4500000123``."""
    k = (kind or "").upper()
    prefix = "PR" if k == "PR" else "PO"
    sid = str(sap_id or "").strip()
    return f"{prefix}-{sid}"


def attachment_upload_slug(*, filename: str, kind: str, sap_id: str) -> str:
    name = (filename or "attachment.pdf").strip() or "attachment.pdf"
    return f"{name};{attachment_filter_document_id(kind=kind, sap_id=sap_id)}"


def _z_service_root(base: str) -> str:
    return f"{odata_base_root(base)}{Z_PR_SERVICE_PATH}"


def _attachment_post_headers(
    *,
    csrf_token: str,
    filename: str,
    kind: str,
    sap_id: str,
    content_type: str,
) -> dict[str, str]:
    return {
        **sap_json_headers(csrf_token=csrf_token),
        "Content-Type": (content_type or "application/pdf").strip() or "application/pdf",
        "slug": attachment_upload_slug(filename=filename, kind=kind, sap_id=sap_id),
    }


def _parse_create_response(data: dict[str, Any]) -> str | None:
    root = data.get("d") if isinstance(data.get("d"), dict) else data
    if not isinstance(root, dict):
        return None
    doc_id = odata_text(root.get("DocumentId"))
    return doc_id or None


def _parse_list_row(entry: dict[str, Any]) -> dict[str, Any] | None:
    doc_id = odata_text(entry.get("DocumentId"))
    if not doc_id:
        return None
    name = odata_text(entry.get("FileName")) or "attachment"
    mime = odata_text(entry.get("MimeType")) or "application/pdf"
    created = odata_text(entry.get("CreatedOn")) or None
    return {
        "sap_document_id": doc_id,
        "name": name,
        "mime_type": mime,
        "sap_synced_at": created,
    }


async def list_attachments(
    *,
    ticket_id: str,
    kind: str,
    sap_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """GET ``AttachmentSet`` filtered by ``PR-{sap_id}`` / ``PO-{sap_id}``."""
    creds, err = _credentials_or_error()
    if err:
        return [], err
    base, user, password = creds
    url = z_attachment_collection_url(base)
    csrf_root = _z_service_root(base)
    filter_val = attachment_filter_document_id(kind=kind, sap_id=sap_id)
    flt = f"DocumentId eq '{filter_val}'"

    try:
        from app.config.settings import settings

        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            csrf, err = await _fetch_csrf(
                client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
            )
            if err:
                return [], err

            async def _get(token: str) -> httpx.Response:
                hdrs = sap_json_headers(csrf_token=token)
                return await client.get(
                    url,
                    params={**sap_gateway_query_params(), "$filter": flt},
                    headers=hdrs,
                )

            resp = await _get(csrf)
            if _is_csrf_validation_failure(resp):
                csrf, err = await _fetch_csrf_refresh(
                    client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
                )
                if err:
                    return [], err
                resp = await _get(csrf)

            if resp.status_code >= 400:
                return [], _sap_error_message(resp)
            try:
                data = resp.json()
            except Exception:
                return [], "SAP attachment list returned non-JSON body"
            if not isinstance(data, dict):
                return [], "SAP attachment list unexpected body"
            root = data.get("d")
            rows: list[dict[str, Any]] = []
            for entry in odata_results_list(root):
                if not isinstance(entry, dict):
                    continue
                parsed = _parse_list_row(entry)
                if parsed:
                    rows.append(parsed)
            return rows, None
    except _SapRequestError as e:
        return [], str(e)
    except httpx.TimeoutException:
        return [], "SAP attachment list timed out"
    except httpx.RequestError as e:
        return [], f"SAP attachment list connection error: {e}"


async def create_attachment(
    *,
    ticket_id: str,
    kind: str,
    sap_id: str,
    filename: str,
    file_bytes: bytes,
    content_type: str = "application/pdf",
) -> tuple[str | None, str | None]:
    """POST raw bytes to ``AttachmentSet``. Returns ``(DocumentId, error)``."""
    creds, err = _credentials_or_error()
    if err:
        return None, err
    base, user, password = creds
    url = z_attachment_collection_url(base)
    csrf_root = _z_service_root(base)
    mime = (content_type or "application/pdf").strip() or "application/pdf"

    try:
        from app.config.settings import settings

        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
                csrf, err = await _fetch_csrf(
                    client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
                )
                if err:
                    return None, err

                async def _post(token: str) -> httpx.Response:
                    hdrs = _attachment_post_headers(
                        csrf_token=token,
                        filename=filename,
                        kind=kind,
                        sap_id=sap_id,
                        content_type=mime,
                    )
                    return await client.post(
                        url,
                        params=sap_gateway_query_params(),
                        content=file_bytes,
                        headers=hdrs,
                    )

                resp = await _post(csrf)
                if _is_csrf_validation_failure(resp):
                    csrf, err = await _fetch_csrf_refresh(
                        client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
                    )
                    if err:
                        return None, err
                    resp = await _post(csrf)

                if resp.status_code >= 400:
                    return None, _sap_error_message(resp)
                try:
                    data = resp.json()
                except Exception:
                    return None, "SAP attachment POST returned non-JSON body"
                if not isinstance(data, dict):
                    return None, "SAP attachment POST unexpected body"
                doc_id = _parse_create_response(data)
                if not doc_id:
                    return None, "SAP attachment POST missing DocumentId"
                return doc_id, None
    except _SapRequestError as e:
        return None, str(e)
    except httpx.TimeoutException:
        return None, "SAP attachment upload timed out"
    except httpx.RequestError as e:
        return None, f"SAP attachment connection error: {e}"


async def download_attachment_bytes(
    *,
    ticket_id: str,
    document_id: str,
) -> tuple[bytes | None, str | None, str | None]:
    """GET ``AttachmentSet('…')/$value`` — returns ``(bytes, mime, error)``."""
    creds, err = _credentials_or_error()
    if err:
        return None, None, err
    base, user, password = creds
    try:
        url = f"{z_attachment_entity_url(base, document_id)}/$value"
    except ValueError as e:
        return None, None, str(e)
    csrf_root = _z_service_root(base)

    try:
        from app.config.settings import settings

        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            csrf, err = await _fetch_csrf(
                client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
            )
            if err:
                return None, None, err
            hdrs = {**sap_json_headers(csrf_token=csrf), "Accept": "*/*"}
            resp = await client.get(url, params=sap_gateway_query_params(), headers=hdrs)
            if _is_csrf_validation_failure(resp):
                csrf, err = await _fetch_csrf_refresh(
                    client, base=base, ticket_id=ticket_id, csrf_service_root=csrf_root
                )
                if err:
                    return None, None, err
                hdrs = {**sap_json_headers(csrf_token=csrf), "Accept": "*/*"}
                resp = await client.get(url, params=sap_gateway_query_params(), headers=hdrs)
            if resp.status_code >= 400:
                return None, None, _sap_error_message(resp)
            mime = (resp.headers.get("content-type") or "application/pdf").split(";")[0].strip()
            return resp.content, mime, None
    except httpx.TimeoutException:
        return None, None, "SAP attachment download timed out"
    except httpx.RequestError as e:
        return None, None, f"SAP attachment download connection error: {e}"
