"""Stage files locally and upload to SAP AttachmentSet (serial); background retry for failures."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.models import ProcurementTicket
from app.procurement import attachment_staging
from app.procurement.sap_attachment_client import (
    create_attachment,
    list_attachments,
    sap_attachment_configured,
)
from app.procurement.sap_config import effective_sap_max_attempts

log = logging.getLogger(__name__)

ATTACHMENT_RETRY_BASE_SECONDS = 60


def new_attachment_meta(
    *,
    filename: str,
    mime: str,
    size: int,
    staging_path: str,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": filename,
        "mime_type": mime or "application/pdf",
        "size": size,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "sap_document_id": None,
        "sap_synced_at": None,
        "sap_sync_error": None,
        "sap_sync_attempt_count": 0,
        "staging_path": staging_path,
    }


def attachment_storage(row: dict[str, Any]) -> str:
    """``sap`` when the file exists in SAP; otherwise ``local`` (AgentOS staging / not uploaded)."""
    if str(row.get("sap_document_id") or "").strip():
        return "sap"
    return "local"


def public_attachment_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip internal staging path from API responses."""
    out = {k: v for k, v in row.items() if k != "staging_path"}
    out["can_retry_upload"] = attachment_retryable(row)
    out["storage"] = attachment_storage(row)
    return out


def public_attachments(rows: list | None) -> list[dict[str, Any]]:
    return [public_attachment_row(a) for a in (rows or []) if isinstance(a, dict)]


