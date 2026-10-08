# app/api/routes/sbi_mis.py
"""SBI MIS — all API endpoints.

Mount prefix: /api/v1/sbi-mis
Auth guard:   require_roles(UserRole.PHARMA_MIS_OPERATOR)
              (SYSTEM_ADMIN passes automatically via require_roles logic)
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.api.deps import get_current_user, require_roles
from app.db.models import User, UserRole
from app.db.session import get_db
from app.sbi_mis import db as sbi_db
from app.sbi_mis import format_spec, jobs as sbi_jobs, state as sbi_state
from app.sbi_mis.models import SbiJob
from app.sbi_mis.engine import export as _export, ingest as _ingest, pf_ingest as _pf_ingest
from app.infra.thread_pools import cpu_executor

router = APIRouter()
_GUARD = Depends(require_roles(UserRole.PHARMA_MIS_OPERATOR))
_MAX_UPLOAD_BYTES = 120 * 1024 * 1024
_upload_sessions: Dict[str, Dict[str, Any]] = {}


# ── Pydantic models ──────────────────────────────────────────────────────────

class RuleUpdate(BaseModel):
    rule_type: str
    config: Dict[str, Any] = {}
    status: Optional[str] = None


class RulesImportBody(BaseModel):
    rules: List[Dict[str, Any]]
    rerun: bool = True


class LookupUpdate(BaseModel):
    data: List[Dict[str, Any]]


class UploadSessionCreate(BaseModel):
    month: str
    kind: str = "raw"
    filename: str = "upload"
    file_size: int = Field(..., ge=1, le=_MAX_UPLOAD_BYTES)


class UploadSessionCompleteBody(BaseModel):
    upload_id: str
    raw_sheet_hint: Optional[str] = None
    skip_rerun: bool = False


class BatchRerunBody(BaseModel):
    month: str
    kinds: List[str]  # subset of ["raw", "ahc", "wallet_checker", "pf_summary"]


class SheetColumnAdd(BaseModel):
    header: str
    col_letter: Optional[str] = None
    number_format: Optional[str] = None
    rule_type: str = "blank"
    config: Dict[str, Any] = {}
    notes: Optional[str] = None


class SheetColumnPatch(BaseModel):
    header: Optional[str] = None
    number_format: Optional[str] = None


class ColumnFormatUpdate(BaseModel):
    number_format: Optional[str] = None


class NLToFormulaBody(BaseModel):
    text: str
    sheet: str
    column: str
    header: Optional[str] = None


# ── Recon helpers ─────────────────────────────────────────────────────────────

def _read_raw_recon_metrics(raw_path: Path) -> tuple:
    """Read row count, GMV sum, and unique order IDs directly from the raw file.

    Deliberately bypasses load_raw() normalisation — this is the independent source
    of truth that recon compares against the pipeline's in-memory DataFrame.

    Returns (rows, gmv_mrp, unique_order_ids).
    """
    headers, source_label = _ingest._read_headers_only(raw_path)
    z_idx = next((i for i, h in enumerate(headers) if h == "gmv_mrp"), None)
    e_idx = next((i for i, h in enumerate(headers) if h == "order_id"), None)
    usecols = [i for i in (e_idx, z_idx) if i is not None]

    if not usecols:
        return 0, 0.0, 0

    if _ingest._is_csv(raw_path):
        raw_df = pd.read_csv(raw_path, usecols=sorted(usecols), dtype=object)
    else:
        try:
            raw_df = pd.read_excel(raw_path, sheet_name=source_label,
                                   header=0, usecols=sorted(usecols),
                                   engine="calamine")
        except Exception:
            raw_df = pd.read_excel(raw_path, sheet_name=source_label,
                                   header=0, usecols=sorted(usecols),
                                   engine="openpyxl", dtype=object)

    raw_df = raw_df.dropna(how="all")
    rows = len(raw_df)
    gmv = 0.0
    oids = 0
    if z_idx is not None and headers[z_idx] in raw_df.columns:
        gmv = float(pd.to_numeric(raw_df[headers[z_idx]], errors="coerce").fillna(0).sum())
    if e_idx is not None and headers[e_idx] in raw_df.columns:
        oids = int(raw_df[headers[e_idx]].dropna().nunique())
    return rows, gmv, oids


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


# ── Schema ────────────────────────────────────────────────────────────────────

@router.get("/schema", dependencies=[_GUARD])
async def schema() -> Dict[str, Any]:
    return format_spec.schema_json()


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status", dependencies=[_GUARD])
async def status(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    active_run = (
        await sbi_db.get_run(session, sbi_state._cache.active_month)
        if sbi_state._cache.active_month else None
    )
    runs = await sbi_db.list_runs(session)
    # _cache.computing is True only during the pipeline run itself.  Any pending/
    # running DB job (including the file-parse phase before do_one_rerun) also
    # counts as "computing" so the nav spinner stays on for the full job duration.
    has_active_job_row = bool(
        (await session.execute(
            select(SbiJob.id).where(SbiJob.status.in_(["pending", "running"])).limit(1)
        )).scalar_one_or_none()
    )
    return {
        "has_upload": sbi_state._cache.dump_df is not None,
        "computing": sbi_state._cache.computing or has_active_job_row,
        "error": sbi_state._cache.last_error,
        "computed_at": sbi_state._cache.computed_at,
        "active_month": sbi_state._cache.active_month,
        "active_run": active_run,
        "available_months": [r["month"] for r in runs],
    }


# ── Runs ──────────────────────────────────────────────────────────────────────

@router.get("/runs", dependencies=[_GUARD])
async def runs_list(session: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    return await sbi_db.list_runs(session)


@router.post("/runs/{month}/activate", dependencies=[_GUARD])
async def activate_run(
    month: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    run = await sbi_db.get_run(session, month)
    if not run:
        raise HTTPException(status_code=404, detail=f"No run found for month {month}")
    p = sbi_db.resolve_stored_path(run["raw_file_path"])
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail=f"Raw file missing for month {month}")

    # Signal computing immediately so the UI shows the indicator before we return.
    sbi_state._cache.active_month = month
    sbi_state._cache.computing = True
    sbi_state._cache.last_error = None
    sbi_state.invalidate_output_cache()

    # Persist activation to DB so server restarts restore this month automatically.
    await sbi_db.set_active_month(session, month)

    job_id = await sbi_jobs.create_job("activate")
    background_tasks.add_task(_run_activate_job, job_id, p, month)

    return {"active_month": month, "computing": True, "job_id": job_id}


async def _run_activate_job(job_id: str, raw_path: Path, month: str) -> None:
    """Background task: load raw file then run pipeline."""
    loop = asyncio.get_event_loop()
    try:
        await sbi_jobs.update_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc),
                                  stage="loading file")
        df = await loop.run_in_executor(cpu_executor(), lambda: _ingest.load_raw(raw_path))
        sbi_state._cache.dump_df = df
        sbi_state._cache.raw_file_path = raw_path
        await sbi_jobs.update_job(job_id, stage="running pipeline", progress=0.3)
        await sbi_state.do_one_rerun(trigger="raw")
        # do_one_rerun swallows pipeline exceptions internally — surface a silent
        # failure as a failed job instead of "done" over an empty working sheet.
        if sbi_state._cache.results is None:
            await sbi_jobs.update_job(
                job_id, status="failed",
                error_detail=sbi_state._cache.last_error or "pipeline produced no results",
                completed_at=datetime.now(timezone.utc))
            return
        await sbi_jobs.update_job(
            job_id, status="done", progress=1.0, stage="done",
            result_json={"month": month, "row_count": len(df)},
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        sbi_state._cache.computing = False
        sbi_state._cache.last_error = str(e)
        await sbi_jobs.update_job(job_id, status="failed", error_detail=str(e),
                                  completed_at=datetime.now(timezone.utc))


# ── Upload (direct) ───────────────────────────────────────────────────────────

@router.post("/upload", dependencies=[_GUARD])
async def upload(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    file: Any = File(...),
    month: str = Form(...),
    kind: str = Form("raw"),
) -> Dict[str, Any]:
    kind = kind.lower().strip()
    if kind not in ("raw", "pf_summary", "ahc", "wallet_checker"):
        return JSONResponse(status_code=400, content={"error": f"Unknown kind '{kind}'"})
    if not re.fullmatch(r"\d{4}-\d{2}", month.strip()):
        return JSONResponse(status_code=400, content={"error": "Invalid month: expected YYYY-MM"})

    upload_dir = sbi_db.upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)

    original_filename = (file.filename or "upload").strip() or "upload"
    ext = Path(original_filename).suffix.lower() or ".xlsx"
    allowed = {".xlsx", ".xlsm", ".csv", ".tsv"} if kind != "pf_summary" else {".xlsx", ".xlsm"}
    if ext not in allowed:
        return JSONResponse(status_code=400, content={
            "error": f"Unsupported file type '{ext}' for kind '{kind}'"
        })

    safe_name = f"{kind}_{month}_{int(time.time())}{ext}"
    dest = upload_dir / safe_name
    total = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
                total += len(chunk)
    except OSError as e:
        dest.unlink(missing_ok=True)
        return JSONResponse(status_code=500, content={"error": f"Could not save upload: {e}"})

    if total == 0:
        dest.unlink(missing_ok=True)
        return JSONResponse(status_code=400, content={"error": "Empty file"})

    job_id = await sbi_jobs.create_job(f"upload:{kind}")
    background_tasks.add_task(_run_upload_job, job_id, dest, month, kind,
                               original_filename, ext, None, session)
    return {"job_id": job_id, "status": "pending", "kind": kind,
            "month": month, "filename": original_filename}


async def _run_upload_job(job_id: str, dest: Path, month: str, kind: str,
                           original_filename: str, ext: str,
                           raw_sheet_hint: Optional[str],
                           session: AsyncSession,
                           skip_rerun: bool = False) -> None:
    loop = __import__("asyncio").get_event_loop()
    try:
        await sbi_jobs.update_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc))
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_session:
            if kind == "raw":
                await sbi_jobs.update_job(job_id, stage="parsing xlsx/csv", progress=0.1)
                df = await loop.run_in_executor(
                    cpu_executor(), lambda: _ingest.load_raw(dest, sheet_hint=raw_sheet_hint)
                )
                rc = len(df)
                await sbi_jobs.update_job(job_id, stage="registering upload", progress=0.3)
                await sbi_db.set_current_run(bg_session, str(dest), month, rc)
                await sbi_db.upsert_run_file(bg_session, month, "raw", str(dest),
                                              original_filename=original_filename, row_count=rc)

                # Compute raw-file recon metrics by reading the file directly —
                # NOT from dump_df (load_raw renames columns to letters, so
                # df.get("gmv_mrp") would silently return an empty Series).
                # The file is hot in the OS page cache right after upload, so
                # this 2-column read is essentially free.
                _, raw_gmv, raw_oids = await loop.run_in_executor(
                    cpu_executor(), lambda: _read_raw_recon_metrics(dest)
                )
                await sbi_db.update_run_recon_metrics(
                    bg_session, month, raw_gmv, raw_oids, from_direct_read=True
                )

                sbi_state._cache.dump_df = df
                sbi_state._cache.active_month = month
                sbi_state._cache.raw_file_path = dest

                # Auto-ingest embedded PF Summary sheet if present in the xlsx.
                # Seeds pf_historicals (pharma/AHC totals + wallet_limits) so the
                # pipeline can populate historical columns and wallet balance without
                # a separate PF summary upload.
                if not _ingest._is_csv(dest):
                    try:
                        from openpyxl import load_workbook as _lw
                        _tmp_wb = _lw(dest, read_only=True, data_only=True)
                        _has_pf_sheet = "PF Summary" in _tmp_wb.sheetnames
                        _tmp_wb.close()
                        if _has_pf_sheet:
                            await sbi_jobs.update_job(job_id, stage="seeding historicals from PF Summary", progress=0.4)
                            _year = int(month.split("-")[0]) if "-" in month else 2026
                            _pf_records, _ = await loop.run_in_executor(
                                cpu_executor(), lambda: _pf_ingest.parse_pf_summary(dest, default_year=_year)
                            )
                            if _pf_records:
                                await sbi_db.bulk_upsert_pf_historicals(bg_session, _pf_records)
                    except Exception as _pf_err:
                        import logging as _log
                        _log.getLogger(__name__).warning(
                            "Auto-ingest of embedded PF Summary failed (non-fatal): %s", _pf_err
                        )

                await sbi_jobs.update_job(job_id, stage="running pipeline", progress=0.5)
                if not skip_rerun:
                    await sbi_state.do_one_rerun(trigger="raw")
                result = {"kind": "raw", "month": month, "row_count": rc,
                          "filename": original_filename}

            elif kind == "pf_summary":
                year = int(month.split("-")[0]) if "-" in month else 2026
                await sbi_jobs.update_job(job_id, stage="parsing PF summary", progress=0.2)
                records, info = await loop.run_in_executor(
                    cpu_executor(), lambda: _pf_ingest.parse_pf_summary(dest, default_year=year)
                )
                await sbi_jobs.update_job(job_id, stage="writing historicals", progress=0.6)
                if records:
                    await sbi_db.bulk_upsert_pf_historicals(bg_session, records)
                await sbi_db.upsert_run_file(bg_session, month, "pf_summary", str(dest),
                                              original_filename=original_filename,
                                              row_count=info.get("row_count"))
                if not skip_rerun:
                    await sbi_state.do_one_rerun(trigger="pf_summary_file")
                result = {"kind": "pf_summary", "month": month,
                          "records_imported": len(records), "filename": original_filename}

            elif kind == "ahc":
                await sbi_db.upsert_run_file(bg_session, month, "ahc", str(dest),
                                              original_filename=original_filename)
                if not skip_rerun:
                    await sbi_state.do_one_rerun(trigger="ahc")
                result = {"kind": "ahc", "month": month, "filename": original_filename}

            else:  # wallet_checker
                await sbi_db.upsert_run_file(bg_session, month, "wallet_checker", str(dest),
                                              original_filename=original_filename)
                if not skip_rerun:
                    await sbi_state.do_one_rerun(trigger="wallet_checker")
                result = {"kind": "wallet_checker", "month": month, "filename": original_filename}

        # do_one_rerun swallows pipeline exceptions internally (sets _cache.last_error,
        # results=None). Surface that as a failed job instead of a misleading "done"
        # over an empty dashboard/working sheet. dump_df None just means nothing to run.
        if (not skip_rerun and sbi_state._cache.dump_df is not None
                and sbi_state._cache.results is None):
            await sbi_jobs.update_job(
                job_id, status="failed",
                error_detail=sbi_state._cache.last_error or "pipeline produced no results",
                completed_at=datetime.now(timezone.utc))
            return

        await sbi_jobs.update_job(job_id, status="done", progress=1.0, stage="done",
                                  result_json=result,
                                  completed_at=datetime.now(timezone.utc))
    except Exception as e:
        await sbi_jobs.update_job(job_id, status="failed", error_detail=str(e),
                                  completed_at=datetime.now(timezone.utc))


# ── Upload (chunked) ──────────────────────────────────────────────────────────

@router.post("/upload/session", dependencies=[_GUARD])
async def upload_session_create(body: UploadSessionCreate) -> Dict[str, Any]:
    import uuid as _uuid
    upload_id = str(_uuid.uuid4())
    upload_dir = sbi_db.upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(body.filename).suffix.lower() or ".xlsx"
    chunk_size = 2 * 1024 * 1024  # 2 MiB
    chunk_count = math.ceil(body.file_size / chunk_size)
    tmp_path = upload_dir / f"tmp_{upload_id}{ext}"
    _upload_sessions[upload_id] = {
        "month": body.month, "kind": body.kind, "filename": body.filename,
        "file_size": body.file_size, "ext": ext,
        "chunk_size": chunk_size, "chunk_count": chunk_count,
        "path": str(tmp_path), "received": set(), "created": time.time(),
    }
    return {"upload_id": upload_id, "chunk_size": chunk_size, "chunk_count": chunk_count}


@router.put("/upload/chunk/{upload_id}/{chunk_index}", dependencies=[_GUARD])
async def upload_chunk(upload_id: str, chunk_index: int, request: Request) -> Dict[str, Any]:
    sess = _upload_sessions.get(upload_id)
    if not sess:
        raise HTTPException(status_code=404, detail="upload session not found")
    data = await request.body()
    p = Path(sess["path"])
    offset = chunk_index * int(sess["chunk_size"])
    with open(p, "r+b" if p.exists() else "wb") as f:
        f.seek(offset)
        f.write(data)
    sess["received"].add(chunk_index)
    return {"ok": True, "chunk_index": chunk_index,
            "received_count": len(sess["received"]),
            "chunk_count": sess["chunk_count"]}


@router.post("/upload/complete", dependencies=[_GUARD])
async def upload_complete(
    body: UploadSessionCompleteBody,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    sess = _upload_sessions.pop(body.upload_id, None)
    if not sess:
        raise HTTPException(status_code=404, detail="upload session not found")
    dest = Path(sess["path"])
    if not dest.exists() or dest.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Upload incomplete")
    month, kind, ext = sess["month"], sess["kind"], sess["ext"]
    original_filename = sess["filename"]
    job_id = await sbi_jobs.create_job(f"upload:{kind}")
    background_tasks.add_task(
        _run_upload_job, job_id, dest, month, kind,
        original_filename, ext, body.raw_sheet_hint, None,
        body.skip_rerun,
    )
    return {"job_id": job_id, "status": "pending", "kind": kind,
            "month": month, "filename": original_filename}


@router.post("/upload/batch-rerun", dependencies=[_GUARD])
async def batch_rerun(
    body: BatchRerunBody,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Trigger a single combined pipeline rerun after a batch of supplementary uploads.

    Called by the frontend after uploading multiple files with skip_rerun=true.
    Selects the most efficient trigger based on which file kinds were uploaded:
    - raw included → full run (trigger="raw")
    - only supplementary files → partial run (trigger="ahc", skips Dump/OL stages)
    """
    if "raw" in body.kinds:
        trigger: Optional[str] = "raw"
    elif body.kinds:
        # ahc and wallet_checker share the same skip_stages; use "ahc" as the trigger
        trigger = "ahc"
    else:
        return {"ok": True, "job_id": "", "message": "no kinds — nothing to rerun"}

    job_id = await sbi_state.schedule_rerun(background_tasks, trigger=trigger)
    return {"ok": True, "job_id": job_id, "trigger": trigger}


