"""Document ingestion pipeline: parse → chunk → embed → store."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.optimus import config
from app.agents.optimus.smartqna.chunker import DocumentChunk, chunk_document
from app.agents.optimus.smartqna.document_parser import parse_document
from app.agents.optimus.smartqna.llm_client import chat_complete_text
from app.agents.optimus.smartqna.retriever import (
    ensure_collection_exists_async,
    get_async_qdrant_client,
    get_embeddings_batch_async,
)
from app.db.models import OptimusDocument, OptimusDocumentStatus, OptimusSection
from app.db.session import AsyncSessionLocal

log = logging.getLogger(__name__)

# Statuses considered "stuck" for cleanup purposes
STUCK_INGESTION_STATUSES = [
    OptimusDocumentStatus.PENDING.value,
    OptimusDocumentStatus.PARSING.value,
    OptimusDocumentStatus.CHUNKING.value,
    OptimusDocumentStatus.SUMMARIZING.value,
]


async def cleanup_stuck_documents(session: AsyncSession) -> int:
    """Mark documents stuck in processing state for > INGESTION_TIMEOUT_MINUTES as FAILED.

    This prevents documents from being permanently stuck in intermediate states
    (pending, parsing, chunking, summarizing) which would block the ingestion queue.

    Args:
        session: Async database session

    Returns:
        Number of documents marked as failed
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.INGESTION_TIMEOUT_MINUTES)

    # Find and update stuck documents
    result = await session.execute(
        update(OptimusDocument)
        .where(OptimusDocument.status.in_(STUCK_INGESTION_STATUSES))
        .where(OptimusDocument.updated_at < cutoff)
        .values(
            status=OptimusDocumentStatus.FAILED.value,
            error_message=f"Timed out after {config.INGESTION_TIMEOUT_MINUTES} minutes",
        )
    )

    count = result.rowcount
    if count > 0:
        await session.commit()
        log.warning("Marked %d stuck documents as FAILED (timeout)", count)

    return count


async def cleanup_stuck_documents_admin(
    delete: bool = False,
    force: bool = True,
) -> dict[str, Any]:
    """Admin-level cleanup for stuck documents with delete/force options.

    Args:
        delete: If True, permanently delete stuck documents. If False, mark as failed.
        force: If True, cleanup all processing documents immediately.
               If False, only cleanup documents older than INGESTION_TIMEOUT_MINUTES.

    Returns:
        Dict with status, cleaned_count, document_ids, and message.
    """
    from datetime import timedelta

    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from app.db.models import OptimusDocument, OptimusDocumentStatus

    if delete:
        # Find and delete stuck documents
        async with AsyncSessionLocal() as session:
            query = sa_select(OptimusDocument).where(
                OptimusDocument.status.in_(STUCK_INGESTION_STATUSES),
            )
            if not force:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.INGESTION_TIMEOUT_MINUTES)
                query = query.where(OptimusDocument.updated_at < cutoff)

            result = await session.scalars(query)
            stuck_docs = result.all()

        deleted_count = 0
        deleted_ids = []
        for doc in stuck_docs:
            doc_id = str(doc.id)
            if await delete_document(doc_id):
                deleted_count += 1
                deleted_ids.append(doc_id)

        return {
            "status": "deleted",
            "cleaned_count": deleted_count,
            "document_ids": deleted_ids,
            "message": f"Permanently deleted {deleted_count} stuck document(s).",
        }
    else:
        # Mark as failed
        async with AsyncSessionLocal() as session:
            if force:
                result = await session.execute(
                    sa_update(OptimusDocument)
                    .where(OptimusDocument.status.in_(STUCK_INGESTION_STATUSES))
                    .values(
                        status=OptimusDocumentStatus.FAILED.value,
                        error_message="Manually stopped by admin",
                    )
                    .returning(OptimusDocument.id)
                )
                affected_ids = [str(row[0]) for row in result.fetchall()]
                count = len(affected_ids)
                await session.commit()
            else:
                count = await cleanup_stuck_documents(session)
                affected_ids = []
                await session.commit()

        return {
            "status": "marked_failed",
            "cleaned_count": count,
            "document_ids": affected_ids,
            "message": f"Marked {count} stuck document(s) as failed.",
        }


