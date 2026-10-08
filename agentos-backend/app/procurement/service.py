"""Procurement ticket business logic."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Integer, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config.settings import settings
from app.db.models import AuditLog, ProcurementTicket, User
from app.procurement import attachment_staging, email_notify, sap_sync
from app.procurement.attachment_sync import (
    attachment_failed,
    attachment_storage,
    hydrate_attachments_from_sap,
    public_attachments,
    refresh_attachment_sync_flags,
    reset_attachment_for_retry,
    stage_uploaded_files,
    sync_pending_attachments_for_ticket,
    ticket_has_pending_attachments,
    ticket_has_retryable_attachments,
)
from app.services.audit import write_audit
from app.procurement.catalogue_validation import (
    apply_vendor_payment_terms_from_catalogue,
    validate_form_against_catalogue,
)
from app.procurement.field_schema import PO_REQUESTOR_EMAIL_MAX_LEN, normalize_form, validate_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.po_sap_enrichment import (
    enrich_po_form_from_sap_pr,
    po_allocations_match_sap_pr,
)
from app.procurement.sap_pr_items import fetch_pr_items
from app.procurement.sap_order_unit import apply_line_order_units_from_reference
from app.procurement.sap_ticket_form_read import load_form_from_sap
from app.procurement.multipart import (
    MAX_PROCUREMENT_ATTACHMENT_BYTES,
    MAX_PROCUREMENT_ATTACHMENTS,
    content_matches_mime,
    resolve_procurement_upload_mime,
)
from app.procurement.sap_config import effective_sap_max_attempts

log = logging.getLogger(__name__)

_SAP_RECOVERY_TIMEOUT_SECONDS = 120.0

_MAX_ATTACHMENTS = MAX_PROCUREMENT_ATTACHMENTS
_MAX_FILE_BYTES = MAX_PROCUREMENT_ATTACHMENT_BYTES


def _default_sap_sync() -> dict[str, Any]:
    return {
        "attempt_count": 0,
        "next_retry_at": None,
        "last_error": None,
        "create_submitted": False,
        "attachments_pending": False,
        "attachment_last_error": None,
        "sync_pending": False,
        "retryable": True,
    }


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or ts == "null":
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except ValueError:
        return None


def _sap_failure_retryable(err: str | None) -> bool:
    """Do not backoff-retry auth, config, or validation failures — only transient/integration errors."""
    if not err:
        return True
    e = err.lower()
    non_retryable = (
        "not configured",
        "unauthorized",
        "forbidden",
        "missing sap pr number",
        "missing sap po number",
        "placeholder pr number",
        "no pr line items",
        "no po line items",
        "vendor",
        "payload error",
        "csrf fetch did not return",
    )
    return not any(s in e for s in non_retryable)


def _schedule_retry(sap_sync: dict[str, Any]) -> None:
    n = int(sap_sync.get("attempt_count") or 0)
    base = max(30, int(settings.procurement_sap_retry_base_seconds))
    delay = base * min(n + 1, 8)
    sap_sync["next_retry_at"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()


async def _po_creator_email_for_ticket(
    session: AsyncSession, ticket: ProcurementTicket
) -> str | None:
    """PO IncotermsLocation1: always the ticket creator's email, not the sync actor."""
    if (ticket.kind or "").upper() != "PO":
        return None
    uid = ticket.created_by_user_id
    if not uid:
        return None
    u = await session.get(User, uid)
    if not u:
        return None
    email = str(getattr(u, "email", None) or "").strip()
    return email or None


def _should_recover_before_create(sync: dict[str, Any]) -> bool:
    """Whether to scan SAP before POST create.

    First UI submit (no prior sync attempts) skips recovery. Run recovery when a create POST
    may already have reached SAP (``create_submitted``) or after a failed/resync attempt.
    """
    if sync.get("create_submitted"):
        return True
    return int(sync.get("attempt_count") or 0) > 0


