"""FastAPI app — backend for SBI MIS.

Serves:
  - /api/...   JSON endpoints
  - /         static frontend (HTML + JS + CSS from frontend/)
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import secrets
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from . import db, format_spec, jobs as _jobs
from .engine import export as _export
from .engine import ingest as _ingest
from .engine import pipeline as _pipeline
from .engine import pf_ingest as _pf_ingest


APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

# When false (default), 500 JSON responses omit stack traces — safer for production clients.
_EXPOSE_ERROR_TRACE = os.environ.get("SBI_EXPOSE_ERROR_TRACE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
FRONTEND_DIR = APP_DIR / "frontend"

# Browser-visible URL prefix — must match Next.js `/sbi` rewrites in agentos-frontend/next.config.ts.
# Use "" only when opening uvicorn directly at http://127.0.0.1:8765/ without Next (root-relative URLs).
PUBLIC_URL_PREFIX = "/sbi"


app = FastAPI(title="SBI MIS", version="0.1.0")


# Catch-all exception handler — guarantees the API never leaks an HTML stack-trace
# page through the proxy (which is what causes "Unexpected token '<'" in the frontend).
from fastapi.exceptions import RequestValidationError as _RVE
from starlette.exceptions import HTTPException as _StarletteHTTPException

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback as _tb

    body: Dict[str, Any] = {
        "error": f"{type(exc).__name__}: {exc}",
        "path": request.url.path,
    }
    if _EXPOSE_ERROR_TRACE:
        body["trace"] = _tb.format_exc()[:2000]
    return JSONResponse(status_code=500, content=body)

@app.exception_handler(_StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: _StarletteHTTPException):
    detail = exc.detail
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": detail if isinstance(detail, str) else (detail or "HTTP error")},
    )

@app.exception_handler(_RVE)
async def _validation_exception_handler(request: Request, exc: _RVE):
    return JSONResponse(status_code=422, content={"error": "validation_error", "details": exc.errors()})


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """Always-OK health check for ALB / nginx / k8s probes. Never depends on cache or DB."""
    return {"ok": True, "ts": int(time.time())}


# -------------- Auth (local-only cosmetic) -------------- #
# Credentials: sbi-mis/.env → SBI_AUTH_USERNAME, SBI_AUTH_PASSWORD (see .env.example).
# Session cookie is regenerated on every server start.

AUTH_USERNAME = os.environ.get("SBI_AUTH_USERNAME", "").strip()
AUTH_PASSWORD = os.environ.get("SBI_AUTH_PASSWORD", "").strip()
if not AUTH_USERNAME or not AUTH_PASSWORD:
    raise RuntimeError(
        "Set SBI_AUTH_USERNAME and SBI_AUTH_PASSWORD in sbi-mis/.env (copy from .env.example)."
    )
SESSION_COOKIE = "sbi_session"
SESSION_TOKEN = secrets.token_urlsafe(16)


def _is_authed(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) == SESSION_TOKEN


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # /, /static/*, and a few auth-public endpoints are always allowed
    public_api = {"/api/login", "/api/logout", "/api/me", "/api/health"}
    if path.startswith("/api/") and path not in public_api:
        if not _is_authed(request):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)


class LoginBody(BaseModel):
    username: str
    password: str


def _session_cookie_flags(request: Request) -> Dict[str, Any]:
    """Consistent Set-Cookie / delete_cookie flags for Chrome, Firefox, and reverse proxies."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()
    is_https = proto == "https"
    # path="/" so the session applies to the whole site (e.g. /sbi and /sbi/api/...).
    return {
        "path": "/",
        "secure": is_https,
        "samesite": "none" if is_https else "lax",
    }


@app.post("/api/login")
def login(request: Request, body: LoginBody, response: Response) -> Dict[str, Any]:
    if body.username == AUTH_USERNAME and body.password == AUTH_PASSWORD:
        flags = _session_cookie_flags(request)
        response.set_cookie(
            SESSION_COOKIE,
            SESSION_TOKEN,
            httponly=True,
            max_age=60 * 60 * 24 * 7,
            **flags,
        )
        return {"ok": True}
    return JSONResponse(status_code=401, content={"error": "Invalid username or password"})