def compute_file_hash(file_path: str | Path) -> str:
    """Compute SHA-256 hash of file content."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


async def ingest_document(
    file_path: str | Path,
    session: AsyncSession | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """
    Ingest a single document into the SmartQnA system.

    Pipeline:
    1. Parse document (PDF, DOCX, TXT, MD)
    2. Check for duplicates via file hash
    3. Chunk content (section-aware splitting)
    4. Generate embeddings
    5. Store in Qdrant + PostgreSQL

    DB sessions are short-lived: released across LLM summary and Qdrant I/O so
    pool connections are not held for minutes. When ``session`` is passed by the
    caller it is never closed here; we still avoid using it during LLM/Qdrant.

    Args:
        file_path: Path to the document
        session: Optional async session (creates short-lived ones if not provided)
        doc_id: Optional existing document ID (for updating uploaded documents)

    Returns:
        Dict with status, document ID, and chunk count
    """
    path = Path(file_path)
    file_hash = compute_file_hash(path)
    filename = path.name
    own_session = session is None

    async def _db_call(fn):
        """Run ``fn(db)`` on a short-lived session or the caller-provided one."""
        if own_session:
            async with AsyncSessionLocal() as db:
                return await fn(db)
        assert session is not None
        return await fn(session)

    # ── Phase 1: resolve / create document row (short DB) ─────────────────────
    async def _prepare(db: AsyncSession) -> dict[str, Any]:
        nonlocal filename
        resolved_id = doc_id
        if resolved_id:
            try:
                doc_uuid = uuid.UUID(resolved_id)
            except (ValueError, TypeError):
                log.warning("Invalid doc_id UUID: %s", resolved_id[:50] if resolved_id else None)
                doc_uuid = None
                resolved_id = None
            doc = await db.get(OptimusDocument, doc_uuid) if doc_uuid else None
            if doc:
                filename = doc.filename
                log.info("Updating existing document record: %s (id=%s)", filename, resolved_id)
                doc.status = OptimusDocumentStatus.PARSING.value
                doc.error_message = None
                await db.commit()
                return {"doc_id": str(doc.id), "filename": filename}
            log.warning("doc_id %s not found, creating new record", resolved_id)
            resolved_id = None

        existing = await db.scalar(
            select(OptimusDocument).where(OptimusDocument.file_hash == file_hash)
        )
        if existing:
            if existing.status == OptimusDocumentStatus.INGESTED.value:
                log.info("Document already ingested: %s (id=%s)", filename, existing.id)
                return {
                    "early": {
                        "status": "already_exists",
                        "document_id": str(existing.id),
                        "filename": existing.filename or filename,
                    }
                }
            doc = existing
            filename = doc.filename
            doc.status = OptimusDocumentStatus.PARSING.value
            doc.error_message = None
            await db.commit()
            return {"doc_id": str(doc.id), "filename": filename}

        doc = OptimusDocument(
            filename=filename,
            file_hash=file_hash,
            status=OptimusDocumentStatus.PARSING.value,
        )
        # Ensure PK before commit (Python default may not fire until flush; mocks never flush).
        if getattr(doc, "id", None) is None:
            doc.id = uuid.uuid4()
        db.add(doc)
        await db.commit()
        return {"doc_id": str(doc.id), "filename": filename}

    prepared = await _db_call(_prepare)
    if "early" in prepared:
        return prepared["early"]

    active_doc_id = prepared["doc_id"]
    filename = prepared["filename"]
    log.info("Starting ingestion for %s (id=%s)", filename, active_doc_id)

    async def _mark_failed(db: AsyncSession, message: str) -> None:
        doc = await db.get(OptimusDocument, uuid.UUID(active_doc_id))
        if doc is None:
            return
        doc.status = OptimusDocumentStatus.FAILED.value
        doc.error_message = message[:2000]
        await db.commit()

    try:
        # ── Phase 2: CPU parse/chunk (no DB connection held) ──────────────────
        parsed = parse_document(path)
        chunks = chunk_document(parsed.content)

        async def _progress_summarizing(db: AsyncSession) -> None:
            doc = await db.get(OptimusDocument, uuid.UUID(active_doc_id))
            if doc is None:
                raise RuntimeError(f"document {active_doc_id} disappeared during ingest")
            doc.page_count = parsed.page_count
            doc.chunk_count = len(chunks)
            doc.status = OptimusDocumentStatus.SUMMARIZING.value
            await db.commit()

        await _db_call(_progress_summarizing)

        # ── Phase 3: LLM + Qdrant (no DB connection held) ─────────────────────
        summary = await _generate_summary(parsed.content[:4000])
        await ensure_collection_exists_async()
        await _store_chunks_in_qdrant(active_doc_id, filename, chunks, summary)

        # ── Phase 4: sections + finalize (short DB) ───────────────────────────
        async def _finalize(db: AsyncSession) -> None:
            doc = await db.get(OptimusDocument, uuid.UUID(active_doc_id))
            if doc is None:
                raise RuntimeError(f"document {active_doc_id} disappeared during ingest")
            doc.summary = summary
            await _store_sections(db, active_doc_id, chunks)
            doc.status = OptimusDocumentStatus.INGESTED.value
            doc.ingested_at = datetime.now(timezone.utc)
            doc.file_path = None
            await db.commit()

        await _db_call(_finalize)

        try:
            path.unlink(missing_ok=True)
            log.debug("Deleted uploaded file: %s", path)
        except Exception as e:
            log.warning("Failed to delete uploaded file %s: %s", path, e)

        log.info("Successfully ingested %s: %d chunks", filename, len(chunks))
        return {
            "status": "success",
            "document_id": active_doc_id,
            "filename": filename,
            "chunk_count": len(chunks),
            "page_count": parsed.page_count,
        }

    except Exception as e:
        log.error("Ingestion failed for %s: %s", filename, e)
        try:
            await _db_call(lambda db: _mark_failed(db, str(e)))
        except Exception:
            log.exception("Failed to mark document %s as FAILED", active_doc_id)
        return {
            "status": "failed",
            "document_id": active_doc_id,
            "filename": filename,
            "error": str(e),
        }

async def ingest_documents_from_directory(
    directory: str | Path,
    extensions: tuple[str, ...] = (".pdf", ".docx", ".txt", ".md"),
) -> list[dict[str, Any]]:
    """
    Ingest all documents from a directory.

    Args:
        directory: Directory path
        extensions: File extensions to process

    Returns:
        List of ingestion results
    """
    path = Path(directory)
    if not path.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    results = []
    for file_path in path.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in extensions:
            result = await ingest_document(file_path)
            results.append(result)

    return results


async def _generate_summary(content: str) -> str:
    """Generate a brief summary of document content using LLM."""
    if not config.OPENAI_API_KEY:
        return content[:500] + "..." if len(content) > 500 else content

    try:
        prompt = f"""Summarize the following document content in 2-3 sentences. Focus on the main topics and purpose.

