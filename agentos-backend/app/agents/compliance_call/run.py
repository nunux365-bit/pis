"""Batch driver: claim workflow_runs, invoke compliance LangGraph per file."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.agents.compliance_call.batch_item import ComplianceBatchItem
from app.agents.compliance_call.adapters.input_gdrive import GDriveInputSource, build_compliance_drive_service
from app.agents.compliance_call.adapters.sheets_append import retry_sheet_row_from_stored
from app.agents.compliance_call.doctor_slug import doctor_name_from_filename, doctor_slug_from_filename
from app.agents.compliance_call.fingerprint import (
    make_ingest_fingerprint,
    make_ingest_fingerprint_mysql,
    make_ingest_fingerprint_mysql_conversation,
)
from app.agents.compliance_call.graph import ComplianceCallState, run_single_compliance_call
from app.agents.compliance_call.mysql_candidates import fetch_mysql_compliance_candidates
from app.agents.compliance_call.mysql_conversation import (
    collapse_mysql_items_by_conversation,
    mysql_conversation_skip_reason,
)
from app.config.settings import settings
from app.db.models import WorkflowRun, WorkflowRunStatus
from app.db.session import AsyncSessionLocal, engine
from app.services.compliance_mysql_watermark import max_completed_mysql_call_id
from app.services.compliance_call_projection import upsert_compliance_call_run
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)

CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY = "compliance_call"


def _system_user_id() -> uuid.UUID | None:
    raw = (settings.compliance_call_system_user_id or "").strip()
    if not raw:
        log.warning("compliance_call: COMPLIANCE_CALL_SYSTEM_USER_ID is not set")
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        log.warning("compliance_call: invalid COMPLIANCE_CALL_SYSTEM_USER_ID")
        return None


async def retry_failed_compliance_sheets(*, max_rows: int = 5) -> list[dict[str, Any]]:
    """Best-effort Sheet append for completed runs that are not yet on the sheet.

    Covers Sheets API errors (``sheet_sync_error``) and a crash after Postgres
    marked the run completed but before the append (``sheet_appended`` still false).
    """
    if not (settings.compliance_sheet_id or "").strip():
        return []
    out: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        q = (
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
            )
            .order_by(WorkflowRun.updated_at.asc())
            .limit(max(10, max_rows * 15))
        )
        rows = (await db.execute(q)).scalars().all()

    for r in rows:
        if len(out) >= max_rows:
            break
        od = r.output_data if isinstance(r.output_data, dict) else {}
        if od.get("sheet_appended") is True:
            continue
        inp = r.input_data if isinstance(r.input_data, dict) else {}
        try:

            def _go():
                return retry_sheet_row_from_stored(
                    workflow_run_id=str(r.id),
                    input_data=inp,
                    output_data=od,
                )

            rown = await run_blocking(_go)
        except Exception as e:
            log.warning("compliance sheet retry failed run=%s: %s", r.id, e)
            continue
        async with AsyncSessionLocal() as db2:
            r2 = await db2.get(WorkflowRun, r.id)
            if not r2:
                continue
            od2 = dict(r2.output_data) if isinstance(r2.output_data, dict) else {}
            od2["sheet_appended"] = True
            od2["sheet_row"] = rown
            od2.pop("sheet_sync_error", None)
            r2.output_data = od2
            await upsert_compliance_call_run(db2, r2)
            await db2.commit()
        out.append({"workflow_run_id": str(r.id), "sheet_row": rown})
    return out


def _skip_tag(item: ComplianceBatchItem) -> dict[str, str]:
    if item.source == "mysql_call":
        tag: dict[str, str] = {"mysql_call_id": str(item.mysql_call_id or "")}
        if item.mysql_second_opinion_conversation_id is not None:
            tag["mysql_second_opinion_conversation_id"] = str(item.mysql_second_opinion_conversation_id)
        return tag
    return {"drive_file_id": item.drive_file_id}


def _mysql_ingest_fingerprint(item: ComplianceBatchItem) -> str:
    legs = item.mysql_call_legs or []
    soc = item.mysql_second_opinion_conversation_id
    if (
        settings.compliance_mysql_merge_conversation_transcripts
        and soc is not None
        and len(legs) > 0
    ):
        return make_ingest_fingerprint_mysql_conversation(conversation_id=int(soc))
    return make_ingest_fingerprint_mysql(mysql_call_id=int(item.mysql_call_id or 0))


async def run_compliance_batch_async(
    *,
    max_files: int | None = None,
) -> dict[str, Any]:
    if not settings.compliance_call_enabled:
        return {"disabled": True, "message": "compliance_call_enabled=false"}

    uid = _system_user_id()
    if uid is None:
        return {"graph_error": "COMPLIANCE_CALL_SYSTEM_USER_ID is not configured"}

    folder = (settings.compliance_call_gdrive_folder_id or "").strip()
    mysql_enabled = bool(settings.compliance_call_mysql_enabled and (settings.compliance_call_mysql_url or "").strip())
    if not folder and not mysql_enabled:
        return {"graph_error": "COMPLIANCE_CALL_GDRIVE_FOLDER_ID is not set and MySQL compliance is disabled or unset"}

    cap = int(max_files if max_files is not None else settings.compliance_max_files_per_tick)
    cap = max(1, min(cap, 500))

    drive_items: list[ComplianceBatchItem] = []
    if folder:
        svc = build_compliance_drive_service()
        src = GDriveInputSource(svc=svc, folder_id=folder)
        for w in src.list_pending():
            slug = doctor_slug_from_filename(w.filename)
            dname = doctor_name_from_filename(w.filename)
            drive_items.append(ComplianceBatchItem.from_gdrive(w, slug=slug, dname=dname))

    mysql_items: list[ComplianceBatchItem] = []
    mysql_after_last_completed_call_id: int | None = None
    if mysql_enabled:
        need_mysql = max(0, cap - len(drive_items))
        try:
            if need_mysql > 0:
                after_call_id = await max_completed_mysql_call_id()
                mysql_after_last_completed_call_id = after_call_id
                mysql_items = await fetch_mysql_compliance_candidates(
                    max_candidates=need_mysql,
                    after_call_id=after_call_id,
                )
            else:
                log.debug(
                    "compliance_call mysql: skip listing (drive_items=%d >= cap=%d)",
                    len(drive_items),
                    cap,
                )
        except Exception:
            log.exception("compliance_call mysql: candidate listing failed")
        try:
            mysql_items = await collapse_mysql_items_by_conversation(mysql_items)
        except Exception:
            log.exception("compliance_call mysql: conversation collapse failed")

    merged = drive_items + mysql_items
    items = merged[:cap]

    sem = asyncio.Semaphore(max(1, settings.compliance_batch_concurrency))
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    dialect = engine.sync_engine.dialect.name
    use_skip = dialect == "postgresql"

    async def one(item: ComplianceBatchItem) -> None:
        async with sem:
            if item.source == "mysql_call":
                if item.mysql_call_id is None:
                    skipped.append({**_skip_tag(item), "reason": "missing_mysql_call_id"})
                    return
                fp = _mysql_ingest_fingerprint(item)
                slug = item.doctor_slug
                dname = item.doctor_name
            else:
                fp = make_ingest_fingerprint(drive_file_id=item.drive_file_id, revision=item.revision)
                slug = item.doctor_slug
                dname = item.doctor_name

            stale_before = datetime.now(timezone.utc) - timedelta(
                minutes=max(5, int(settings.compliance_stale_running_minutes or 45))
            )
            wid: uuid.UUID | None = None

            for _attempt in range(5):
                try:
                    async with AsyncSessionLocal() as db:
                        async with db.begin():
                            if (
                                item.source == "mysql_call"
                                and settings.compliance_mysql_merge_conversation_transcripts
                                and item.mysql_second_opinion_conversation_id is not None
                            ):
                                legs = item.mysql_call_legs or []
                                leg_ids = [
                                    int(x["mysql_call_id"])
                                    for x in legs
                                    if x.get("mysql_call_id") is not None
                                ]
                                if not leg_ids and item.mysql_call_id is not None:
                                    leg_ids = [int(item.mysql_call_id)]
                                skip_conv = await mysql_conversation_skip_reason(
                                    db,
                                    conversation_id=int(item.mysql_second_opinion_conversation_id),
                                    leg_call_ids=leg_ids,
                                )
                                if skip_conv:
                                    skipped.append({**_skip_tag(item), "reason": skip_conv})
                                    return

                            done = (
                                await db.execute(
                                    select(WorkflowRun).where(
                                        WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                                        WorkflowRun.ingest_fingerprint == fp,
                                        WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
                                    )
                                )
                            ).scalars().first()
                            if done is not None:
                                skipped.append({**_skip_tag(item), "reason": "already_completed"})
                                return

                            stmt = (
                                select(WorkflowRun)
                                .where(
                                    WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                                    WorkflowRun.ingest_fingerprint == fp,
                                    or_(
                                        WorkflowRun.status == WorkflowRunStatus.FAILED.value,
                                        and_(
                                            WorkflowRun.status == WorkflowRunStatus.RUNNING.value,
                                            WorkflowRun.updated_at < stale_before,
                                        ),
                                    ),
                                )
                                .limit(1)
                            )
                            if use_skip:
                                stmt = stmt.with_for_update(skip_locked=True)
                            else:
                                stmt = stmt.with_for_update()

                            row = (await db.execute(stmt)).scalars().first()

                            if item.source == "mysql_call":
                                legs = item.mysql_call_legs or []
                                call_ids = [int(x["mysql_call_id"]) for x in legs if x.get("mysql_call_id") is not None]
                                if not call_ids:
                                    call_ids = [int(item.mysql_call_id or 0)]
                                wm_call_id = max(call_ids) if call_ids else int(item.mysql_call_id or 0)
                                inp_mysql = {
                                    "source": "mysql_call",
                                    "source_type": "mysql",
                                    "mysql_call_id": wm_call_id,
                                    "mysql_call_ids": call_ids,
                                    "mysql_second_opinion_id": item.mysql_second_opinion_id,
                                    "mysql_second_opinion_conversation_id": item.mysql_second_opinion_conversation_id,
                                    "mysql_call_updated_at": item.mysql_call_updated_at,
                                    "source_file_id": str(wm_call_id),
                                    "mysql_room_name": item.mysql_room_name,
                                    "mysql_metadata": item.mysql_metadata,
                                    "mysql_provider_reference_id": item.mysql_provider_reference_id,
                                    "mime": item.mime,
                                    "filename": item.filename,
                                    "doctor_slug": slug,
                                    "doctor_name": dname,
                                    "relative_path": item.relative_path,
                                    "ingest_fingerprint": fp,
                                    "mysql_merge_legs": legs if legs else None,
                                }
                                upd_payload = inp_mysql
                                ins_payload = inp_mysql
                            else:
                                inp_g = {
                                    "source": "gdrive",
                                    "drive_file_id": item.drive_file_id,
                                    "revision": item.revision,
                                    "mime": item.mime,
                                    "filename": item.filename,
                                    "doctor_slug": slug,
                                    "doctor_name": dname,
                                    "relative_path": item.relative_path,
                                    "ingest_fingerprint": fp,
                                }
                                upd_payload = inp_g
                                ins_payload = inp_g

                            if row is not None:
                                row.status = WorkflowRunStatus.RUNNING.value
                                row.error_message = None
                                row.output_data = None
                                row.user_id = uid
                                row.input_data = upd_payload
                                if item.source == "mysql_call":
                                    legs = item.mysql_call_legs or []
                                    ids = [int(x["mysql_call_id"]) for x in legs if x.get("mysql_call_id") is not None]
                                    row.mysql_call_id = max(ids) if ids else int(item.mysql_call_id or 0)
                                else:
                                    row.mysql_call_id = None
                                await db.flush()
                                await upsert_compliance_call_run(db, row)
                                wid = row.id
                                break

                            wr = WorkflowRun(
                                user_id=uid,
                                workflow_key=CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                                status=WorkflowRunStatus.RUNNING.value,
                                ingest_fingerprint=fp,
                                input_data=ins_payload,
                                mysql_call_id=(
                                    max(
                                        int(x["mysql_call_id"])
                                        for x in (item.mysql_call_legs or [])
                                        if x.get("mysql_call_id") is not None
                                    )
                                    if item.source == "mysql_call" and item.mysql_call_legs
                                    else (int(item.mysql_call_id) if item.source == "mysql_call" else None)
                                ),
                            )
                            db.add(wr)
                            await db.flush()
                            await upsert_compliance_call_run(db, wr)
                            wid = wr.id
                            break
                except IntegrityError:
                    await asyncio.sleep(0.05)
                    continue

            if wid is None:
                async with AsyncSessionLocal() as dbx:
                    r0 = (
                        await dbx.execute(
                            select(WorkflowRun).where(
                                WorkflowRun.workflow_key == CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY,
                                WorkflowRun.ingest_fingerprint == fp,
                            )
                        )
                    ).scalars().first()
                if r0 and r0.status == WorkflowRunStatus.COMPLETED.value:
                    skipped.append({**_skip_tag(item), "reason": "already_completed"})
                    return
                if r0 and r0.status == WorkflowRunStatus.RUNNING.value:
                    skipped.append({**_skip_tag(item), "reason": "concurrent_running"})
                    return
                skipped.append({**_skip_tag(item), "reason": "claim_unavailable"})
                return

            st: ComplianceCallState = {
                "workflow_run_id": str(wid),
                "user_id": str(uid),
                "ingest_source": item.source,
                "filename": item.filename,
                "mime": item.mime,
                "relative_path": item.relative_path,
                "media_mode": item.media_mode,
                "doctor_slug": slug,
                "doctor_name": dname,
            }
            if item.source == "gdrive":
                st["drive_file_id"] = item.drive_file_id
                st["revision"] = item.revision
            else:
                legs = item.mysql_call_legs or []
                if legs:
                    st["mysql_call_legs"] = legs
                    st["mysql_transcript_merged"] = True
                st["mysql_call_id"] = int(item.mysql_call_id or 0)
                st["mysql_room_name"] = str(item.mysql_room_name or "")
                st["mysql_metadata"] = item.mysql_metadata if isinstance(item.mysql_metadata, dict) else {}
                if item.mysql_second_opinion_id is not None:
                    st["mysql_second_opinion_id"] = int(item.mysql_second_opinion_id)
                if item.mysql_second_opinion_conversation_id is not None:
                    st["mysql_second_opinion_conversation_id"] = int(item.mysql_second_opinion_conversation_id)
                if item.mysql_call_updated_at:
                    st["mysql_call_updated_at"] = item.mysql_call_updated_at
                st["drive_file_id"] = ""
                st["revision"] = ""

            try:
                out = await run_single_compliance_call(st)
                results.append({"workflow_run_id": str(wid), "out": out})
            except Exception as e:
                log.exception("compliance batch item failed")
                async with AsyncSessionLocal() as db2:
                    r2 = await db2.get(WorkflowRun, wid)
                    if r2:
                        r2.status = WorkflowRunStatus.FAILED.value
                        r2.error_message = str(e)[:2000]
                        r2.output_data = {"error_code": "batch_invoke", "error_message": str(e)[:2000]}
                        await upsert_compliance_call_run(db2, r2)
                        await db2.commit()
                results.append({"workflow_run_id": str(wid), "error": str(e)[:2000]})

    await asyncio.gather(*[one(it) for it in items])

    sheet_retries: list[dict[str, Any]] = []
    try:
        sheet_retries = await retry_failed_compliance_sheets(max_rows=5)
    except Exception:
        log.exception("compliance sheet retry sweep failed")

    return {
        "disabled": False,
        "candidates": len(items),
        "drive_candidates": len(drive_items),
        "mysql_candidates_in_tick": len([x for x in items if x.source == "mysql_call"]),
        "mysql_candidates_total_listed": len(mysql_items),
        "mysql_after_last_completed_call_id": mysql_after_last_completed_call_id,
        "results": results,
        "skipped": skipped,
        "sheet_retries": sheet_retries,
        "message": (
            f"batch: candidates={len(items)} drive_list={len(drive_items)} mysql_tick={len([x for x in items if x.source == 'mysql_call'])} "
            f"mysql_total={len(mysql_items)} finished={len(results)} skipped={len(skipped)}"
        ),
    }