async def _recover_sap_id_before_create(
    ticket: ProcurementTicket,
    session: AsyncSession,
    *,
    kind_u: str,
    doc_type: str,
    existing_sid: str,
) -> str | None:
    """Find an existing SAP PR/PO by AgentOS marker before POST create.

    Skipped when the ticket already has ``sap_id``. On miss or timeout, returns ``None``
    so the caller may create; recovery runs first on resync to avoid duplicate documents.
    """
    if existing_sid:
        return existing_sid
    await session.refresh(ticket, attribute_names=["sap_id"])
    sid = (ticket.sap_id or "").strip()
    if sid:
        return sid
    recover = sap_sync.try_recover_pr if kind_u == "PR" else sap_sync.try_recover_po
    try:
        recovered, _rec_err = await asyncio.wait_for(
            recover(ticket_id=str(ticket.id), document_type=doc_type),
            timeout=_SAP_RECOVERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("SAP recovery timed out before create ticket=%s", ticket.id)
        return None
    return (recovered or "").strip() or None


async def _run_sap_sync(
    ticket: ProcurementTicket, user_email: str, session: AsyncSession, *, resubmit: bool = False
) -> None:
    """Execute SAP sync (async HTTP for live PR); update ticket sap_sync / sap_id; email if exhausted.

    ``resubmit=True`` (from PATCH ``resync_sap``): send current form to SAP for the **existing**
    ``sap_id`` when set; do not allocate a new document number. If there is no ``sap_id`` yet,
    falls back to create (same as initial submission).
    """
    # Real SAP: enqueue post-commit instead of blocking this request when latency matters.
    form = ticket.form or {}
    kind_u = ticket.kind.upper()
    po_creator_email = await _po_creator_email_for_ticket(session, ticket)
    max_attempts = effective_sap_max_attempts()
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)

    prev = int(sync.get("attempt_count") or 0)
    parent_sap_id: str | None = None
    if ticket.kind.upper() == "PO" and ticket.parent_pr_id:
        res = await session.execute(
            select(ProcurementTicket.sap_id).where(ProcurementTicket.id == ticket.parent_pr_id)
        )
        parent_sap_id = res.scalar_one_or_none()

    existing_sid = (ticket.sap_id or "").strip()
    use_update = bool(resubmit and existing_sid)

    doc_type = str(ticket.document_type or "")

    is_pr_create = not use_update and kind_u == "PR"
    is_po_create = not use_update and kind_u == "PO"
    sap_id: str | None = None
    err: str | None = None

    if (is_pr_create or is_po_create) and not existing_sid and _should_recover_before_create(sync):
        sap_id = await _recover_sap_id_before_create(
            ticket,
            session,
            kind_u=kind_u,
            doc_type=doc_type,
            existing_sid=existing_sid,
        )

    if sap_id is None and err is None:
        if use_update:
            if ticket.kind.upper() == "PR":
                sap_id, err = await sap_sync.update_pr(
                    sap_id=existing_sid,
                    ticket_id=str(ticket.id),
                    form=form,
                    document_type=doc_type,
                )
            else:
                sap_id, err = await sap_sync.update_po(
                    sap_id=existing_sid,
                    ticket_id=str(ticket.id),
                    form=form,
                    document_type=doc_type,
                    parent_sap_id=parent_sap_id,
                    creator_email=po_creator_email,
                )
        elif kind_u == "PR":
            if is_pr_create:
                sync["create_submitted"] = True
            sap_id, err = await sap_sync.create_pr(
                ticket_id=str(ticket.id), form=form, document_type=doc_type
            )
        else:
            if is_po_create:
                sync["create_submitted"] = True
            sap_id, err = await sap_sync.create_po(
                ticket_id=str(ticket.id),
                form=form,
                document_type=doc_type,
                parent_sap_id=parent_sap_id,
                creator_email=po_creator_email,
            )

    if sap_id:
        ticket.sap_id = sap_id
        sync["last_error"] = None
        sync["next_retry_at"] = None
        sync["create_submitted"] = False
        sync["sync_pending"] = False
        sync["retryable"] = True
        log.info("SAP ok ticket=%s sap_id=%s", ticket.id, sap_id)
    else:
        sync["last_error"] = err or "Unknown SAP error"
        n = prev + 1
        sync["attempt_count"] = n
        if n >= max_attempts:
            sync["next_retry_at"] = None
            sync["sync_pending"] = False
            log.warning("SAP exhausted ticket=%s attempts=%s", ticket.id, n)
            if settings.procurement_sap_failure_email_enabled:
                try:
                    email_notify.send_mail(
                        to_email=user_email,
                        subject=f"[AgentOS] Procurement {ticket.kind} — SAP needs attention ({ticket.id})",
                        body_text=(
                            f"We could not sync this document with SAP after {max_attempts} attempts.\n\n"
                            f"Ticket id: {ticket.id}\n"
                            f"Last error: {sync['last_error']}\n\n"
                            f"Open the app to review or retry: "
                            f"{settings.procurement_app_public_url.rstrip('/')}/procurement/tickets/{ticket.id}\n"
                        ),
                    )
                except Exception as e:
                    log.exception("procurement email failed: %s", e)
            else:
                log.info(
                    "procurement SAP failure email skipped (procurement_sap_failure_email_enabled=false) ticket=%s",
                    ticket.id,
                )
        elif _sap_failure_retryable(err):
            sync["retryable"] = True
            _schedule_retry(sync)
            log.info("SAP retry scheduled ticket=%s next=%s", ticket.id, sync.get("next_retry_at"))
        else:
            sync["next_retry_at"] = None
            sync["retryable"] = False
            sync["sync_pending"] = False
            log.warning("SAP failed (non-retryable) ticket=%s err=%s", ticket.id, sync["last_error"])
    ticket.sap_sync = sync
    # JSONB with a plain ``dict`` mapping: SQLAlchemy does not auto-detect in-place mutations.
    # Force the attribute dirty so the UPDATE always carries the new ``sap_sync`` / ``sap_id``.
    flag_modified(ticket, "sap_sync")
    session.add(ticket)


def _mark_document_sync_deferred(ticket: ProcurementTicket) -> None:
    """Queue SAP create/update off the HTTP request (background task or retry job)."""
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)
    sync["sync_pending"] = True
    sync["retryable"] = True
    if not (ticket.sap_id or "").strip():
        sync["create_submitted"] = False
    # New attempt queued — do not show the previous failure until background work finishes.
    sync["last_error"] = None
    sync["next_retry_at"] = None
    ticket.sap_sync = sync
    flag_modified(ticket, "sap_sync")


def _mark_attachment_sync_deferred(ticket: ProcurementTicket) -> None:
    """Queue attachment upload only — document is already in SAP."""
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)
    sync["attachments_pending"] = True
    sync["attachment_last_error"] = None
    ticket.sap_sync = sync
    flag_modified(ticket, "sap_sync")


def _mark_sap_sync_deferred(ticket: ProcurementTicket) -> None:
    """Backward-compatible alias for document sync deferral."""
    _mark_document_sync_deferred(ticket)


def schedule_procurement_sap_work(
    *,
    ticket_id: UUID,
    user_email: str,
    resubmit: bool,
    sync_attachments: bool,
) -> None:
    """Queue SAP work on the running event loop (``run_procurement_sap_work`` sleeps briefly for commit)."""
    from app.infra.task_tracker import spawn

    spawn(
        run_procurement_sap_work(
            ticket_id,
            user_email,
            resubmit=resubmit,
            sync_attachments=sync_attachments,
        ),
        name=f"procurement-sap-{ticket_id}",
    )