Content:
{content}

Summary:"""

        summary = await chat_complete_text(
            messages=prompt,
            step="document_summary",
            temperature=0,
        )
        if not summary:
            log.warning("Document summary LLM returned empty content")
            return content[:500] + "..." if len(content) > 500 else content
        return summary

    except Exception as e:
        log.warning("Summary generation failed: %s", e)
        return content[:500] + "..." if len(content) > 500 else content


async def _store_chunks_in_qdrant(
    doc_id: str,
    filename: str,
    chunks: list[DocumentChunk],
    document_summary: str,
) -> None:
    """Store chunks and summaries in Qdrant using batch embedding for performance."""
    import asyncio

    from qdrant_client.http.models import PointStruct

    client = get_async_qdrant_client()
    collection = config.QDRANT_COLLECTION

    # 1. Generate section summaries for chunks that qualify (async, parallel)
    section_summary_map: dict[int, str] = {}
    summary_tasks = [
        (i, _generate_section_summary(chunk.text, chunk.section_title))
        for i, chunk in enumerate(chunks)
        if chunk.section_title and len(chunk.text) > 200
    ]
    if summary_tasks:
        indices, coros = zip(*summary_tasks)
        summaries = await asyncio.gather(*coros)
        section_summary_map = dict(zip(indices, summaries))

    # 2. Collect all texts to embed in one batch
    texts_to_embed: list[str] = []
    text_meta: list[dict] = []

    for i, chunk in enumerate(chunks):
        texts_to_embed.append(chunk.text)
        text_meta.append({"type": "chunk", "chunk_index": i})

        if i in section_summary_map:
            texts_to_embed.append(section_summary_map[i])
            text_meta.append({"type": "section_summary", "chunk_index": i})

    if document_summary:
        texts_to_embed.append(document_summary)
        text_meta.append({"type": "document_summary", "chunk_index": -1})

    # 3. Single batch embedding call (major performance improvement)
    log.info("Batch embedding %d texts for doc %s (chunks=%d, section_summaries=%d, doc_summary=%d)",
             len(texts_to_embed), doc_id[:8], len(chunks),
             len(section_summary_map), 1 if document_summary else 0)
    embeddings = await get_embeddings_batch_async(texts_to_embed)

    # 4. Build Qdrant points
    points = []
    for meta, embedding, text in zip(text_meta, embeddings, texts_to_embed):
        i = meta["chunk_index"]
        t = meta["type"]

        if t == "chunk":
            chunk = chunks[i]
            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    "doc_id": doc_id,
                    "filename": filename,
                    "text": chunk.text,
                    "type": "chunk",
                    "section_title": chunk.section_title,
                    "chunk_index": i,
                    "level": chunk.level,
                },
            ))
        elif t == "section_summary":
            chunk = chunks[i]
            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    "doc_id": doc_id,
                    "filename": filename,
                    "text": text,
                    "type": "section_summary",
                    "section_title": chunk.section_title,
                    "level": chunk.level,
                },
            ))
        else:  # document_summary
            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    "doc_id": doc_id,
                    "filename": filename,
                    "text": document_summary,
                    "type": "document_summary",
                },
            ))

    # 5. Batch upsert to Qdrant
    await client.upsert(collection_name=collection, points=points)
    log.debug("Stored %d points in Qdrant for doc %s", len(points), doc_id)


async def _generate_section_summary(content: str, section_title: str) -> str:
    """Generate a summary for a document section."""
    if not config.OPENAI_API_KEY:
        return content[:200]

    try:
        prompt = f"""Summarize this section titled "{section_title}" in 1-2 sentences:

