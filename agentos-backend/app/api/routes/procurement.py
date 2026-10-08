"""Procurement PR/PO — HTTP surface only; rules and persistence live in ``app.procurement``.

Patterns (same spirit as ``app.api.routes.o2c`` / approvals):

- **Thin router**: auth + map exceptions to status codes; no raw SQL or domain branching here beyond HTTP.
- **Pydantic contracts** in ``app.schemas.procurement``: ticket DTOs, PATCH (JSON or multipart + files).
- **Side effects / queries** in ``app.procurement.service``, ``reference_handlers``, ``multipart``.
- **Errors**: ``ValueError`` / ``LookupError`` from procurement code → ``HTTPException`` (ticket updates via ``ticket_http``).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import ProcurementTicket, User
from app.db.session import get_db
from app.procurement.field_schema import schema_payload
from app.procurement import service as proc_service
from app.procurement.service import PrAlreadyHasLinkedPoError
from app.procurement.multipart import read_upload_files
from app.procurement.reference_handlers import (
    cost_center_facet_options_payload,
    list_reference_value_dicts,
    search_reference_values_payload,
)
from app.procurement.reference_domains import REFERENCE_SEARCH_DEFAULT_LIMIT, REFERENCE_SEARCH_MAX_LIMIT
from app.procurement.reference_query import assert_searchable_domain
from app.procurement.ticket_http import raise_http_for_ticket_update
from app.procurement.ticket_links import link_fields_for_ticket, link_fields_for_tickets
from app.schemas.procurement import (
    ProcurementAuditEntryOut,
    ProcurementPoCreateFormPayload,
    ProcurementTicketCreateFormPayload,
    ProcurementTicketOut,
    ProcurementTicketPatchMultipartPayload,
    ProcurementTicketSyncStatusOut,
    ProcurementTicketUpdateBody,
)

router = APIRouter()


def _linked_po_conflict_http(exc: PrAlreadyHasLinkedPoError) -> HTTPException:
    detail: dict[str, str] = {"message": str(exc)}
    if exc.linked_po_id is not None:
        detail["linked_po_id"] = str(exc.linked_po_id)
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _to_out(t: ProcurementTicket, *, link_fields: dict | None = None) -> ProcurementTicketOut:
    from app.procurement.attachment_sync import public_attachments

    base = ProcurementTicketOut.model_validate(t)
    extra: dict = {
        "form_source": getattr(t, "form_source", "db"),
        "sap_form_read_error": getattr(t, "sap_form_read_error", None),
        "sap_no_active_lines": bool(getattr(t, "sap_no_active_lines", False)),
        "sap_attachment_hydrate_error": getattr(t, "sap_attachment_hydrate_error", None),
        "attachments": public_attachments(t.attachments),
    }
    if link_fields:
        extra.update(link_fields)
    return base.model_copy(update=extra)


async def _to_out_enriched(db: AsyncSession, t: ProcurementTicket) -> ProcurementTicketOut:
    links = await link_fields_for_ticket(db, t)
    return _to_out(t, link_fields=links)


async def _to_out_list_enriched(
    db: AsyncSession, tickets: list[ProcurementTicket]
) -> list[ProcurementTicketOut]:
    bundles = await link_fields_for_tickets(db, tickets)
    return [_to_out(t, link_fields=bundles.get(t.id)) for t in tickets]


@router.get("/schema")
async def get_schema(_user: Annotated[User, Depends(get_current_user)]):
    return schema_payload()


@router.get("/reference-values")
async def list_reference_values(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    domain: str,
    document_type: str | None = None,
    ticket_kind: str | None = Query(
        None,
        description="PR or PO — include rows scoped to this kind (and '' = both). Omit for rows with applies_to_kind='' only.",
    ),
    purchasing_org: str | None = Query(
        None,
        max_length=64,
        description="When domain is plant: filter to plants valid for this purchasing organisation (H→1MGH, L→1LFS, T→1MGT).",
    ),
    plant: str | None = Query(
        None,
        max_length=16,
        description="When domain is storage_location: only rows for this plant (``H001|3021`` or extra.plant).",
    ),
):
    try:
        return await list_reference_value_dicts(
            db,
            domain=domain,
            document_type=document_type,
            ticket_kind=ticket_kind,
            purchasing_org=purchasing_org,
            plant=plant,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.get("/reference-values/cost-center-facets")
async def list_cost_center_facet_options(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    document_type: str = Query(..., description="Workflow document type: YSER | YUNB | YAST"),
    ticket_kind: str | None = Query(None, description="PR or PO — same filter as other reference calls"),
    purchasing_org: str | None = Query(
        None,
        max_length=64,
        description="When set (including empty string), profit centre / department facets are scoped to this "
        "purchasing organisation when it matches a directory entity; omit for legacy unscoped lists.",
    ),
):
    """Distinct Entity / Profit centre / Department values from imported cost centre ``extra`` (searchable dropdowns)."""
    try:
        return await cost_center_facet_options_payload(
            db,
            document_type=document_type,
            ticket_kind=ticket_kind,
            purchasing_org=purchasing_org,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.get("/reference-values/search")
async def search_reference_values(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    domain: str,
    document_type: str = Query(..., description="Workflow document type: YSER | YUNB | YAST"),
    ticket_kind: str | None = Query(None, description="PR or PO — filters purchasing_doc_type duplicates"),
    q: str = Query("", max_length=200),
    company_code: str | None = Query(
        None,
        max_length=16,
        description="For vendor domain: optional filter matching vendor|cocd suffix / CoCd",
    ),
    material_group: str | None = Query(None, max_length=64, description="Material domain: exact extra.material_group"),
    service_group: str | None = Query(
        None, max_length=64, description="Service domain: exact extra.service_group or Material Group"
    ),
    cc_entity: str | None = Query(None, max_length=64, description="Cost center: facet on extra.Entity"),
    cc_profit_center: str | None = Query(None, max_length=64),
    cc_department: str | None = Query(None, max_length=64),
    cc_business_area: str | None = Query(
        None,
        max_length=64,
        description=(
            "Cost center: exact match on extra.Business Area (legacy single-sloc scope). "
            "Ignored when ``plant`` is set for cost_center (plant-union takes precedence)."
        ),
    ),
    plant: str | None = Query(
        None,
        max_length=16,
        description=(
            "storage_location: only rows for this plant. "
            "cost_center: Business Area must match any storage location under this plant."
        ),
    ),
    limit: int = Query(REFERENCE_SEARCH_DEFAULT_LIMIT, ge=1, le=REFERENCE_SEARCH_MAX_LIMIT),
    offset: int = Query(0, ge=0, le=500000),
):
    try:
        assert_searchable_domain(domain)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        return await search_reference_values_payload(
            db,
            domain=domain,
            document_type=document_type,
            ticket_kind=ticket_kind,
            q=q,
            company_code=company_code,
            limit=limit,
            offset=offset,
            material_group=material_group,
            service_group=service_group,
            cc_entity=cc_entity,
            cc_profit_center=cc_profit_center,
            cc_department=cc_department,
            cc_business_area=cc_business_area,
            plant=plant,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.get("/tickets", response_model=list[ProcurementTicketOut])
async def list_my_tickets(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    kind: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items = await proc_service.list_tickets(db, user_id=user.id, kind=kind, limit=limit, offset=offset)
    return await _to_out_list_enriched(db, items)


@router.get("/tickets/parent-prs", response_model=list[ProcurementTicketOut])
async def list_parent_prs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    items = await proc_service.list_parent_prs_for_po(db, user_id=user.id)
    return await _to_out_list_enriched(db, items)


@router.get("/tickets/{ticket_id}", response_model=ProcurementTicketOut)
async def get_ticket(
    ticket_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    t = await proc_service.get_ticket(db, ticket_id=ticket_id, user_id=user.id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return await _to_out_enriched(db, t)


def _to_sync_status_out(t: ProcurementTicket) -> ProcurementTicketSyncStatusOut:
    from app.procurement.attachment_sync import public_attachments

    base = ProcurementTicketSyncStatusOut.model_validate(t)
    return base.model_copy(
        update={
            "attachments": public_attachments(t.attachments),
            "sap_attachment_hydrate_error": getattr(t, "sap_attachment_hydrate_error", None),
        }
    )


@router.get("/tickets/{ticket_id}/sync-status", response_model=ProcurementTicketSyncStatusOut)
async def get_ticket_sync_status(
    ticket_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Poll attachment/sync flags without SAP form hydrate."""
    t = await proc_service.get_ticket_sync_status(db, ticket_id=ticket_id, user_id=user.id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return _to_sync_status_out(t)


@router.get("/tickets/{ticket_id}/audit", response_model=list[ProcurementAuditEntryOut])
async def get_ticket_audit(
    ticket_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(100, ge=1, le=300),
):
    rows = await proc_service.list_ticket_audit_entries(db, ticket_id=ticket_id, user_id=user.id, limit=limit)
    if rows is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return [ProcurementAuditEntryOut.from_row(a) for a in rows]


@router.get("/tickets/{pr_id}/prefill-po")
async def prefill_po(
    pr_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    pr = await proc_service.get_pr_for_po_prefill(db, pr_id=pr_id, user_id=user.id)
    if not pr or pr.kind != "PR":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PR not found")
    if not (pr.sap_id or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PR has no SAP id yet")
    try:
        await proc_service.ensure_pr_available_for_po_link(db, parent_pr_id=pr.id)
    except PrAlreadyHasLinkedPoError as e:
        raise _linked_po_conflict_http(e) from e
    form, sap_warn = await proc_service.build_po_prefill_form(pr)
    out: dict = {
        "form": form,
        "document_type": pr.document_type,
        "form_source": "db" if sap_warn else "sap",
    }
    if sap_warn:
        out["sap_prefill_fallback"] = True
        out["sap_prefill_message"] = sap_warn
    return out


@router.post("/tickets/pr", response_model=ProcurementTicketOut)
async def create_pr(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    payload: str = Form(..., description="JSON: {document_type, form}"),
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    try:
        body = ProcurementTicketCreateFormPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()) from e
    try:
        file_tuples = await read_upload_files(files)
        ticket = await proc_service.create_ticket(
            db,
            user=user,
            kind="PR",
            document_type=body.document_type,
            form=body.form,
            parent_pr_id=None,
            files=file_tuples,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    proc_service.schedule_procurement_sap_work(
        ticket_id=ticket.id,
        user_email=user.email,
        resubmit=False,
        sync_attachments=bool(file_tuples),
    )
    return await _to_out_enriched(db, ticket)


@router.post("/tickets/po", response_model=ProcurementTicketOut)
async def create_po(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    payload: str = Form(...),
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    try:
        body = ProcurementPoCreateFormPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()) from e
    try:
        parent_uuid = body.parent_pr_uuid()
        file_tuples = await read_upload_files(files)
        ticket = await proc_service.create_ticket(
            db,
            user=user,
            kind="PO",
            document_type=body.document_type,
            form=body.form,
            parent_pr_id=parent_uuid,
            files=file_tuples,
        )
    except PrAlreadyHasLinkedPoError as e:
        raise _linked_po_conflict_http(e) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    proc_service.schedule_procurement_sap_work(
        ticket_id=ticket.id,
        user_email=user.email,
        resubmit=False,
        sync_attachments=bool(file_tuples),
    )
    return await _to_out_enriched(db, ticket)


@router.patch("/tickets/{ticket_id}", response_model=ProcurementTicketOut)
async def update_ticket(
    ticket_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Update ticket form and/or attachments in one call.

    **JSON** (``application/json``): ``ProcurementTicketUpdateBody`` — ``form``, ``version``, ``resync_sap``.

    **Multipart** (``multipart/form-data``): ``version`` (form field), ``payload`` (JSON string with optional
    ``form`` and ``resync_sap``), optional ``files`` parts — staged then uploaded to SAP AttachmentSet.
    """
    ct = (request.headers.get("content-type") or "").lower()
    resync_sap_flag = False
    try:
        if "multipart/form-data" in ct:
            fd = await request.form()
            ver_raw = fd.get("version")
            if ver_raw is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="multipart PATCH requires a form field `version` (integer).",
                )
            try:
                version = int(ver_raw)
            except (TypeError, ValueError) as e:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="`version` must be an integer.",
                ) from e
            payload_raw = fd.get("payload")
            if payload_raw is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="multipart PATCH requires a form field `payload` (JSON string).",
                )
            if not isinstance(payload_raw, str):
                payload_raw = str(payload_raw)
            try:
                mp = ProcurementTicketPatchMultipartPayload.model_validate_json(payload_raw)
            except ValidationError as e:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()) from e
            # ``request.form()`` yields ``starlette.datastructures.UploadFile``, not
            # ``fastapi.datastructures.UploadFile`` (a subclass) — ``isinstance(..., UploadFile)``
            # from FastAPI would wrongly drop every file.
            raw_files = [x for x in fd.getlist("files") if isinstance(x, StarletteUploadFile)]
            try:
                file_tuples = await read_upload_files(raw_files) if raw_files else None
            except ValueError as e:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
            resync_sap_flag = bool(mp.resync_sap)
            ticket = await proc_service.update_ticket(
                db,
                user=user,
                ticket_id=ticket_id,
                version=version,
                form=mp.form.model_dump() if mp.form is not None else None,
                files=file_tuples,
                resync_sap=resync_sap_flag,
            )
        else:
            try:
                body = ProcurementTicketUpdateBody.model_validate(await request.json())
            except ValidationError as e:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors()) from e
            resync_sap_flag = bool(body.resync_sap)
            ticket = await proc_service.update_ticket(
                db,
                user=user,
                ticket_id=ticket_id,
                version=body.version,
                form=body.form.model_dump() if body.form is not None else None,
                files=None,
                resync_sap=resync_sap_flag,
            )
    except (LookupError, ValueError) as e:
        raise_http_for_ticket_update(
            e, conflict_message="Version conflict — refresh and try again"
        )
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
    if proc_service.ticket_needs_background_sap_work(ticket):
        proc_service.schedule_procurement_sap_work(
            ticket_id=ticket.id,
            user_email=user.email,
            resubmit=resync_sap_flag,
            sync_attachments=proc_service.ticket_has_retryable_attachments(ticket),
        )
    return await _to_out_enriched(db, ticket)


@router.delete("/tickets/{ticket_id}/attachments/{attachment_id}", response_model=ProcurementTicketOut)
async def delete_ticket_attachment(
    ticket_id: UUID,
    attachment_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Remove a local (not yet in SAP) attachment from the ticket."""
    try:
        t = await proc_service.delete_attachment(
            db, user=user, ticket_id=ticket_id, attachment_id=attachment_id
        )
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from e
    except ValueError as e:
        if str(e) == "sap_delete_not_available":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Delete is not available for files already in SAP.",
            ) from e
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return await _to_out_enriched(db, t)


@router.post(
    "/tickets/{ticket_id}/attachments/{attachment_id}/retry",
    response_model=ProcurementTicketOut,
)
async def retry_ticket_attachment(
    ticket_id: UUID,
    attachment_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Re-queue a failed attachment upload to SAP (document must already exist)."""
    try:
        t = await proc_service.retry_attachment_upload(
            db, user=user, ticket_id=ticket_id, attachment_id=attachment_id
        )
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from e
    except ValueError as e:
        code = str(e)
        if code == "already_synced":
            raise HTTPException(status.HTTP_409_CONFLICT, "This file is already in SAP.") from e
        if code == "document_not_in_sap":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Save the document to SAP before retrying attachments.",
            ) from e
        if code == "reattach_required":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This file is no longer on the server. Remove it, attach it again, and save.",
            ) from e
        raise HTTPException(status.HTTP_400_BAD_REQUEST, code) from e
    proc_service.schedule_procurement_sap_work(
        ticket_id=t.id,
        user_email=user.email,
        resubmit=False,
        sync_attachments=True,
    )
    return await _to_out_enriched(db, t)


@router.get("/tickets/{ticket_id}/attachments/{attachment_id}/download")
async def download_attachment(
    ticket_id: UUID,
    attachment_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    try:
        data, mime, name = await proc_service.download_attachment(
            db, ticket_id=ticket_id, user_id=user.id, attachment_id=attachment_id
        )
    except LookupError as e:
        msg = str(e)
        if msg == "staging_gone":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "This file is no longer on the server. Remove it, attach it again, and save.",
            ) from e
        code = status.HTTP_404_NOT_FOUND if msg == "file_not_found" else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, "Not found") from e
    ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"'},
    )
