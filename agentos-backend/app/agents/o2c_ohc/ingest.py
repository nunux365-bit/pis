"""
Contract relational ingest — **sync API** only for callers without an event loop.

Persistence is implemented in ``ingest_async`` (asyncpg). This module re-exports shared helpers
and ``ingest_contract_payload``, which ignores ``conn`` and runs ``run_agenos_async(...)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agents.o2c_ohc.ingest_helpers import (
    _incoming_site_key,
    _normalize_rate_lines_attendance_required,
    _normalize_site_label_for_dedupe,
    _parse_iso_date,
    _pick_existing_service_site_from_matches,
    _service_site_label_matches_from_rows,
    _site_key_token_overlap,
    _slug,
)

__all__ = [
    "_incoming_site_key",
    "_normalize_rate_lines_attendance_required",
    "_normalize_site_label_for_dedupe",
    "_parse_iso_date",
    "_pick_existing_service_site_from_matches",
    "_service_site_label_matches_from_rows",
    "_site_key_token_overlap",
    "_slug",
    "ingest_contract_payload",
]


def ingest_contract_payload(
    conn: Any,
    payload: dict[str, Any],
    *,
    billing_client_id_hint: uuid.UUID | None,
    pdf_path: Path,
    relative_path: str,
    ingestion_root: str,
    file_sha256: str,
    page_count: int,
    ingestion_class: str,
    markdown: str,
    llm_raw: dict[str, Any],
    source_file_modified_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """
    Insert full contract graph. Returns (contract_document_id, contract_terms_version_id).

    ``conn`` is ignored; implementation uses asyncpg via ``run_agenos_async``.
    """
    _ = conn
    from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async
    from app.agents.o2c_ohc.ingest_async import ingest_contract_payload_async

    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                return await ingest_contract_payload_async(
                    session,
                    payload,
                    billing_client_id_hint=billing_client_id_hint,
                    pdf_path=pdf_path,
                    relative_path=relative_path,
                    ingestion_root=ingestion_root,
                    file_sha256=file_sha256,
                    page_count=page_count,
                    ingestion_class=ingestion_class,
                    markdown=markdown,
                    llm_raw=llm_raw,
                    source_file_modified_at=source_file_modified_at,
                )

    return run_agenos_async(_run())
