"""Optimus API routes — SmartQnA, Prosight, QuickML, TextToWorkflow, Library, Flock."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from app.agents.optimus import config
from app.agents.optimus.flock import routes as flock_routes
from app.agents.optimus.library import service as library_service
from app.agents.optimus.smartqna import session_storage
from app.agents.optimus.smartqna.agent import answer_question
from app.agents.optimus.smartqna.ingestion import (
    cleanup_stuck_documents,
    cleanup_stuck_documents_admin,
    delete_document,
    ingest_document,
)
from app.agents.optimus.smartqna.retriever import ensure_collection_exists_async
from app.api.deps import get_current_user, require_roles
from app.config.settings import settings
from app.db.models import Feature, User, UserRole

log = logging.getLogger(__name__)

router = APIRouter(tags=["optimus"])

# Upload size limit (10MB)
MAX_UPLOAD_SIZE_MB = 10
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Guards (Optimus-only multi-role check)
# ─────────────────────────────────────────────────────────────────────────────


def _require_optimus() -> None:
    if not config.OPTIMUS_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Optimus is disabled")


def _require_smartqna() -> None:
    _require_optimus()
    if not config.SMARTQNA_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="SmartQnA is disabled")


def _require_flock() -> None:
    _require_optimus()
    if not config.FLOCK_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Flock integration is disabled")


async def require_optimus_access(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Optimus access check — feature flag, then the shared access policy.

    The rule itself lives in ``FEATURE_ACCESS`` in ``app.security.rbac``
    (currently: open to every authenticated user). Admin operations stay on
    ``require_optimus_admin``.
    """
    _require_smartqna()

    if user.has_feature_access(Feature.OPTIMUS):
        return user

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail="Optimus access required. Contact your administrator.",
    )


def require_optimus_admin(user: Annotated[User, Depends(require_roles(UserRole.OPTIMUS_ADMIN))]) -> User:
    """Dependency that requires system_admin or optimus_admin for document management."""
    _require_smartqna()
    return user


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    message_id: str
    answer: str
    confidence: str
    citations: list[dict[str, Any]]
    conversation_id: str
    needs_clarification: bool = False  # True when bot is asking for clarification
    suggestions: list[str] | None = None  # Suggested topics when clarifying


class ConversationCreateRequest(BaseModel):
    title: str = Field(default="New conversation", max_length=500)


class IngestRequest(BaseModel):
    directory: str | None = None  # Use configured dir if None


class FlockWebhookRequest(BaseModel):
    name: str
    userId: str | None = None
    message: dict | None = None
    chat: str | None = None
    command: str | None = None
    text: str | None = None


