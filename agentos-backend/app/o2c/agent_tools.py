"""Stable entrypoints for an O2C_OHC LangGraph agent (ingest, attendance map)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.o2c_ohc.mis_gdrive_finalize import (
    build_mis_drive_svc,
    resolve_attendance_xlsx_path,
    unlink_o2c_temp_paths,
)
from app.o2c.attendance import load_ohc_summary_by_client_site
from app.o2c.runner import run_o2c_folder_ingest


async def tool_ingest_contract_folder(
    *,
    contracts_root: str | None = None,
    max_files: int | None = None,
) -> dict[str, Any]:
    """Scan PDFs under O2C_CONTRACTS_ROOT (or override), LLM extract, validate, DB persist."""
    return await run_o2c_folder_ingest(contracts_root=contracts_root, max_files=max_files)


def tool_load_ohc_attendance_map(
    xlsx_path: str | None = None,
    *,
    sheet_name: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Load Summary tab; returns client_site → rows (see attendance.load_ohc_summary_by_client_site).

    Resolution order matches MIS/graph: optional ``xlsx_path``, then env local paths, then
    ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID`` export (temp file removed after load).
    """
    cleanup_paths: list[Path] = []
    try:
        explicit = (xlsx_path or "").strip() or None
        svc = build_mis_drive_svc()
        p = resolve_attendance_xlsx_path(
            explicit=explicit,
            cleanup_paths=cleanup_paths,
            drive_svc=svc,
        )
        if not p:
            raise ValueError(
                "No attendance workbook: pass xlsx_path, set O2C_ATTENDANCE_XLSX_PATH / "
                "O2C_OHC_ATTENDANCE_XLSX_PATH, or O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID with working "
                "Drive credentials."
            )
        return load_ohc_summary_by_client_site(p, sheet_name=sheet_name)
    finally:
        unlink_o2c_temp_paths(cleanup_paths)