# ── Jobs ──────────────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}", dependencies=[_GUARD])
async def get_job(job_id: str) -> Dict[str, Any]:
    job = await sbi_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ── Run files ─────────────────────────────────────────────────────────────────

@router.get("/run-files", dependencies=[_GUARD])
async def get_run_files(session: AsyncSession = Depends(get_db),
                         month: Optional[str] = None) -> List[Dict[str, Any]]:
    return await sbi_db.all_run_files(session)


# ── Billing summary ───────────────────────────────────────────────────────────

@router.get("/billing-summary", dependencies=[_GUARD])
async def billing_summary() -> Dict[str, Any]:
    """Permissible vs non-permissible GMV + order counts for the dashboard header.

    Result is memoised per pipeline run (keyed by computed_at) so repeated
    dashboard refreshes don't re-aggregate the DataFrames.
    """
    if sbi_state._cache.results is None:
        return {"empty": True}

    cache_key = f"billing_{sbi_state._cache.computed_at}"
    if cache_key in sbi_state._dict_cache:
        return sbi_state._dict_cache[cache_key]

    sheets = sbi_state._cache.results.get("sheets", {})
    dump_df = sbi_state._cache.dump_df
    active_month = sbi_state._cache.active_month

    mrp_col  = next((c.col for c in format_spec.ORDER_LEVEL.columns if c.header == "GMV_MRP"),  "J")
    list_col = next((c.col for c in format_spec.ORDER_LEVEL.columns if c.header == "GMV_LIST"), "L")

    def _agg(sheet_name: str) -> Dict[str, Any]:
        df = sheets.get(sheet_name)
        if df is None or not hasattr(df, "__len__"):
            return {"rows": 0, "gmv_mrp": 0.0, "gmv_list": 0.0}
        mrp  = float(pd.to_numeric(df[mrp_col],  errors="coerce").fillna(0).sum()) if mrp_col  in df.columns else 0.0
        list_ = float(pd.to_numeric(df[list_col], errors="coerce").fillna(0).sum()) if list_col in df.columns else 0.0
        return {"rows": len(df), "gmv_mrp": round(mrp, 2), "gmv_list": round(list_, 2)}

    perm = _agg("Order level ")
    np_ = _agg("Order level - non permissible")

    result = {
        "empty": False,
        "active_month": active_month,
        "row_count": len(dump_df) if dump_df is not None else 0,
        "permissible": perm,
        "non_permissible": np_,
        "grand_total": {
            "rows": perm["rows"] + np_["rows"],
            "gmv_mrp":  round(perm["gmv_mrp"]  + np_["gmv_mrp"],  2),
            "gmv_list": round(perm["gmv_list"] + np_["gmv_list"], 2),
        },
    }
    sbi_state._dict_cache[cache_key] = result
    return result


