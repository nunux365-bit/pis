# app/sbi_mis/state.py
"""Module-level in-process pipeline cache + async orchestration.

The pandas DataFrame and pipeline results cannot be stored in PostgreSQL —
they are kept in process memory exactly as in the original sbi-mis.
asyncio.Lock replaces threading.Lock; pandas runs in thread pool via run_in_executor.

Disk cache
----------
After every successful pipeline run the results dict + dump_df are pickled to
  {data_dir}/pipeline_cache/{month}.pkl
alongside a fingerprint file
  {data_dir}/pipeline_cache/{month}.fp

The fingerprint is SHA-256(raw_file_bytes + sorted_rules_json).  On startup,
_restore_sbi_cache (main.py) checks whether the fingerprint still matches; if so
it loads from pickle rather than re-running the pipeline, making restarts instant
when neither the raw file nor the rules have changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pickle
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from app.infra.task_tracker import spawn
from app.infra.thread_pools import cpu_executor

log = logging.getLogger(__name__)

# Maps trigger name → set of pipeline stages that can be safely skipped because
# that trigger does not affect those stages' outputs.
_TRIGGER_SKIP: Dict[str, frozenset] = {
    "ahc":                                frozenset({"dump", "order_level", "order_level_np"}),
    "wallet_checker":                     frozenset({"dump", "order_level", "order_level_np"}),
    "pf_summary_file":                    frozenset({"dump", "order_level", "order_level_np"}),
    "historicals":                        frozenset({"dump", "order_level", "order_level_np"}),
    "rule:Summary":                       frozenset({"dump", "order_level", "order_level_np", "pf_summary"}),
    "rule:PF Summary":                    frozenset({"dump", "order_level", "order_level_np"}),
    "rule:Order level ":                  frozenset({"dump", "pf_summary"}),
    "rule:Order level - non permissible": frozenset({"dump", "pf_summary"}),
    # "rule:Dump", "raw", None → skip nothing (full run)
}


@dataclass
class _SbiCache:
    dump_df: Optional[pd.DataFrame] = None
    results: Optional[Dict[str, Any]] = None
    active_month: Optional[str] = None
    computing: bool = False
    last_error: Optional[str] = None
    computed_at: Optional[float] = None
    # Path of the raw file currently loaded — used for fingerprinting
    raw_file_path: Optional[Path] = None
    # Cached supplementary DataFrames — keyed by file mtime_ns to detect changes
    ahc_df: Optional[pd.DataFrame] = None
    ahc_mtime_ns: Optional[int] = None
    wallet_checker_df: Optional[pd.DataFrame] = None
    wallet_checker_mtime_ns: Optional[int] = None
    # Whether the active raw xlsx contains an embedded AHC sheet (None = not yet checked)
    raw_has_ahc_sheet: Optional[bool] = None
    # Path that raw_has_ahc_sheet was computed for — reset flag when path changes
    _raw_ahc_checked_for: Optional[Path] = None
    # Intermediate results cached between pipeline runs for partial reruns
    _dump_enriched: Optional[pd.DataFrame] = None   # Dump after enrichment (cols AH..AX applied)
    _ol_df: Optional[pd.DataFrame] = None            # Order Level pivot result
    _olnp_df: Optional[pd.DataFrame] = None          # Order Level - non permissible pivot result
    _pfs_df: Optional[pd.DataFrame] = None           # PF Summary result
    # Which pipeline stages were skipped in the most recent run — used by xlsx pre-gen
    # to decide which sheets can be reused from the existing file vs re-written.
    _last_skip_stages: frozenset = field(default_factory=frozenset)


_cache = _SbiCache()
_pipeline_lock: asyncio.Lock = asyncio.Lock()
_pipeline_pending: bool = False
_pending_lock: asyncio.Lock = asyncio.Lock()
_output_cache: Dict[str, bytes] = {}   # {cache_key: xlsx_bytes}
_dict_cache: Dict[str, Any] = {}       # {cache_key: any JSON-serialisable value}


def invalidate_output_cache() -> None:
    _output_cache.clear()
    _dict_cache.clear()
    # Note: do NOT clear _dump_enriched/_ol_df etc here — those are intermediate
    # results intentionally reused across partial reruns


# ── Disk cache helpers ────────────────────────────────────────────────────────

def _pipeline_cache_dir() -> Path:
    from app.sbi_mis import db as _db
    p = _db.data_dir() / "pipeline_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def compute_fingerprint(raw_path: Path, month: Optional[str] = None) -> str:
    """Return SHA-256 of raw file bytes + supplementary file mtimes + sorted rules JSON.

    Captures raw-file changes, supplementary file changes (AHC, wallet_checker),
    and rule changes so the disk cache is automatically invalidated whenever any
    pipeline input changes.  Pass *month* to include supplementary files.
    """
    from app.sbi_mis import db as _db
    h = hashlib.sha256()
    # Use mtime_ns + file size as a cheap proxy for content change — avoids reading
    # up to 120 MB on every fingerprint check. Consistent with how supplementary
    # files are already fingerprinted below.
    st = raw_path.stat()
    h.update(f"raw:{st.st_mtime_ns}:{st.st_size}".encode())
    # Include supplementary files so uploading AHC/wallet_checker invalidates the cache.
    if month:
        for kind in ("ahc", "wallet_checker"):
            rec = _db.get_run_file_sync(month, kind)
            if rec:
                sup_path = _db.resolve_stored_path(rec["file_path"])
                if sup_path and sup_path.exists():
                    # mtime_ns is cheap — no file read needed
                    h.update(f"{kind}:{sup_path.stat().st_mtime_ns}".encode())
    rules = _db.get_all_rules_sync()
    # Sort for a stable serialisation order
    stable = sorted(rules, key=lambda r: (r.get("sheet", ""), r.get("column_letter", "")))
    h.update(json.dumps(stable, sort_keys=True, default=str).encode())
    return h.hexdigest()


def save_pipeline_cache() -> None:
    """Persist pipeline results + dump_df to disk with a fingerprint.

    Called synchronously from a thread-pool executor after a successful run.
    Non-fatal — a failure here just means the next restart will recompute.
    """
    if _cache.results is None or _cache.active_month is None or _cache.raw_file_path is None:
        return
    month = _cache.active_month
    raw_path = _cache.raw_file_path
    try:
        fp = compute_fingerprint(raw_path, month=month)
        payload = {
            "results": _cache.results,
            "dump_df": _cache.dump_df,
            "computed_at": _cache.computed_at,
            "active_month": month,
        }
        cache_dir = _pipeline_cache_dir()
        pkl_path = cache_dir / f"{month}.pkl"
        fp_path  = cache_dir / f"{month}.fp"
        with open(pkl_path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        fp_path.write_text(fp)
        log.info("SBI MIS: pipeline results cached to disk for %s", month)
    except Exception:
        log.exception("SBI MIS: failed to save pipeline cache for %s (non-fatal)", month)


def try_restore_from_disk(month: str, raw_path: Path) -> bool:
    """Try to populate _cache from the disk pickle.

    Returns True and fills _cache if the stored fingerprint matches the current
    raw file + rules; returns False otherwise (caller should run the pipeline).

    Designed to be called from asyncio via run_in_executor.
    """
    cache_dir = _pipeline_cache_dir()
    pkl_path = cache_dir / f"{month}.pkl"
    fp_path  = cache_dir / f"{month}.fp"

    if not pkl_path.exists() or not fp_path.exists():
        log.info("SBI MIS: no disk cache found for %s — will run pipeline", month)
        return False

    try:
        stored_fp  = fp_path.read_text().strip()
        current_fp = compute_fingerprint(raw_path, month=month)
        if stored_fp != current_fp:
            log.info("SBI MIS: disk cache fingerprint mismatch for %s — will recompute", month)
            return False

        with open(pkl_path, "rb") as fh:
            payload = pickle.load(fh)

        _cache.dump_df        = payload["dump_df"]
        _cache.results        = payload["results"]
        _cache.computed_at    = payload.get("computed_at") or time.time()
        _cache.active_month   = month
        _cache.raw_file_path  = raw_path
        _cache.last_error     = None
        _cache.computing      = False
        log.info("SBI MIS: restored pipeline results from disk cache for %s ✓", month)
        return True
    except Exception:
        log.exception("SBI MIS: failed to load pipeline cache for %s (will recompute)", month)
        return False


# ── Pipeline orchestration ────────────────────────────────────────────────────

async def _load_supplementary_files(active_month: str) -> tuple:
    """Load AHC and wallet_checker DataFrames, using mtime-keyed cache to avoid re-reads.

    Returns (ahc_df, wallet_checker_df) — either may be None.
    Runs in the event loop; file I/O is offloaded to a thread executor.
    """
    from app.sbi_mis import db as _db
    from app.sbi_mis.engine import ingest as _ingest
    import asyncio

    loop = asyncio.get_event_loop()

    async def _maybe_load(kind: str, cached_df_attr: str, cached_mtime_attr: str):
        rec = _db.get_run_file_sync(active_month, kind)
        if not rec:
            return None
        path = _db.resolve_stored_path(rec["file_path"])
        if path is None or not path.exists():
            return None
        mtime = path.stat().st_mtime_ns
        # Return cached df if mtime unchanged
        if getattr(_cache, cached_mtime_attr) == mtime and getattr(_cache, cached_df_attr) is not None:
            return getattr(_cache, cached_df_attr)
        # Load from disk
        if kind == "ahc":
            df = await loop.run_in_executor(cpu_executor(), lambda: _ingest.load_ahc(path))
        else:
            df = await loop.run_in_executor(
                cpu_executor(), lambda: _ingest.load_wallet_checker(path)
            )
        setattr(_cache, cached_df_attr, df)
        setattr(_cache, cached_mtime_attr, mtime)
        return df

    ahc_df = await _maybe_load("ahc", "ahc_df", "ahc_mtime_ns")
    wallet_checker_df = await _maybe_load("wallet_checker", "wallet_checker_df", "wallet_checker_mtime_ns")

    # AHC fallback: check if raw file has an embedded AHC sheet.
    # Only do the openpyxl open once per raw file (cache the result keyed by path).
    if ahc_df is None and _cache.raw_file_path is not None:
        from app.sbi_mis.engine.ingest import _is_csv
        raw_path = _cache.raw_file_path
        if not _is_csv(raw_path):
            # Reset the cached flag if the raw file path has changed
            if _cache._raw_ahc_checked_for != raw_path:
                _cache.raw_has_ahc_sheet = None
                _cache._raw_ahc_checked_for = None

            if _cache.raw_has_ahc_sheet is None:
                # First time for this raw file — check sheetnames (read_only openpyxl)
                def _check_ahc_sheet():
                    from openpyxl import load_workbook as _lw
                    wb = _lw(raw_path, read_only=True, data_only=True)
                    has_it = "AHC" in wb.sheetnames
                    wb.close()
                    return has_it
                _cache.raw_has_ahc_sheet = await loop.run_in_executor(
                    cpu_executor(), _check_ahc_sheet
                )
                _cache._raw_ahc_checked_for = raw_path

            if _cache.raw_has_ahc_sheet:
                # Load AHC from raw file — cache by raw file mtime
                raw_mtime = raw_path.stat().st_mtime_ns
                if _cache.ahc_mtime_ns == raw_mtime and _cache.ahc_df is not None:
                    ahc_df = _cache.ahc_df
                else:
                    ahc_df = await loop.run_in_executor(
                        cpu_executor(), lambda: _ingest.load_ahc(raw_path)
                    )
                    _cache.ahc_df = ahc_df
                    _cache.ahc_mtime_ns = raw_mtime

    return ahc_df, wallet_checker_df


async def do_one_rerun(trigger: Optional[str] = None) -> None:
    """Run the pipeline once in a thread pool. Caller must hold _pipeline_lock.

    *trigger* identifies what caused this rerun (e.g. "ahc", "raw", "rule:Summary").
    Used to derive *skip_stages* so unchanged pipeline stages are not recomputed.
    Pass None or "raw" for a full run (no stages skipped).
    """
    if _cache.dump_df is None:
        return

    # Full runs (trigger=None or "raw") clear all intermediate caches so nothing
    # stale is reused if the raw file has changed.
    if trigger in (None, "raw"):
        _cache._dump_enriched = None
        _cache._ol_df = None
        _cache._olnp_df = None
        _cache._pfs_df = None

    skip_stages = _TRIGGER_SKIP.get(trigger, frozenset())
    _cache._last_skip_stages = skip_stages

    _cache.computing = True
    invalidate_output_cache()
    loop = asyncio.get_event_loop()
    try:
        from app.sbi_mis.engine import pipeline as _pipeline
        df_copy = _cache.dump_df.copy()
        active = _cache.active_month

        # Pre-load supplementary files (async, mtime-cached) before entering thread pool
        ahc_df = None
        wallet_checker_df = None
        if active:
            try:
                ahc_df, wallet_checker_df = await _load_supplementary_files(active)
            except Exception as e:
                log.warning("SBI MIS: failed to pre-load supplementary files: %s", e)

        results = await loop.run_in_executor(
            cpu_executor(), lambda: _pipeline.run(
                df_copy, active_month=active,
                skip_stages=skip_stages,
                ahc_df=ahc_df, wallet_checker_df=wallet_checker_df,
            )
        )
        _cache.results    = results
        _cache.last_error = None
        _cache.computed_at = time.time()
        invalidate_output_cache()
        # Save to disk and pre-generate xlsx in background — doesn't delay callers
        spawn(_save_cache_background(), name="sbi-save-cache")
        spawn(_pregenerate_xlsx_background(), name="sbi-pregenerate-xlsx")
    except Exception as e:
        _cache.last_error = f"{e}\n{traceback.format_exc()[:1000]}"
        _cache.results = None
    finally:
        _cache.computing = False


async def _save_cache_background() -> None:
    """Fire-and-forget: run save_pipeline_cache in a thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(cpu_executor(), save_pipeline_cache)