async def run_procurement_sap_work(
    ticket_id: UUID,
    user_email: str,
    *,
    resubmit: bool,
    sync_attachments: bool,
) -> None:
    import asyncio

    from app.db.session import AsyncSessionLocal

    # Yield so the HTTP request's ``get_db`` commit finishes before we read the ticket.
    await asyncio.sleep(0.05)

    async with AsyncSessionLocal() as session:
        ticket = await session.get(ProcurementTicket, ticket_id)
        if not ticket:
            log.warning("background SAP sync: ticket not found %s", ticket_id)
            return
        sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
        pending_att = ticket_has_retryable_attachments(ticket)
        doc_pending = bool(sync.get("sync_pending"))
        att_pending = bool(sync.get("attachments_pending")) or pending_att
        if not doc_pending and not att_pending:
            return
        log.info(
            "background SAP work start ticket=%s sync_pending=%s attachments_pending=%s pending_att=%s",
            ticket_id,
            doc_pending,
            sync.get("attachments_pending"),
            pending_att,
        )
        try:
            sid = (ticket.sap_id or "").strip()
            if doc_pending and (not sid or resubmit):
                await _run_sap_sync(ticket, user_email, session, resubmit=bool(resubmit and sid))
            sid = (ticket.sap_id or "").strip()
            if sync_attachments and pending_att and sid:
                await _sync_attachments_after_sap(ticket)
            sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
            if sid and not sync.get("last_error"):
                sync["sync_pending"] = False
                refresh_attachment_sync_flags(sync, ticket)
                ticket.sap_sync = sync
                flag_modified(ticket, "sap_sync")
            session.add(ticket)
            await session.commit()
        except Exception:
            log.exception("background SAP sync failed ticket=%s", ticket_id)
            await session.rollback()
            await _recover_background_sap_work_failure(ticket_id)


async def _recover_background_sap_work_failure(ticket_id: UUID) -> None:
    """After a background worker crash, schedule document retry or clear stale flags."""
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        ticket = await session.get(ProcurementTicket, ticket_id)
        if not ticket:
            return
        sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
        if not isinstance(sync, dict):
            sync = _default_sap_sync()
        for k, v in _default_sap_sync().items():
            sync.setdefault(k, v)
        changed = False
        if sync.get("sync_pending") and not (ticket.sap_id or "").strip():
            n = int(sync.get("attempt_count") or 0) + 1
            sync["attempt_count"] = n
            max_attempts = effective_sap_max_attempts()
            if n >= max_attempts:
                sync["sync_pending"] = False
                sync["last_error"] = sync.get("last_error") or "Background SAP sync failed"
            else:
                _schedule_retry(sync)
            changed = True
        elif sync.get("sync_pending") and (ticket.sap_id or "").strip():
            sync["sync_pending"] = False
            changed = True
        refresh_attachment_sync_flags(sync, ticket)
        if changed:
            ticket.sap_sync = sync
            flag_modified(ticket, "sap_sync")
            session.add(ticket)
            await session.commit()


async def _sync_attachments_after_sap(ticket: ProcurementTicket) -> None:
    """Upload staged PDFs to SAP AttachmentSet when ``sap_id`` is set (serial POSTs)."""
    if not (ticket.sap_id or "").strip():
        return
    if not ticket_has_retryable_attachments(ticket):
        sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
        if not isinstance(sync, dict):
            sync = _default_sap_sync()
        for k, v in _default_sap_sync().items():
            sync.setdefault(k, v)
        refresh_attachment_sync_flags(sync, ticket)
        ticket.sap_sync = sync
        flag_modified(ticket, "sap_sync")
        return
    _ok, _fail, att_err = await sync_pending_attachments_for_ticket(
        ticket, ticket_id=str(ticket.id)
    )
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)
    refresh_attachment_sync_flags(sync, ticket, transient_error=att_err)
    ticket.sap_sync = sync
    flag_modified(ticket, "sap_sync")
    flag_modified(ticket, "attachments")


def ticket_needs_background_sap_work(ticket: ProcurementTicket) -> bool:
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
    return bool(sync.get("sync_pending")) or bool(sync.get("attachments_pending")) or ticket_has_retryable_attachments(
        ticket
    )


PR_ALREADY_HAS_LINKED_PO_MSG = (
    "This purchase request already has a linked purchase order. "
    "Open that order to update it, or raise a new purchase request."
)


class PrAlreadyHasLinkedPoError(ValueError):
    """Raised when a PR already has a linked PO ticket."""

    def __init__(self, *, linked_po_id: UUID | None) -> None:
        super().__init__(PR_ALREADY_HAS_LINKED_PO_MSG)
        self.linked_po_id = linked_po_id


async def ensure_pr_available_for_po_link(
    session: AsyncSession, *, parent_pr_id: UUID
) -> None:
    """One AgentOS PO per PR — block a second linked PO (SAP follow-on POs need a fresh PR)."""
    await session.execute(
        select(ProcurementTicket.id)
        .where(ProcurementTicket.id == parent_pr_id)
        .with_for_update()
    )
    res = await session.execute(
        select(ProcurementTicket.id)
        .where(
            ProcurementTicket.kind == "PO",
            ProcurementTicket.parent_pr_id == parent_pr_id,
        )
        .order_by(ProcurementTicket.updated_at.desc())
        .limit(1)
    )
    linked_po_id = res.scalar_one_or_none()
    if linked_po_id is not None:
        raise PrAlreadyHasLinkedPoError(linked_po_id=linked_po_id)


async def _validate_po_allocations_against_parent_pr(
    session: AsyncSession,
    *,
    ticket: ProcurementTicket,
    form: dict[str, Any],
) -> str | None:
    """Linked PO: cost centres must stay within the parent SAP PR account assignment."""
    if str(ticket.kind or "").upper() != "PO" or not ticket.parent_pr_id:
        return None
    parent = await session.get(ProcurementTicket, ticket.parent_pr_id)
    if not parent or not (parent.sap_id or "").strip():
        return None
    items, err = await fetch_pr_items(
        pr_number=(parent.sap_id or "").strip(),
        ticket_id=str(ticket.id),
        document_type=str(ticket.document_type or ""),
    )
    if err:
        return f"Could not verify account assignment against linked PR: {err}"
    return po_allocations_match_sap_pr(
        form,
        document_type=str(ticket.document_type or ""),
        sap_items=items,
    )