class LinkAccountRequest(BaseModel):
    flock_user_id: str
    user_email: str
    flock_email: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# SmartQnA — Chat
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/smartqna/chat", response_model=ChatResponse)
async def smartqna_chat(
    body: ChatRequest,
    user: Annotated[User, Depends(require_optimus_access)],
):
    """Send a message to SmartQnA and get a response."""

    user_id = str(user.id)

    # Get or create conversation
    conversation_id = body.conversation_id
    if not conversation_id:
        conv = await session_storage.create_conversation(user_id)
        conversation_id = conv["id"]

    # Verify conversation belongs to user
    conv = await session_storage.get_conversation(conversation_id, user_id)
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    # Store user message
    await session_storage.add_message(conversation_id, user_id, "user", body.message)

    # Get conversation history for context
    history = await session_storage.get_conversation_history(conversation_id)

    # Generate answer (with tracing context)
    result = await answer_question(
        body.message,
        conversation_history=history,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    # Store assistant message with full metadata for follow-up detection
    msg = await session_storage.add_message(
        conversation_id,
        user_id,
        "assistant",
        result.answer,
        citations=result.citations,
        confidence=result.confidence.value,
        query_type=result.query_type,
        needs_clarification=result.needs_clarification,
        suggestions=result.suggestions,
    )

    return ChatResponse(
        message_id=msg["id"],
        answer=result.answer,
        confidence=result.confidence.value,
        citations=result.citations,
        conversation_id=conversation_id,
        needs_clarification=result.needs_clarification,
        suggestions=result.suggestions,
    )


@router.post("/smartqna/chat/reset")
async def smartqna_reset_chat(
    conversation_id: str,
    user: Annotated[User, Depends(require_optimus_access)],
):
    """Clear conversation history."""
    if not await session_storage.reset_conversation(conversation_id, str(user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return {"status": "reset", "conversation_id": conversation_id}


# ─────────────────────────────────────────────────────────────────────────────
# SmartQnA — Conversations
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/smartqna/conversations")
async def list_conversations(
    user: Annotated[User, Depends(require_optimus_access)],
    channel: str | None = None,
    limit: int = 50,
):
    """
    List user's SmartQnA conversations.

    Args:
        channel: Optional filter by channel ("web", "flock", or None for all)
        limit: Max conversations to return
    """
    return await session_storage.list_conversations(
        str(user.id),
        service="smartqna",
        channel=channel,
        limit=limit,
    )


@router.post("/smartqna/conversations/new")
async def create_conversation(
    body: ConversationCreateRequest,
    user: Annotated[User, Depends(require_optimus_access)],
):
    """Create a new SmartQnA conversation."""
    return await session_storage.create_conversation(str(user.id), title=body.title)


@router.get("/smartqna/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: Annotated[User, Depends(require_optimus_access)],
):
    """Get conversation details."""
    conv = await session_storage.get_conversation(conversation_id, str(user.id))
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conv


@router.get("/smartqna/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    user: Annotated[User, Depends(require_optimus_access)],
    limit: int = 100,
):
    """Get messages in a conversation."""
    messages = await session_storage.get_messages(conversation_id, str(user.id), limit=limit)
    if messages is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return {"messages": messages}


@router.delete("/smartqna/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: Annotated[User, Depends(require_optimus_access)],
):
    """Delete a conversation."""
    if not await session_storage.delete_conversation(conversation_id, str(user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return {"status": "deleted", "conversation_id": conversation_id}


# ─────────────────────────────────────────────────────────────────────────────
# SmartQnA — Documents
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/smartqna/documents")
async def list_documents(
    user: Annotated[User, Depends(require_optimus_access)],
):
    """List indexed documents. Available to all Optimus users (read-only)."""

    from sqlalchemy import select

    from app.db.models import OptimusDocument
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.scalars(
            select(OptimusDocument).order_by(OptimusDocument.created_at.desc())
        )
        docs = result.all()

    return {
        "documents": [
            {
                "id": str(d.id),
                "filename": d.filename,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "page_count": d.page_count,
                "summary": d.summary,
                "is_active": d.is_active if hasattr(d, "is_active") else True,
                "created_at": d.created_at.isoformat(),
                "ingested_at": d.ingested_at.isoformat() if d.ingested_at else None,
            }
            for d in docs
        ]
    }


class IngestDocumentsRequest(BaseModel):
    document_ids: list[str] = Field(..., min_length=1)


@router.post("/smartqna/documents/upload")
async def upload_document(
    request: Request,
    user: Annotated[User, Depends(require_optimus_admin)],
    file: UploadFile = File(...),
):
    """Upload a document without ingesting. Admin only.

    File is saved and recorded as 'uploaded' status.
    Use /smartqna/documents/ingest to trigger ingestion.
    Maximum file size: 10MB.
    """
    import hashlib

    from app.db.models import OptimusDocument, OptimusDocumentStatus
    from app.db.session import AsyncSessionLocal

    # Check Content-Length header for early rejection
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB}MB.",
        )

    # Validate file type and sanitize filename (prevent path traversal)
    allowed_extensions = {".pdf", ".docx", ".txt", ".md"}
    # Sanitize filename: extract only the base name, strip any path components
    raw_filename = file.filename or "document"
    sanitized_filename = Path(raw_filename).name.replace("\\", "").replace("/", "")
    suffix = Path(sanitized_filename).suffix.lower()
    if suffix not in allowed_extensions:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {suffix}. Allowed: {', '.join(allowed_extensions)}",
        )

    # Read file content
    content = await file.read()

    # Verify actual size (safety net if header was missing/spoofed)
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB}MB.",
        )
    file_hash = hashlib.sha256(content).hexdigest()

    # Create upload directory if needed
    upload_dir = Path(settings.optimus_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Save file with unique name
    import uuid as uuid_mod
    file_id = str(uuid_mod.uuid4())
    file_path = upload_dir / f"{file_id}{suffix}"
    file_path.write_bytes(content)

    # Create database record
    async with AsyncSessionLocal() as session:
        # Check for duplicate
        from sqlalchemy import select
        existing = await session.scalar(
            select(OptimusDocument).where(OptimusDocument.file_hash == file_hash)
        )
        if existing:
            # Remove uploaded file
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Document already exists: {existing.filename}",
            )

        doc = OptimusDocument(
            filename=sanitized_filename or f"document{suffix}",
            file_hash=file_hash,
            file_path=str(file_path),
            status=OptimusDocumentStatus.UPLOADED.value,
            is_active=True,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)

        log.info("Document uploaded: %s (id=%s)", file.filename, doc.id)
        return {
            "status": "uploaded",
            "document_id": str(doc.id),
            "filename": doc.filename,
            "message": "Document uploaded. Use 'Ingest Now' to process it.",
        }


