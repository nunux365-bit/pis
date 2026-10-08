"""In-memory background-job manager.

Used to run heavy work (upload-pipeline, xlsx generation) in a daemon thread so the
HTTP request returns in <2s. Frontend polls /api/jobs/{id} for status.

Single-process, single-instance: state lives in memory and is lost on server restart.
For one-shot monthly use that's acceptable; if you ever scale to multiple workers
we'd swap this for Redis or a DB-backed queue.
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class Job:
    id: str
    kind: str                                # "upload" | "download" | …
    status: str = "queued"                   # "queued" | "running" | "done" | "error"
    progress: float = 0.0                    # 0.0 — 1.0
    stage: str = ""                          # human-readable current stage
    result: Optional[Dict[str, Any]] = None  # final payload on success
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    cancel_requested: bool = False           # cooperative cancellation flag

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": (
                (self.completed_at or time.time()) - (self.started_at or self.created_at)
            ),
        }


_jobs: Dict[str, Job] = {}
_lock = threading.Lock()


def new_job(kind: str) -> Job:
    job_id = secrets.token_urlsafe(8)
    job = Job(id=job_id, kind=kind)
    with _lock:
        _jobs[job_id] = job
    return job


def get(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def list_recent(limit: int = 20) -> list:
    items = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.to_dict() for j in items[:limit]]


def update(job_id: str, **fields) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    for k, v in fields.items():
        if hasattr(job, k):
            setattr(job, k, v)


def run(job: Job, fn: Callable[[Job], Dict[str, Any]], timeout_seconds: int = 600) -> None:
    """Spawn a daemon thread to run `fn(job)`. fn should call update(...) on the job
    object as it progresses, and return a dict for `result` on completion.

    A watchdog thread marks the job as errored if it exceeds `timeout_seconds`
    (default 10 min). The actual worker thread is left to finish in the background
    (Python doesn't safely interrupt threads), but the job state reflects timeout
    so the frontend stops polling and shows a useful error.
    """

    def _wrap():
        job.status = "running"
        job.started_at = time.time()
        try:
            result = fn(job)
            if job.cancel_requested:
                job.status = "error"
                job.error = "cancelled by user"
            elif job.status != "error":  # watchdog may have flipped to error
                job.status = "done"
                job.result = result or {}
                job.progress = 1.0
                job.stage = "done"
        except Exception as e:
            if job.status != "error":
                job.status = "error"
                job.error = f"{e}\n{traceback.format_exc()}"
        finally:
            if not job.completed_at:
                job.completed_at = time.time()

    def _watchdog():
        time.sleep(timeout_seconds)
        if job.status == "running":
            job.status = "error"
            job.error = f"job exceeded {timeout_seconds}s timeout (still running in background — server may be overloaded)"
            job.completed_at = time.time()

    t = threading.Thread(target=_wrap, daemon=True, name=f"job-{job.id[:6]}")
    t.start()
    threading.Thread(target=_watchdog, daemon=True, name=f"wd-{job.id[:6]}").start()


def cleanup_old(max_age_seconds: int = 60 * 60) -> int:
    """Drop jobs older than max_age. Called periodically to bound memory."""
    cutoff = time.time() - max_age_seconds
    removed = 0
    with _lock:
        for jid in list(_jobs.keys()):
            j = _jobs[jid]
            done_at = j.completed_at or j.created_at
            if j.status in ("done", "error") and done_at < cutoff:
                del _jobs[jid]
                removed += 1
    return removed