# Maps pipeline stage name → xlsx sheet display name.
# A sheet can be reused from the existing file when its stage was skipped.
_STAGE_TO_XLSX_SHEET: Dict[str, str] = {
    "dump":           "Dump",
    "order_level":    "Order level ",
    "order_level_np": "Order level - non permissible",
    "pf_summary":     "PF Summary",
    # Summary and AHC have no skippable stage — always refreshed
}


async def _pregenerate_xlsx_background() -> None:
    """Pre-generate the xlsx right after a pipeline run so downloads are instant.

    Without this, the first download triggers xlsx generation synchronously inside
    the request handler.  On large datasets that can take 30-60 s, causing nginx
    to return 504 before a single byte reaches the browser.  Generating here
    (fire-and-forget, in a thread-pool executor) means the file is in _output_cache
    by the time the user clicks Download.

    Partial write optimisation: if a partial pipeline run skipped some stages (e.g.
    AHC upload only changes PF Summary + Summary), reuse the unchanged sheets' XML
    from the existing xlsx instead of re-writing the 5 M-cell Dump sheet.
    """
    if _cache.results is None or _cache.active_month is None or _cache.computed_at is None:
        return
    cache_key = f"output_{_cache.computed_at}"
    if cache_key in _output_cache:
        return  # already generated (e.g. download was clicked before this task ran)
    month = _cache.active_month
    results_snapshot = _cache.results  # capture — do_one_rerun may run again later
    skip_stages = _cache._last_skip_stages
    loop = asyncio.get_event_loop()
    try:
        from app.sbi_mis import db as _db
        from app.sbi_mis.engine import export as _export_mod
        output_dir = _db.output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"sbi_mis_{month}.xlsx"

        # Decide whether a partial write is worthwhile:
        # - At least one expensive stage (dump) must be skippable
        # - The base xlsx must already exist (something to copy from)
        all_xlsx_sheets = [
            "Dump", "Order level ", "Order level - non permissible", "PF Summary", "Summary",
        ]
        if ("AHC" in results_snapshot.get("sheets", {}) and
                results_snapshot["sheets"]["AHC"] is not None):
            all_xlsx_sheets.append("AHC")

        reusable = {
            _STAGE_TO_XLSX_SHEET[stage]
            for stage in skip_stages
            if stage in _STAGE_TO_XLSX_SHEET
        }
        refresh_sheets = [s for s in all_xlsx_sheets if s not in reusable]

        use_partial = (
            skip_stages                           # something was skipped
            and "dump" in skip_stages             # Dump is the expensive sheet
            and out_path.exists()                 # base file exists
            and len(refresh_sheets) < len(all_xlsx_sheets)
        )

        if use_partial:
            log.info(
                "SBI MIS: partial xlsx write for %s — refreshing %s, reusing %s",
                month, refresh_sheets, sorted(reusable),
            )
            await loop.run_in_executor(
                cpu_executor(), lambda: _export_mod.write_xlsx_partial(
                    results_snapshot, out_path, out_path,
                    refresh_sheets, month_label=month,
                )
            )
        else:
            await loop.run_in_executor(
                cpu_executor(), lambda: _export_mod.write_xlsx(
                    results_snapshot, out_path, month_label=month
                )
            )

        with open(out_path, "rb") as fh:
            _output_cache[cache_key] = fh.read()
        log.info("SBI MIS: xlsx pre-generated for %s (%d bytes)", month, len(_output_cache[cache_key]))
    except Exception:
        log.exception("SBI MIS: failed to pre-generate xlsx for %s (non-fatal)", month)