@router.post("/smartqna/documents/ingest")
async def ingest_documents(
    body: IngestDocumentsRequest,
    user: Annotated[User, Depends(require_optimus_admin)],
    background_tasks: BackgroundTasks,
):
    """Trigger ingestion for selected documents. Admin only."""
    from sqlalchemy import select

    from app.db.models import OptimusDocument, OptimusDocumentStatus
    from app.db.session import AsyncSessionLocal

    # Ensure Qdrant collection exists
    await ensure_collection_exists_async()

    async with AsyncSessionLocal() as session:
        # Fetch documents
        result = await session.scalars(
            select(OptimusDocument).where(
                OptimusDocument.id.in_(body.document_ids),
                OptimusDocument.status.in_([
                    OptimusDocumentStatus.UPLOADED.value,
                    OptimusDocumentStatus.FAILED.value,
                ]),
            )
        )
        docs = result.all()

        if not docs:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail="No documents found to ingest (must be in 'uploaded' or 'failed' status)",
            )

        # Mark as pending
        doc_ids = []
        for doc in docs:
            doc.status = OptimusDocumentStatus.PENDING.value
            doc.error_message = None
            doc_ids.append(str(doc.id))
        await session.commit()

    # Start ingestion in background
    async def _ingest_batch():
        from app.db.session import AsyncSessionLocal

        # First, clean up any stuck documents from previous runs
        async with AsyncSessionLocal() as sess:
            stuck_count = await cleanup_stuck_documents(sess)
            if stuck_count:
                log.info("Cleaned up %d stuck documents before batch ingestion", stuck_count)

        for doc_id in doc_ids:
            async with AsyncSessionLocal() as sess:
                doc = await sess.get(OptimusDocument, doc_id)
                if not doc or not doc.file_path:
                    continue
                try:
                    result = await ingest_document(doc.file_path, doc_id=str(doc.id))
                    log.info("Ingested document %s: %s", doc_id, result)
                except Exception as e:
                    log.error("Ingestion failed for %s: %s", doc_id, e)

    background_tasks.add_task(_ingest_batch)

    return {
        "status": "processing",
        "document_count": len(doc_ids),
        "document_ids": doc_ids,
        "message": f"Ingestion started for {len(doc_ids)} document(s).",
    }


@router.post("/smartqna/documents/{doc_id}/toggle-active")
async def toggle_document_active(
    doc_id: str,
    user: Annotated[User, Depends(require_optimus_admin)],
):
    """Toggle document active/inactive status. Admin only."""
    from app.db.models import OptimusDocument
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        doc = await session.get(OptimusDocument, doc_id)
        if not doc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")

        doc.is_active = not doc.is_active
        await session.commit()

        return {
            "document_id": str(doc.id),
            "is_active": doc.is_active,
            "message": f"Document {'activated' if doc.is_active else 'deactivated'}",
        }