def _validate_files(files: list[tuple[str, str, bytes]]) -> str | None:
    if len(files) > _MAX_ATTACHMENTS:
        return f"At most {_MAX_ATTACHMENTS} files allowed."
    for name, mime, data in files:
        if len(data) > _MAX_FILE_BYTES:
            return f"File {name} exceeds {_MAX_FILE_BYTES // (1024 * 1024)} MB."
        resolved = resolve_procurement_upload_mime(filename=name, mime=mime)
        if resolved is None:
            return f"File type not allowed for {name}: {mime}"
        if not content_matches_mime(mime=resolved, data=data):
            return f"File content does not match declared type for {name}: {resolved}"
    return None


async def create_ticket(
    session: AsyncSession,
    *,
    user: User,
    kind: str,
    document_type: str,
    form: dict[str, Any],
    parent_pr_id: UUID | None,
    files: list[tuple[str, str, bytes]],
) -> ProcurementTicket:
    kind_u = kind.upper()
    dt = document_type.upper()
    if kind_u not in ("PR", "PO"):
        raise ValueError("kind must be PR or PO")
    parent_pr: ProcurementTicket | None = None
    if kind_u == "PO" and parent_pr_id is not None:
        parent_pr = await session.get(ProcurementTicket, parent_pr_id)
        if not parent_pr or parent_pr.kind.upper() != "PR":
            raise ValueError("Invalid parent PR")
        if parent_pr.created_by_user_id != user.id:
            raise ValueError("Invalid parent PR")
        if (parent_pr.document_type or "").upper() != dt:
            raise ValueError(
                "Purchase order workflow type must match the linked purchase request "
                f"({(parent_pr.document_type or '').upper()})."
            )
        if not (parent_pr.sap_id or "").strip():
            raise ValueError("Parent PR must have a SAP id before creating a PO")
        await ensure_pr_available_for_po_link(session, parent_pr_id=parent_pr_id)
    norm = normalize_form(dt, form)
    apply_procurement_defaults(norm, document_type=dt, kind=kind_u)
    if kind_u == "PO":
        header = norm.get("header") if isinstance(norm.get("header"), dict) else {}
        if not str(header.get("requestor_email") or "").strip():
            user_email = str(getattr(user, "email", None) or "").strip()
            if user_email and len(user_email) <= PO_REQUESTOR_EMAIL_MAX_LEN:
                header["requestor_email"] = user_email
                norm["header"] = header
    await apply_line_order_units_from_reference(
        session, form=norm, document_type=dt, ticket_kind=kind_u
    )
    await apply_vendor_payment_terms_from_catalogue(session, form=norm, kind=kind_u)
    errs = validate_form(kind=kind_u, document_type=dt, form=norm)
    if errs:
        raise ValueError("; ".join(errs))
    cat_errs = await validate_form_against_catalogue(
        session, kind=kind_u, document_type=dt, form=norm
    )
    if cat_errs:
        raise ValueError("; ".join(cat_errs))
    verr = _validate_files(files)
    if verr:
        raise ValueError(verr)

    ticket = ProcurementTicket(
        kind=kind_u,
        parent_pr_id=parent_pr_id if kind_u == "PO" else None,
        document_type=dt,
        form=norm,
        attachments=[],
        drive_folder_id=None,  # legacy column; attachments live in SAP + staging only
        sap_id=None,
        sap_sync=_default_sap_sync(),
        version=1,
        created_by_user_id=user.id,
    )
    session.add(ticket)
    await session.flush()

    if kind_u == "PO" and parent_pr is not None:
        pr_sap = (parent_pr.sap_id or "").strip()
        if pr_sap:
            enrich_err = await enrich_po_form_from_sap_pr(
                norm,
                document_type=dt,
                pr_sap_id=pr_sap,
                ticket_id=str(ticket.id),
            )
            if enrich_err:
                raise ValueError(enrich_err)
            alloc_err = await _validate_po_allocations_against_parent_pr(
                session, ticket=ticket, form=norm
            )
            if alloc_err:
                raise ValueError(alloc_err)
            ticket.form = norm
            flag_modified(ticket, "form")

    if files:
        try:
            ticket.attachments = [
                *(ticket.attachments or []),
                *stage_uploaded_files(ticket_id=str(ticket.id), files=files),
            ]
            flag_modified(ticket, "attachments")
        except Exception as e:
            log.exception("attachment staging failed ticket=%s", ticket.id)
            raise ValueError(f"Attachment staging error: {e}") from e

    _mark_sap_sync_deferred(ticket)
    session.add(ticket)
    await write_audit(
        session,
        actor_user_id=user.id,
        action="procurement.ticket.create",
        resource_type="procurement_ticket",
        resource_id=str(ticket.id),
        details={
            "kind": kind_u,
            "document_type": dt,
            "parent_pr_id": str(parent_pr_id) if parent_pr_id else None,
            "sap_id": ticket.sap_id,
            "attachment_count": len(ticket.attachments or []),
        },
    )
    return await _ticket_for_write_response(ticket)


async def _ticket_for_write_response(ticket: ProcurementTicket) -> ProcurementTicket:
    if _ticket_response_skip_sap_hydrate(ticket):
        ticket.form_source = "db"  # type: ignore[attr-defined]
        ticket.sap_form_read_error = None  # type: ignore[attr-defined]
        return ticket
    return await _hydrate_ticket_form_from_sap(ticket)


