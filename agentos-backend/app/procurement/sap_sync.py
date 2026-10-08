"""SAP sync entrypoint: live PR/PO via OData when configured."""

from __future__ import annotations

from typing import Any

from app.procurement import sap_po_client, sap_pr_client


async def try_recover_pr(
    *, ticket_id: str, document_type: str | None = None
) -> tuple[str | None, str | None]:
    """Find an existing SAP PR by AgentOS tag (item text, then legacy header; YSER: + Z ShortText/HeaderNote)."""
    if sap_pr_client.sap_pr_configured():
        return await sap_pr_client.try_recover_pr(
            ticket_id=ticket_id, document_type=document_type
        )
    return None, None


async def try_recover_po(
    *, ticket_id: str, document_type: str | None = None
) -> tuple[str | None, str | None]:
    """Find an existing SAP PO by AgentOS tag (``CorrespncExternalReference``; YSER: + Z ``Extsourcesystem``)."""
    if sap_po_client.sap_po_configured():
        return await sap_po_client.try_recover_po(
            ticket_id=ticket_id, document_type=document_type
        )
    return None, None


async def create_pr(
    *, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    if not sap_pr_client.sap_pr_configured():
        return None, "SAP credentials not configured for PR create"
    return await sap_pr_client.create_pr(
        ticket_id=ticket_id, form=form, document_type=document_type
    )


async def update_pr(
    *, sap_id: str, ticket_id: str, form: dict[str, Any], document_type: str
) -> tuple[str | None, str | None]:
    if not sap_pr_client.sap_pr_configured():
        return None, "SAP credentials not configured for PR update"
    return await sap_pr_client.update_pr(
        sap_id=sap_id,
        ticket_id=ticket_id,
        form=form,
        document_type=document_type,
    )


async def create_po(
    *,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str,
    parent_sap_id: str | None = None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    if not sap_po_client.sap_po_configured():
        return None, "SAP credentials not configured for PO create"
    return await sap_po_client.create_po(
        ticket_id=ticket_id,
        form=form,
        document_type=document_type,
        parent_sap_id=parent_sap_id,
        creator_email=creator_email,
    )


async def update_po(
    *,
    sap_id: str,
    ticket_id: str,
    form: dict[str, Any],
    document_type: str,
    parent_sap_id: str | None,
    creator_email: str | None = None,
) -> tuple[str | None, str | None]:
    if not sap_po_client.sap_po_configured():
        return None, "SAP credentials not configured for PO update"
    return await sap_po_client.update_po(
        sap_id=sap_id,
        ticket_id=ticket_id,
        form=form,
        document_type=document_type,
        parent_sap_id=parent_sap_id,
        creator_email=creator_email,
    )