@router.delete("/smartqna/documents/{doc_id}")
async def remove_document(
    doc_id: str,
    user: Annotated[User, Depends(require_optimus_admin)],
):
    """Delete a document and its chunks. Admin only."""

    if not await delete_document(doc_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")

    return {"status": "deleted", "document_id": doc_id}


@router.post("/smartqna/documents/cleanup-stuck")
async def cleanup_stuck(
    user: Annotated[User, Depends(require_optimus_admin)],
    delete: bool = Query(default=False, description="If true, delete stuck documents instead of marking as failed"),
    force: bool = Query(default=True, description="If true, cleanup all processing documents immediately (ignore timeout)"),
):
    """Clean up documents stuck in processing state. Admin only.

    By default (force=True), immediately cleans up ALL documents currently in processing states.
    If force=False, only cleans up documents that have exceeded the configured timeout.

    Args:
        delete: If False (default), marks stuck documents as 'failed' so they can be retried.
                If True, permanently deletes stuck documents (DB record, file on disk, Qdrant chunks).
        force: If True (default), cleans up all processing documents immediately.
               If False, only cleans up documents older than INGESTION_TIMEOUT_MINUTES.
    """
    result = await cleanup_stuck_documents_admin(delete=delete, force=force)
    log.info("Admin %s triggered cleanup (delete=%s, force=%s): %s", user.email, delete, force, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# SmartQnA — Admin: User Access Management (Multi-Role System)
# ─────────────────────────────────────────────────────────────────────────────


class UserAccessResponse(BaseModel):
    id: str
    email: str
    full_name: str
    department: str
    roles: list[str]  # All roles assigned to user
    is_active: bool


class GrantAccessRequest(BaseModel):
    user_id: str


@router.get("/admin/users")
async def list_users_with_access(
    user: Annotated[User, Depends(require_optimus_admin)],
    include_inactive: bool = False,
    search: str = "",
):
    """List users matching search query with their Optimus access status. Admin only."""
    from sqlalchemy import or_, select

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        query = select(User).order_by(User.full_name).limit(50)
        if not include_inactive:
            query = query.where(User.is_active.is_(True))
        if search.strip():
            term = f"%{search.strip()}%"
            query = query.where(or_(User.full_name.ilike(term), User.email.ilike(term)))
        result = await session.scalars(query)
        users = result.all()

    return {
        "users": [
            UserAccessResponse(
                id=str(u.id),
                email=u.email,
                full_name=u.full_name,
                department=u.department,
                roles=list(u.roles or []),
                is_active=u.is_active,
            ).model_dump()
            for u in users
        ]
    }


@router.post("/admin/users/{user_id}/grant-access")
async def grant_optimus_access(
    user_id: str,
    admin: Annotated[User, Depends(require_optimus_admin)],
):
    """Grant Optimus access to a user by adding optimus_user role. Admin only."""
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.scalar(select(User).where(User.id == user_id))
        if not result:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")

        # System admins already have implicit access
        if result.is_admin:
            return {"status": "already_granted", "message": "System admins always have access"}

        # Check if user already has explicit optimus_user role
        if UserRole.OPTIMUS_USER.value in (result.roles or []):
            return {"status": "already_granted", "message": "User already has optimus_user role"}

        # Grant access by adding optimus_user role
        current_roles = list(result.roles or [])
        if UserRole.OPTIMUS_USER.value not in current_roles:
            current_roles.append(UserRole.OPTIMUS_USER.value)
            result.roles = current_roles
        await session.commit()

    log.info("Optimus access granted to user %s by admin %s", user_id, admin.email)
    return {"status": "granted", "user_id": user_id}


@router.post("/admin/users/{user_id}/revoke-access")
async def revoke_optimus_access(
    user_id: str,
    admin: Annotated[User, Depends(require_optimus_admin)],
):
    """Revoke Optimus access from a user by removing optimus_user role. Admin only."""
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.scalar(select(User).where(User.id == user_id))
        if not result:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")

        # Cannot revoke from system admin (they have implicit access)
        if result.is_admin:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Cannot revoke access from system admin (they have implicit access)",
            )

        # Remove optimus_user role from roles array
        current_roles = list(result.roles or [])
        if UserRole.OPTIMUS_USER.value in current_roles:
            current_roles.remove(UserRole.OPTIMUS_USER.value)
            result.roles = current_roles
        await session.commit()

    log.info("Optimus access revoked from user %s by admin %s", user_id, admin.email)
    return {"status": "revoked", "user_id": user_id}


# ─────────────────────────────────────────────────────────────────────────────
# Library — Unified History
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/library/conversations")
async def library_list_conversations(
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List all conversations across Optimus services."""
    _require_optimus()

    return await library_service.get_all_conversations(str(user.id), limit=limit, offset=offset)


@router.get("/library/search")
async def library_search(
    q: str,
    user: Annotated[User, Depends(get_current_user)],
    service: str | None = None,
    limit: int = 20,
):
    """Search conversations by title or content."""
    _require_optimus()

    return await library_service.search_conversations(
        str(user.id), query=q, service=service, limit=limit
    )


# ─────────────────────────────────────────────────────────────────────────────
# Flock Integration
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/flock/webhook")
async def flock_webhook_verify():
    """Health check for Flock webhook URL verification."""
    return {"status": "ok", "service": "optimus-flock"}


@router.post("/flock/webhook")
@router.post("/flock/webhook/")
async def flock_webhook(
    request: Request,
):
    """
    Flock webhook receiver.

    Uses raw JSON parsing so any payload shape Flock sends — including
    URL-verification pings — is accepted without validation errors.

    Flock has a ~5s webhook timeout, so we use fire-and-forget for slow operations.
    """
    _require_flock()

    # Parse body (tolerate empty / malformed bodies during URL validation)
    try:
        body = await request.json()
    except Exception:
        # Flock sometimes sends a plain-text ping during URL validation
        return {"text": ""}

    event_name = body.get("name")
    user_id = body.get("userId")

    # Handle Flock's URL verification challenge (if any)
    challenge = body.get("challenge")
    if challenge:
        return {"challenge": challenge}

    # Handle lifecycle events (app.install / app.uninstall) BEFORE token verification
    # These events have their own lifecycle token, not the bot verification token
    if event_name in ("app.install", "app.uninstall"):
        if event_name == "app.install":
            user_token = body.get("userToken", "")  # Note: userToken, not token
            # Fire-and-forget: process in background so Flock gets HTTP 200 immediately
            if user_id and user_token:
                from app.infra.task_tracker import spawn

                spawn(
                    flock_routes.handle_install_async(user_id, user_token),
                    name="flock-install",
                )
        return {}  # Empty response for lifecycle events

    # Verify Flock's request signature for regular events.
    # Flock signs every event with a JWT (HS256, app secret) in the
    # `x-flock-event-token` header. There is NO token in the JSON body.
    event_token = request.headers.get("x-flock-event-token", "")
    if not flock_routes.verify_event_token(event_token, expected_user_id=user_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication")

    # Handle regular events
    return await flock_routes.handle_webhook(body)


@router.post("/flock/link-account")
async def flock_link_account(
    body: LinkAccountRequest,
    user: Annotated[User, Depends(get_current_user)],
):
    """Link a Flock account to an AgentOS user (admin operation)."""
    _require_flock()

    from app.agents.optimus.flock import service as flock_service

    try:
        return await flock_service.link_account(
            flock_user_id=body.flock_user_id,
            user_email=body.user_email,
            flock_email=body.flock_email,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Flock Admin
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/admin/flock/config")
async def get_flock_config(
    request: Request,
    user: Annotated[User, Depends(require_optimus_admin)],
):
    """Get Flock integration configuration for admin setup."""
    # Build the webhook URL from the current request
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/api/optimus/flock/webhook"

    return {
        "enabled": config.FLOCK_ENABLED,
        "webhook_url": webhook_url,
        "app_id": config.FLOCK_APP_ID or None,
        "bot_configured": bool(config.FLOCK_BOT_TOKEN),
    }


@router.get("/admin/flock/accounts")
async def list_flock_accounts(
    user: Annotated[User, Depends(require_optimus_admin)],
):
    """List all linked Flock accounts. Admin only."""
    _require_flock()

    from app.agents.optimus.flock import service as flock_service

    accounts = await flock_service.list_linked_accounts()
    return {"accounts": accounts, "total": len(accounts)}


@router.delete("/admin/flock/accounts/{flock_user_id}")
async def unlink_flock_account(
    flock_user_id: str,
    admin: Annotated[User, Depends(require_optimus_admin)],
):
    """Unlink a Flock account. Admin only."""
    _require_flock()

    from app.agents.optimus.flock import service as flock_service

    if await flock_service.unlink_account(flock_user_id):
        log.info("Flock account %s unlinked by admin %s", flock_user_id, admin.email)
        return {"status": "unlinked", "flock_user_id": flock_user_id}

    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Flock account not found")