async def update_ticket(
    session: AsyncSession,
    *,
    user: User,
    ticket_id: UUID,
    version: int,
    form: dict[str, Any] | None,
    files: list[tuple[str, str, bytes]] | None,
    resync_sap: bool,
) -> ProcurementTicket:
    # Optimistic-locking: lock this ticket row for the rest of the transaction.
    # Prevents two concurrent PATCHes from reading the same ``version`` and both winning.
    row = await session.execute(
        select(ProcurementTicket)
        .where(ProcurementTicket.id == ticket_id)
        .with_for_update()
    )
    ticket = row.scalar_one_or_none()
    if not ticket or ticket.created_by_user_id != user.id:
        raise LookupError("not_found")
    if ticket.version != version:
        raise ValueError("version_conflict")
    dt = ticket.document_type
    if form is not None:
        norm = normalize_form(dt, form)
        dt_u = str(dt or "").upper()
        apply_procurement_defaults(norm, document_type=dt_u, kind=str(ticket.kind or "").upper())
        await apply_line_order_units_from_reference(
            session, form=norm, document_type=dt_u, ticket_kind=str(ticket.kind or "").upper()
        )
        await apply_vendor_payment_terms_from_catalogue(
            session, form=norm, kind=str(ticket.kind or "").upper()
        )
        errs = validate_form(kind=ticket.kind, document_type=dt, form=norm)
        if errs:
            raise ValueError("; ".join(errs))
        cat_errs = await validate_form_against_catalogue(
            session,
            kind=str(ticket.kind or "").upper(),
            document_type=dt_u,
            form=norm,
        )
        if cat_errs:
            raise ValueError("; ".join(cat_errs))
        if str(ticket.kind or "").upper() == "PO" and ticket.parent_pr_id:
            alloc_err = await _validate_po_allocations_against_parent_pr(
                session, ticket=ticket, form=norm
            )
            if alloc_err:
                raise ValueError(alloc_err)
        ticket.form = norm
    if files:
        verr = _validate_files(files)
        if verr:
            raise ValueError(verr)
        existing = len(ticket.attachments or [])
        if existing + len(files) > _MAX_ATTACHMENTS:
            raise ValueError(
                f"At most {_MAX_ATTACHMENTS} attachments per ticket; already have {existing}, "
                f"cannot add {len(files)} more."
            )
        try:
            staged = stage_uploaded_files(ticket_id=str(ticket.id), files=files)
            ticket.attachments = [*(ticket.attachments or []), *staged]
            flag_modified(ticket, "attachments")
        except Exception as e:
            log.exception("attachment staging failed ticket=%s", ticket.id)
            raise ValueError(f"Attachment staging error: {e}") from e
    ticket.version = ticket.version + 1
    needs_sap = resync_sap or not (ticket.sap_id or "").strip()
    needs_attachments = bool(files)
    if needs_sap:
        _mark_document_sync_deferred(ticket)
    elif needs_attachments:
        _mark_attachment_sync_deferred(ticket)
    session.add(ticket)
    await write_audit(
        session,
        actor_user_id=user.id,
        action="procurement.ticket.update",
        resource_type="procurement_ticket",
        resource_id=str(ticket.id),
        details={
            "version": ticket.version,
            "resync_sap": resync_sap,
            "had_form": form is not None,
            "had_new_attachments": bool(files),
            "sap_id": ticket.sap_id,
        },
    )
    return await _ticket_for_write_response(ticket)


async def list_tickets(
    session: AsyncSession,
    *,
    user_id: UUID,
    kind: str | None,
    limit: int = 100,
    offset: int = 0,
) -> list[ProcurementTicket]:
    q = select(ProcurementTicket).where(ProcurementTicket.created_by_user_id == user_id)
    if kind and kind.upper() in ("PR", "PO"):
        q = q.where(ProcurementTicket.kind == kind.upper())
    q = (
        q.order_by(ProcurementTicket.updated_at.desc())
        .limit(max(1, min(500, limit)))
        .offset(max(0, offset))
    )
    res = await session.execute(q)
    rows = list(res.scalars().all())
    dirty = False
    for t in rows:
        if _reconcile_stale_sync_flags_in_place(t):
            session.add(t)
            dirty = True
    if dirty:
        await session.commit()
    return rows


async def list_parent_prs_for_po(session: AsyncSession, *, user_id: UUID) -> list[ProcurementTicket]:
    """PRs eligible as PO parents: caller's own SAP-backed PRs without a linked PO yet."""
    linked_pr_ids = (
        select(ProcurementTicket.parent_pr_id)
        .where(ProcurementTicket.kind == "PO")
        .where(ProcurementTicket.parent_pr_id.isnot(None))
        .distinct()
    )
    q = (
        select(ProcurementTicket)
        .where(ProcurementTicket.kind == "PR")
        .where(ProcurementTicket.created_by_user_id == user_id)
        .where(ProcurementTicket.sap_id.isnot(None))
        .where(ProcurementTicket.sap_id != "")
        .where(ProcurementTicket.id.not_in(linked_pr_ids))
        .order_by(ProcurementTicket.updated_at.desc())
    )
    res = await session.execute(q)
    return list(res.scalars().all())


async def get_pr_for_po_prefill(
    session: AsyncSession, *, pr_id: UUID, user_id: UUID
) -> ProcurementTicket | None:
    """Load a PR for PO prefill only if the caller owns it (blocks id-guessing across users)."""
    t = await session.get(ProcurementTicket, pr_id)
    if not t or t.kind.upper() != "PR" or t.created_by_user_id != user_id:
        return None
    return t


def _ticket_document_sync_in_flight(ticket: ProcurementTicket) -> bool:
    """True while SAP document create/resubmit may still be running (not attachment-only work)."""
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
    if not sync.get("sync_pending"):
        return False
    sap_id = (ticket.sap_id or "").strip()
    if not sap_id:
        return True
    if (sync.get("last_error") or "").strip():
        return False
    return True


def _ticket_response_skip_sap_hydrate(ticket: ProcurementTicket) -> bool:
    """Avoid SAP reads on write responses while document sync may still be running."""
    return _ticket_document_sync_in_flight(ticket)


