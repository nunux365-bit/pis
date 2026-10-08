"""
LangGraph wrapper: Phase A contract ingest (``product_flow`` + ``pipeline``) → shared-resource resolve → MIS.

Pipeline ends at MIS draft generation; invoice batching from attendance is not part of this graph.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph

from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records
from app.agents.o2c_ohc.mis_drafts import create_or_refresh_mis_draft_for_site_async
from app.agents.o2c_ohc.agenos_async_session import run_agenos_async
from app.agents.o2c_ohc.mis_db import client_site_keys_with_existing_mis_summary
from app.agents.o2c_ohc.mis_gdrive_finalize import (
    build_mis_drive_svc,
    finalize_mis_xlsx_to_gdrive_after_draft_async,
    unlink_o2c_temp_paths,
)
from app.agents.o2c_ohc.pipeline import run_o2c_pipeline
from app.agents.o2c_ohc.shared_resource_resolver import resolve_shared_resources_for_existing_contracts
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking
from app.services.o2c.attendance_workbook_cache import load_parsed_ohc_attendance_workbook_cached
from app.services.o2c.mis_auto_approve import attach_auto_approve_after_draft, try_auto_approve_mis_run


class O2CGraphState(TypedDict, total=False):
    contracts_root: str
    ignore_mtime_watermark: bool
    pipeline_result: dict
    attendance_xlsx: str
    period_start: str
    period_end: str
    invoice_out_dir: str  # backwards-compatible alias for mis_out_dir
    mis_out_dir: str
    mis_template_path: str
    mis_results: list[dict]
    shared_resource_resolver_result: dict
    errors: list[str]


async def _node_contracts(state: O2CGraphState) -> dict:
    root = (state.get("contracts_root") or settings.o2c_contracts_root or "").strip()
    errs = list(state.get("errors") or [])
    gdrive_parent = (settings.o2c_gdrive_contracts_parent_folder_id or "").strip()
    ingestion_db = (settings.o2c_contracts_ingestion_root_db or "").strip()
    drive_all_sites = bool(settings.o2c_gdrive_contracts_all_sites)
    run_drive_all = bool(gdrive_parent and ingestion_db and drive_all_sites and not root)
    if not root and not run_drive_all:
        return {
            "pipeline_result": {
                "skipped": True,
                "contracts_root": "",
                "candidates": 0,
                "ok": 0,
                "failed": 0,
                "results": [],
            }
        }

    def _pipeline() -> dict:
        kw: dict[str, Any] = {"contracts_root": root if root else None}
        if "ignore_mtime_watermark" in state:
            kw["ignore_mtime_watermark"] = bool(state["ignore_mtime_watermark"])
        return run_o2c_pipeline(**kw)

    try:
        pr = await run_blocking(_pipeline)
        failed = int(pr.get("failed") or 0)
        if failed:
            errs.append(
                f"contracts: {failed} PDF(s) failed ingest (see pipeline_result.results[].reason)"
            )
        return {"pipeline_result": pr, "errors": errs}
    except Exception as e:
        errs.append(str(e))
        return {"errors": errs, "pipeline_result": {"candidates": 0, "ok": 0, "failed": 0, "results": []}}


async def _node_mis(state: O2CGraphState) -> dict:
    errs = list(state.get("errors") or [])
    cleanup_paths: list[Path] = []
    ps = (state.get("period_start") or "").strip()
    pe = (state.get("period_end") or "").strip()

    try:
        if not (ps and pe):
            return {"mis_results": []}
        try:
            d0 = date.fromisoformat(ps)
            d1 = date.fromisoformat(pe)
        except ValueError as e:
            errs.append(f"invalid period: {e}")
            return {"errors": errs, "mis_results": []}

        def _drive_load_parse() -> tuple[Any, Any]:
            mis_drive_svc = build_mis_drive_svc()
            parsed = load_parsed_ohc_attendance_workbook_cached(
                explicit=(state.get("attendance_xlsx") or "").strip() or None,
                cleanup_paths=cleanup_paths,
                drive_svc=mis_drive_svc,
                engine="auto",
            )
            return mis_drive_svc, parsed

        try:
            mis_drive_svc, parsed_wb = await run_blocking(_drive_load_parse)
        except Exception as e:
            errs.append(f"attendance: {e}")
            return {"errors": errs, "mis_results": []}

        amap = parsed_wb.amap
        if not amap:
            return {"mis_results": []}

        out = (
            state.get("mis_out_dir")
            or state.get("invoice_out_dir")
            or settings.o2c_invoice_out_dir
            or ""
        ).strip()
        template = (state.get("mis_template_path") or settings.o2c_invoice_mis_template_path or "").strip()
        if not (out and template):
            errs.append("mis: missing mis_out_dir or mis_template_path")
            return {"errors": errs, "mis_results": []}

        out_dir = Path(out)
        template_path = Path(template)
        try:
            existing_summary_by_key = await client_site_keys_with_existing_mis_summary(
                period_start=d0,
                period_end=d1,
            )
        except Exception as e:
            errs.append(f"mis: existing-summary lookup failed: {e}")
            existing_summary_by_key = {}

        results: list[dict] = []
        for site_key in sorted(amap.keys()):
            sk = (site_key or "").strip()
            if sk and sk in existing_summary_by_key:
                existing = existing_summary_by_key[sk]
                entry: dict[str, Any] = {
                    "client_site_key": site_key,
                    "status": "skipped",
                    "reason": "mis_summary_exists",
                    "mis_run_id": existing.get("mis_run_id"),
                    "xlsx_path": None,
                }
                if str(existing.get("status") or "") == "pending_human" and existing.get("mis_run_id"):
                    try:
                        entry["auto_approve"] = await try_auto_approve_mis_run(
                            UUID(str(existing["mis_run_id"]))
                        )
                    except Exception as e:
                        entry["auto_approve"] = {
                            "attempted": True,
                            "approved": False,
                            "error": str(e),
                        }
                else:
                    entry["auto_approve"] = {
                        "attempted": False,
                        "approved": False,
                        "reason": "mis_summary_exists_not_pending_human",
                    }
                results.append(entry)
                continue

            rows = amap.get(site_key) or []
            detailed_json = build_detailed_json_from_records(
                site_key,
                rows,
                period_start=d0,
                period_end=d1,
            )
            r = await create_or_refresh_mis_draft_for_site_async(
                client_site_key=site_key,
                attendance_records=rows,
                detailed_json=detailed_json,
                period_start=d0,
                period_end=d1,
                out_dir=out_dir,
                template_path=template_path,
                attendance_workbook_path=parsed_wb.resolved_path,
                allow_expired_contract_terms=bool(
                    getattr(settings, "o2c_mis_allow_expired_contract_terms", False)
                ),
            )
            xlsx_ref = r.xlsx_path
            if r.status == "ok" and r.mis_run_id and r.xlsx_path:
                xlsx_ref, upload_err = await finalize_mis_xlsx_to_gdrive_after_draft_async(
                    mis_run_id=r.mis_run_id,
                    period_start=d0,
                    xlsx_path=r.xlsx_path,
                    drive_svc=mis_drive_svc,
                )
                if upload_err:
                    errs.append(f"mis gdrive upload ({site_key}): {upload_err}")
            entry: dict[str, Any] = {
                "client_site_key": r.client_site_key,
                "status": r.status,
                "reason": r.reason,
                "mis_run_id": r.mis_run_id,
                "xlsx_path": xlsx_ref,
            }
            entry = await attach_auto_approve_after_draft(
                entry,
                mis_run_id=r.mis_run_id,
                draft_status=str(r.status or ""),
            )
            results.append(entry)
        return {
            "mis_results": results,
            "errors": errs,
        }
    finally:
        await run_blocking(lambda: unlink_o2c_temp_paths(cleanup_paths))


async def _node_shared_resource_resolve(state: O2CGraphState) -> dict:
    """
    Non-blocking resolver stage between contract ingest and MIS generation.
    Populates/updates shared_resource metadata on already ingested ambulance lines.
    """
    errs = list(state.get("errors") or [])
    try:
        out = await resolve_shared_resources_for_existing_contracts()
        return {"shared_resource_resolver_result": out, "errors": errs}
    except Exception as e:
        # Resolver should not block MIS; record and continue.
        errs.append(f"shared_resource_resolver: {e}")
        return {"shared_resource_resolver_result": {"ok": False, "error": str(e)}, "errors": errs}


def build_o2c_ohc_graph():
    g = StateGraph(O2CGraphState)
    g.add_node("contracts", _node_contracts)
    g.add_node("shared_resource_resolve", _node_shared_resource_resolve)
    g.add_node("mis", _node_mis)
    g.set_entry_point("contracts")
    g.add_edge("contracts", "shared_resource_resolve")
    g.add_edge("shared_resource_resolve", "mis")
    g.add_edge("mis", END)
    return g.compile()


async def run_o2c_full_graph_async(initial: O2CGraphState | None = None) -> O2CGraphState:
    app = build_o2c_ohc_graph()
    return await app.ainvoke(initial or {})


def run_o2c_full_graph(initial: O2CGraphState | None = None) -> O2CGraphState:
    """Invoke the full O2C_OHC graph with optional initial state overrides (sync; no running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_o2c_full_graph_async(initial))
    raise RuntimeError(
        "run_o2c_full_graph() must not be used under a running event loop; use run_o2c_full_graph_async instead."
    )


def run_o2c_mis_node_only(state: O2CGraphState | None = None) -> dict[str, Any]:
    """
    Run only the LangGraph **mis** node (no contract ingest, no shared-resource resolver).

    Uses the same code path as the full graph MIS step: ``settings`` + ``.env`` for
    ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID``, service account, MIS parent folder, template,
    output dir, etc. State keys: ``period_start`` / ``period_end`` (``YYYY-MM-DD``), optional
    ``attendance_xlsx``, ``invoice_out_dir`` / ``mis_out_dir``, ``mis_template_path``.
    """
    return run_agenos_async(_node_mis(state or {}))
