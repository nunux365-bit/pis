"""1:1 projection row for ``compliance_call`` workflow runs (dashboard list / charts)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComplianceCallRun, WorkflowRun
from app.services.workflow_runner import CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY


def _norm_source_type(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in ("gdrive", "google_drive", "drive"):
        return "drive"
    if s in ("mysql_call", "mysql"):
        return "mysql"
    if s == "aws":
        return "aws"
    if s == "ozontel":
        return "ozontel"
    return "drive"


def _source_type_from_input(inp: dict[str, Any]) -> str:
    """Prefer explicit ``source_type``; else derive from ``source`` (e.g. gdrive → drive)."""
    st = inp.get("source_type")
    if st is not None and str(st).strip():
        return _norm_source_type(str(st))
    return _norm_source_type(str(inp.get("source") or "") or None)


def _source_file_id(inp: dict[str, Any]) -> str | None:
    v = inp.get("source_file_id") or inp.get("drive_file_id")
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _parse_composite_pct(ev: dict[str, Any]) -> Decimal | None:
    raw = ev.get("composite_pct")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return None
        try:
            return Decimal(t)
        except Exception:
            return None
    return None


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _bool_or_none(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        lo = v.lower()
        if lo == "true":
            return True
        if lo == "false":
            return False
    return None


def compliance_call_run_from_workflow_run(wr: WorkflowRun) -> dict[str, Any]:
    """Build column dict for insert/update from a hydrated ``WorkflowRun``."""
    inp = wr.input_data if isinstance(wr.input_data, dict) else {}
    outp = wr.output_data if isinstance(wr.output_data, dict) else {}
    ev = outp.get("eval") if isinstance(outp.get("eval"), dict) else {}
    return {
        "workflow_run_id": wr.id,
        "created_at": wr.created_at,
        "updated_at": wr.updated_at,
        "status": wr.status,
        "filename": inp.get("filename"),
        "doctor_slug": inp.get("doctor_slug"),
        "doctor_name": inp.get("doctor_name"),
        "source_file_id": _source_file_id(inp),
        "source_type": _source_type_from_input(inp),
        "composite_pct": _parse_composite_pct(ev),
        "grade": _str_or_none(ev.get("grade")),
        "grade_label": _str_or_none(ev.get("grade_label")),
        "sheet_appended": _bool_or_none(outp.get("sheet_appended")),
        "error_message": wr.error_message,
        "ingest_fingerprint": wr.ingest_fingerprint,
    }


async def upsert_compliance_call_run(db: AsyncSession, wr: WorkflowRun) -> None:
    """Upsert projection row for a ``compliance_call`` workflow run (no-op for other keys)."""
    if wr.workflow_key != CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY:
        return
    # After flush, server-side defaults / onupdate can expire ORM attrs; lazy reload is sync
    # and breaks under AsyncSession (MissingGreenlet). Refresh eagerly while we still have IO context.
    insp = inspect(wr)
    if insp.persistent and (insp.expired or insp.expired_attributes):
        await db.refresh(wr)
    payload = compliance_call_run_from_workflow_run(wr)
    wid = payload.pop("workflow_run_id")
    row = (await db.execute(select(ComplianceCallRun).where(ComplianceCallRun.workflow_run_id == wid))).scalar_one_or_none()
    if row is None:
        db.add(
            ComplianceCallRun(
                id=uuid.uuid4(),
                workflow_run_id=wid,
                **payload,
            )
        )
        return
    for k, v in payload.items():
        setattr(row, k, v)


__all__ = ["compliance_call_run_from_workflow_run", "upsert_compliance_call_run"]