async def _hydrate_ticket_form_from_sap(ticket: ProcurementTicket) -> ProcurementTicket:
    """When ``sap_id`` is set, replace ``form`` with live SAP read (in-memory only)."""
    from app.procurement.sap_ticket_form_read import is_sap_no_active_lines_error

    if _ticket_response_skip_sap_hydrate(ticket):
        ticket.form_source = "db"  # type: ignore[attr-defined]
        ticket.sap_form_read_error = None  # type: ignore[attr-defined]
        ticket.sap_no_active_lines = False  # type: ignore[attr-defined]
        return ticket
    ticket.form_source = "db"  # type: ignore[attr-defined]
    ticket.sap_form_read_error = None  # type: ignore[attr-defined]
    ticket.sap_no_active_lines = False  # type: ignore[attr-defined]
    if not settings.procurement_sap_hydrate_on_read:
        return ticket
    sap_id = (ticket.sap_id or "").strip()
    if not sap_id or sap_id.startswith("#"):
        return ticket
    seed = ticket.form if isinstance(ticket.form, dict) else None
    form, source, err = await load_form_from_sap(
        kind=str(ticket.kind or ""),
        document_type=str(ticket.document_type or ""),
        sap_id=sap_id,
        ticket_id=f"read-{ticket.id}",
        seed_form=seed,
    )
    ticket.form_source = source or "db"  # type: ignore[attr-defined]
    ticket.sap_form_read_error = err  # type: ignore[attr-defined]
    if is_sap_no_active_lines_error(err):
        ticket.sap_no_active_lines = True  # type: ignore[attr-defined]
        seed_header = seed.get("header") if isinstance(seed, dict) and isinstance(seed.get("header"), dict) else {}
        ticket.form = {"header": dict(seed_header), "lines": []}
        return ticket
    if form and source == "sap":
        ticket.form = form
    return ticket


async def _hydrate_ticket_attachments_from_sap(ticket: ProcurementTicket) -> ProcurementTicket:
    """Merge SAP AttachmentSet list into ``ticket.attachments`` (in-memory only)."""
    ticket.sap_attachment_hydrate_error = None  # type: ignore[attr-defined]
    if not settings.procurement_sap_hydrate_on_read:
        return ticket
    err = await hydrate_attachments_from_sap(ticket)
    ticket.sap_attachment_hydrate_error = err  # type: ignore[attr-defined]
    return ticket


async def _enrich_ticket_on_read(
    ticket: ProcurementTicket,
    *,
    hydrate_form: bool = True,
    hydrate_attachments: bool = True,
) -> ProcurementTicket:
    if hydrate_form:
        ticket = await _hydrate_ticket_form_from_sap(ticket)
        if isinstance(ticket.form, dict):
            dt = str(ticket.document_type or "").upper()
            if dt:
                ticket.form = normalize_form(dt, ticket.form)
    if hydrate_attachments:
        ticket = await _hydrate_ticket_attachments_from_sap(ticket)
    return ticket


def _reconcile_stale_sync_flags_in_place(ticket: ProcurementTicket) -> bool:
    """Fix legacy/orphan flags on read so list + detail reflect document vs attachment state."""
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)
    changed = False
    sid = (ticket.sap_id or "").strip()
    if sync.get("sync_pending") and sid and not (sync.get("last_error") or "").strip():
        sync["sync_pending"] = False
        changed = True
    prev_att = sync.get("attachments_pending")
    refresh_attachment_sync_flags(sync, ticket)
    if prev_att != sync.get("attachments_pending"):
        changed = True
    if changed:
        ticket.sap_sync = sync
        flag_modified(ticket, "sap_sync")
    return changed


async def get_ticket(session: AsyncSession, *, ticket_id: UUID, user_id: UUID) -> ProcurementTicket | None:
    t = await session.get(ProcurementTicket, ticket_id)
    if not t or t.created_by_user_id != user_id:
        return None
    if _reconcile_stale_sync_flags_in_place(t):
        session.add(t)
        await session.commit()
        await session.refresh(t)
    return await _enrich_ticket_on_read(t)


async def get_ticket_sync_status(
    session: AsyncSession, *, ticket_id: UUID, user_id: UUID
) -> ProcurementTicket | None:
    """Lightweight read for attachment-only polling — no SAP form hydrate."""
    t = await session.get(ProcurementTicket, ticket_id)
    if not t or t.created_by_user_id != user_id:
        return None
    if _reconcile_stale_sync_flags_in_place(t):
        session.add(t)
        await session.commit()
        await session.refresh(t)
    return await _enrich_ticket_on_read(t, hydrate_form=False, hydrate_attachments=True)


