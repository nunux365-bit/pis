"""MIS draft generation, rerun, save, approve orchestration (async API layer).

See ``app.agents.o2c_ohc.mis_drafts`` module docstring for site-alias / LLM auto-unlink vs controlled unlink.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

# Approve-path Google Drive upload retries (backoff seconds before attempts 2 and 3).
_GDRIVE_UPLOAD_MAX_ATTEMPTS = 3
_GDRIVE_UPLOAD_BACKOFF_SEC = (1.0, 2.0)

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records
from app.agents.o2c_ohc.mis_drafts import _build_mis_xlsx_to_path, create_or_refresh_mis_draft_for_site_async
from app.agents.o2c_ohc.mis_gdrive_finalize import (
    build_mis_drive_svc,
    finalize_mis_xlsx_to_gdrive_after_draft_async,
    unlink_o2c_temp_paths,
)
from app.services.o2c.attendance_workbook_cache import load_parsed_ohc_attendance_workbook_cached
from app.agents.o2c_ohc.mis_db import (
    FinalAmountLock,
    approve_mis_run,
    build_final_amount_locks_from_db,
    build_final_amount_locks_from_row_edits,
    count_mis_runs_by_status,
    fetch_mis_run_xlsx_bundle,
    get_mis_run,
    list_mis_runs,
    persist_mis_row_edits,
    reapply_final_amount_locks,
)
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking
from app.services.o2c.mis_auto_approve import (
    attach_auto_approve_after_draft,
    parse_latest_auto_approve_audit,
)
from app.services.o2c.mis_rerun import run_single_site_mis_rerun_async


def _mis_value_error_to_http(e: ValueError) -> HTTPException:
    msg = str(e)
    if "not found" in msg.lower():
        return HTTPException(status.HTTP_404_NOT_FOUND, msg)
    if "already approved" in msg.lower():
        return HTTPException(status.HTTP_409_CONFLICT, msg)
    return HTTPException(status.HTTP_400_BAD_REQUEST, msg)


async def list_mis_runs_with_counts(
    *, status: str | None, limit: int, period_scope: str = "prior_month"
) -> dict[str, Any]:
    items = await list_mis_runs(status=status, limit=limit, period_scope=period_scope)
    status_counts = await count_mis_runs_by_status(period_scope=period_scope)
    return {"count": len(items), "items": items, "status_counts": status_counts}


async def get_mis_run_or_404(*, mis_run_id: UUID) -> dict[str, Any]:
    try:
        run = await get_mis_run(mis_run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    audit = parse_latest_auto_approve_audit(run.get("notes"))
    if audit is not None:
        run["auto_approve"] = audit
    return run


async def generate_mis_draft_api(
    *,
    attendance_xlsx: str | None,
    client_site_key: str,
    period_start_s: str,
    period_end_s: str,
    out_dir: str,
    template_path: str,
    allow_expired_contract_terms: bool | None = None,
) -> dict[str, Any]:
    try:
        ps = date.fromisoformat(period_start_s)
        pe = date.fromisoformat(period_end_s)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid period: {e}") from e

    cleanup_paths: list[Path] = []
    key = (client_site_key or "").strip()
    if not key:
        return {
            "status": "skipped",
            "mis_run_id": None,
            "client_site_key": client_site_key,
            "reason": "empty_key",
            "xlsx_path": None,
        }

    def _drive_and_parse() -> tuple[Any, list[dict[str, Any]]]:
        """Google Drive + OpenPyXL: must not block the event loop."""
        svc = build_mis_drive_svc()
        parsed = load_parsed_ohc_attendance_workbook_cached(
            explicit=(attendance_xlsx or "").strip() or None,
            cleanup_paths=cleanup_paths,
            drive_svc=svc,
            engine="auto",
        )
        amap = parsed.amap
        if not amap:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "No attendance workbook: pass attendance_xlsx, set O2C_ATTENDANCE_XLSX_PATH / "
                "O2C_OHC_ATTENDANCE_XLSX_PATH, or O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID with working "
                "Drive credentials (O2C_GDRIVE_SERVICE_ACCOUNT_JSON).",
            )
        records = amap.get(key) or []
        return parsed.resolved_path, records

    try:
        workbook_path, records = await run_blocking(_drive_and_parse)
        detailed_json = build_detailed_json_from_records(key, records, period_start=ps, period_end=pe)
        allow_expired = (
            bool(allow_expired_contract_terms)
            if allow_expired_contract_terms is not None
            else bool(getattr(settings, "o2c_mis_allow_expired_contract_terms", False))
        )
        try:
            out = await create_or_refresh_mis_draft_for_site_async(
                client_site_key=key,
                attendance_records=records,
                detailed_json=detailed_json,
                period_start=ps,
                period_end=pe,
                out_dir=Path(out_dir),
                template_path=Path(template_path),
                attendance_workbook_path=workbook_path,
                allow_expired_contract_terms=allow_expired,
            )
        except FileNotFoundError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

        xlsx_path = out.xlsx_path
        g_err: str | None = None
        if out.status == "ok" and out.mis_run_id and out.xlsx_path:
            drive_svc = await run_blocking(lambda: build_mis_drive_svc())
            xlsx_path, g_err = await finalize_mis_xlsx_to_gdrive_after_draft_async(
                mis_run_id=out.mis_run_id,
                period_start=ps,
                xlsx_path=out.xlsx_path,
                drive_svc=drive_svc,
            )
        resp: dict[str, Any] = {
            "status": out.status,
            "mis_run_id": out.mis_run_id,
            "client_site_key": out.client_site_key,
            "reason": out.reason,
            "xlsx_path": xlsx_path,
        }
        if g_err:
            resp["gdrive_upload_error"] = g_err
        return await attach_auto_approve_after_draft(
            resp, mis_run_id=out.mis_run_id, draft_status=str(out.status or "")
        )
    finally:
        await run_blocking(lambda: unlink_o2c_temp_paths(cleanup_paths))


async def _mis_run_rerun_context(mis_run_id: UUID) -> tuple[dict[str, Any], str, date, date]:
    try:
        run = await get_mis_run(mis_run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e

    alias = (run.get("client_site_key") or "").strip()
    if not alias:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MIS run has no client_site_key")
    try:
        ps = date.fromisoformat(str(run.get("billing_period_start", ""))[:10])
        pe = date.fromisoformat(str(run.get("billing_period_end", ""))[:10])
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid period on MIS run: {e}") from e

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            ar = await session.execute(
                text("""
                SELECT sa.service_site_id::text AS service_site_id
                FROM site_alias sa
                WHERE sa.alias_code = :ac
                LIMIT 1
                """),
                {"ac": alias},
            )
            alias_row = ar.mappings().first()
            service_site_id = str(alias_row["service_site_id"]) if alias_row else ""

    return run, alias, ps, pe


async def rerun_mis_for_existing_run(
    *,
    mis_run_id: UUID,
    allow_expired_contract_terms: bool = False,
    finalize_gdrive: bool = True,
    attempt_auto_approve: bool = False,
    amount_locks: list[FinalAmountLock] | None = None,
    preserved_by: str | None = None,
) -> dict[str, Any]:
    locks = (
        amount_locks
        if amount_locks is not None
        else await build_final_amount_locks_from_db(mis_run_id)
    )
    who = (preserved_by or "").strip()[:200] or "rerun"
    _run, alias, ps, pe = await _mis_run_rerun_context(mis_run_id)
    service_site_id = str(_run.get("service_site_id") or "").strip()
    if not service_site_id:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                ar = await session.execute(
                    text("""
                    SELECT sa.service_site_id::text AS service_site_id
                    FROM site_alias sa
                    WHERE sa.alias_code = :ac
                    LIMIT 1
                    """),
                    {"ac": alias},
                )
                alias_row = ar.mappings().first()
                service_site_id = str(alias_row["service_site_id"]) if alias_row else ""

    rerun = await run_single_site_mis_rerun_async(
        alias_code=alias,
        service_site_id=service_site_id,
        period_start=ps,
        period_end=pe,
        allow_expired_contract_terms=allow_expired_contract_terms,
        finalize_gdrive=finalize_gdrive,
    )
    out: dict[str, Any] = {"status": "ok", "rerun": rerun}
    if str(rerun.get("status") or "") == "ok" and locks:
        out["final_amount_locks"] = await reapply_final_amount_locks(
            mis_run_id,
            locks,
            saved_by=who,
        )
    if attempt_auto_approve and str(rerun.get("status") or "") == "ok" and rerun.get("mis_run_id"):
        out = await attach_auto_approve_after_draft(
            out,
            mis_run_id=str(rerun["mis_run_id"]),
            draft_status="ok",
        )
    return out


async def _upload_mis_xlsx_to_gdrive_with_retry(
    *,
    mis_run_id: str,
    period_start: date,
    xlsx_path: Path,
    drive_svc: Any | None,
) -> tuple[str, str | None]:
    """Upload local MIS xlsx to Drive; up to ``_GDRIVE_UPLOAD_MAX_ATTEMPTS`` with backoff."""
    xlsx_ref = str(xlsx_path)
    last_err: str | None = None
    for attempt in range(_GDRIVE_UPLOAD_MAX_ATTEMPTS):
        xlsx_ref, err = await finalize_mis_xlsx_to_gdrive_after_draft_async(
            mis_run_id=mis_run_id,
            period_start=period_start,
            xlsx_path=xlsx_path,
            drive_svc=drive_svc,
        )
        if not err:
            if attempt > 0:
                log.info(
                    "MIS Drive upload succeeded on attempt %s for run %s",
                    attempt + 1,
                    mis_run_id,
                )
            return xlsx_ref, None
        last_err = err
        if attempt < _GDRIVE_UPLOAD_MAX_ATTEMPTS - 1:
            delay = _GDRIVE_UPLOAD_BACKOFF_SEC[attempt]
            log.warning(
                "MIS Drive upload attempt %s/%s failed for run %s: %s; retry in %ss",
                attempt + 1,
                _GDRIVE_UPLOAD_MAX_ATTEMPTS,
                mis_run_id,
                err,
                delay,
            )
            await asyncio.sleep(delay)
    log.error(
        "MIS Drive upload failed after %s attempts for run %s: %s",
        _GDRIVE_UPLOAD_MAX_ATTEMPTS,
        mis_run_id,
        last_err,
    )
    return xlsx_ref, last_err


async def _export_mis_xlsx_to_gdrive(
    *,
    mis_run_id: UUID,
    billing_period_start: str,
) -> dict[str, Any]:
    out_dir = (getattr(settings, "o2c_invoice_out_dir", "") or "/tmp/o2c_out").strip() or "/tmp/o2c_out"
    tpl = (settings.o2c_invoice_mis_template_path or "").strip()
    mid = str(mis_run_id)

    mr_b, rows_b = await fetch_mis_run_xlsx_bundle(mid)
    tpl_path = Path(tpl) if tpl else Path("/dev/null")

    def _build() -> Path:
        return _build_mis_xlsx_to_path(
            mr_b,
            rows_b,
            out_dir=Path(out_dir),
            template_path=tpl_path,
        )

    out_path = await run_blocking(_build)
    ps = date.fromisoformat(str(billing_period_start)[:10])
    drive_svc = await run_blocking(lambda: build_mis_drive_svc())
    xlsx_final, g_err = await _upload_mis_xlsx_to_gdrive_with_retry(
        mis_run_id=mid,
        period_start=ps,
        xlsx_path=out_path,
        drive_svc=drive_svc,
    )
    payload: dict[str, Any] = {"xlsx_path": xlsx_final}
    if g_err:
        payload["xlsx_gdrive_error"] = g_err
        payload["xlsx_gdrive_upload_attempts"] = _GDRIVE_UPLOAD_MAX_ATTEMPTS
    return payload


async def save_mis_with_rerun(
    *,
    mis_run_id: UUID,
    row_edits: list[dict[str, Any]],
    saved_by: str | None,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any]:
    """Persist grid edits, then regenerate MIS (LLM draft) without Drive finalize."""
    who = (saved_by or "").strip()[:200] or "human"
    try:
        result = await persist_mis_row_edits(
            mis_run_id=mis_run_id,
            row_edits=row_edits,
            saved_by=who,
        )
    except ValueError as e:
        raise _mis_value_error_to_http(e) from e

    amount_locks = await build_final_amount_locks_from_row_edits(mis_run_id, row_edits)

    rerun_resp = await rerun_mis_for_existing_run(
        mis_run_id=mis_run_id,
        allow_expired_contract_terms=allow_expired_contract_terms,
        finalize_gdrive=False,
        attempt_auto_approve=False,
        amount_locks=amount_locks,
        preserved_by=who,
    )
    rerun = rerun_resp.get("rerun") if isinstance(rerun_resp.get("rerun"), dict) else {}
    result["rerun"] = rerun
    if str(rerun.get("status") or "") != "ok":
        reason = rerun.get("reason") or rerun.get("error") or "mis_rerun_failed"
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"MIS recalculation failed: {reason}",
        )

    if "final_amount_locks" in rerun_resp:
        result["final_amount_locks"] = rerun_resp["final_amount_locks"]
    return result


async def approve_mis_with_xlsx_export(
    *,
    mis_run_id: UUID,
    approved_by: str | None,
) -> dict[str, Any]:
    """Approve MIS run and build/upload invoice XLSX (no row edits, no LLM)."""
    who = (approved_by or "").strip()[:200] or "human"
    try:
        result = await approve_mis_run(mis_run_id=mis_run_id, approved_by=who)
    except ValueError as e:
        raise _mis_value_error_to_http(e) from e

    period_start = str(result.get("billing_period_start") or "")

    try:
        xlsx_payload = await _export_mis_xlsx_to_gdrive(
            mis_run_id=mis_run_id,
            billing_period_start=period_start,
        )
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE o2c_mis_run SET xlsx_path = :xp, updated_at = now() "
                        "WHERE id = CAST(:mid AS uuid)"
                    ),
                    {"xp": xlsx_payload.get("xlsx_path"), "mid": str(mis_run_id)},
                )
        result.update(xlsx_payload)
    except Exception as e:
        result["xlsx_error"] = str(e)

    return result


async def save_and_approve_mis_with_xlsx_export(
    *,
    mis_run_id: UUID,
    row_edits: list[dict[str, Any]],
    approved_by: str | None,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any]:
    """Save (persist + MIS rerun) then approve with XLSX — legacy combined endpoint."""
    who = (approved_by or "").strip()[:200] or "human"
    save_result = await save_mis_with_rerun(
        mis_run_id=mis_run_id,
        row_edits=row_edits,
        saved_by=who,
        allow_expired_contract_terms=allow_expired_contract_terms,
    )
    approve_result = await approve_mis_with_xlsx_export(
        mis_run_id=mis_run_id,
        approved_by=who,
    )
    return {
        **approve_result,
        "correction_count": save_result.get("correction_count", 0),
        "rerun": save_result.get("rerun"),
    }