@app.post("/api/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    flags = _session_cookie_flags(request)
    response.delete_cookie(
        SESSION_COOKIE,
        path=flags["path"],
        secure=flags["secure"],
        httponly=True,
        samesite=flags["samesite"],
    )
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> Dict[str, Any]:
    return {"authenticated": _is_authed(request), "username": AUTH_USERNAME if _is_authed(request) else None}


@app.get("/api/clients")
def clients() -> List[Dict[str, Any]]:
    return db.list_clients()


# In-memory cache for the active month's pipeline result.
# Switching months reloads the dump from disk and reruns the pipeline.
class Cache:
    active_month: Optional[str] = None
    dump_df: Optional[pd.DataFrame] = None
    results: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    computing: bool = False
    computed_at: float = 0.0
    # Output xlsx cache: regenerated only when the pipeline result changes (computed_at moves).
    # Subsequent download clicks stream the file from disk in <100 ms instead of regenerating.
    output_path: Optional[Path] = None
    output_version: float = 0.0
    cache_revision: int = 0


_cache = Cache()

# Single-flight locks. Prevent two background workers from running the same heavy
# operation simultaneously (which would otherwise fight over _cache state, the output
# file, and waste CPU/RAM).
import threading as _threading
_pipeline_lock = _threading.Lock()
_export_lock = _threading.Lock()
# When a rerun is requested while another is in-flight, set this flag so we run one
# more time after the current one finishes (collapses bursts of edits into 2 reruns max).
_pipeline_pending = False
_pipeline_pending_lock = _threading.Lock()


def _invalidate_output_cache() -> None:
    """Mark the generated xlsx cache stale."""
    _cache.output_path = None
    _cache.output_version = 0.0
    _cache.cache_revision += 1


def _mark_pipeline_dirty() -> None:
    """Immediately reflect that cached pipeline/output data is no longer current."""
    _invalidate_output_cache()
    if _cache.dump_df is not None:
        _cache.computing = True


@app.on_event("startup")
def _startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    # Rehydrate the most recent run (if any) into the active-month cache.
    runs = db.list_runs()
    if runs:
        latest = runs[0]  # runs are ordered by month DESC
        _activate_month(latest["month"])


def _activate_month(month: str) -> None:
    """Load the given month's raw file from disk and rerun pipeline. Sets active_month on success."""
    run = db.get_run(month)
    if not run:
        _cache.last_error = f"No upload found for month {month}"
        return
    p = db.resolve_stored_path(run["raw_file_path"])
    if p is None or not p.exists():
        _cache.last_error = f"Raw file missing on disk: {p}"
        return
    try:
        _cache.dump_df = _ingest.load_raw(p)
        _cache.active_month = month
        _rerun_pipeline()
    except Exception as e:
        _cache.last_error = f"Activate month {month} failed: {e}"


def _rerun_pipeline() -> None:
    """Synchronous rerun. Holds the pipeline lock so concurrent calls serialize."""
    if _cache.dump_df is None:
        return
    with _pipeline_lock:
        _do_one_rerun(None)
        _drain_pending_reruns(None)


def _rerun_pipeline_async() -> str:
    """Schedule a background pipeline rerun. Returns a job_id.

    Single-flight + pending-coalescing: if a rerun is already in progress, set a
    'pending' flag so we run exactly once more after it finishes. This collapses a
    burst of rule edits into at most 2 reruns rather than N.
    """
    job = _jobs.new_job("pipeline_rerun")

    def _run(j):
        acquired_immediately = _pipeline_lock.acquire(blocking=False)
        if not acquired_immediately:
            with _pipeline_pending_lock:
                global _pipeline_pending
                _pipeline_pending = True
            j.stage = "queued (rerun in progress)"; j.progress = 0.1
            _pipeline_lock.acquire()
        try:
            if acquired_immediately:
                j.stage = "running pipeline"; j.progress = 0.5
                _do_one_rerun(j)
            _drain_pending_reruns(j)
            j.stage = "done"; j.progress = 1.0
            return {"computed_at": _cache.computed_at}
        finally:
            _pipeline_lock.release()

    _jobs.run(job, _run, timeout_seconds=3600)
    return job.id


def _do_one_rerun(j) -> None:
    """Run the pipeline once. Caller must hold _pipeline_lock."""
    if _cache.dump_df is None: return
    _cache.computing = True
    _invalidate_output_cache()
    try:
        _cache.results = _pipeline.run(_cache.dump_df, active_month=_cache.active_month)
        _cache.last_error = None
        _cache.computed_at = time.time()
        _invalidate_output_cache()
    except Exception as e:
        _cache.last_error = f"{e}\n{traceback.format_exc()}"
        _cache.results = None
    finally:
        _cache.computing = False


def _drain_pending_reruns(j) -> None:
    """Run one collapsed rerun for any mutation that arrived while the lock was held."""
    global _pipeline_pending
    while True:
        with _pipeline_pending_lock:
            if not _pipeline_pending:
                break
            _pipeline_pending = False
        if j is not None:
            j.stage = "running pipeline (collapsed pending)"; j.progress = 0.7
        _do_one_rerun(j)


# ----- Chunked upload (each HTTP request stays under short proxy / ALB timeouts) ----- #
_upload_sessions: Dict[str, Dict[str, Any]] = {}
_upload_sessions_lock = _threading.Lock()
_UPLOAD_SESSION_TTL_SEC = int(os.environ.get("SBI_UPLOAD_SESSION_TTL_SEC", "7200"))
_MAX_UPLOAD_BYTES = 120 * 1024 * 1024


def _upload_chunk_size_bytes() -> int:
    """Default 2 MiB/chunk fits ~60s gateways on ~270 kbps uplink; override via SBI_UPLOAD_CHUNK_BYTES."""
    raw = os.environ.get("SBI_UPLOAD_CHUNK_BYTES", "").strip()
    if raw:
        try:
            n = int(raw)
            if 262_144 <= n <= 32 * 1024 * 1024:
                return n
        except ValueError:
            pass
    return 2 * 1024 * 1024


def _upload_session_sweep() -> None:
    now = time.time()
    with _upload_sessions_lock:
        for uid in list(_upload_sessions.keys()):
            s = _upload_sessions[uid]
            if now - float(s.get("created", 0)) > _UPLOAD_SESSION_TTL_SEC:
                try:
                    Path(s["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
                del _upload_sessions[uid]


def _expected_upload_chunk_len(sess: Dict[str, Any], chunk_index: int) -> int:
    fs = int(sess["file_size"])
    cs = int(sess["chunk_size"])
    n = int(sess["chunk_count"])
    if chunk_index < 0 or chunk_index >= n:
        return -1
    if chunk_index == n - 1:
        return fs - (n - 1) * cs
    return cs


def _start_upload_job(
    dest: Path,
    month: str,
    kind: str,
    original_filename: str,
    ext: str,
    raw_sheet_hint: Optional[str],
) -> Dict[str, Any]:
    """Run parse + DB + pipeline in a background job; return the usual /api/upload JSON."""
    job = _jobs.new_job(f"upload:{kind}")

    def _do_upload(j):
        if kind == "raw":
            file_kind = "csv" if ext in (".csv", ".tsv") else "xlsx"
            j.stage = f"parsing {file_kind}"
            j.progress = 0.1
            df = _ingest.load_raw(dest, sheet_hint=raw_sheet_hint)
            rc = len(df)
            j.stage = "registering upload"
            j.progress = 0.3
            db.set_current_run(str(dest), month, rc)
            db.upsert_run_file(month, "raw", str(dest),
                               original_filename=original_filename, row_count=rc)
            _cache.dump_df = df
            _cache.active_month = month
            j.stage = "running pipeline"
            j.progress = 0.5
            _rerun_pipeline()
            j.stage = "done"
            j.progress = 1.0
            return {
                "kind": "raw", "month": month, "active_month": month,
                "row_count": rc, "filename": original_filename,
                "anomalies": _ingest.anomalies(df),
            }

        if kind == "pf_summary":
            try:
                year = int(month.split("-")[0])
            except Exception:
                year = 2026
            j.stage = "parsing PF summary"
            j.progress = 0.2
            records, info = _pf_ingest.parse_pf_summary(dest, default_year=year)
            j.stage = "writing historicals"
            j.progress = 0.6
            if records:
                db.bulk_upsert_pf_historicals(records)
            db.upsert_run_file(month, "pf_summary", str(dest),
                               original_filename=original_filename,
                               row_count=info.get("row_count"))
            j.stage = "rerunning pipeline"
            j.progress = 0.8
            _rerun_pipeline()
            return {
                "kind": "pf_summary", "month": month,
                "records_imported": len(records),
                "months_detected": info.get("months_detected", []),
                "row_count": info.get("row_count"),
                "filename": original_filename,
            }

        j.stage = "saving AHC"
        j.progress = 0.4
        db.upsert_run_file(month, "ahc", str(dest), original_filename=original_filename)
        j.stage = "rerunning pipeline"
        j.progress = 0.7
        _rerun_pipeline()
        return {"kind": "ahc", "month": month, "filename": original_filename}

    _jobs.run(job, _do_upload, timeout_seconds=3600)
    return {
        "job_id": job.id,
        "status": job.status,
        "kind": kind,
        "month": month,
        "filename": original_filename,
    }


class UploadSessionCreate(BaseModel):
    month: str
    kind: str = "raw"
    filename: str = "upload"
    file_size: int = Field(..., ge=1, le=_MAX_UPLOAD_BYTES)


class UploadSessionCompleteBody(BaseModel):
    upload_id: str


# ================= API ================= #


@app.get("/api/schema")
def schema() -> Dict[str, Any]:
    return format_spec.schema_json()


@app.get("/api/status")
def status() -> Dict[str, Any]:
    active_run = db.get_run(_cache.active_month) if _cache.active_month else None
    return {
        "has_upload": _cache.dump_df is not None,
        "computing": _cache.computing,
        "error": _cache.last_error,
        "computed_at": _cache.computed_at,
        "active_month": _cache.active_month,
        "active_run": active_run,
        "available_months": [r["month"] for r in db.list_runs()],
    }


@app.get("/api/runs")
def runs_list() -> List[Dict[str, Any]]:
    return db.list_runs()


@app.post("/api/runs/{month}/activate")
def activate_run(month: str) -> Dict[str, Any]:
    _activate_month(month)
    return {
        "active_month": _cache.active_month,
        "error": _cache.last_error,
        "row_count": len(_cache.dump_df) if _cache.dump_df is not None else 0,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...),
                 month: str = Form(...),
                 kind: str = Form("raw")) -> Dict[str, Any]:
    """Upload a file for the given month. Returns a job_id immediately; client polls
    /api/jobs/{id} for parse + pipeline progress.

    kind ∈ {raw, pf_summary, ahc}
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    kind = (kind or "raw").lower().strip()
    if kind not in ("raw", "pf_summary", "ahc"):
        return JSONResponse(status_code=400, content={"error": f"Unknown kind '{kind}'"})

    month = (month or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid month: expected YYYY-MM (e.g. 2026-04)."},
        )

    original_filename = (file.filename or "upload").strip() or "upload"
    ext = Path(original_filename).suffix.lower() or ".xlsx"
    allowed = {".xlsx", ".xlsm", ".csv", ".tsv"} if kind != "pf_summary" else {".xlsx", ".xlsm"}
    if ext not in allowed:
        return JSONResponse(status_code=400, content={
            "error": f"Unsupported file type '{ext}' for kind '{kind}'. Allowed: {sorted(allowed)}"
        })
    safe_name = f"{kind}_{month}_{int(time.time())}{ext}"
    dest = UPLOAD_DIR / safe_name
    CHUNK = 1024 * 1024
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
    except OSError as e:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=500, content={"error": f"Could not save upload: {e}"})

    if total == 0:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=400, content={"error": "Empty file"})

    raw_source: Optional[str] = None
    if kind == "raw":
        try:
            _, _, raw_source = _ingest.validate_headers(dest)
        except _ingest.SchemaError as e:
            try:
                dest.unlink()
            except OSError:
                pass
            return JSONResponse(status_code=400, content={"error": str(e)})

    raw_sheet_hint: Optional[str] = None
    if kind == "raw" and raw_source and raw_source != "csv":
        raw_sheet_hint = raw_source

    return _start_upload_job(dest, month, kind, original_filename, ext, raw_sheet_hint)


@app.post("/api/upload/session")
def upload_session_create(body: UploadSessionCreate) -> Dict[str, Any]:
    """Start a chunked upload: many small PUTs avoid a single long POST hitting proxy timeouts."""
    _upload_session_sweep()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    kind = (body.kind or "raw").lower().strip()
    if kind not in ("raw", "pf_summary", "ahc"):
        return JSONResponse(status_code=400, content={"error": f"Unknown kind '{kind}'"})

    month = (body.month or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid month: expected YYYY-MM (e.g. 2026-04)."},
        )

    sz = body.file_size

    original_filename = (body.filename or "upload").strip() or "upload"
    ext = Path(original_filename).suffix.lower() or ".xlsx"
    allowed = {".xlsx", ".xlsm", ".csv", ".tsv"} if kind != "pf_summary" else {".xlsx", ".xlsm"}
    if ext not in allowed:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unsupported file type '{ext}' for kind '{kind}'. Allowed: {sorted(allowed)}"},
        )

    chunk_size = _upload_chunk_size_bytes()
    chunk_count = max(1, math.ceil(sz / chunk_size))
    upload_id = secrets.token_urlsafe(16)
    part_path = UPLOAD_DIR / f".part_{upload_id}{ext}"
    try:
        part_path.write_bytes(b"")
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Could not create upload session: {e}"})

    with _upload_sessions_lock:
        _upload_sessions[upload_id] = {
            "path": part_path,
            "next_chunk": 0,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
            "file_size": sz,
            "received_bytes": 0,
            "month": month,
            "kind": kind,
            "original_filename": original_filename,
            "ext": ext,
            "created": time.time(),
        }

    return {
        "upload_id": upload_id,
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "file_size": sz,
    }


@app.put("/api/upload/chunk/{upload_id}/{chunk_index:int}")
async def upload_session_chunk(upload_id: str, chunk_index: int, request: Request) -> Dict[str, Any]:
    _upload_session_sweep()
    body = await request.body()
    with _upload_sessions_lock:
        sess = _upload_sessions.get(upload_id)
        if not sess:
            return JSONResponse(status_code=404, content={"error": "Unknown or expired upload session"})
        if time.time() - float(sess["created"]) > _UPLOAD_SESSION_TTL_SEC:
            try:
                Path(sess["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            del _upload_sessions[upload_id]
            return JSONResponse(
                status_code=404,
                content={"error": "Upload session expired — start again."},
            )
        if chunk_index != sess["next_chunk"]:
            return JSONResponse(
                status_code=400,
                content={
                    "error": f"Expected chunk index {sess['next_chunk']}, got {chunk_index}. Send chunks in order.",
                },
            )
        exp_len = _expected_upload_chunk_len(sess, chunk_index)
        if exp_len < 0:
            return JSONResponse(status_code=400, content={"error": "Invalid chunk index."})
        if len(body) != exp_len:
            return JSONResponse(
                status_code=400,
                content={"error": f"Chunk {chunk_index}: expected {exp_len} bytes, got {len(body)}."},
            )
        path = Path(sess["path"])
        try:
            with open(path, "ab") as out:
                out.write(body)
        except OSError as e:
            return JSONResponse(status_code=500, content={"error": f"Could not write chunk: {e}"})
        sess["next_chunk"] = int(sess["next_chunk"]) + 1
        sess["received_bytes"] = int(sess["received_bytes"]) + len(body)
        next_c = int(sess["next_chunk"])

    return {"ok": True, "next_chunk": next_c}


@app.post("/api/upload/complete")
def upload_session_complete(body: UploadSessionCompleteBody) -> Dict[str, Any]:
    _upload_session_sweep()
    upload_id = (body.upload_id or "").strip()
    with _upload_sessions_lock:
        sess = _upload_sessions.pop(upload_id, None)
    if not sess:
        return JSONResponse(status_code=404, content={"error": "Unknown or expired upload session"})

    part_path = Path(sess["path"])
    if int(sess["next_chunk"]) != int(sess["chunk_count"]):
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=400, content={"error": "Incomplete upload — not all chunks received."})
    if int(sess["received_bytes"]) != int(sess["file_size"]):
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=400, content={"error": "Upload size mismatch — start again."})

    try:
        fs = part_path.stat().st_size
    except OSError:
        return JSONResponse(status_code=500, content={"error": "Temp upload file missing."})
    if fs != int(sess["file_size"]):
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        return JSONResponse(status_code=400, content={"error": "Assembled file size does not match declared size."})

    month = str(sess["month"])
    kind = str(sess["kind"])
    original_filename = str(sess["original_filename"])
    ext = str(sess["ext"])
    # Avoid same-second collisions; session already popped so failures must not leak .part files.
    dest = UPLOAD_DIR / f"{kind}_{month}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    try:
        part_path.rename(dest)
    except OSError:
        try:
            shutil.move(str(part_path), str(dest))
        except OSError as e:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            return JSONResponse(status_code=500, content={"error": f"Could not finalize upload: {e}"})

    raw_sheet_hint: Optional[str] = None
    if kind == "raw":
        try:
            _, _, raw_source = _ingest.validate_headers(dest)
        except _ingest.SchemaError as e:
            try:
                dest.unlink()
            except OSError:
                pass
            return JSONResponse(status_code=400, content={"error": str(e)})
        if raw_source and raw_source != "csv":
            raw_sheet_hint = raw_source

    return _start_upload_job(dest, month, kind, original_filename, ext, raw_sheet_hint)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j.to_dict()


@app.get("/api/run-files")
def get_run_files(month: Optional[str] = None) -> List[Dict[str, Any]]:
    if month:
        return [{"month": month, "kind": k, **v} for k, v in db.run_files_for_month(month).items()]
    return db.all_run_files()


@app.get("/api/historicals")
def historicals(month: Optional[str] = None) -> Dict[str, Any]:
    rows = db.pf_historicals_list(month_filter=month)
    return {
        "months": db.pf_historicals_months(),
        "rows": rows,
        "count": len(rows),
    }


class HistoricalsDelete(BaseModel):
    month: str


@app.delete("/api/historicals/{month}")
def historicals_delete(month: str) -> Dict[str, Any]:
    n = db.pf_historicals_delete_month(month)
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"ok": True, "month": month, "deleted": n, "rerun_job_id": job_id}


@app.get("/api/rules")
def get_rules() -> List[Dict[str, Any]]:
    return db.get_all_rules()


@app.get("/api/rules/{sheet}/{col}")
def get_rule(sheet: str, col: str) -> Dict[str, Any]:
    r = db.get_rule(sheet, col)
    if r is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return r


class RuleUpdate(BaseModel):
    rule_type: str
    config: Dict[str, Any] = {}
    status: Optional[str] = None


@app.put("/api/rules/{sheet}/{col}")
def update_rule(sheet: str, col: str, body: RuleUpdate) -> Dict[str, Any]:
    updated = db.update_rule(sheet, col, body.rule_type, body.config, body.status)
    # Pipeline rerun runs as a background job — request returns in <100ms
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"rule": updated, "rerun_job_id": job_id, "error": _cache.last_error}


@app.get("/api/lookup-tables")
def lookup_tables() -> Dict[str, Any]:
    return {name: db.get_lookup(name) for name in db.list_lookups()}


class LookupUpdate(BaseModel):
    data: List[Dict[str, Any]]


@app.put("/api/lookup-tables/{name}")
def set_lookup(name: str, body: LookupUpdate) -> Dict[str, Any]:
    db.set_lookup(name, body.data)
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"ok": True, "rerun_job_id": job_id}


def _clean_for_json(v: Any) -> Any:
    import datetime
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (int, bool, str)):
        return v
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    try:
        import pandas as _pd
        import numpy as _np
        if isinstance(v, (_pd.Timestamp,)):
            return v.isoformat()
        if isinstance(v, _np.generic):
            x = v.item()
            return _clean_for_json(x)
    except Exception:
        pass
    return str(v)


@app.get("/api/preview/{sheet:path}")
def preview(sheet: str, offset: int = 0, limit: int = 200) -> Dict[str, Any]:
    if _cache.results is None:
        return {"error": _cache.last_error or "no data yet", "rows": [], "total": 0, "columns": []}
    data = _cache.results["sheets"].get(sheet)
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown sheet '{sheet}'")

    # Summary is dict of cell -> value
    if sheet == "Summary":
        # Flatten to list of {cell, value}
        cells = [
            {"cell": sc.cell, "value": _clean_for_json(data.get(sc.cell)),
             "number_format": sc.number_format, "rule_type": sc.default_rule["type"]}
            for sc in format_spec.SUMMARY_CELLS
        ]
        return {"sheet": sheet, "kind": "summary", "cells": cells}

    df: pd.DataFrame = data
    total = len(df)
    chunk = df.iloc[offset: offset + limit]
    # Build column list
    format_overrides = db.get_column_formats().get(sheet, {})
    if sheet == "Dump":
        headers = list(format_spec.DUMP_RAW_HEADERS) + [c["header"] for c in format_spec.DUMP_ENRICHMENT_COLS]
        cols = []
        for i, h in enumerate(headers, start=1):
            from openpyxl.utils import get_column_letter as _gl
            letter = _gl(i)
            rule = db.get_rule("Dump", letter) if i > len(format_spec.DUMP_RAW_HEADERS) else None
            cols.append({
                "col": letter,
                "header": h,
                "is_derived": i > len(format_spec.DUMP_RAW_HEADERS),
                "rule_type": rule["rule_type"] if rule else ("raw" if i <= len(format_spec.DUMP_RAW_HEADERS) else None),
                "number_format": format_overrides.get(letter),
            })
    else:
        spec = next((s for s in format_spec.OUTPUT_SHEETS if s.name == sheet), None)
        if spec is None:
            cols = [{"col": c, "header": c, "is_derived": True, "rule_type": None,
                     "number_format": format_overrides.get(c)} for c in df.columns]
        else:
            cols = []
            # Use live columns from the sheet_columns table
            for c in format_spec.effective_columns(spec):
                rule = db.get_rule(sheet, c.col)
                nf = format_overrides.get(c.col, c.number_format)
                cols.append({
                    "col": c.col,
                    "header": c.header,
                    "is_derived": True,
                    "rule_type": rule["rule_type"] if rule else c.default_rule["type"],
                    "number_format": nf,
                })
    # Rows
    rows: List[List[Any]] = []
    col_order = [c["col"] for c in cols]
    for _, r in chunk.iterrows():
        rows.append([_clean_for_json(r.get(k)) for k in col_order])

    # Grand totals — compute for any column whose rule is a sum-style aggregation.
    grand_totals: Optional[Dict[str, Any]] = None
    spec = next((s for s in format_spec.OUTPUT_SHEETS if s.name == sheet), None)
    if spec and spec.grand_total_row:
        gt: Dict[str, Any] = {}
        for c in format_spec.effective_columns(spec):
            rtype = c.default_rule.get("type")
            cfg = c.default_rule.get("config", {})
            is_sum = rtype == "pivot_agg" and (cfg.get("agg") or "sum").lower() == "sum"
            is_currency_fmt = c.number_format and ("₹" in c.number_format or "#,##0" in c.number_format)
            if is_sum or is_currency_fmt:
                try:
                    gt[c.col] = float(pd.to_numeric(df[c.col], errors="coerce").fillna(0).sum())
                except Exception:
                    gt[c.col] = None
        grand_totals = gt

    return {
        "sheet": sheet,
        "kind": "grid",
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": cols,
        "rows": rows,
        "grand_totals": grand_totals,
    }


@app.get("/api/sheet-columns/{sheet}")
def sheet_columns_list(sheet: str) -> List[Dict[str, Any]]:
    return db.sheet_columns_for(sheet)


class SheetColumnAdd(BaseModel):
    header: str
    col_letter: Optional[str] = None
    number_format: Optional[str] = None
    rule_type: str = "blank"
    config: Dict[str, Any] = {}
    notes: Optional[str] = None


@app.post("/api/sheet-columns/{sheet}")
def sheet_columns_add(sheet: str, body: SheetColumnAdd) -> Dict[str, Any]:
    try:
        col = db.add_sheet_column(
            sheet, header=body.header, col_letter=body.col_letter,
            number_format=body.number_format, notes=body.notes,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    # Also seed a rule for this new column
    db.update_rule(sheet, col["col_letter"], body.rule_type, body.config)
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"ok": True, "column": col, "rerun_job_id": job_id}


class SheetColumnPatch(BaseModel):
    header: Optional[str] = None
    number_format: Optional[str] = None


@app.patch("/api/sheet-columns/{sheet}/{col}")
def sheet_columns_update(sheet: str, col: str, body: SheetColumnPatch) -> Dict[str, Any]:
    updated = db.update_sheet_column(sheet, col, header=body.header, number_format=body.number_format)
    if not updated:
        raise HTTPException(status_code=404, detail="column not found")
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"ok": True, "column": updated, "rerun_job_id": job_id}


@app.delete("/api/sheet-columns/{sheet}/{col}")
def sheet_columns_delete(sheet: str, col: str) -> Dict[str, Any]:
    ok = db.delete_sheet_column(sheet, col)
    if not ok:
        raise HTTPException(status_code=404, detail="column not found")
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {"ok": True, "sheet": sheet, "col": col, "rerun_job_id": job_id}


@app.get("/api/column-formats")
def column_formats() -> Dict[str, Dict[str, str]]:
    return db.get_column_formats()


class ColumnFormatUpdate(BaseModel):
    number_format: Optional[str] = None


@app.put("/api/column-formats/{sheet}/{col}")
def update_column_format(sheet: str, col: str, body: ColumnFormatUpdate) -> Dict[str, Any]:
    db.set_column_format(sheet, col, body.number_format)
    # Format changes only affect xlsx output, not pipeline values. Just invalidate the
    # output cache so next download regenerates with the new format.
    _invalidate_output_cache()
    return {"ok": True, "sheet": sheet, "column_letter": col, "number_format": body.number_format}


# ---- Natural-language → Excel formula (OpenAI) ---- #

class NLToFormulaBody(BaseModel):
    text: str
    sheet: str
    column: str
    header: Optional[str] = None


@app.post("/api/nl-to-formula")
def nl_to_formula(body: NLToFormulaBody) -> Dict[str, Any]:
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse(
            status_code=400,
            content={"error": "OPENAI_API_KEY not set on this machine. Add it to your shell (~/.zshrc) and restart the app."},
        )

    # Build schema context
    dump_cols_ctx = ", ".join(
        f"{chr(65 + i) if i < 26 else 'A' + chr(65 + i - 26)} {h}"
        for i, h in enumerate(format_spec.DUMP_RAW_HEADERS)
    )
    enrich = ", ".join(f"{c['col']} {c['header']}" for c in format_spec.DUMP_ENRICHMENT_COLS)

    system = (
        "You translate plain-English logic into a single Excel-compatible formula used by an "
        "internal billing platform. Output ONLY the formula starting with '='. No prose, no backticks, "
        "no explanation. Keep it on one line.\n\n"
        "Supported functions: IF, AND, OR, NOT, SUM, SUMIF, SUMIFS, MAX, MIN, ABS, ROUND, ROW.\n"
        "Comparisons: = != <> < > <= >=.\n"
        "Column refs: bare letters (AH, Z, AG) refer to the current row of the source sheet.\n"
        "Cross-sheet refs: SheetName.Col (e.g. Dump.AF, 'PF Summary'.K) for aggregate functions only.\n"
        "String literals use double quotes.\n"
        "Empty cells compare equal to \"\".\n"
    )

    user = (
        f"Target sheet: {body.sheet}\n"
        f"Target column: {body.column}"
        + (f" ({body.header})" if body.header else "") + "\n\n"
        f"Dump raw columns (A-AS):\n  {dump_cols_ctx}\n\n"
        f"Dump enrichment columns (AT-AX, already computed by other rules):\n  {enrich}\n\n"
        f"Request: {body.text.strip()}\n\n"
        "Return ONLY the formula."
    )

    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": 0.0,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            return JSONResponse(status_code=502, content={"error": f"OpenAI error: {r.status_code} {r.text[:400]}"})
        data = r.json()
        out = data["choices"][0]["message"]["content"].strip()
        # Strip code fences if the model added them
        if out.startswith("```"):
            out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
        if not out.startswith("="):
            out = "=" + out
        return {"formula": out, "model": "gpt-4o-mini"}
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Conversion failed: {e}"})


@app.get("/api/recon")
def recon() -> Dict[str, Any]:
    """Fresh recon: compute row count, GMV_MRP grand total, and distinct order_id count
    on BOTH the raw input file (read directly from disk via openpyxl) AND the transformed
    Dump sheet (in-memory pandas DataFrame produced by the pipeline).

    Both sides are computed at request-time — no caching of recon values, no pasting.
    Two independent code paths: any pipeline bug that drops rows or corrupts values
    surfaces as a mismatch.
    """
    import time as _time
    if _cache.dump_df is None or not _cache.active_month:
        return {"empty": True, "error": "No raw input loaded yet"}

    # Look up the raw file FOR THE ACTIVE MONTH (not the latest upload, which may belong
    # to a different month).
    active = _cache.active_month
    rf = db.get_run_file(active, "raw")
    if rf is None:
        # Fallback to runs table
        run_row = db.get_run(active)
        raw_path = db.resolve_stored_path(run_row["raw_file_path"]) if run_row and run_row.get("raw_file_path") else None
    else:
        raw_path = db.resolve_stored_path(rf["file_path"])
    if raw_path is None or not raw_path.exists():
        return {"empty": True, "error": f"Raw file for {active} not on disk"}

    started = _time.time()

    # --- Side A: raw input file, read fresh. Handles xlsx OR csv. --- #
    headers, source_label = _ingest._read_headers_only(raw_path)
    def _idx(col_name):
        try: return headers.index(col_name)
        except ValueError: return None
    z_idx = _idx("gmv_mrp")
    e_idx = _idx("order_id")

    usecols = [i for i in (e_idx, z_idx) if i is not None]
    raw_rows = 0
    raw_gmv = 0.0
    raw_oids_count = 0
    if usecols:
        if _ingest._is_csv(raw_path):
            # CSV: read only needed columns via usecols (column position indices)
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    raw_df = pd.read_csv(raw_path, encoding=enc, sep=None, engine="python",
                                          usecols=sorted(usecols), dtype=object)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raw_df = pd.read_csv(raw_path, usecols=sorted(usecols), dtype=object)
            sheet_name = "(csv)"
        else:
            sheet_name = source_label
            try:
                raw_df = pd.read_excel(
                    raw_path, sheet_name=sheet_name, header=0,
                    usecols=sorted(usecols), engine="calamine",
                )
            except Exception:
                raw_df = pd.read_excel(
                    raw_path, sheet_name=sheet_name, header=0,
                    usecols=sorted(usecols), engine="openpyxl", dtype=object,
                )
        # Drop fully-empty rows (shouldn't exist in well-formed dumps but defensive)
        raw_df = raw_df.dropna(how="all")
        raw_rows = len(raw_df)
        if z_idx is not None:
            z_col_name = headers[z_idx]
            if z_col_name in raw_df.columns:
                raw_gmv = float(pd.to_numeric(raw_df[z_col_name], errors="coerce").fillna(0).sum())
        if e_idx is not None:
            e_col_name = headers[e_idx]
            if e_col_name in raw_df.columns:
                raw_oids_count = int(raw_df[e_col_name].dropna().astype(str).nunique())

    raw_metrics = {
        "source": f"{raw_path.name} ({source_label})",
        "row_count": raw_rows,
        "gmv_mrp_total": raw_gmv,
        "unique_order_ids": raw_oids_count,
    }

    # --- Side B: transformed Dump (in-memory DataFrame) --- #
    # The pipeline's dump_df is what gets written to the Dump sheet of the downloaded xlsx,
    # plus enrichment cols AT–AX which don't affect these metrics.
    tdf = _cache.dump_df
    # Build the post-pipeline DataFrame (applies enrichment cols too) so we are testing
    # what actually gets written to the output xlsx.
    pipeline_dump = _cache.results["sheets"]["Dump"] if _cache.results else tdf
    n_rows = len(pipeline_dump)
    z_col = pipeline_dump["Z"] if "Z" in pipeline_dump.columns else None
    gmv_total = float(pd.to_numeric(z_col, errors="coerce").fillna(0).sum()) if z_col is not None else 0.0
    e_col = pipeline_dump["E"] if "E" in pipeline_dump.columns else None
    oid_count = int(e_col.dropna().astype(str).nunique()) if e_col is not None else 0

    transformed_metrics = {
        "source": "Dump sheet of transformed output (in-memory pipeline result)",
        "row_count": n_rows,
        "gmv_mrp_total": gmv_total,
        "unique_order_ids": oid_count,
    }

    # Diffs
    def _diff(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return round(a - b, 2)
        return "n/a"

    diffs = {
        "row_count":        _diff(transformed_metrics["row_count"], raw_metrics["row_count"]),
        "gmv_mrp_total":    _diff(transformed_metrics["gmv_mrp_total"], raw_metrics["gmv_mrp_total"]),
        "unique_order_ids": _diff(transformed_metrics["unique_order_ids"], raw_metrics["unique_order_ids"]),
    }
    matches = all(d == 0 for d in diffs.values() if isinstance(d, (int, float)))

    return {
        "empty": False,
        "month": _cache.active_month,
        "raw": raw_metrics,
        "transformed": transformed_metrics,
        "diffs": diffs,
        "matches": matches,
        "computed_at": _time.time(),
        "compute_seconds": round(_time.time() - started, 2),
    }


@app.get("/api/dashboard")
def dashboard() -> Dict[str, Any]:
    """Monthly billing snapshot for the live dashboard."""
    if _cache.results is None:
        return {"empty": True, "error": _cache.last_error}
    sheets = _cache.results["sheets"]
    dump = sheets.get("Dump")
    ol = sheets.get("Order level ")
    olnp = sheets.get("Order level - non permissible")
    pfs = sheets.get("PF Summary")
    summary_cells = sheets.get("Summary", {})

    def _num(series, col):
        if series is None or col not in series.columns:
            return 0.0
        return float(pd.to_numeric(series[col], errors="coerce").fillna(0).sum())

    # Next Step breakdown from Dump
    ns_breakdown: List[Dict[str, Any]] = []
    if dump is not None and "AX" in dump.columns:
        vc = dump["AX"].value_counts(dropna=False)
        for k, v in vc.items():
            label = "(blank)" if pd.isna(k) else str(k)
            ns_breakdown.append({"label": label, "count": int(v)})

    # Top overutilised PFs
    top_pfs: List[Dict[str, Any]] = []
    if pfs is not None and "K" in pfs.columns:
        overs = pd.to_numeric(pfs["K"], errors="coerce").fillna(0)
        idx = overs.sort_values(ascending=False).head(10).index
        for i in idx:
            row = pfs.loc[i]
            top_pfs.append({
                "pf": _clean_for_json(row.get("A")),
                "type": _clean_for_json(row.get("B")),
                "wallet": _clean_for_json(row.get("C")),
                "mar": _clean_for_json(row.get("H")),
                "overutilised": _clean_for_json(row.get("K")),
            })

    return {
        "empty": False,
        "month": _cache.active_month or (db.get_current_run() or {}).get("month"),
        "row_count": len(dump) if dump is not None else 0,
        "permissible": {
            "count": len(ol) if ol is not None else 0,
            "gmv": _num(ol, "L"),           # GMV_LIST (col L in Order level pivot)
            "gmv_basis": "GMV List Price",
            "user_paid": _num(ol, "N"),
            "copay": _num(ol, "M"),
        },
        "non_permissible": {
            "count": len(olnp) if olnp is not None else 0,
            "gmv": _num(olnp, "L"),
            "gmv_basis": "GMV List Price",
            "copay": _num(olnp, "M"),
        },
        "overutilised_total": _num(pfs, "K"),
        "final_to_be_billed": _clean_for_json(summary_cells.get("B10")) if isinstance(summary_cells, dict) else None,
        "grand_total_pharma": _clean_for_json(summary_cells.get("B7")) if isinstance(summary_cells, dict) else None,
        "annual_health_checkup": _clean_for_json(summary_cells.get("B9")) if isinstance(summary_cells, dict) else None,
        "next_step_breakdown": ns_breakdown,
        "top_overutilised_pfs": top_pfs,
        "updated_at": _cache.computed_at,
    }


@app.get("/api/rules/export")
def rules_export() -> Response:
    """Download all rules + lookups as JSON — for backup / transfer."""
    import datetime as _dt
    payload = {
        "rules": db.get_all_rules(),
        "lookup_tables": {name: db.get_lookup(name) for name in db.list_lookups()},
        "exported_at": _dt.datetime.utcnow().isoformat() + "Z",
    }
    data = json.dumps(payload, indent=2, default=str).encode("utf-8")
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=sbi_rules.json"},
    )


class RulesImport(BaseModel):
    rules: Optional[List[Dict[str, Any]]] = None
    lookup_tables: Optional[Dict[str, List[Dict[str, Any]]]] = None


@app.post("/api/rules/import")
def rules_import(body: RulesImport) -> Dict[str, Any]:
    """Replace rules and lookup tables with provided payload. Re-runs pipeline."""
    count = 0
    if body.rules:
        for r in body.rules:
            cfg = r.get("config_json", "{}")
            if isinstance(cfg, str):
                try: cfg_dict = json.loads(cfg)
                except: cfg_dict = {}
            else:
                cfg_dict = cfg
            db.update_rule(r["sheet"], r["column_letter"], r["rule_type"], cfg_dict, r.get("status"))
            count += 1
    if body.lookup_tables:
        for name, data in body.lookup_tables.items():
            db.set_lookup(name, data)
    _mark_pipeline_dirty()
    job_id = _rerun_pipeline_async()
    return {
        "ok": True,
        "rules_imported": count,
        "lookups_imported": len(body.lookup_tables or {}),
        "rerun_job_id": job_id,
    }


@app.get("/api/download")
def download() -> FileResponse:
    """Cache-only file fetch. Returns the previously-generated xlsx if it exists; otherwise
    409 Conflict — the client must POST /api/downloads first to kick off generation.

    This prevents accidental long-blocking GETs (e.g. user refreshes the URL or copies it
    into a tab) from sitting on the request for ~100s and timing out the proxy. All
    generation now flows through the background-job pattern."""
    if _cache.results is None:
        raise HTTPException(status_code=400, detail="nothing to download — upload a raw Dump first")
    if _cache.computing:
        raise HTTPException(status_code=409, detail="pipeline is recomputing — try download again when it finishes")
    month_label = _cache.active_month or (db.get_current_run() or {}).get("month", "output")
    out_path = OUTPUT_DIR / f"SBI_MIS_{month_label}.xlsx"
    if (_cache.output_path is not None
            and _cache.output_version == _cache.computed_at
            and _cache.output_path.exists()
            and _cache.output_path == out_path):
        return FileResponse(path=str(out_path), filename=out_path.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    raise HTTPException(
        status_code=409,
        detail="No cached output for the current pipeline result. POST /api/downloads first to generate.",
    )


@app.post("/api/downloads")
def start_download_job() -> Dict[str, Any]:
    """Kick off xlsx generation in the background. Returns job_id; client polls
    /api/jobs/{id} until status='done', then GET /api/download to actually fetch."""
    if _cache.results is None:
        raise HTTPException(status_code=400, detail="nothing to download — upload a raw Dump first")
    if _cache.computing:
        raise HTTPException(status_code=409, detail="pipeline is recomputing — try download again when it finishes")
    month_label = _cache.active_month or (db.get_current_run() or {}).get("month", "output")
    out_path = OUTPUT_DIR / f"SBI_MIS_{month_label}.xlsx"

    # Cache hit → no work needed; return a synthetic completed job.
    if (_cache.output_path is not None
            and _cache.output_version == _cache.computed_at
            and _cache.output_path.exists()
            and _cache.output_path == out_path):
        job = _jobs.new_job("download")
        job.status = "done"; job.progress = 1.0; job.stage = "cached"
        job.result = {"download_url": "/api/download", "filename": out_path.name, "cached": True}
        job.completed_at = job.created_at
        return {"job_id": job.id, "status": "done"}

    job = _jobs.new_job("download")

    def _do_download(j):
        def _on_progress(stage: str, frac: float) -> None:
            j.stage = stage; j.progress = frac
        # Single-flight: if another export is already running, wait. After acquiring
        # the lock, re-check the cache — the other run may have produced exactly what
        # we need, in which case we can skip generation entirely.
        j.stage = "queued (waiting for export slot)"; j.progress = 0.02
        with _export_lock:
            j.stage = "generating xlsx"; j.progress = 0.05
            current_month = _cache.active_month or (db.get_current_run() or {}).get("month", "output")
            current_out_path = OUTPUT_DIR / f"SBI_MIS_{current_month}.xlsx"
            if (_cache.output_path == current_out_path and _cache.output_version == _cache.computed_at
                    and current_out_path.exists()):
                j.stage = "ready (cache hit after wait)"; j.progress = 1.0
                return {"download_url": "/api/download", "filename": current_out_path.name, "cached": True}
            if _cache.computing:
                raise RuntimeError("pipeline is recomputing — start download again when it finishes")
            snapshot_results = _cache.results
            snapshot_version = _cache.computed_at
            snapshot_revision = _cache.cache_revision
            snapshot_month = current_month
            snapshot_out_path = current_out_path
            if snapshot_results is None:
                raise RuntimeError("nothing to download — upload a raw Dump first")
            tmp_path = snapshot_out_path.with_name(f".{snapshot_out_path.stem}.{j.id}.tmp.xlsx")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
                _export.write_xlsx(snapshot_results, tmp_path, month_label=snapshot_month,
                                   progress_cb=_on_progress)
                if (_cache.computing
                        or _cache.computed_at != snapshot_version
                        or _cache.cache_revision != snapshot_revision
                        or (_cache.active_month or (db.get_current_run() or {}).get("month", "output")) != snapshot_month):
                    raise RuntimeError("pipeline changed while export was running — start download again")
                import shutil
                shutil.move(tmp_path, snapshot_out_path)
                _cache.output_path = snapshot_out_path
                _cache.output_version = snapshot_version
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
        j.stage = "ready"; j.progress = 1.0
        return {"download_url": "/api/download", "filename": snapshot_out_path.name, "cached": False}

    _jobs.run(job, _do_download, timeout_seconds=3600)
    return {"job_id": job.id, "status": job.status}


# ================= Static frontend ================= #

# Serve /static (css/js/etc.)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def _render_index_html() -> str:
    """Inject PUBLIC_URL_PREFIX into index.html for /sbi (or standalone when prefix is "")."""
    index_path = FRONTEND_DIR / "index.html"
    raw = index_path.read_text()
    prefix = PUBLIC_URL_PREFIX.strip()
    if prefix:
        prefix = "/" + prefix.strip("/")
    raw = raw.replace("__SBI_PREFIX_JSON__", json.dumps(prefix))
    static_src = f"{prefix}/static/app.js?v=33" if prefix else "/static/app.js?v=33"
    return raw.replace("__STATIC_SRC__", static_src)


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(_render_index_html())
    return HTMLResponse("<h1>SBI MIS</h1><p>Frontend not built yet.</p>")