async def list_ticket_audit_entries(
    session: AsyncSession, *, ticket_id: UUID, user_id: UUID, limit: int
) -> list[AuditLog] | None:
    """Returns audit rows for the ticket, or ``None`` if the caller cannot see the ticket."""
    t = await get_ticket(session, ticket_id=ticket_id, user_id=user_id)
    if not t:
        return None
    lim = max(1, min(300, limit))
    q = (
        select(AuditLog)
        .where(
            AuditLog.resource_type == "procurement_ticket",
            AuditLog.resource_id == str(ticket_id),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(lim)
    )
    res = await session.execute(q)
    return list(res.scalars().all())


def prefill_po_form_from_pr(pr: ProcurementTicket) -> dict[str, Any]:
    """Deep copy PR form for editable PO draft (fallback when SAP read unavailable)."""
    import copy

    return copy.deepcopy(pr.form) if isinstance(pr.form, dict) else {"header": {}, "lines": []}


async def build_po_prefill_form(
    pr: ProcurementTicket,
) -> tuple[dict[str, Any], str | None]:
    """PO prefill from live SAP PR items when possible; else DB copy of PR form."""
    from app.procurement.po_sap_enrichment import build_po_prefill_form_from_pr

    sap_id = (pr.sap_id or "").strip()
    if not sap_id:
        return prefill_po_form_from_pr(pr), None
    seed = pr.form if isinstance(pr.form, dict) else None
    return await build_po_prefill_form_from_pr(
        seed,
        document_type=str(pr.document_type or ""),
        pr_sap_id=sap_id,
        ticket_id=f"prefill-{pr.id}",
    )


def sap_retry_ticket_select(max_attempts: int, batch: int, now):
    """Pending SAP retries. ``FOR UPDATE SKIP LOCKED`` so workers take different tickets."""
    attempt_expr = func.coalesce(
        cast(ProcurementTicket.sap_sync["attempt_count"].as_string(), Integer),
        0,
    )
    nxt_text = ProcurementTicket.sap_sync["next_retry_at"].as_string()
    sync_pending = ProcurementTicket.sap_sync["sync_pending"].as_string() == "true"
    retry_due = and_(
        nxt_text.isnot(None),
        func.trim(nxt_text) != "",
        nxt_text != "null",
        cast(nxt_text, DateTime(timezone=True)) <= now,
    )
    retry_ready = or_(sync_pending, retry_due)
    retryable_text = ProcurementTicket.sap_sync["retryable"].as_string()
    retryable_ok = or_(retryable_text.is_(None), retryable_text != "false")
    err_text = ProcurementTicket.sap_sync["last_error"].as_string()
    pending_resubmit = and_(
        ProcurementTicket.sap_id.isnot(None),
        func.trim(ProcurementTicket.sap_id) != "",
        err_text.isnot(None),
        func.trim(err_text) != "",
        err_text != "null",
    )
    pending_create = ProcurementTicket.sap_id.is_(None)
    return (
        select(ProcurementTicket)
        .where(or_(pending_create, pending_resubmit, sync_pending))
        .where(attempt_expr < max_attempts)
        .where(retry_ready)
        .where(retryable_ok)
        .order_by(ProcurementTicket.updated_at.asc())
        .limit(batch)
        .with_for_update(skip_locked=True)
    )


async def sap_retry_job_batch(session: AsyncSession) -> int:
    """Process pending SAP retries. Returns number of tickets attempted.

    Claims **one** ticket at a time (``FOR UPDATE SKIP LOCKED`` + commit) so SAP
    HTTP does not pin the rest of the batch.

    - **Create retry:** ``sap_id`` unset, ``next_retry_at`` due, attempts remaining.
    - **Resubmit retry:** ``sap_id`` set, ``last_error`` set (failed MERGE/resync), same schedule.
    """
    max_attempts = effective_sap_max_attempts()
    batch = max(1, int(settings.procurement_sap_retry_batch_size))
    skip_ids: set[UUID] = set()
    n = 0
    for _ in range(batch):
        now = datetime.now(UTC)
        q = sap_retry_ticket_select(max_attempts, 1, now)
        if skip_ids:
            q = q.where(ProcurementTicket.id.notin_(skip_ids))
        ticket = (await session.execute(q)).scalars().first()
        if not ticket:
            break
        uid = ticket.created_by_user_id
        u = await session.get(User, uid)
        if not u:
            skip_ids.add(ticket.id)
            await session.rollback()
            continue
        sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else {}
        sid = (ticket.sap_id or "").strip()
        try:
            if sync.get("sync_pending"):
                if not sid:
                    await _run_sap_sync(ticket, u.email, session, resubmit=False)
                else:
                    await _run_sap_sync(ticket, u.email, session, resubmit=True)
                sid = (ticket.sap_id or "").strip()
                if sid and ticket_has_retryable_attachments(ticket):
                    await _sync_attachments_after_sap(ticket)
                sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
                if sid and not sync.get("last_error"):
                    sync["sync_pending"] = False
                    refresh_attachment_sync_flags(sync, ticket)
                    ticket.sap_sync = sync
                    flag_modified(ticket, "sap_sync")
            else:
                resubmit = bool(sid)
                await _run_sap_sync(ticket, u.email, session, resubmit=resubmit)
                await _sync_attachments_after_sap(ticket)
            session.add(ticket)
            n += 1
            await session.commit()
        except Exception:
            log.exception("sap retry failed ticket=%s", ticket.id)
            skip_ids.add(ticket.id)
            await session.rollback()
    return n


async def reconcile_orphan_sync_flags(session: AsyncSession, *, batch: int) -> int:
    """Clear stale ``sync_pending`` / ``attachments_pending`` on tickets left by crashed workers."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    res = await session.execute(
        select(ProcurementTicket)
        .where(
            or_(
                ProcurementTicket.sap_sync["sync_pending"].as_string() == "true",
                ProcurementTicket.sap_sync["attachments_pending"].as_string() == "true",
            )
        )
        .where(ProcurementTicket.updated_at <= stale_before)
        .order_by(ProcurementTicket.updated_at.asc())
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    n = 0
    for ticket in res.scalars().all():
        sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
        if not isinstance(sync, dict):
            sync = _default_sap_sync()
        for k, v in _default_sap_sync().items():
            sync.setdefault(k, v)
        changed = False
        sid = (ticket.sap_id or "").strip()
        if sync.get("sync_pending") and sid and not (sync.get("last_error") or "").strip():
            sync["sync_pending"] = False
            changed = True
        prev_att = sync.get("attachments_pending")
        refresh_attachment_sync_flags(sync, ticket)
        if prev_att != sync.get("attachments_pending"):
            changed = True
        if changed:
            ticket.sap_sync = sync
            flag_modified(ticket, "sap_sync")
            session.add(ticket)
            n += 1
    return n


def attachment_retry_ticket_select(*, exclude_ids: set[UUID] | None = None):
    """One pending attachment ticket. ``FOR UPDATE SKIP LOCKED``."""
    att_pending_flag = ProcurementTicket.sap_sync["attachments_pending"].as_string() == "true"
    q = (
        select(ProcurementTicket)
        .where(ProcurementTicket.sap_id.isnot(None))
        .where(func.trim(ProcurementTicket.sap_id) != "")
        .where(
            or_(
                att_pending_flag,
                ProcurementTicket.attachments.isnot(None),
            )
        )
        .order_by(ProcurementTicket.updated_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if exclude_ids:
        q = q.where(ProcurementTicket.id.notin_(exclude_ids))
    return q


async def attachment_retry_job_batch(session: AsyncSession) -> int:
    """Retry SAP attachment upload for staged rows (document already in SAP).

    One ticket per transaction so attachment HTTP does not pin the rest of the batch.
    """
    batch = max(1, int(settings.procurement_sap_retry_batch_size))
    await reconcile_orphan_sync_flags(session, batch=batch)
    await session.commit()
    skip_ids: set[UUID] = set()
    n = 0
    for _ in range(batch):
        ticket = (await session.execute(attachment_retry_ticket_select(exclude_ids=skip_ids))).scalars().first()
        if not ticket:
            break
        if not ticket_has_retryable_attachments(ticket):
            skip_ids.add(ticket.id)
            await session.rollback()
            continue
        u = await session.get(User, ticket.created_by_user_id)
        if not u:
            skip_ids.add(ticket.id)
            await session.rollback()
            continue
        try:
            await _sync_attachments_after_sap(ticket)
            session.add(ticket)
            n += 1
            await session.commit()
        except Exception:
            log.exception("attachment retry failed ticket=%s", ticket.id)
            skip_ids.add(ticket.id)
            await session.rollback()
    return n


def _find_attachment_row(
    ticket: ProcurementTicket, attachment_ref: str
) -> dict[str, Any] | None:
    ref = (attachment_ref or "").strip()
    if not ref:
        return None
    for a in ticket.attachments or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("id") or "").strip() == ref:
            return a
        if str(a.get("sap_document_id") or "").strip() == ref:
            return a
    return None


async def _ticket_for_attachment_access(
    session: AsyncSession, *, ticket_id: UUID, user_id: UUID
) -> ProcurementTicket | None:
    """Load ticket + attachment hydrate only (no SAP form read)."""
    ticket = await session.get(ProcurementTicket, ticket_id)
    if not ticket or ticket.created_by_user_id != user_id:
        return None
    if _reconcile_stale_sync_flags_in_place(ticket):
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
    await _hydrate_ticket_attachments_from_sap(ticket)
    return ticket


async def download_attachment(
    session: AsyncSession, *, ticket_id: UUID, user_id: UUID, attachment_id: str
) -> tuple[bytes, str, str]:
    ticket = await _ticket_for_attachment_access(session, ticket_id=ticket_id, user_id=user_id)
    if not ticket:
        raise LookupError("not_found")
    row = _find_attachment_row(ticket, attachment_id)
    if not row:
        raise LookupError("file_not_found")
    name = str(row.get("name") or "download.pdf")
    doc_id = str(row.get("sap_document_id") or "").strip()
    if doc_id:
        from app.procurement.sap_attachment_client import download_attachment_bytes

        data, mime, err = await download_attachment_bytes(
            ticket_id=str(ticket_id), document_id=doc_id
        )
        if err or data is None:
            raise LookupError("file_not_found" if not err else "sap_error")
        return data, mime or "application/pdf", name
    staging = str(row.get("staging_path") or "").strip()
    if staging:
        try:
            data = attachment_staging.read_staging_file(
                ticket_id=str(ticket_id), path=staging
            )
        except FileNotFoundError:
            raise LookupError("staging_gone") from None
        return data, str(row.get("mime_type") or "application/pdf"), name
    if attachment_failed(row):
        raise LookupError("staging_gone")
    raise LookupError("file_not_found")


async def delete_attachment(
    session: AsyncSession, *, user: User, ticket_id: UUID, attachment_id: str
) -> ProcurementTicket:
    """Remove local (not yet in SAP) attachment from DB and staging."""
    row = await session.execute(
        select(ProcurementTicket).where(ProcurementTicket.id == ticket_id).with_for_update()
    )
    ticket = row.scalar_one_or_none()
    if not ticket or ticket.created_by_user_id != user.id:
        raise LookupError("not_found")
    atts = [a for a in (ticket.attachments or []) if isinstance(a, dict)]
    target = _find_attachment_row(ticket, attachment_id)
    if not target:
        raise LookupError("file_not_found")
    if attachment_storage(target) == "sap":
        raise ValueError("sap_delete_not_available")
    rid = str(target.get("id") or "").strip()
    attachment_staging.delete_staging_file(
        str(target.get("staging_path") or ""), ticket_id=str(ticket.id)
    )
    atts = [a for a in atts if str(a.get("id") or "").strip() != rid]
    ticket.attachments = atts
    ticket.version = int(ticket.version or 0) + 1
    flag_modified(ticket, "attachments")
    sync = ticket.sap_sync if isinstance(ticket.sap_sync, dict) else _default_sap_sync()
    if not isinstance(sync, dict):
        sync = _default_sap_sync()
    for k, v in _default_sap_sync().items():
        sync.setdefault(k, v)
    refresh_attachment_sync_flags(sync, ticket)
    ticket.sap_sync = sync
    flag_modified(ticket, "sap_sync")
    await write_audit(
        session,
        actor_user_id=user.id,
        action="procurement.ticket.attachment.remove",
        resource_type="procurement_ticket",
        resource_id=str(ticket.id),
        details={"version": ticket.version, "attachment_id": rid},
    )
    session.add(ticket)
    return await _hydrate_ticket_form_from_sap(ticket)


async def retry_attachment_upload(
    session: AsyncSession, *, user: User, ticket_id: UUID, attachment_id: str
) -> ProcurementTicket:
    """Re-queue one failed attachment for SAP upload (requires staged file on disk)."""
    row = await session.execute(
        select(ProcurementTicket).where(ProcurementTicket.id == ticket_id).with_for_update()
    )
    ticket = row.scalar_one_or_none()
    if not ticket or ticket.created_by_user_id != user.id:
        raise LookupError("not_found")
    if not (ticket.sap_id or "").strip():
        raise ValueError("document_not_in_sap")
    target = _find_attachment_row(ticket, attachment_id)
    if not target:
        raise LookupError("file_not_found")
    if str(target.get("sap_document_id") or "").strip():
        raise ValueError("already_synced")
    staging = str(target.get("staging_path") or "").strip()
    if not staging:
        raise ValueError("reattach_required")
    try:
        attachment_staging.read_staging_file(ticket_id=str(ticket.id), path=staging)
    except (FileNotFoundError, PermissionError):
        target.pop("staging_path", None)
        target["sap_sync_error"] = "staging file missing"
        flag_modified(ticket, "attachments")
        raise ValueError("reattach_required") from None
    reset_attachment_for_retry(target)
    _mark_attachment_sync_deferred(ticket)
    ticket.version = int(ticket.version or 0) + 1
    flag_modified(ticket, "attachments")
    await write_audit(
        session,
        actor_user_id=user.id,
        action="procurement.ticket.attachment.retry",
        resource_type="procurement_ticket",
        resource_id=str(ticket.id),
        details={"version": ticket.version, "attachment_id": attachment_id},
    )
    session.add(ticket)
    return await _ticket_for_write_response(ticket)
