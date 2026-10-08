"""Scan contracts folder, call LLM, validate, persist or log failures; update ingestion watermark."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.agenos_models import ContractDocument, FailedContractParsing, O2cIngestionState
from app.db.session import AsyncSessionLocal
from app.o2c.folder_scan import PdfCandidate, iter_pdf_candidates
from app.o2c.llm_contract import extract_markdown_and_structured
from app.o2c.mapper import persist_structured_contract
from app.o2c.pdf_text import extract_text_for_llm
from app.o2c.validate_schema import validate_contract_json

log = logging.getLogger(__name__)

PIPELINE_VERSION = "o2c_ohc_v1"


def _llm_model_label() -> str:
    if settings.llm_provider == "anthropic":
        return f"anthropic:{settings.anthropic_chat_model}"
    return f"openai:{settings.openai_chat_model}"


async def _get_or_create_state(session: AsyncSession, source_root: str) -> O2cIngestionState:
    row = await session.scalar(select(O2cIngestionState).where(O2cIngestionState.source_root == source_root))
    if row:
        return row
    row = O2cIngestionState(source_root=source_root, last_processed_max_mtime=None)
    session.add(row)
    await session.flush()
    return row


async def _record_failure(
    session: AsyncSession,
    *,
    source_root: str,
    pdf: PdfCandidate,
    err: str,
    validation_errors: list[str] | None,
    last_json: dict | None,
    markdown_excerpt: str | None,
    attempt_count: int,
) -> None:
    rel = pdf.relative_path
    ofn = pdf.absolute_path.name
    snap = (markdown_excerpt or "")[:8000]
    row = await session.scalar(
        select(FailedContractParsing).where(
            FailedContractParsing.folder_root == source_root,
            FailedContractParsing.relative_path == rel,
        )
    )
    if row:
        row.original_filename = ofn
        row.file_sha256 = pdf.sha256
        row.source_file_modified_at = pdf.mtime
        row.last_error = err
        row.last_validation_errors = validation_errors
        row.last_llm_json = last_json
        row.markdown_snapshot = snap
        row.attempt_count = attempt_count
    else:
        session.add(
            FailedContractParsing(
                folder_root=source_root,
                relative_path=rel,
                original_filename=ofn,
                file_sha256=pdf.sha256,
                source_file_modified_at=pdf.mtime,
                last_error=err,
                last_validation_errors=validation_errors,
                last_llm_json=last_json,
                markdown_snapshot=snap,
                attempt_count=attempt_count,
            )
        )


async def _already_ingested_same_bytes(session: AsyncSession, sha256: str) -> bool:
    q = await session.scalar(select(ContractDocument.id).where(ContractDocument.sha256 == sha256).limit(1))
    return q is not None


async def process_single_pdf(
    session: AsyncSession,
    *,
    source_root: str,
    pdf: PdfCandidate,
) -> dict[str, Any]:
    """One PDF in an open session (caller commits)."""
    if await _already_ingested_same_bytes(session, pdf.sha256):
        return {"status": "skipped", "reason": "duplicate_sha256", "path": pdf.relative_path}

    max_r = max(1, settings.o2c_llm_repair_attempts)
    last_err = ""
    last_val: list[str] = []
    last_json: dict | None = None
    md_excerpt = ""
    raw_text, _ing = await asyncio.to_thread(extract_text_for_llm, pdf.absolute_path)

    for attempt in range(max_r):
        repair = None if attempt == 0 else "\n".join(last_val[:20])
        prev = last_json if attempt else None
        try:
            env = await extract_markdown_and_structured(
                pdf.absolute_path,
                repair_context=repair,
                previous_json=prev,
            )
        except Exception as e:
            last_err = f"llm_error:{e}"
            log.exception("LLM failed %s", pdf.relative_path)
            continue

        structured = env.structured_contract
        last_json = structured
        md_excerpt = env.markdown_document[:4000]
        ok, errs = validate_contract_json(structured)
        if not ok:
            last_val = errs
            last_err = "validation_failed"
            continue

        try:
            meta = await persist_structured_contract(
                session,
                structured=structured,
                markdown=env.markdown_document,
                pdf=pdf,
                source_root=source_root,
                pipeline_version=PIPELINE_VERSION,
                llm_model=_llm_model_label(),
                raw_text_excerpt=raw_text[:50000],
            )
        except IntegrityError as ie:
            await session.rollback()
            log.warning("integrity ingest %s: %s", pdf.relative_path, ie)
            return {"status": "skipped", "reason": "integrity_error", "path": pdf.relative_path, "detail": str(ie)}
        except Exception as e:
            await session.rollback()
            last_err = f"persist:{e}"
            log.exception("Persist failed %s", pdf.relative_path)
            continue

        return {"status": "ok", "path": pdf.relative_path, **meta}

    await _record_failure(
        session,
        source_root=source_root,
        pdf=pdf,
        err=last_err or "unknown",
        validation_errors=last_val or None,
        last_json=last_json,
        markdown_excerpt=md_excerpt,
        attempt_count=max_r,
    )
    return {
        "status": "failed",
        "path": pdf.relative_path,
        "error": last_err,
        "validation_errors": last_val[:10],
    }


async def run_o2c_folder_ingest(
    *,
    contracts_root: str | None = None,
    max_files: int | None = None,
    ignore_mtime_watermark: bool = False,
) -> dict[str, Any]:
    """
    Incremental ingest: PDFs under root with mtime > o2c_ingestion_state.last_processed_max_mtime.
    Skips PDFs whose sha256 already exists in contract_document.

    Set ``ignore_mtime_watermark=True`` to include PDFs whose ``st_mtime`` is older than the
    watermark (e.g. copy/unzip preserved original times); unchanged bytes still skip as duplicates.
    """
    root_s = (contracts_root or settings.o2c_contracts_root or "").strip()
    if not root_s:
        return {"error": "o2c_contracts_root not set", "processed": []}
    root = Path(root_s).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"not a directory: {root}", "processed": []}

    source_root = str(root)

    async with AsyncSessionLocal() as session:
        state = await _get_or_create_state(session, source_root)
        since = state.last_processed_max_mtime
        await session.commit()

    candidates = await asyncio.to_thread(
        iter_pdf_candidates,
        root,
        since_mtime=since,
        ignore_mtime_watermark=ignore_mtime_watermark,
    )
    if max_files is not None:
        candidates = candidates[: max(0, max_files)]

    results: list[dict[str, Any]] = []
    max_seen_mtime: datetime | None = since

    for pdf in candidates:
        async with AsyncSessionLocal() as inner:
            try:
                r = await process_single_pdf(inner, source_root=source_root, pdf=pdf)
                await inner.commit()
            except Exception as e:
                await inner.rollback()
                r = {"status": "error", "path": pdf.relative_path, "error": str(e)}
                log.exception("process_single_pdf outer %s", pdf.relative_path)
            results.append(r)

        if max_seen_mtime is None or pdf.mtime > max_seen_mtime:
            max_seen_mtime = pdf.mtime

    if candidates:
        async with AsyncSessionLocal() as s2:
            st = await _get_or_create_state(s2, source_root)
            if max_seen_mtime is not None:
                if st.last_processed_max_mtime is None or max_seen_mtime > st.last_processed_max_mtime:
                    st.last_processed_max_mtime = max_seen_mtime
            await s2.commit()

    return {
        "source_root": source_root,
        "candidate_count": len(candidates),
        "watermark_after": max_seen_mtime.isoformat() if max_seen_mtime else None,
        "results": results,
    }