async def run_pipeline_rerun(job_id: str, trigger: Optional[str] = None) -> None:
    """Background coroutine: acquire lock, run pipeline, drain pending, update job.

    *trigger* is forwarded to do_one_rerun to enable partial-stage skipping.
    """
    from app.sbi_mis import jobs as _jobs

    global _pipeline_pending

    await _jobs.update_job(job_id, status="running",
                           started_at=datetime.now(timezone.utc), stage="queued")
    try:
        async with _pipeline_lock:
            await _jobs.update_job(job_id, stage="running pipeline", progress=0.5)
            await do_one_rerun(trigger=trigger)
            # Drain any pending reruns that arrived while lock was held.
            # Coalesced reruns use no trigger (full run) to ensure correctness.
            while True:
                async with _pending_lock:
                    if not _pipeline_pending:
                        break
                    _pipeline_pending = False
                await _jobs.update_job(job_id, stage="running pipeline (coalesced)", progress=0.8)
                await do_one_rerun()

        # do_one_rerun swallows its own exceptions (sets _cache.last_error and
        # results=None) so a failure never propagates to the ``except`` below.
        # Detect it here so the job reflects reality instead of a misleading "done"
        # over an empty working sheet. dump_df is None only means "nothing to run".
        if _cache.dump_df is not None and _cache.results is None:
            await _jobs.update_job(
                job_id, status="failed",
                error_detail=_cache.last_error or "pipeline produced no results",
                completed_at=datetime.now(timezone.utc),
            )
            return

        await _jobs.update_job(
            job_id, status="done", progress=1.0, stage="done",
            result_json={"computed_at": _cache.computed_at},
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        await _jobs.update_job(
            job_id, status="failed", error_detail=str(e),
            completed_at=datetime.now(timezone.utc),
        )


async def schedule_rerun(background_tasks: Any, trigger: Optional[str] = None) -> str:
    """Create a job row, set pending flag if lock busy, enqueue background task.

    *trigger* identifies what caused this rerun and is forwarded to the pipeline
    so unchanged stages can be skipped.
    """
    global _pipeline_pending
    from app.sbi_mis import jobs as _jobs

    if _pipeline_lock.locked():
        async with _pending_lock:
            _pipeline_pending = True

    job_id = await _jobs.create_job("pipeline_rerun")
    background_tasks.add_task(run_pipeline_rerun, job_id, trigger)
    return job_id
