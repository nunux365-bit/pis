#!/usr/bin/env python3
"""Run compliance_call LangGraph once for a MySQL ``calls.id`` (no date filter).

From ``agentos-backend/``:

  PYTHONPATH=. python scripts/compliance_mysql_e2e_one.py 7644

  # Twilio Video row present in DB (``calls.id=40999581`` — composition + UniqueName):
  PYTHONPATH=. python scripts/compliance_mysql_e2e_one.py 40999581

  # If your DB row lacks Twilio ``metadata`` / ``room_name``, overlay canonical sample fields:
  PYTHONPATH=. python scripts/compliance_mysql_e2e_one.py 9587 --twilio-sample

Requires in ``.env``: ``DATABASE_URL*``, ``COMPLIANCE_CALL_SYSTEM_USER_ID``,
``COMPLIANCE_DEEPGRAM_API_KEY``, OpenAI for rubric, ``COMPLIANCE_CALL_MYSQL_URL``
(URL-encode special chars in the password, e.g. ``@`` → ``%40``).
For Twilio: ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_BACKEND_ROOT / ".env")
except Exception:
    pass

import uuid  # noqa: E402

from sqlalchemy import select, text  # noqa: E402

from app.agents.compliance_call.fingerprint import (
    make_ingest_fingerprint_mysql,
    make_ingest_fingerprint_mysql_conversation,
)
from app.agents.compliance_call.mysql_conversation import fetch_mysql_legs_for_conversation  # noqa: E402
from app.agents.compliance_call.graph import ComplianceCallState, run_single_compliance_call  # noqa: E402
from app.agents.compliance_call.run import CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.db.models import ComplianceCallRun, WorkflowRun, WorkflowRunStatus  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.infra.mysql_compliance import get_mysql_compliance_engine  # noqa: E402
from app.services.compliance_call_projection import upsert_compliance_call_run  # noqa: E402

log = logging.getLogger("compliance_mysql_e2e")

# Canonical Twilio Video reference (production-shaped row). Used only with ``--twilio-sample``.
TWILIO_SAMPLE_METADATA = {
    "media_url": "/v1/Compositions/CJ88ee4a584f17bc42d8bccdb42ca54efe/Media",
    "composition_sid": "CJ88ee4a584f17bc42d8bccdb42ca54efe",
    "recording_status": "composition-available",
    "external_media_location_url": None,
}
# UniqueName on Twilio Room — matches ``calls.room_name`` on calls.id 40999581.
TWILIO_SAMPLE_ROOM_NAME = "5357624a-fd59-498d-be33-ccabe04658e7"


async def _fetch_call_row(call_id: int) -> dict[str, Any]:
    engine = get_mysql_compliance_engine()
    sql = text(
        """
        SELECT id, doctor_id, room_name, metadata, provider_reference_id,
               second_opinion_conversation_id
        FROM calls WHERE id = :cid
        """
    )
    async with engine.connect() as conn:
        row = (await conn.execute(sql, {"cid": call_id})).mappings().first()
    if row is None:
        raise SystemExit(f"MySQL: no calls row id={call_id}")
    return dict(row)


async def _delete_workflow_runs_for_mysql_call(call_id: int) -> int:
    """Remove prior ``workflow_runs`` rows for ``mysql:{call_id}`` fingerprint (dev / rerun E2E)."""
    fp = make_ingest_fingerprint_mysql(mysql_call_id=call_id)
    n = 0
    async with AsyncSessionLocal() as db:
        async with db.begin():
            wrs = (
                (
                    await db.execute(
                        select(WorkflowRun).where(
                            WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                            WorkflowRun.ingest_fingerprint == fp,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for wr in wrs:
                await db.execute(
                    ComplianceCallRun.__table__.delete().where(
                        ComplianceCallRun.workflow_run_id == wr.id
                    )
                )
                await db.delete(wr)
                n += 1
    log.info("removed %s prior workflow_run(s) for mysql_call_id=%s", n, call_id)
    return n


async def _doctor_name(doctor_id: int | None) -> tuple[str, str]:
    if doctor_id is None:
        return "doctor_unknown", "unknown"
    engine = get_mysql_compliance_engine()
    sql = text("SELECT id, first_name, last_name FROM doctors WHERE id = :id LIMIT 1")
    async with engine.connect() as conn:
        d = (await conn.execute(sql, {"id": doctor_id})).mappings().first()
    if not d:
        di = int(doctor_id)
        return f"doctor_{di}", f"doctor_{di}"
    di = int(d["id"])
    fn = str(d.get("first_name") or "").strip()
    ln = str(d.get("last_name") or "").strip()
    disp = f"{fn} {ln}".strip() or f"doctor_{di}"
    return f"doctor_{di}", disp


async def _delete_workflow_runs_for_fingerprint(fp: str) -> int:
    n = 0
    async with AsyncSessionLocal() as db:
        async with db.begin():
            wrs = (
                (
                    await db.execute(
                        select(WorkflowRun).where(
                            WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                            WorkflowRun.ingest_fingerprint == fp,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for wr in wrs:
                await db.execute(
                    ComplianceCallRun.__table__.delete().where(
                        ComplianceCallRun.workflow_run_id == wr.id
                    )
                )
                await db.delete(wr)
                n += 1
    log.info("removed %s prior workflow_run(s) for fingerprint=%s…", n, fp[:12])
    return n


async def _run_one(
    *,
    call_id: int | None,
    conversation_id: int | None,
    twilio_sample: bool,
    replace: bool,
) -> dict[str, Any]:
    legs: list[dict[str, Any]] = []
    if conversation_id is not None:
        legs = await fetch_mysql_legs_for_conversation(int(conversation_id))
        if not legs:
            raise SystemExit(f"MySQL: no call legs for conversation_id={conversation_id}")
        call_id = max(int(x["mysql_call_id"]) for x in legs)
        raw = await _fetch_call_row(call_id)
    elif call_id is not None:
        raw = await _fetch_call_row(call_id)
    else:
        raise SystemExit("Provide call_id or --conversation-id")
    meta = raw.get("metadata")
    if isinstance(meta, str):
        try:
            meta_obj: dict[str, Any] = json.loads(meta)
        except Exception:
            meta_obj = {}
    elif isinstance(meta, dict):
        meta_obj = dict(meta)
    else:
        meta_obj = {}

    rn = raw.get("room_name")
    room_s = str(rn).strip() if rn is not None else ""

    if twilio_sample:
        meta_obj = {**meta_obj, **TWILIO_SAMPLE_METADATA}
        room_s = TWILIO_SAMPLE_ROOM_NAME

    did = raw.get("doctor_id")
    doctor_id = int(did) if did is not None else None
    slug, dname = await _doctor_name(doctor_id)

    soc_raw = raw.get("second_opinion_conversation_id")
    soc_id: int | None = int(soc_raw) if soc_raw is not None else None

    if conversation_id is not None and settings.compliance_mysql_merge_conversation_transcripts:
        fp = make_ingest_fingerprint_mysql_conversation(conversation_id=int(conversation_id))
    else:
        fp = make_ingest_fingerprint_mysql(mysql_call_id=int(call_id))
    if replace:
        if conversation_id is not None:
            await _delete_workflow_runs_for_fingerprint(fp)
        else:
            await _delete_workflow_runs_for_mysql_call(int(call_id))

    uid_raw = (settings.compliance_call_system_user_id or "").strip()
    if not uid_raw:
        raise SystemExit("COMPLIANCE_CALL_SYSTEM_USER_ID is required")
    uid = uuid.UUID(uid_raw)

    inp = {
        "source": "mysql_call",
        "source_type": "mysql",
        "mysql_call_id": call_id,
        "mysql_call_ids": [int(x["mysql_call_id"]) for x in legs] if legs else [call_id],
        "mysql_second_opinion_conversation_id": soc_id,
        "source_file_id": str(call_id),
        "mysql_room_name": room_s or None,
        "mysql_metadata": meta_obj,
        "mysql_provider_reference_id": str(raw.get("provider_reference_id") or ""),
        "mime": "application/octet-stream",
        "filename": f"call_{call_id}.media",
        "doctor_slug": slug,
        "doctor_name": dname,
        "relative_path": f"mysql/e2e/{call_id}",
        "ingest_fingerprint": fp,
        "mysql_merge_legs": legs if legs else None,
    }

    async with AsyncSessionLocal() as db:
        wr = WorkflowRun(
            user_id=uid,
            workflow_key=CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
            status=WorkflowRunStatus.RUNNING.value,
            ingest_fingerprint=fp,
            input_data=inp,
            mysql_call_id=call_id,
        )
        db.add(wr)
        await db.flush()
        await upsert_compliance_call_run(db, wr)
        wid = wr.id
        await db.commit()

    st: ComplianceCallState = {
        "workflow_run_id": str(wid),
        "user_id": str(uid),
        "ingest_source": "mysql_call",
        "filename": inp["filename"],
        "mime": inp["mime"],
        "relative_path": inp["relative_path"],
        "media_mode": "file",
        "doctor_slug": slug,
        "doctor_name": dname,
        "mysql_call_id": call_id,
        "mysql_room_name": room_s,
        "mysql_metadata": meta_obj,
        "drive_file_id": "",
        "revision": "",
    }
    if soc_id is not None:
        st["mysql_second_opinion_conversation_id"] = soc_id
    if legs:
        st["mysql_call_legs"] = legs
        st["mysql_transcript_merged"] = True

    log.info(
        "starting graph workflow_run_id=%s call_id=%s conv_id=%s legs=%s twilio_sample=%s",
        wid,
        call_id,
        conversation_id,
        len(legs) if legs else 1,
        twilio_sample,
    )
    out = await run_single_compliance_call(st)
    log.info("finished workflow_run_id=%s", wid)
    return {"workflow_run_id": str(wid), "out": out}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="compliance_call E2E for one MySQL calls.id")
    p.add_argument("call_id", type=int, nargs="?", help="calls.id")
    p.add_argument(
        "--conversation-id",
        type=int,
        default=None,
        help="second_opinion_conversations.id — merge all call legs in compliance window",
    )
    p.add_argument(
        "--twilio-sample",
        action="store_true",
        help="Merge Twilio sample metadata + UniqueName (for rows missing composition in DB)",
    )
    p.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing workflow_runs / projection rows for this mysql_call_id fingerprint, then run",
    )
    args = p.parse_args()
    if not (settings.compliance_call_mysql_url or "").strip():
        raise SystemExit("Set COMPLIANCE_CALL_MYSQL_URL (mysql+asyncmy://user:pass@host:port/db; encode @ in pass as %40)")

    if args.call_id is None and args.conversation_id is None:
        p.error("call_id or --conversation-id is required")
    asyncio.run(
        _run_one(
            call_id=args.call_id,
            conversation_id=args.conversation_id,
            twilio_sample=args.twilio_sample,
            replace=args.replace,
        )
    )


if __name__ == "__main__":
    main()
