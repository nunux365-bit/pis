"""LangGraph: ingest → normalize → transcribe → rubric_eval → persist.

Vendor I/O: Google Drive download, FFmpeg, and Google Sheets stay on :func:`app.infra.sync_bridge.run_blocking`.
Deepgram (``AsyncDeepgramClient``) and OpenAI (``AsyncOpenAI`` / Responses API) run natively async from nodes.
SQLAlchemy remains async for persistence.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.compliance_call.adapters.input_gdrive import GDriveInputSource, WorkItem, build_compliance_drive_service
from app.agents.compliance_call.adapters.telephony_ingest_media import resolve_mysql_call_media_file
from app.agents.compliance_call.adapters.normalize_ffmpeg import normalize_audio_file
from app.agents.compliance_call.adapters.rubric_openai import run_rubric_eval, run_transcript_normalize
from app.agents.compliance_call.adapters.sheets_append import append_compliance_row, sheet_serial_no_for_state
from app.agents.compliance_call.adapters.transcribe_deepgram import transcribe_wav_to_result
from app.agents.compliance_call.mysql_leg_transcribe import transcribe_mysql_legs_merged
from app.agents.compliance_call.api_retry import vendor_client_refresh_recommended
from app.agents.compliance_call.doctor_slug import doctor_name_from_filename, doctor_slug_from_filename
from app.agents.compliance_call.vendor_clients import (
    close_deepgram_client_safely,
    close_openai_client_safely,
    create_compliance_deepgram_client,
    create_compliance_openai_client,
)
from app.config.settings import settings
from app.db.models import WorkflowRun, WorkflowRunStatus
from app.db.session import AsyncSessionLocal
from app.services.compliance_call_projection import upsert_compliance_call_run
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)

_compiled = None

MediaMode = Literal["file", "stream"]


class ComplianceCallState(TypedDict, total=False):
    workflow_run_id: str
    user_id: str
    run_started_perf: float
    drive_file_id: str
    revision: str
    filename: str
    mime: str
    relative_path: str
    doctor_slug: str
    media_mode: MediaMode
    source_media_path: str
    normalized_wav_path: str
    transcript_text: str
    grading_transcript: str
    normalized_transcript_text: str
    deepgram_summary: dict[str, Any]
    doctor_name: str
    eval: dict[str, Any]
    processing_duration_ms: int
    sheet_row: int
    error: str
    # Long-lived vendor SDK clients (one pair per graph run; not persisted to checkpoints).
    deepgram_client: Any
    openai_client: Any
    # --- MySQL / telephony ingest (optional) ---
    ingest_source: str
    mysql_call_id: int
    mysql_second_opinion_id: int
    mysql_second_opinion_conversation_id: int
    mysql_room_name: str
    mysql_metadata: dict[str, Any]
    mysql_call_updated_at: str
    mysql_call_legs: list[dict[str, Any]]
    mysql_transcript_merged: bool
    mysql_merged_call_ids: list[int]
    mysql_skipped_call_ids: list[dict[str, Any]]


async def _node_ingest(state: ComplianceCallState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    out_first: dict[str, Any] = {}
    if state.get("run_started_perf") is None:
        out_first["run_started_perf"] = time.perf_counter()
    t0 = time.perf_counter()
    try:
        ingest_src = str(state.get("ingest_source") or "gdrive")

        if ingest_src == "mysql_call":
            slug = str(state.get("doctor_slug") or "unknown")
            dname = str(state.get("doctor_name") or "")
            legs = state.get("mysql_call_legs")
            if isinstance(legs, list) and legs:
                dg = state.get("deepgram_client")
                oai = state.get("openai_client")
                if dg is None or oai is None:
                    return {"error": "internal: vendor clients missing for mysql leg merge"}
                merged_tr = await transcribe_mysql_legs_merged(
                    legs,
                    doctor_name=dname,
                    deepgram_client=dg,
                    openai_client=oai,
                )
                wid = uuid.UUID(str(state["workflow_run_id"]))
                async with AsyncSessionLocal() as db:
                    r = await db.get(WorkflowRun, wid)
                    if r:
                        prev = r.input_data if isinstance(r.input_data, dict) else {}
                        mid = state.get("mysql_call_id")
                        r.input_data = {
                            **prev,
                            "source": "mysql_call",
                            "source_type": "mysql",
                            "mysql_call_id": mid,
                            "source_file_id": str(mid) if mid is not None else None,
                            "filename": str(state.get("filename") or ""),
                            "mime": str(state.get("mime") or ""),
                            "relative_path": str(state.get("relative_path") or ""),
                            "doctor_slug": slug,
                            "doctor_name": dname,
                            "mysql_merged_call_ids": merged_tr.get("mysql_merged_call_ids"),
                        }
                        await upsert_compliance_call_run(db, r)
                        await db.commit()
                log.info(
                    "compliance_call mysql merged ingest ok workflow_run_id=%s legs_used=%s ms=%d",
                    state.get("workflow_run_id"),
                    merged_tr.get("mysql_merged_call_ids"),
                    int((time.perf_counter() - t0) * 1000),
                )
                out_merged: dict[str, Any] = {
                    **out_first,
                    "mysql_transcript_merged": True,
                    "doctor_slug": slug,
                    "doctor_name": dname,
                    "transcript_text": merged_tr.get("transcript_text"),
                    "grading_transcript": merged_tr.get("grading_transcript"),
                    "deepgram_summary": merged_tr.get("deepgram_summary"),
                    "mysql_merged_call_ids": merged_tr.get("mysql_merged_call_ids"),
                    "mysql_skipped_call_ids": merged_tr.get("mysql_skipped_call_ids"),
                }
                if merged_tr.get("openai_client") is not None:
                    out_merged["openai_client"] = merged_tr["openai_client"]
                return out_merged

            path = await resolve_mysql_call_media_file(
                room_name=state.get("mysql_room_name") or None,
                metadata_raw=state.get("mysql_metadata"),
            )
            wid = uuid.UUID(str(state["workflow_run_id"]))
            async with AsyncSessionLocal() as db:
                r = await db.get(WorkflowRun, wid)
                if r:
                    prev = r.input_data if isinstance(r.input_data, dict) else {}
                    mid = state.get("mysql_call_id")
                    r.input_data = {
                        **prev,
                        "source": "mysql_call",
                        "source_type": "mysql",
                        "mysql_call_id": mid,
                        "source_file_id": str(mid) if mid is not None else None,
                        "filename": str(state.get("filename") or ""),
                        "mime": str(state.get("mime") or ""),
                        "relative_path": str(state.get("relative_path") or ""),
                        "doctor_slug": slug,
                        "doctor_name": dname,
                    }
                    await upsert_compliance_call_run(db, r)
                    await db.commit()
            log.info(
                "compliance_call mysql ingest ok workflow_run_id=%s mysql_call_id=%s ms=%d",
                state.get("workflow_run_id"),
                str(state.get("mysql_call_id")),
                int((time.perf_counter() - t0) * 1000),
            )
            return {
                **out_first,
                "source_media_path": str(path),
                "doctor_slug": slug,
                "doctor_name": dname,
            }

        folder = (settings.compliance_call_gdrive_folder_id or "").strip()
        if not folder:
            return {"error": "COMPLIANCE_CALL_GDRIVE_FOLDER_ID is not set"}
        item = WorkItem(
            drive_file_id=str(state.get("drive_file_id") or ""),
            revision=str(state.get("revision") or ""),
            filename=str(state.get("filename") or "file"),
            mime=str(state.get("mime") or ""),
            relative_path=str(state.get("relative_path") or ""),
            media_mode=state.get("media_mode") or "file",
        )

        def _drive_download() -> tuple[Path, str, str, WorkItem]:
            svc = build_compliance_drive_service()
            src = GDriveInputSource(svc=svc, folder_id=folder)
            pth = src.open_media_path(item)
            slug = doctor_slug_from_filename(item.filename)
            dname = doctor_name_from_filename(item.filename)
            return pth, slug, dname, item

        path, slug, dname, item = await run_blocking(_drive_download)

        wid = uuid.UUID(str(state["workflow_run_id"]))
        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, wid)
            if r:
                prev = r.input_data if isinstance(r.input_data, dict) else {}
                r.input_data = {
                    **prev,
                    "source": "gdrive",
                    "drive_file_id": item.drive_file_id,
                    "revision": item.revision,
                    "mime": item.mime,
                    "filename": item.filename,
                    "doctor_slug": slug,
                    "doctor_name": dname,
                    "relative_path": item.relative_path,
                }
                await upsert_compliance_call_run(db, r)
                await db.commit()
        log.info(
            "compliance_call ingest ok workflow_run_id=%s drive_file_id=%s ms=%d",
            state.get("workflow_run_id"),
            item.drive_file_id,
            int((time.perf_counter() - t0) * 1000),
        )
        return {
            **out_first,
            "source_media_path": str(path),
            "doctor_slug": slug,
            "doctor_name": dname,
        }
    except Exception as e:
        log.exception("compliance_call ingest failed")
        return {**out_first, "error": f"{type(e).__name__}: {e}"[:2000]}


async def _node_normalize(state: ComplianceCallState) -> dict[str, Any]:
    if state.get("mysql_transcript_merged"):
        return {}
    if state.get("error") or not state.get("source_media_path"):
        return {}
    mode = state.get("media_mode") or "file"
    try:
        p = Path(state["source_media_path"])

        def _norm() -> Path:
            return normalize_audio_file(
                p,
                loudnorm=settings.compliance_ffmpeg_loudnorm,
                streaming_mode=(mode == "stream"),
            )

        wav = await run_blocking(_norm)
        return {"normalized_wav_path": str(wav)}
    except Exception as e:
        log.exception("compliance_call normalize failed")
        return {"error": f"{type(e).__name__}: {e}"[:2000]}


async def _node_transcribe(state: ComplianceCallState) -> dict[str, Any]:
    if state.get("mysql_transcript_merged"):
        return {}
    if state.get("error") or not state.get("normalized_wav_path"):
        return {}
    wav_path = Path(state["normalized_wav_path"])
    dg = state.get("deepgram_client")
    oai = state.get("openai_client")
    if dg is None or oai is None:
        return {"error": "internal: vendor clients missing from compliance graph state"}

    for attempt in range(2):
        try:
            tr = await transcribe_wav_to_result(
                wav_path,
                doctor_name_hint=str(state.get("doctor_name") or ""),
                deepgram_client=dg,
                openai_client=oai,
            )
            canon = str(tr.get("transcript_text") or "")
            grading = str(tr.get("grading_transcript") or canon)
            out: dict[str, Any] = {
                "transcript_text": canon,
                "grading_transcript": grading,
                "deepgram_summary": tr.get("deepgram_summary") if isinstance(tr.get("deepgram_summary"), dict) else {},
            }
            if attempt > 0:
                out["deepgram_client"] = dg
            refreshed_oai = tr.get("openai_client")
            if refreshed_oai is not None:
                out["openai_client"] = refreshed_oai
            return out
        except Exception as e:
            if attempt == 0 and vendor_client_refresh_recommended(e, vendor="deepgram"):
                log.warning(
                    "compliance_call transcribe: refreshing Deepgram client after %s: %s",
                    type(e).__name__,
                    e,
                )
                await close_deepgram_client_safely(dg)
                try:
                    dg = create_compliance_deepgram_client()
                except Exception as e2:
                    log.exception("compliance_call transcribe: failed to recreate Deepgram client")
                    # Old client already closed; nothing new to attach.
                    return {"error": f"{type(e2).__name__}: {e2}"[:2000]}
                continue
            log.exception("compliance_call transcribe failed")
            # After refresh, return replacement so run_single finally closes it.
            err: dict[str, Any] = {"error": f"{type(e).__name__}: {e}"[:2000]}
            if attempt > 0:
                err["deepgram_client"] = dg
            return err
    return {"error": "compliance_call transcribe: exhausted retries", "deepgram_client": dg}


async def _node_rubric_eval(state: ComplianceCallState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    src = str(
        state.get("normalized_transcript_text")
        or state.get("grading_transcript")
        or state.get("transcript_text")
        or ""
    ).strip()
    min_c = int(settings.compliance_min_transcript_chars or 0)
    if min_c > 0 and len(src) < min_c:
        return {
            "error": (
                f"TRANSCRIPT_TOO_SHORT: len={len(src)} min_required={min_c} "
                "(no usable speech for rubric)"
            )[:2000]
        }

    dg = (
        state.get("deepgram_summary")
        if isinstance(state.get("deepgram_summary"), dict)
        else None
    )

    oai = state.get("openai_client")
    if oai is None:
        return {"error": "internal: OpenAI client missing from compliance graph state"}

    for attempt in range(2):
        try:
            doc = await run_rubric_eval(
                transcript_text=src,
                doctor_slug=str(state.get("doctor_slug") or "unknown"),
                doctor_name=str(state.get("doctor_name") or ""),
                rubric_version=settings.compliance_rubric_version,
                deepgram_summary=dg,
                openai_client=oai,
            )
            out_ev: dict[str, Any] = {"eval": doc}
            if attempt > 0:
                out_ev["openai_client"] = oai
            return out_ev
        except Exception as e:
            if attempt == 0 and vendor_client_refresh_recommended(e, vendor="openai"):
                log.warning(
                    "compliance_call rubric_eval: refreshing OpenAI client after %s: %s",
                    type(e).__name__,
                    e,
                )
                await close_openai_client_safely(oai)
                try:
                    oai = create_compliance_openai_client(timeout_sec=600.0)
                except Exception as e2:
                    log.exception("compliance_call rubric_eval: failed to recreate OpenAI client")
                    return {"error": f"{type(e2).__name__}: {e2}"[:2000]}
                continue
            log.exception("compliance_call rubric_eval failed")
            err: dict[str, Any] = {"error": f"{type(e).__name__}: {e}"[:2000]}
            if attempt > 0:
                err["openai_client"] = oai
            return err
    return {"error": "compliance_call rubric_eval: exhausted retries", "openai_client": oai}


async def _node_transcript_normalize(state: ComplianceCallState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    src = str(state.get("grading_transcript") or state.get("transcript_text") or "").strip()
    if not src:
        return {}
    if not settings.compliance_transcript_normalize_enabled:
        return {}

    oai = state.get("openai_client")
    if oai is None:
        return {"error": "internal: OpenAI client missing from compliance graph state"}

    for attempt in range(2):
        try:
            out = await run_transcript_normalize(transcript_text=src, openai_client=oai)
            if not out.strip():
                # Still re-attach refreshed client so finally owns the live instance.
                return {"openai_client": oai} if attempt > 0 else {}
            res: dict[str, Any] = {"normalized_transcript_text": out}
            if attempt > 0:
                res["openai_client"] = oai
            return res
        except Exception as e:
            if attempt == 0 and vendor_client_refresh_recommended(e, vendor="openai"):
                log.warning(
                    "compliance_call transcript_normalize: refreshing OpenAI client after %s: %s",
                    type(e).__name__,
                    e,
                )
                await close_openai_client_safely(oai)
                try:
                    oai = create_compliance_openai_client(timeout_sec=600.0)
                except Exception as e2:
                    log.exception("compliance_call transcript_normalize: failed to recreate OpenAI client")
                    return {"error": f"{type(e2).__name__}: {e2}"[:2000]}
                continue
            log.exception("compliance_call transcript_normalize failed")
            err: dict[str, Any] = {"error": f"{type(e).__name__}: {e}"[:2000]}
            if attempt > 0:
                err["openai_client"] = oai
            return err
    return {"error": "compliance_call transcript_normalize: exhausted retries", "openai_client": oai}


async def _node_persist(state: ComplianceCallState) -> dict[str, Any]:
    wid = uuid.UUID(str(state["workflow_run_id"]))
    err = state.get("error")
    t0 = state.get("run_started_perf")
    duration_ms = int((time.perf_counter() - t0) * 1000) if t0 else int(state.get("processing_duration_ms") or 0)

    if err:
        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, wid)
            if not r:
                _cleanup_temp_paths(state)
                return {}
            r.status = WorkflowRunStatus.FAILED.value
            r.error_message = err[:2000]
            r.output_data = {
                "error_code": "pipeline_error",
                "error_message": err[:2000],
                "processing_duration_ms": duration_ms,
            }
            await upsert_compliance_call_run(db, r)
            await db.commit()
        _cleanup_temp_paths(state)
        return {}

    eval_doc = state.get("eval") if isinstance(state.get("eval"), dict) else {}
    dg = state.get("deepgram_summary") if isinstance(state.get("deepgram_summary"), dict) else {}
    canon_t = str(state.get("transcript_text") or "").strip()
    grad_t = str(state.get("grading_transcript") or "").strip()
    norm_t = str(state.get("normalized_transcript_text") or "").strip()
    out: dict[str, Any] = {
        "transcript_text": str(state.get("transcript_text") or ""),
        "normalized_transcript_text": str(state.get("normalized_transcript_text") or ""),
        "transcript_grading_source": (
            "normalized"
            if norm_t
            else ("utterances" if grad_t and grad_t != canon_t else "canonical")
        ),
        "deepgram_summary": dg,
        "eval": eval_doc,
        "sheet_appended": False,
        "processing_duration_ms": duration_ms,
    }
    merged_ids = state.get("mysql_merged_call_ids")
    if isinstance(merged_ids, list) and merged_ids:
        out["mysql_merged_call_ids"] = merged_ids
    skipped_legs = state.get("mysql_skipped_call_ids")
    if isinstance(skipped_legs, list) and skipped_legs:
        out["mysql_skipped_call_ids"] = skipped_legs

    already_appended = False
    async with AsyncSessionLocal() as db:
        r = await db.get(WorkflowRun, wid)
        if not r:
            _cleanup_temp_paths(state)
            return {}
        existing = r.output_data if isinstance(r.output_data, dict) else {}
        if existing.get("sheet_appended"):
            already_appended = True
            out["sheet_appended"] = True
            out["sheet_row"] = existing.get("sheet_row")
        r.status = WorkflowRunStatus.COMPLETED.value
        r.error_message = None
        r.output_data = out
        await upsert_compliance_call_run(db, r)
        await db.commit()

    sheet_id = (settings.compliance_sheet_id or "").strip()
    if not already_appended and sheet_id:
        stamp: dict[str, Any]
        try:

            def _append():
                return append_compliance_row(
                    workflow_run_id=str(wid),
                    doctor_slug=str(state.get("doctor_slug") or ""),
                    doctor_name=str(state.get("doctor_name") or ""),
                    drive_file_id=str(state.get("drive_file_id") or ""),
                    filename=str(state.get("filename") or ""),
                    relative_path=str(state.get("relative_path") or ""),
                    eval_doc=eval_doc,
                    status="completed",
                    deepgram_summary=dg,
                    mysql_call_updated_at_raw=str(state.get("mysql_call_updated_at") or "").strip()
                    or None,
                    sheet_serial_no=sheet_serial_no_for_state(
                        ingest_source=str(state.get("ingest_source") or ""),
                        mysql_second_opinion_conversation_id=state.get(
                            "mysql_second_opinion_conversation_id"
                        ),
                    ),
                )

            rown = await run_blocking(_append)
            stamp = {"sheet_appended": True, "sheet_row": rown}
        except Exception as e:
            log.warning(
                "compliance_call sheet append failed run=%s: %s",
                wid,
                e,
            )
            stamp = {"sheet_appended": False, "sheet_sync_error": str(e)[:500]}
        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, wid)
            if r:
                existing = r.output_data if isinstance(r.output_data, dict) else {}
                r.output_data = {**existing, **stamp}
                await upsert_compliance_call_run(db, r)
                await db.commit()

    _cleanup_temp_paths(state)
    return {}


def _cleanup_temp_paths(state: ComplianceCallState) -> None:
    for key in ("source_media_path", "normalized_wav_path"):
        p = state.get(key)
        if p:
            try:
                Path(str(p)).unlink(missing_ok=True)
            except Exception:
                pass


def build_compliance_call_graph():
    global _compiled
    if _compiled is not None:
        return _compiled

    g = StateGraph(ComplianceCallState)

    g.add_node("ingest", _node_ingest)
    g.add_node("normalize", _node_normalize)
    g.add_node("transcribe", _node_transcribe)
    g.add_node("transcript_normalize", _node_transcript_normalize)
    g.add_node("rubric_eval", _node_rubric_eval)
    g.add_node("persist", _node_persist)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "normalize")
    g.add_edge("normalize", "transcribe")
    g.add_edge("transcribe", "transcript_normalize")
    g.add_edge("transcript_normalize", "rubric_eval")
    g.add_edge("rubric_eval", "persist")
    g.add_edge("persist", END)

    _compiled = g.compile()
    return _compiled


async def run_single_compliance_call(state: ComplianceCallState) -> dict[str, Any]:
    """Run the linear graph for one claimed workflow run."""
    graph = build_compliance_call_graph()
    merged: dict[str, Any] = dict(state)

    try:
        if merged.get("deepgram_client") is None:
            merged["deepgram_client"] = create_compliance_deepgram_client()
        if merged.get("openai_client") is None:
            merged["openai_client"] = create_compliance_openai_client(timeout_sec=600.0)
    except Exception as e:
        await close_deepgram_client_safely(merged.get("deepgram_client"))
        await close_openai_client_safely(merged.get("openai_client"))
        return {"error": f"{type(e).__name__}: {e}"[:2000], "wall_clock_ms": 0}

    t0 = time.perf_counter()
    final: dict[str, Any] | None = None
    try:
        final = await graph.ainvoke(merged)
    finally:
        src = final if isinstance(final, dict) else merged
        await close_openai_client_safely(src.get("openai_client"))
        await close_deepgram_client_safely(src.get("deepgram_client"))

    wall_ms = int((time.perf_counter() - t0) * 1000)
    out = final if isinstance(final, dict) else {"result": final}
    if isinstance(out, dict):
        out = {**out, "wall_clock_ms": wall_ms}
    return out if isinstance(out, dict) else {"result": out}
