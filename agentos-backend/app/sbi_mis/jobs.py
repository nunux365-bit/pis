# app/sbi_mis/jobs.py
"""DB-persisted job CRUD — replaces sbi-mis in-memory jobs.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import update as sa_update

from app.db.session import AsyncSessionLocal
from app.sbi_mis.models import SbiJob


async def create_job(kind: str) -> str:
    job_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        session.add(SbiJob(id=job_id, kind=kind, status="pending", progress=0.0))
        await session.commit()
    return job_id


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        job = await session.get(SbiJob, job_id)
        if job is None:
            return None
        return {
            "id": job.id, "kind": job.kind, "status": job.status,
            "stage": job.stage, "progress": job.progress,
            "result": job.result_json, "error": job.error_detail,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }


async def update_job(job_id: str, **kwargs: Any) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(sa_update(SbiJob).where(SbiJob.id == job_id).values(**kwargs))
        await session.commit()