def hidden_sap_attachment_ids(sync: dict[str, Any] | None) -> set[str]:
    """DocumentIds the user removed in AgentOS (SAP copy may still exist)."""
    raw = sync.get("hidden_sap_attachment_ids") if isinstance(sync, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def record_hidden_sap_attachment(sync: dict[str, Any], document_id: str) -> None:
    doc = str(document_id or "").strip()
    if not doc:
        return
    hidden = list(hidden_sap_attachment_ids(sync))
    if doc not in hidden:
        hidden.append(doc)
    sync["hidden_sap_attachment_ids"] = hidden


def attachment_row_from_sap(sap_row: dict[str, Any]) -> dict[str, Any]:
    """New in-memory row for a file present in SAP but not yet tracked in AgentOS."""
    return {
        "id": str(uuid.uuid4()),
        "name": str(sap_row.get("name") or "attachment"),
        "mime_type": str(sap_row.get("mime_type") or "application/pdf"),
        "size": 0,
        "uploaded_at": sap_row.get("sap_synced_at") or datetime.now(UTC).isoformat(),
        "sap_document_id": str(sap_row.get("sap_document_id") or "").strip(),
        "sap_synced_at": sap_row.get("sap_synced_at"),
        "sap_sync_error": None,
        "sap_sync_attempt_count": 0,
        "sap_source": "sap",
    }


def merge_attachments_with_sap(
    db_rows: list[dict[str, Any]] | None,
    sap_rows: list[dict[str, Any]] | None,
    *,
    hidden_doc_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Merge DB attachment rows with SAP list; preserve pending uploads and user removals."""
    hidden = hidden_doc_ids or set()
    by_doc_id: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []

    for row in db_rows or []:
        if not isinstance(row, dict):
            continue
        merged.append(row)
        doc_id = str(row.get("sap_document_id") or "").strip()
        if doc_id:
            by_doc_id[doc_id] = row

    for sap in sap_rows or []:
        if not isinstance(sap, dict):
            continue
        doc_id = str(sap.get("sap_document_id") or "").strip()
        if not doc_id or doc_id in hidden:
            continue
        existing = by_doc_id.get(doc_id)
        if existing is not None:
            if sap.get("name"):
                existing["name"] = sap["name"]
            if sap.get("mime_type"):
                existing["mime_type"] = sap["mime_type"]
            if sap.get("sap_synced_at") and not existing.get("sap_synced_at"):
                existing["sap_synced_at"] = sap["sap_synced_at"]
            continue
        merged.append(attachment_row_from_sap(sap))

    return merged


async def hydrate_attachments_from_sap(
    ticket: ProcurementTicket,
    *,
    ticket_id: str | None = None,
) -> str | None:
    """Merge SAP AttachmentSet list into ``ticket.attachments`` (in-memory only)."""
    if not sap_attachment_configured():
        return None
    sap_id = (ticket.sap_id or "").strip()
    if not sap_id or sap_id.startswith("#"):
        return None
    tid = ticket_id or str(ticket.id)
    sap_rows, err = await list_attachments(
        ticket_id=tid,
        kind=str(ticket.kind or "PR"),
        sap_id=sap_id,
    )
    if err:
        return err
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
    hidden = hidden_sap_attachment_ids(sync)
    ticket.attachments = merge_attachments_with_sap(
        ticket.attachments if isinstance(ticket.attachments, list) else [],
        sap_rows,
        hidden_doc_ids=hidden,
    )
    return None


def attachment_pending(row: dict[str, Any]) -> bool:
    """True when a staged file still needs upload to SAP (retryable)."""
    return attachment_retryable(row)


def attachment_failed(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("sap_document_id"):
        return False
    return bool((row.get("sap_sync_error") or "").strip())


def attachment_retryable(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("sap_document_id"):
        return False
    if not (row.get("staging_path") or "").strip():
        return False
    if int(row.get("sap_sync_attempt_count") or 0) >= effective_sap_max_attempts():
        return False
    return True


def ticket_has_pending_attachments(ticket: ProcurementTicket) -> bool:
    return ticket_has_retryable_attachments(ticket)


def ticket_has_retryable_attachments(ticket: ProcurementTicket) -> bool:
    return any(attachment_retryable(a) for a in (ticket.attachments or []) if isinstance(a, dict))


def ticket_has_failed_attachments(ticket: ProcurementTicket) -> bool:
    return any(attachment_failed(a) for a in (ticket.attachments or []) if isinstance(a, dict))


def attachment_failure_summary(ticket: ProcurementTicket, *, limit: int = 3) -> str | None:
    errors: list[str] = []
    for row in ticket.attachments or []:
        if not isinstance(row, dict) or not attachment_failed(row):
            continue
        name = str(row.get("name") or "attachment")
        err = str(row.get("sap_sync_error") or "SAP upload failed").strip()
        errors.append(f"{name}: {err}")
    if not errors:
        return None
    summary = "; ".join(errors[:limit])
    if len(errors) > limit:
        summary += f" (+{len(errors) - limit} more)"
    return summary


def reset_attachment_for_retry(row: dict[str, Any]) -> None:
    """Clear failure counters so a manual or background retry may run again."""
    row["sap_sync_error"] = None
    row["sap_sync_attempt_count"] = 0
    row["sap_next_retry_at"] = None


def _terminalize_attachment_failure(row: dict[str, Any], *, message: str) -> None:
    row["sap_sync_error"] = message
    row.pop("staging_path", None)
    row.pop("sap_next_retry_at", None)


def _schedule_attachment_retry(row: dict[str, Any]) -> None:
    n = int(row.get("sap_sync_attempt_count") or 0)
    delay = min(ATTACHMENT_RETRY_BASE_SECONDS * (2 ** max(0, n - 1)), 3600)
    row["sap_next_retry_at"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()


def _attachment_retry_due(row: dict[str, Any]) -> bool:
    raw = row.get("sap_next_retry_at")
    if not raw:
        return True
    try:
        due = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return due <= datetime.now(UTC)
    except ValueError:
        return True


def refresh_attachment_sync_flags(
    sync: dict[str, Any],
    ticket: ProcurementTicket,
    *,
    transient_error: str | None = None,
) -> None:
    """Update ``attachments_pending`` / ``attachment_last_error`` from attachment rows."""
    retryable = ticket_has_retryable_attachments(ticket)
    sync["attachments_pending"] = retryable
    if retryable:
        sync["attachment_last_error"] = transient_error
    else:
        sync["attachment_last_error"] = attachment_failure_summary(ticket) or None


async def sync_pending_attachments_for_ticket(
    ticket: ProcurementTicket,
    *,
    ticket_id: str,
) -> tuple[int, int, str | None]:
    """Upload each pending attachment to SAP. Returns ``(ok_count, fail_count, summary_error)``."""
    if not sap_attachment_configured():
        return 0, 0, "SAP not configured"
    sap_id = (ticket.sap_id or "").strip()
    if not sap_id:
        return 0, 0, "SAP document id missing"
    kind = str(ticket.kind or "PR").upper()
    atts = [a for a in (ticket.attachments or []) if isinstance(a, dict)]
    ok = 0
    fail = 0
    errors: list[str] = []
    max_attempts = effective_sap_max_attempts()

    for row in atts:
        if row.get("sap_document_id"):
            continue
        attempts = int(row.get("sap_sync_attempt_count") or 0)
        if attempts >= max_attempts and (row.get("staging_path") or "").strip():
            _terminalize_attachment_failure(
                row,
                message=str(row.get("sap_sync_error") or "attachment retry exhausted"),
            )
            fail += 1
            errors.append(f"{row.get('name')}: attachment retry exhausted")
            continue
        if not attachment_retryable(row):
            continue
        if not _attachment_retry_due(row):
            continue
        path = str(row.get("staging_path") or "").strip()
        if not path:
            _terminalize_attachment_failure(row, message="missing staging file")
            fail += 1
            errors.append(f"{row.get('name')}: missing staging file")
            continue
        try:
            file_bytes = attachment_staging.read_staging_file(ticket_id=ticket_id, path=path)
        except (FileNotFoundError, PermissionError):
            _terminalize_attachment_failure(row, message="staging file missing")
            fail += 1
            errors.append(f"{row.get('name')}: staging file missing")
            continue

        doc_id, err = await create_attachment(
            ticket_id=ticket_id,
            kind=kind,
            sap_id=sap_id,
            filename=str(row.get("name") or "attachment.pdf"),
            file_bytes=file_bytes,
            content_type=str(row.get("mime_type") or "application/pdf"),
        )
        if doc_id:
            attachment_staging.delete_staging_file(path, ticket_id=ticket_id)
            row["sap_document_id"] = doc_id
            row["sap_synced_at"] = datetime.now(UTC).isoformat()
            row["sap_sync_error"] = None
            row["sap_next_retry_at"] = None
            row.pop("staging_path", None)
            ok += 1
        else:
            row["sap_sync_attempt_count"] = int(row.get("sap_sync_attempt_count") or 0) + 1
            row["sap_sync_error"] = err or "SAP attachment upload failed"
            if int(row.get("sap_sync_attempt_count") or 0) >= max_attempts:
                _terminalize_attachment_failure(row, message=str(row["sap_sync_error"]))
            else:
                _schedule_attachment_retry(row)
            fail += 1
            errors.append(f"{row.get('name')}: {row['sap_sync_error']}")

    ticket.attachments = atts
    summary = "; ".join(errors[:3]) if errors else None
    if fail and ok:
        summary = (summary or "") + f" ({ok} ok, {fail} pending/failed)"
    elif fail and not ok:
        summary = summary or f"{fail} attachment(s) failed"
    return ok, fail, summary


def stage_uploaded_files(
    *,
    ticket_id: str,
    files: list[tuple[str, str, bytes]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filename, mime, data in files:
        path = attachment_staging.write_staging_file(
            ticket_id=ticket_id, filename=filename, data=data
        )
        rows.append(
            new_attachment_meta(
                filename=filename,
                mime=mime,
                size=len(data),
                staging_path=path,
            )
        )
    return rows
