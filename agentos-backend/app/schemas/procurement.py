"""Pydantic request/response models for procurement PR/PO APIs (shared by routes and tests)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models import AuditLog


class LinkedTicketSummary(BaseModel):
    """Lightweight PR/PO row for cross-links in list/detail."""

    id: UUID
    kind: str
    sap_id: str | None = None
    document_type: str

    model_config = {"from_attributes": True}


class ProcurementTicketOut(BaseModel):
    """ORM-backed ticket row returned by list/get/create/patch."""

    id: UUID
    kind: str
    parent_pr_id: UUID | None
    document_type: str
    form: dict[str, Any]
    attachments: list[dict[str, Any]]
    drive_folder_id: str | None
    sap_id: str | None
    sap_sync: dict[str, Any]
    version: int
    created_at: Any
    updated_at: Any
    form_source: str = "db"
    sap_form_read_error: str | None = None
    sap_no_active_lines: bool = False
    sap_attachment_hydrate_error: str | None = None
    parent_pr_summary: LinkedTicketSummary | None = None
    linked_pos: list[LinkedTicketSummary] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ProcurementTicketSyncStatusOut(BaseModel):
    """Lightweight ticket sync slice for attachment-only polling."""

    id: UUID
    sap_id: str | None
    sap_sync: dict[str, Any]
    attachments: list[dict[str, Any]]
    version: int
    updated_at: Any
    sap_attachment_hydrate_error: str | None = None

    model_config = {"from_attributes": True}


class ProcurementFormShape(BaseModel):
    """Minimal shape contract — ``header`` is a dict, ``lines`` is a list of dicts.

    We keep the inner values as ``Any`` because procurement forms are workflow-specific
    and validated deeply by :func:`app.procurement.field_schema.validate_form`. The shape
    check is here to reject obvious garbage payloads (``form: 42`` / ``form: "hi"``) with
    a structured 422 before service code ever sees them.
    """

    header: dict[str, Any] = Field(default_factory=dict)
    lines: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class ProcurementTicketUpdateBody(BaseModel):
    form: ProcurementFormShape | None = None
    version: int
    resync_sap: bool = False


class ProcurementTicketPatchMultipartPayload(BaseModel):
    """JSON string inside multipart field ``payload`` for ``PATCH /tickets/{id}`` with optional files."""

    form: ProcurementFormShape | None = None
    resync_sap: bool = False


class ProcurementAuditEntryOut(BaseModel):
    id: UUID
    action: str
    resource_type: str
    resource_id: str | None
    actor_user_id: UUID | None
    details: dict[str, Any] | None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(cls, a: AuditLog) -> ProcurementAuditEntryOut:
        return cls(
            id=a.id,
            action=a.action,
            resource_type=a.resource_type,
            resource_id=a.resource_id,
            actor_user_id=a.actor_user_id,
            details=a.details,
            created_at=a.created_at.isoformat(),
        )


class ProcurementTicketCreateFormPayload(BaseModel):
    """JSON envelope inside multipart ``payload`` for PR create (and base for PO)."""

    document_type: str = ""
    form: dict[str, Any] = Field(default_factory=dict)

    @field_validator("form", mode="before")
    @classmethod
    def _form_must_be_object(cls, v: Any) -> dict[str, Any]:
        if isinstance(v, dict):
            return v
        return {}


class ProcurementPoCreateFormPayload(ProcurementTicketCreateFormPayload):
    """PO multipart JSON: same as PR plus optional ``parent_pr_id``."""

    parent_pr_id: Any = None

    def parent_pr_uuid(self) -> UUID | None:
        """Missing/empty → ``None``; invalid UUID string → ``ValueError`` (map to HTTP 400 in the route)."""
        raw = self.parent_pr_id
        if raw is None or str(raw).strip() in ("", "null"):
            return None
        try:
            return UUID(str(raw).strip())
        except ValueError as e:
            raise ValueError("Invalid parent_pr_id") from e
