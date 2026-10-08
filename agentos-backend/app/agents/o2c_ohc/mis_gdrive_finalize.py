"""Shared MIS xlsx → Google Drive finalize + attendance path resolution (graph + API)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking
from app.integrations.gdrive_o2c import (
    build_drive_service,
    contract_folder_segment_for_mis,
    drive_id_from_url_or_id,
    export_google_sheet_to_xlsx,
    upload_mis_xlsx_layout,
)

log = logging.getLogger(__name__)


def unlink_o2c_temp_paths(paths: list[Path]) -> None:
    """Remove temp files (e.g. Drive-exported attendance xlsx); clear the list."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.debug("temp unlink %s: %s", p, e)
    paths.clear()


def build_mis_drive_svc() -> Any | None:
    need = bool(
        (settings.o2c_gdrive_attendance_sheet_url_or_id or "").strip()
        or (settings.o2c_gdrive_mis_parent_folder_id or "").strip()
    )
    if not need:
        return None
    try:
        return build_drive_service(settings_sa_json=settings.o2c_gdrive_service_account_json)
    except FileNotFoundError as e:
        log.warning("O2C MIS Drive client unavailable: %s", e)
        return None


def resolve_attendance_xlsx_path(
    *,
    explicit: str | None,
    cleanup_paths: list[Path],
    drive_svc: Any | None,
) -> str:
    """
    Same precedence as LangGraph MIS node: optional explicit path, then settings local file,
    then export ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID`` to a temp file (listed in ``cleanup_paths``).
    """
    ex = (explicit or "").strip()
    if ex:
        p = Path(ex).expanduser()
        if p.is_file():
            return str(p.resolve())
        return ""
    local = (settings.o2c_attendance_xlsx_path or settings.o2c_ohc_attendance_xlsx_path or "").strip()
    if local:
        p = Path(local).expanduser()
        if p.is_file():
            return str(p.resolve())
    raw = (settings.o2c_gdrive_attendance_sheet_url_or_id or "").strip()
    if not raw or drive_svc is None:
        return ""
    sid = drive_id_from_url_or_id(raw)
    fd, name = tempfile.mkstemp(prefix="o2c_attendance_", suffix=".xlsx")
    os.close(fd)
    dest = Path(name)
    cleanup_paths.append(dest)
    export_google_sheet_to_xlsx(drive_svc, sid, dest)
    return str(dest)


async def _folder_segment_from_linked_contract_async(
    session: AsyncSession, terms_version_id: str
) -> str | None:
    """Latest linked ``contract_document.folder_path`` for this terms version; None if no row."""
    r = await session.execute(
        text("""
        SELECT cd.folder_path
        FROM contract_document cd
        INNER JOIN contract_terms_document ctd ON ctd.contract_document_id = cd.id
        WHERE ctd.contract_terms_version_id = CAST(:tv AS uuid)
        ORDER BY cd.created_at DESC NULLS LAST, cd.id DESC
        LIMIT 1
        """),
        {"tv": terms_version_id},
    )
    doc = r.mappings().first()
    if not doc:
        return None
    d = dict(doc)
    return contract_folder_segment_for_mis((d.get("folder_path") or ""))


async def finalize_mis_xlsx_to_gdrive_after_draft_async(
    *,
    mis_run_id: str,
    period_start: date,
    xlsx_path: str | Path,
    drive_svc: Any | None,
) -> tuple[str, str | None]:
    """
    Upload MIS xlsx when configured; update ``o2c_mis_run.xlsx_path`` and optionally delete local file.
    Returns ``(ref_for_ui, upload_error_or_none)``.
    """
    lp = Path(xlsx_path)
    ref = str(xlsx_path)
    if not mis_run_id or not lp.is_file():
        return ref, None
    parent = (settings.o2c_gdrive_mis_parent_folder_id or "").strip()
    if not parent or drive_svc is None:
        return ref, None
    seg = "_contracts_root"
    row: dict[str, Any] | None = None
    from_contract: str | None = None
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT detailed_json, contract_terms_version_id::text AS tv_id
                FROM o2c_mis_run WHERE id = CAST(:mid AS uuid)
                """),
                {"mid": mis_run_id},
            )
            m = r.mappings().first()
            if m:
                row = dict(m)
            if row and (row.get("tv_id") or "").strip():
                from_contract = await _folder_segment_from_linked_contract_async(
                    session, str(row["tv_id"]).strip()
                )
    if from_contract is not None:
        seg = from_contract
    elif row and row.get("detailed_json") is not None:
        dj = row["detailed_json"]
        if isinstance(dj, str):
            try:
                dj = json.loads(dj)
            except json.JSONDecodeError:
                dj = None
        if isinstance(dj, dict):
            block = dj.get("_o2c_drive")
            if isinstance(block, dict):
                s = (block.get("contract_folder_segment") or "").strip()
                if s:
                    seg = s
    try:

        def _upload() -> str | None:
            return upload_mis_xlsx_layout(
                drive_svc,
                mis_parent_folder_id=parent,
                contract_folder_segment=seg,
                period_start=period_start,
                local_xlsx=lp,
            )

        drive_fid = await run_blocking(_upload)
    except Exception as e:
        return ref, str(e)
    if drive_fid and not settings.o2c_gdrive_keep_local_mis_xlsx:
        uri = f"gdrive://file/{drive_fid}"
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE o2c_mis_run SET xlsx_path = :uri, updated_at = now() "
                        "WHERE id = CAST(:mid AS uuid)"
                    ),
                    {"uri": uri, "mid": mis_run_id},
                )
        lp.unlink(missing_ok=True)
        return uri, None
    return ref, None


def finalize_mis_xlsx_to_gdrive_after_draft(
    *,
    mis_run_id: str,
    period_start: date,
    xlsx_path: str | Path,
    drive_svc: Any | None,
) -> tuple[str, str | None]:
    """Sync entrypoint for LangGraph / threadpool callers (runs async agenos + threadpool Drive)."""
    return run_agenos_async(
        finalize_mis_xlsx_to_gdrive_after_draft_async(
            mis_run_id=mis_run_id,
            period_start=period_start,
            xlsx_path=xlsx_path,
            drive_svc=drive_svc,
        )
    )
