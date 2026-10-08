"""MIS draft rerun for one site/period: Drive + OpenPyXL on threadpool; agenos + LLM async."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from app.agents.o2c_ohc.agenos_async_session import run_agenos_async
from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records
from app.agents.o2c_ohc.mis_drafts import create_or_refresh_mis_draft_for_site_async
from app.agents.o2c_ohc.mis_gdrive_finalize import (
    build_mis_drive_svc,
    finalize_mis_xlsx_to_gdrive_after_draft_async,
    unlink_o2c_temp_paths,
)
from app.services.o2c.attendance_workbook_cache import load_parsed_ohc_attendance_workbook_cached
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking


async def run_single_site_mis_rerun_async(
    *,
    alias_code: str,
    service_site_id: str,
    period_start: date,
    period_end: date,
    allow_expired_contract_terms: bool = False,
    finalize_gdrive: bool = True,
) -> dict[str, Any]:
    tpl = (settings.o2c_invoice_mis_template_path or "").strip()
    if not tpl:
        return {
            "attempted": False,
            "error": "missing_settings:o2c_invoice_mis_template_path",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "client_site_key": alias_code,
            "service_site_id": service_site_id,
        }
    cleanup_paths: list[Path] = []
    key = (alias_code or "").strip()

    def _drive_parse() -> tuple[bool, Any, list[dict[str, Any]]]:
        svc = build_mis_drive_svc()
        parsed = load_parsed_ohc_attendance_workbook_cached(
            explicit=None, cleanup_paths=cleanup_paths, drive_svc=svc, engine="auto"
        )
        amap = parsed.amap
        if not amap:
            return False, None, []
        return True, parsed.resolved_path, (amap.get(key) or [])

    try:
        ok, workbook_path, records = await run_blocking(_drive_parse)
        if not ok:
            return {
                "attempted": False,
                "error": (
                    "missing_settings: set O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID or "
                    "O2C_ATTENDANCE_XLSX_PATH (or O2C_OHC_ATTENDANCE_XLSX_PATH)"
                ),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "client_site_key": alias_code,
                "service_site_id": service_site_id,
            }

        out_root = (getattr(settings, "o2c_invoice_out_dir", "") or "").strip() or tempfile.gettempdir()
        out_dir = Path(out_root)

        def _mkdir() -> None:
            out_dir.mkdir(parents=True, exist_ok=True)

        await run_blocking(_mkdir)

        detailed_json = build_detailed_json_from_records(
            key, records, period_start=period_start, period_end=period_end
        )
        r = await create_or_refresh_mis_draft_for_site_async(
            client_site_key=key,
            attendance_records=records,
            detailed_json=detailed_json,
            period_start=period_start,
            period_end=period_end,
            out_dir=out_dir,
            template_path=Path(tpl),
            attendance_workbook_path=workbook_path,
            allow_expired_contract_terms=allow_expired_contract_terms,
        )
        xlsx_ref = r.xlsx_path
        upload_err: str | None = None
        if finalize_gdrive and r.status == "ok" and r.mis_run_id and r.xlsx_path:
            drive_svc = await run_blocking(lambda: build_mis_drive_svc())
            xlsx_ref, upload_err = await finalize_mis_xlsx_to_gdrive_after_draft_async(
                mis_run_id=r.mis_run_id,
                period_start=period_start,
                xlsx_path=r.xlsx_path,
                drive_svc=drive_svc,
            )
        row: dict[str, Any] = {
            "attempted": True,
            "status": r.status,
            "reason": r.reason,
            "mis_run_id": r.mis_run_id,
            "xlsx_path": xlsx_ref,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "client_site_key": r.client_site_key,
            "service_site_id": service_site_id,
        }
        if upload_err:
            row["gdrive_upload_error"] = upload_err
        return row
    except Exception as e:
        return {
            "attempted": True,
            "error": str(e),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "client_site_key": alias_code,
            "service_site_id": service_site_id,
        }
    finally:
        await run_blocking(lambda: unlink_o2c_temp_paths(cleanup_paths))


def run_single_site_mis_rerun(
    *,
    alias_code: str,
    service_site_id: str,
    period_start: date,
    period_end: date,
    allow_expired_contract_terms: bool = False,
    finalize_gdrive: bool = True,
) -> dict[str, Any]:
    """Sync entry (no event loop): runs async implementation via ``run_agenos_async``."""
    return run_agenos_async(
        run_single_site_mis_rerun_async(
            alias_code=alias_code,
            service_site_id=service_site_id,
            period_start=period_start,
            period_end=period_end,
            allow_expired_contract_terms=allow_expired_contract_terms,
            finalize_gdrive=finalize_gdrive,
        )
    )