{content[:2000]}

Summary:"""

        summary = await chat_complete_text(
            messages=prompt,
            step="section_summary",
            temperature=0,
        )
        if not summary:
            log.warning("Section summary LLM returned empty content")
            return content[:200]
        return summary

    except Exception as e:
        log.warning("Section summary failed: %s", e)
        return content[:200]


async def _store_sections(
    session: AsyncSession,
    doc_id: str,
    chunks: list[DocumentChunk],
) -> None:
    """Store section records in PostgreSQL."""
    doc_uuid = uuid.UUID(doc_id)
    sections_by_title: dict[str | None, OptimusSection] = {}

    for i, chunk in enumerate(chunks):
        title = chunk.section_title

        if title not in sections_by_title:
            section = OptimusSection(
                document_id=doc_uuid,
                title=title,
                level=chunk.level,
                content=chunk.text,
                sort_order=i,
            )
            session.add(section)
            sections_by_title[title] = section
        else:
            # Append to existing section's content
            sections_by_title[title].content += f"\n\n{chunk.text}"

    await session.commit()


async def delete_document(doc_id: str, session: AsyncSession | None = None) -> bool:
    """
    Delete a document and all its chunks from the system.

    Args:
        doc_id: Document UUID
        session: Optional async session

    Returns:
        True if deleted, False if not found
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Get document
        doc = await session.get(OptimusDocument, uuid.UUID(doc_id))
        if not doc:
            return False

        # Delete uploaded file if it still exists (for failed ingestions)
        if doc.file_path:
            try:
                Path(doc.file_path).unlink(missing_ok=True)
                log.debug("Deleted uploaded file: %s", doc.file_path)
            except Exception as e:
                log.warning("Failed to delete file %s: %s", doc.file_path, e)

        # Delete from Qdrant
        client = get_async_qdrant_client()
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        await client.delete(
            collection_name=config.QDRANT_COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
        )

        # Delete from PostgreSQL (cascades to sections)
        await session.delete(doc)
        await session.commit()

        log.info("Deleted document %s (%s)", doc_id, doc.filename)
        return True

    finally:
        if own_session:
            await session.close()