# ── Historicals ───────────────────────────────────────────────────────────────

@router.get("/historicals/stats", dependencies=[_GUARD])
async def historicals_stats(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Fast — returns only per-month aggregates (GROUP BY, 2-3 rows).
    Load this first so the month cards render immediately."""
    month_stats, months = await asyncio.gather(
        sbi_db.pf_historicals_month_stats(session),
        sbi_db.pf_historicals_months(session),
    )
    total = sum(s["pf_count"] for s in month_stats.values())
    return {"months": months, "month_stats": month_stats, "total_records": total}


@router.get("/historicals", dependencies=[_GUARD])
async def historicals(session: AsyncSession = Depends(get_db),
                       month: Optional[str] = None) -> Dict[str, Any]:
    """Slow — returns full row-level detail. Call after /historicals/stats."""
    rows = await sbi_db.pf_historicals_list(session, month_filter=month)
    return {"rows": rows, "count": len(rows)}


@router.delete("/historicals/{month}", dependencies=[_GUARD])
async def historicals_delete(
    month: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    n = await sbi_db.pf_historicals_delete_month(session, month)
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger="historicals")
    return {"ok": True, "month": month, "deleted": n, "rerun_job_id": job_id}


# ── Rules ─────────────────────────────────────────────────────────────────────

@router.get("/rules", dependencies=[_GUARD])
async def get_rules(session: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    return await sbi_db.get_all_rules(session)


@router.get("/rules/export", dependencies=[_GUARD])
async def export_rules(session: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    return await sbi_db.get_all_rules(session)


@router.post("/rules/import", dependencies=[_GUARD])
async def import_rules(
    body: RulesImportBody,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Bulk-upsert rules from an exported JSON array.

    Skips rows missing ``sheet``, ``column_letter``, or ``rule_type``.
    Triggers a pipeline rerun by default (pass ``rerun: false`` to suppress).
    """
    imported = await sbi_db.bulk_import_rules(session, body.rules)
    job_id = ""
    if body.rerun and imported > 0:
        job_id = await sbi_state.schedule_rerun(background_tasks, trigger=None)
    return {"imported": imported, "rerun_job_id": job_id}


@router.get("/rules/{sheet}/{col}", dependencies=[_GUARD])
async def get_rule(sheet: str, col: str,
                   session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    r = await sbi_db.get_rule(session, sheet, col)
    if r is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return r


@router.put("/rules/{sheet}/{col}", dependencies=[_GUARD])
async def update_rule(
    sheet: str, col: str, body: RuleUpdate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    updated = await sbi_db.update_rule(session, sheet, col, body.rule_type,
                                        body.config, body.status)
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger=f"rule:{sheet}")
    return {"rule": updated, "rerun_job_id": job_id, "error": sbi_state._cache.last_error}


# ── Lookup tables ─────────────────────────────────────────────────────────────

@router.get("/lookup-tables", dependencies=[_GUARD])
async def lookup_tables(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    names = await sbi_db.list_lookups(session)
    result = {}
    for name in names:
        result[name] = await sbi_db.get_lookup(session, name)
    return result


@router.put("/lookup-tables/{name}", dependencies=[_GUARD])
async def set_lookup(
    name: str, body: LookupUpdate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    await sbi_db.set_lookup(session, name, body.data)
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger="rule:Dump")
    return {"ok": True, "rerun_job_id": job_id}


# ── Preview ───────────────────────────────────────────────────────────────────

def _clean_for_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, bool, str, float)):
        return v
    try:
        import pandas as _pd
        import numpy as _np
        if isinstance(v, _pd.Timestamp):
            return v.isoformat()
        if isinstance(v, _np.generic):
            return _clean_for_json(v.item())
    except Exception:
        pass
    try:
        from datetime import datetime as _dt, date as _date
        if isinstance(v, (_dt, _date)):
            return v.isoformat()
    except Exception:
        pass
    return str(v)


@router.get("/preview/{sheet:path}", dependencies=[_GUARD])
async def preview(sheet: str, offset: int = 0, limit: int = 200,
                  session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    if sbi_state._cache.results is None:
        return {"error": sbi_state._cache.last_error or "no data yet",
                "rows": [], "total": 0, "columns": []}

    data = sbi_state._cache.results["sheets"].get(sheet)
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown sheet '{sheet}'")

    if sheet == "Summary":
        summary_rules_map = await sbi_db.get_rules_for_sheet(session, sheet)
        cells = [
            {"cell": sc.cell, "value": _clean_for_json(data.get(sc.cell)),
             "number_format": sc.number_format,
             "rule_type": (summary_rules_map.get(sc.cell) or {}).get("rule_type")
                           or sc.default_rule["type"]}
            for sc in format_spec.SUMMARY_CELLS
        ]
        return {"sheet": sheet, "kind": "summary", "cells": cells}

    df: pd.DataFrame = data
    total = len(df)
    chunk = df.iloc[offset: offset + limit]
    # Batch-fetch all rules for this sheet in one query (avoids N+1 per column).
    format_overrides = await sbi_db.get_column_formats(session)
    rules_map = await sbi_db.get_rules_for_sheet(session, sheet)

    if sheet == "Dump":
        from openpyxl.utils import get_column_letter as _gl
        headers = list(format_spec.DUMP_RAW_HEADERS) + \
                  [c["header"] for c in format_spec.DUMP_ENRICHMENT_COLS]
        raw_col_count = len(format_spec.DUMP_RAW_HEADERS)
        cols = []
        for i, h in enumerate(headers, start=1):
            letter = _gl(i)
            is_derived = i > raw_col_count
            rule = rules_map.get(letter) if is_derived else None
            cols.append({"col": letter, "header": h,
                         "is_derived": is_derived,
                         "rule_type": rule["rule_type"] if rule else "raw",
                         "number_format": format_overrides.get("Dump", {}).get(letter)})
    else:
        # PF Summary uses dynamic spec so column headers reflect the active month
        # (e.g. Mar/Apr/May for a May run, not the static Jan/Feb/Mar defaults).
        if sheet == "PF Summary":
            spec = format_spec.pf_summary_dynamic_spec(sbi_state._cache.active_month)
        else:
            spec = next((s for s in format_spec.OUTPUT_SHEETS if s.name == sheet), None)
        cols = []
        if spec:
            for c in format_spec.effective_columns(spec):
                rule = rules_map.get(c.col)
                nf = format_overrides.get(sheet, {}).get(c.col, c.number_format)
                cols.append({"col": c.col, "header": c.header, "is_derived": True,
                             "rule_type": rule["rule_type"] if rule else c.default_rule["type"],
                             "number_format": nf})
        else:
            from openpyxl.utils import get_column_letter as _gl
            cols = [{"col": _gl(i), "header": c, "is_derived": True,
                     "rule_type": None,
                     "number_format": format_overrides.get(sheet, {}).get(_gl(i))}
                    for i, c in enumerate(df.columns, start=1)]

    col_order = [c["col"] for c in cols]
    # itertuples is ~10-30x faster than iterrows for row serialisation.
    # For pass-through sheets (e.g. AHC) the DataFrame has original column names while
    # col_order holds display letter aliases ("A","B",...) — reindex by position instead.
    if col_order and col_order[0] not in chunk.columns:
        chunk_ordered = chunk.iloc[:, : len(col_order)].copy()
        chunk_ordered.columns = col_order
    else:
        chunk_ordered = chunk.reindex(columns=col_order)
    rows = [
        [_clean_for_json(v) for v in row]
        for row in chunk_ordered.itertuples(index=False, name=None)
    ]

    grand_totals = None
    spec = next((s for s in format_spec.OUTPUT_SHEETS if s.name == sheet), None)
    if spec and getattr(spec, "grand_total_row", False):
        gt: Dict[str, Any] = {}
        for c in format_spec.effective_columns(spec):
            if c.default_rule.get("type") == "pivot_agg":
                try:
                    gt[c.col] = float(
                        pd.to_numeric(df[c.col], errors="coerce").fillna(0).sum()
                    )
                except Exception:
                    gt[c.col] = None
        grand_totals = gt

    return {"sheet": sheet, "kind": "grid", "total": total, "offset": offset,
            "limit": limit, "columns": cols, "rows": rows, "grand_totals": grand_totals}


# ── Sheet columns ─────────────────────────────────────────────────────────────

@router.get("/sheet-columns/{sheet}", dependencies=[_GUARD])
async def sheet_columns_list(sheet: str,
                              session: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    return await sbi_db.sheet_columns_for(session, sheet)


@router.post("/sheet-columns/{sheet}", dependencies=[_GUARD])
async def sheet_columns_add(
    sheet: str, body: SheetColumnAdd,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    try:
        col = await sbi_db.add_sheet_column(session, sheet, header=body.header,
                                             col_letter=body.col_letter,
                                             number_format=body.number_format,
                                             notes=body.notes)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    await sbi_db.update_rule(session, sheet, col["col_letter"], body.rule_type, body.config)
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger=f"rule:{sheet}")
    return {"ok": True, "column": col, "rerun_job_id": job_id}


@router.patch("/sheet-columns/{sheet}/{col}", dependencies=[_GUARD])
async def sheet_columns_update(
    sheet: str, col: str, body: SheetColumnPatch,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    updated = await sbi_db.update_sheet_column(session, sheet, col,
                                                header=body.header,
                                                number_format=body.number_format)
    if not updated:
        raise HTTPException(status_code=404, detail="column not found")
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger=f"rule:{sheet}")
    return {"ok": True, "column": updated, "rerun_job_id": job_id}


@router.delete("/sheet-columns/{sheet}/{col}", dependencies=[_GUARD])
async def sheet_columns_delete(
    sheet: str, col: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    ok = await sbi_db.delete_sheet_column(session, sheet, col)
    if not ok:
        raise HTTPException(status_code=404, detail="column not found")
    job_id = await sbi_state.schedule_rerun(background_tasks, trigger=f"rule:{sheet}")
    return {"ok": True, "sheet": sheet, "col": col, "rerun_job_id": job_id}


# ── Column formats ────────────────────────────────────────────────────────────

@router.get("/column-formats", dependencies=[_GUARD])
async def column_formats(session: AsyncSession = Depends(get_db)) -> Dict[str, Dict[str, str]]:
    return await sbi_db.get_column_formats(session)


@router.put("/column-formats/{sheet}/{col}", dependencies=[_GUARD])
async def update_column_format(sheet: str, col: str, body: ColumnFormatUpdate,
                                session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    await sbi_db.set_column_format(session, sheet, col, body.number_format)
    sbi_state.invalidate_output_cache()
    return {"ok": True, "sheet": sheet, "column_letter": col,
            "number_format": body.number_format}


# ── Download ──────────────────────────────────────────────────────────────────

@router.get("/download", dependencies=[_GUARD])
async def download(session: AsyncSession = Depends(get_db)) -> Response:
    """Serve the xlsx from disk.

    Disk is shared across all uvicorn/gunicorn workers so any worker can serve
    the file regardless of which worker ran the pipeline.  The file is pre-generated
    by _pregenerate_xlsx_background after every pipeline run.

    Fallback: if the file is missing (e.g. first run before pre-gen completes) and
    this worker has in-memory results, generates it on demand.  If neither is
    available, returns 503 so the client retries.
    """
    current_run = await sbi_db.get_current_run(session)
    if current_run is None:
        raise HTTPException(status_code=400, detail="No pipeline results — upload a file first")

    month = current_run.get("month") or "unknown"
    output_dir = sbi_db.output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"sbi_mis_{month}.xlsx"

    if not out_path.exists():
        # Pre-generation hasn't finished yet — try to generate now if this worker
        # has the in-memory results, otherwise ask the client to retry.
        if sbi_state._cache.results is None:
            raise HTTPException(
                status_code=503,
                detail="Output file not ready yet — pipeline may still be running. Try again in a moment.",
            )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(cpu_executor(), lambda: _export.write_xlsx(
            sbi_state._cache.results, out_path, month_label=month
        ))

    return FileResponse(
        path=str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"SBI_MIS_{month}.xlsx",
    )


# ── Recon ─────────────────────────────────────────────────────────────────────

@router.post("/recon/run", dependencies=[_GUARD])
async def recon_run(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Start an async reconciliation job.

    Returns a job_id immediately.  The job re-reads the raw xlsx from disk
    (fresh, independent of in-memory state) and stores the result in the job
    record.  Frontend polls via GET /jobs/{job_id} and reads result_json.
    """
    if sbi_state._cache.dump_df is None or not sbi_state._cache.active_month:
        raise HTTPException(status_code=400, detail="No raw input loaded yet")

    active = sbi_state._cache.active_month
    rf = await sbi_db.get_run_file(session, active, "raw")
    raw_path = sbi_db.resolve_stored_path(rf["file_path"]) if rf else None
    if raw_path is None or not raw_path.exists():
        raise HTTPException(status_code=404, detail=f"Raw file for {active} not on disk")

    job_id = await sbi_jobs.create_job("recon")
    background_tasks.add_task(_run_recon_job, job_id, raw_path, active)
    return {"job_id": job_id, "status": "pending"}


async def _run_recon_job(job_id: str, raw_path: Path, active_month: str) -> None:
    """Background task: compare raw-file metrics against the pipeline's in-memory DataFrame.

    Fast path: if the raw metrics were pre-computed at upload time via a direct
    file read (raw_metrics_from_direct_read=True on the run row), use the DB values
    directly — no disk I/O needed.  Falls back to a live disk read for older runs
    where the flag is not set.
    """
    loop = asyncio.get_event_loop()
    try:
        # ── Pipeline metrics: from in-memory dump_df (always fast) ───────────
        df = sbi_state._cache.dump_df
        if df is None:
            raise ValueError("Pipeline cache cleared during recon — re-activate first")
        from openpyxl.utils import get_column_letter as _gcl
        _headers = list(format_spec.DUMP_RAW_HEADERS)
        _gmv_col = _gcl(_headers.index("gmv_mrp") + 1)
        _oid_col = _gcl(_headers.index("order_id") + 1)
        pipe_gmv  = float(pd.to_numeric(df.get(_gmv_col, pd.Series(dtype=float)),
                                        errors="coerce").fillna(0).sum())
        pipe_oids = int(df.get(_oid_col, pd.Series(dtype=object)).dropna().nunique())

        # ── Raw metrics: use pre-computed DB values if available ─────────────
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_session:
            run = await sbi_db.get_run(bg_session, active_month)

        if run and run.get("raw_metrics_from_direct_read"):
            await sbi_jobs.update_job(job_id, status="running",
                                      started_at=datetime.now(timezone.utc),
                                      stage="using pre-computed metrics")
            raw_rows = run["row_count"] or 0
            raw_gmv  = run["raw_gmv_mrp"] or 0.0
            raw_oids = run["raw_unique_order_ids"] or 0
        else:
            await sbi_jobs.update_job(job_id, status="running",
                                      started_at=datetime.now(timezone.utc),
                                      stage="reading raw file")
            raw_rows, raw_gmv, raw_oids = await loop.run_in_executor(
                cpu_executor(), lambda: _read_raw_recon_metrics(raw_path)
            )
            # Store for future recon calls on this run.
            async with AsyncSessionLocal() as bg_session:
                await sbi_db.update_run_recon_metrics(
                    bg_session, active_month, raw_gmv, raw_oids, from_direct_read=True
                )

        result = {
            "raw":      {"rows": raw_rows, "gmv_mrp": raw_gmv, "unique_order_ids": raw_oids},
            "pipeline": {"rows": len(df),  "gmv_mrp": pipe_gmv, "unique_order_ids": pipe_oids},
            "match": {
                "rows":      raw_rows == len(df),
                "gmv_mrp":   abs(raw_gmv - pipe_gmv) < 0.01,
                "order_ids": raw_oids == pipe_oids,
            },
        }
        await sbi_jobs.update_job(job_id, status="done", progress=1.0, stage="done",
                                  result_json=result,
                                  completed_at=datetime.now(timezone.utc))
    except Exception as e:
        await sbi_jobs.update_job(job_id, status="failed", error_detail=str(e),
                                  completed_at=datetime.now(timezone.utc))


# ── Clients ───────────────────────────────────────────────────────────────────

@router.get("/clients", dependencies=[_GUARD])
async def clients(session: AsyncSession = Depends(get_db)) -> List[Dict[str, Any]]:
    return await sbi_db.list_clients(session)


# ── NL → formula ──────────────────────────────────────────────────────────────

@router.post("/nl-to-formula", dependencies=[_GUARD])
async def nl_to_formula(body: NLToFormulaBody) -> Dict[str, Any]:
    import httpx
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse(status_code=400, content={"error": "OPENAI_API_KEY not set"})
    dump_cols_ctx = ", ".join(
        f"{chr(65+i) if i < 26 else 'A'+chr(65+i-26)} {h}"
        for i, h in enumerate(format_spec.DUMP_RAW_HEADERS)
    )
    enrich = ", ".join(f"{c['col']} {c['header']}" for c in format_spec.DUMP_ENRICHMENT_COLS)
    system = (
        "Translate plain-English logic into a single Excel-compatible formula. "
        "Output ONLY the formula starting with '='. Supported: IF, AND, OR, SUM, SUMIF, "
        "SUMIFS, MAX, MIN, ABS, ROUND. Column refs: bare letters (AH, Z). "
        "Cross-sheet: SheetName.Col. String literals: double quotes."
    )
    user = (
        f"Target: {body.sheet} col {body.column}"
        + (f" ({body.header})" if body.header else "")
        + f"\nDump cols: {dump_cols_ctx}\nEnrich cols: {enrich}\nRequest: {body.text.strip()}"
    )
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                  "temperature": 0.0},
            timeout=30.0,
        )
    if r.status_code != 200:
        return JSONResponse(status_code=502, content={"error": f"OpenAI error {r.status_code}"})
    out = r.json()["choices"][0]["message"]["content"].strip()
    out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
    if not out.startswith("="):
        out = "=" + out
    return {"formula": out}
