"""
O2C_OHC contract folder scan → per-PDF ingest pipeline.

Follows ``app.agents.o2c_ohc.product_flow.CONTRACT_INGEST_PRODUCT_FLOW``:
LLM extract → ``_apply_file_truth`` (normalize file metadata) → jsonschema validate
(repair loop) → ``ingest_contract_payload`` (DB). Not a blind save of raw model JSON.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async
from app.agents.o2c_ohc.failures import (
    clear_failed_contract_parsing_async,
    log_failed_contract_parsing_async,
)
from app.agents.o2c_ohc.folder_scanner import PdfCandidate, _file_sha256, list_new_or_updated_pdfs
from app.agents.o2c_ohc.billing_profile import infer_billing_profile
from app.agents.o2c_ohc.ingest_async import billing_client_id_for_slug_async, ingest_contract_payload_async
from app.integrations.gdrive_o2c import download_pdf_to_temp_file
from app.agents.o2c_ohc.json_validation import validate_contract_payload
from app.agents.o2c_ohc.llm_extract import (
    CONTRACT_PROMPT_PROFILE_GENERIC,
    CONTRACT_PROMPT_PROFILE_TACO,
    extract_markdown_and_payload_async,
)
from app.agents.o2c_ohc.markdown_vector_store import index_contract_markdown_async
from app.agents.o2c_ohc.pdf_probe import infer_ingestion_class, pdf_page_count
from app.config.settings import settings

log = logging.getLogger(__name__)


def _is_retryable_llm_error(exc: Exception) -> bool:
    """
    Classify LLM extraction failures that should consume another repair attempt.

    Retryable:
    - invalid/truncated JSON text from model output
    - missing contract_payload object
    - transient OpenAI call wrapper failures
    """
    msg = str(exc).lower()
    if "llm_invalid_json" in msg:
        return True
    if "missing or invalid contract_payload object" in msg:
        return True
    if "llm returned empty content" in msg:
        return True
    if "openai_responses_pdf_error" in msg:
        return True
    return False


_TACO_TOKEN = re.compile(r"(?i)(^|[^a-z0-9])taco([^a-z0-9]|$)")


def infer_contract_prompt_profile(relative_path: str) -> str:
    """
    First path segment under ``contracts_root`` selects the LLM instruction profile.

    - **TACO prompt** if that segment contains the token ``taco`` as a separate word (case-insensitive):
      matches e.g. ``taco``, ``TACO``, ``my_taco``, ``taco-imports``; does **not** match a bare
      substring inside another word (e.g. ``montacola``).
    - **Generic** prompt for everything else (e.g. ``tcs``, ``client_tcs``, ``tcs-2025``). PDFs
      directly under the root also use generic.
    """
    rel = (relative_path or "").strip().replace("\\", "/")
    if "/" not in rel:
        return CONTRACT_PROMPT_PROFILE_GENERIC
    folder = rel.split("/", 1)[0].strip()
    if not folder:
        return CONTRACT_PROMPT_PROFILE_GENERIC
    if _TACO_TOKEN.search(folder):
        return CONTRACT_PROMPT_PROFILE_TACO
    return CONTRACT_PROMPT_PROFILE_GENERIC


def _apply_file_truth(
    payload: dict[str, Any],
    pdf_path: Path,
    page_count: int,
    ingestion_class: str,
    *,
    contract_prompt_profile: str,
    relative_path: str,
) -> None:
    em = payload.setdefault("extraction_metadata", {})
    em["source_filename"] = pdf_path.name
    em["page_count"] = page_count
    em["ingestion_class"] = ingestion_class
    em["contract_prompt_profile"] = contract_prompt_profile
    em["billing_profile"] = infer_billing_profile(relative_path, contract_prompt_profile)


async def _ingest_contract_db_and_index_vector_async(
    md: str,
    payload: dict[str, Any],
    *,
    slug: str | None,
    pdf_path: Path,
    rel: str,
    root_s: str,
    file_sha256: str,
    page_count: int,
    ingestion_class: str,
    llm_audit: dict[str, Any],
    source_file_modified_at: datetime | None,
    doc_title: str | None,
) -> dict[str, Any]:
    """
    One transaction: relational ingest + vector index. If vector indexing fails, DB rolls back
    (same as legacy ``agenos_connection`` + ``ingest_contract_payload`` + ``index_contract_markdown``).
    """
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            bc_hint = await billing_client_id_for_slug_async(session, slug)
            doc_id, tv_id = await ingest_contract_payload_async(
                session,
                payload,
                billing_client_id_hint=bc_hint,
                pdf_path=pdf_path,
                relative_path=rel,
                ingestion_root=root_s,
                file_sha256=file_sha256,
                page_count=page_count,
                ingestion_class=ingestion_class,
                markdown=md,
                llm_raw=llm_audit,
                source_file_modified_at=source_file_modified_at,
            )
            return await index_contract_markdown_async(
                md,
                contract_document_id=str(doc_id),
                contract_terms_version_id=str(tv_id),
                relative_path=rel,
                source_filename=pdf_path.name,
                client_slug=slug,
                document_title=doc_title,
                contract_payload=payload,
            )


async def _process_one_contract_pdf_async(
    candidate: PdfCandidate,
    *,
    contracts_root: Path | None = None,
    ingestion_root_db: str | None = None,
    drive_svc: Any | None = None,
    max_repair_attempts: int | None = None,
) -> dict[str, Any]:
    """
    Single-PDF pipeline (async): ``extract_markdown_and_payload_async`` → file truth → validate → DB + vector.

    Google Drive download and PyMuPDF probes run on ``asyncio.to_thread`` so the event loop stays responsive.
    """
    if ingestion_root_db is not None and str(ingestion_root_db).strip():
        root_s = str(ingestion_root_db).strip()
    else:
        if contracts_root is None:
            return {
                "status": "failed",
                "relative_path": candidate.relative_path,
                "reason": "missing_contracts_root",
                "error": "contracts_root or ingestion_root_db required",
            }
        root_s = str(contracts_root.resolve())

    rel = candidate.relative_path

    pdf_path = candidate.absolute_path
    tmp_drive_pdf: Path | None = None
    if candidate.drive_file_id:
        if drive_svc is None:
            return {
                "status": "failed",
                "relative_path": candidate.relative_path,
                "reason": "missing_drive_svc",
                "error": "drive_svc required when candidate.drive_file_id is set",
            }
        try:
            tmp_drive_pdf = await asyncio.to_thread(
                download_pdf_to_temp_file, drive_svc, candidate.drive_file_id
            )
        except Exception as e:
            log.exception("Drive PDF download failed for %s", candidate.relative_path)
            return {
                "status": "failed",
                "relative_path": candidate.relative_path,
                "reason": "drive_download_error",
                "error": str(e),
            }
        pdf_path = tmp_drive_pdf

    file_sha256 = (candidate.sha256 or "").strip()
    if not file_sha256:
        file_sha256 = await asyncio.to_thread(_file_sha256, pdf_path)

    contract_prompt_profile = infer_contract_prompt_profile(rel)
    attempts = max_repair_attempts if max_repair_attempts is not None else settings.o2c_llm_repair_attempts

    last_val_errs: list[str] = []
    last_payload: dict[str, Any] | None = None
    markdown_snapshot = ""
    last_llm_audit: dict[str, Any] = {}

    try:
        page_count = await asyncio.to_thread(pdf_page_count, pdf_path)
        ingestion_class = await asyncio.to_thread(infer_ingestion_class, pdf_path)

        for attempt in range(attempts):
            hint = "\n".join(last_val_errs[:25]) if last_val_errs else None
            try:
                md, payload, llm_audit = await extract_markdown_and_payload_async(
                    pdf_path,
                    validation_error_hint=hint,
                    contract_prompt_profile=contract_prompt_profile,
                )
            except Exception as e:
                if attempt + 1 < attempts and _is_retryable_llm_error(e):
                    msg = f"llm_extract_attempt_{attempt + 1}: {e}"
                    last_val_errs = [msg[:2000]]
                    log.warning(
                        "Retryable LLM extract failure %s (attempt %s/%s): %s",
                        rel,
                        attempt + 1,
                        attempts,
                        e,
                    )
                    continue
                log.exception("LLM extract failed for %s", rel)
                await log_failed_contract_parsing_async(
                    folder_root=root_s,
                    relative_path=rel,
                    original_filename=pdf_path.name,
                    file_sha256=file_sha256,
                    source_file_modified_at=candidate.mtime,
                    attempt_count=attempt + 1,
                    last_error=f"llm_error: {e}",
                    last_validation_errors=last_val_errs,
                    last_llm_json=last_payload,
                    markdown_snapshot=markdown_snapshot or None,
                )
                return {
                    "status": "failed",
                    "relative_path": rel,
                    "reason": "llm_error",
                    "error": str(e),
                    "attempts": attempt + 1,
                }

            markdown_snapshot = md
            last_payload = payload
            last_llm_audit = llm_audit
            _apply_file_truth(
                payload,
                pdf_path,
                page_count,
                ingestion_class,
                contract_prompt_profile=contract_prompt_profile,
                relative_path=rel,
            )

            val_errs = validate_contract_payload(payload)
            if val_errs:
                last_val_errs = val_errs
                log.warning("Validation failed %s (attempt %s): %s", rel, attempt + 1, val_errs[:3])
                continue

            slug = (payload.get("client") or {}).get("slug")
            if isinstance(slug, str):
                slug = slug.strip().lower() or None

            index_meta = {"status": "skipped", "reason": "not_attempted"}
            ct = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
            doc_title_raw = ct.get("title")
            doc_title = doc_title_raw.strip() if isinstance(doc_title_raw, str) else None
            if doc_title == "":
                doc_title = None
            try:
                index_meta = await _ingest_contract_db_and_index_vector_async(
                    md,
                    payload,
                    slug=slug,
                    pdf_path=pdf_path,
                    rel=rel,
                    root_s=root_s,
                    file_sha256=file_sha256,
                    page_count=page_count,
                    ingestion_class=ingestion_class,
                    llm_audit=last_llm_audit,
                    source_file_modified_at=candidate.mtime,
                    doc_title=doc_title,
                )
            except (DBAPIError, IntegrityError) as e:
                detail = str(e.orig) if getattr(e, "orig", None) is not None else str(e)
                last_val_errs = [f"database: {detail}"]
                log.warning("DB ingest failed %s: %s", rel, e)
                continue
            except Exception as e:
                last_val_errs = [f"ingest_or_vector: {e}"]
                log.exception("Ingest/vector failed %s", rel)
                continue

            await clear_failed_contract_parsing_async(folder_root=root_s, relative_path=rel)
            log.info("Ingested contract PDF %s", rel)
            return {
                "status": "ok",
                "relative_path": rel,
                "attempts": attempt + 1,
                "vector_index": index_meta,
                "contract_prompt_profile": contract_prompt_profile,
            }

        err_summary = "; ".join(last_val_errs[:5]) if last_val_errs else "unknown"
        await log_failed_contract_parsing_async(
            folder_root=root_s,
            relative_path=rel,
            original_filename=pdf_path.name,
            file_sha256=file_sha256,
            source_file_modified_at=candidate.mtime,
            attempt_count=attempts,
            last_error=err_summary[:20000],
            last_validation_errors=last_val_errs,
            last_llm_json=last_payload,
            markdown_snapshot=markdown_snapshot or None,
        )
        return {
            "status": "failed",
            "relative_path": rel,
            "reason": "max_attempts",
            "validation_errors": last_val_errs,
            "attempts": attempts,
        }
    finally:
        if tmp_drive_pdf is not None:
            tmp_drive_pdf.unlink(missing_ok=True)


def process_one_contract_pdf(
    candidate: PdfCandidate,
    *,
    contracts_root: Path | None = None,
    ingestion_root_db: str | None = None,
    drive_svc: Any | None = None,
    max_repair_attempts: int | None = None,
) -> dict[str, Any]:
    """
    Sync entry (no running event loop): one ``asyncio.run`` for the full per-PDF async pipeline.

    For async callers, ``await _process_one_contract_pdf_async(...)`` directly.
    """
    return run_agenos_async(
        _process_one_contract_pdf_async(
            candidate,
            contracts_root=contracts_root,
            ingestion_root_db=ingestion_root_db,
            drive_svc=drive_svc,
            max_repair_attempts=max_repair_attempts,
        )
    )


def run_o2c_pipeline(
    *,
    contracts_root: str | None = None,
    since: datetime | None = None,
    max_repair_attempts: int | None = None,
    ignore_mtime_watermark: bool = True,
) -> dict[str, Any]:
    """
    Phase A entry: scan ``contracts_root`` and run ``process_one_contract_pdf`` per candidate.

    Design: ``CONTRACT_INGEST_PRODUCT_FLOW`` (see ``product_flow`` module).

    Folder-based LLM profile: the **first path segment** must contain the token ``taco`` (e.g.
    ``taco/``, ``my_taco/``, ``taco-imports/``) to use the full merged-fee prompt; otherwise generic
    (including ``tcs/`` and PDFs at the root).

    ``ignore_mtime_watermark``: include PDFs even when ``st_mtime`` is not after the DB watermark
    (e.g. added via copy/unzip with preserved mtimes); dedupe by sha256 still applies.

    **Google Drive:** when ``O2C_GDRIVE_CONTRACTS_PARENT_FOLDER_ID`` is set, set
    ``O2C_CONTRACTS_INGESTION_ROOT_DB``. Then either:

    - **Single-site (default):** set ``O2C_CONTRACTS_ROOT`` / ``contracts_root=`` to the **child**
      folder name under that parent (e.g. ``taco``). PDFs under that folder get paths like
      ``taco/file.pdf`` for profile detection.

    - **All sites:** set ``O2C_GDRIVE_CONTRACTS_ALL_SITES=true`` and leave ``O2C_CONTRACTS_ROOT``
      (and payload ``contracts_root``) **empty**. PDFs are listed recursively under the parent;
      paths are like ``taco/Annex.pdf`` when files live in subfolders (same as the smoke script).
      Non-empty ``contracts_root`` **overrides** and selects one child folder only.

    Watermark / sha256 dedupe use ``ingestion_root`` + ``list_new_or_updated_pdfs`` in both modes.
    """
    gdrive_parent = (settings.o2c_gdrive_contracts_parent_folder_id or "").strip()
    ingestion_db = (settings.o2c_contracts_ingestion_root_db or "").strip()
    root_s = (settings.o2c_contracts_root or contracts_root or  "").strip()

    if gdrive_parent:
        if not ingestion_db:
            raise ValueError(
                "O2C_CONTRACTS_INGESTION_ROOT_DB is required when O2C_GDRIVE_CONTRACTS_PARENT_FOLDER_ID is set."
            )
        from app.integrations.gdrive_o2c import build_drive_service, drive_id_from_url_or_id, find_child_folder_id

        try:
            svc = build_drive_service(settings_sa_json=settings.o2c_gdrive_service_account_json)
        except FileNotFoundError as e:
            raise ValueError(str(e)) from e
        parent_id = drive_id_from_url_or_id(gdrive_parent)
        use_all_sites = bool(settings.o2c_gdrive_contracts_all_sites) and not root_s

        if use_all_sites:
            list_folder_id = parent_id
            path_prefix: str | None = None
            display_root = f"gdrive://all-sites/{parent_id}"
            proc_root = Path(".")
        else:
            if not root_s:
                raise ValueError(
                    "For Drive single-site ingest, set O2C_CONTRACTS_ROOT (or contracts_root=) to the "
                    "child folder name under the parent (e.g. taco). For all sites under the parent, set "
                    "O2C_GDRIVE_CONTRACTS_ALL_SITES=true and leave contracts root empty."
                )
            site_folder_name = Path(root_s.rstrip("/")).name
            child_id = find_child_folder_id(svc, parent_id=parent_id, folder_name=site_folder_name)
            if not child_id:
                return {
                    "contracts_root": root_s,
                    "ingestion_root_db": ingestion_db,
                    "candidates": 0,
                    "ok": 0,
                    "failed": 0,
                    "results": [],
                    "error": f"no Google Drive folder named {site_folder_name!r} under parent {parent_id!r}",
                }
            list_folder_id = child_id
            path_prefix = site_folder_name
            display_root = root_s
            proc_root = Path(root_s)

        candidates = list_new_or_updated_pdfs(
            Path("."),
            since=since,
            ignore_mtime_watermark=ignore_mtime_watermark,
            drive_svc=svc,
            drive_folder_id=list_folder_id,
            ingestion_root_key=ingestion_db,
            drive_relative_path_prefix=path_prefix,
        )
        results: list[dict[str, Any]] = []
        for c in candidates:
            results.append(
                process_one_contract_pdf(
                    c,
                    contracts_root=proc_root,
                    ingestion_root_db=ingestion_db,
                    drive_svc=svc,
                    max_repair_attempts=max_repair_attempts,
                )
            )
        ok = sum(1 for r in results if r.get("status") == "ok")
        failed = sum(1 for r in results if r.get("status") == "failed")
        return {
            "contracts_root": display_root,
            "ingestion_root_db": ingestion_db,
            "gdrive_all_sites": use_all_sites,
            "candidates": len(candidates),
            "ok": ok,
            "failed": failed,
            "results": results,
        }

    if not root_s:
        raise ValueError("Set O2C_CONTRACTS_ROOT or pass contracts_root= to a directory of PDFs.")
    root = Path(root_s).expanduser().resolve()
    candidates = list_new_or_updated_pdfs(
        root,
        since=since,
        ignore_mtime_watermark=ignore_mtime_watermark,
    )
    results = []
    for c in candidates:
        results.append(
            process_one_contract_pdf(
                c,
                contracts_root=root,
                max_repair_attempts=max_repair_attempts,
            )
        )
    ok = sum(1 for r in results if r.get("status") == "ok")
    failed = sum(1 for r in results if r.get("status") == "failed")
    return {
        "contracts_root": str(root),
        "candidates": len(candidates),
        "ok": ok,
        "failed": failed,
        "results": results,
    }
