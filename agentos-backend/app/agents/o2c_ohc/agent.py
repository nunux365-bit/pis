"""
O2C_OHC Agent — same LangGraph as ``graph.run_o2c_full_graph``, exposed for the workflow API.

Phase A (contracts): each PDF follows ``product_flow.CONTRACT_INGEST_PRODUCT_FLOW``
(LLM → file-truth normalize → validate → agenos inserts via ``pipeline`` / ``ingest``).

The CLI remains the lightweight ops/debug surface (parse-only, MIS audit, roll export).
This module is what ``POST /api/workflows/trigger`` runs when ``workflow_key`` matches
``O2C_OHC_WORKFLOW_KEYS`` in ``app.services.workflow_runner``.

Local test entrypoint: ``scripts/debug_o2c_agent.py`` (see ``docs/agentos/O2C_AGENT_TEST_ENTRYPOINTS.md``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.o2c_ohc.graph import run_o2c_full_graph

log = logging.getLogger(__name__)


def _parse_ignore_mtime_watermark(raw: dict[str, Any]) -> bool | None:
    v = raw.get("ignore_mtime_watermark")
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _payload_to_graph_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize trigger payload keys → graph state (empty string values omitted)."""
    aliases = [
        ("contracts_root", ("contracts_root", "o2c_contracts_root")),
        (
            "attendance_xlsx",
            (
                "attendance_xlsx",
                "attendance_path",
                "o2c_attendance_xlsx_path",
                "o2c_ohc_attendance_xlsx_path",
            ),
        ),
        ("period_start", ("period_start", "period_from", "billing_period_start")),
        ("period_end", ("period_end", "period_to", "billing_period_end")),
        ("invoice_out_dir", ("invoice_out_dir", "invoice_out", "o2c_invoice_out_dir")),
        ("mis_out_dir", ("mis_out_dir",)),
        ("mis_template_path", ("mis_template_path", "template_path", "o2c_invoice_mis_template_path")),
    ]
    state: dict[str, Any] = {}
    for out_key, keys in aliases:
        for k in keys:
            v = raw.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                state[out_key] = s
                break
    imw = _parse_ignore_mtime_watermark(raw)
    if imw is not None:
        state["ignore_mtime_watermark"] = imw
    return state


def run_o2c_ohc_agent(
    input_data: dict | None,
    *,
    thread_id: str = "default",
) -> dict[str, Any]:
    """
    Run Phase A (contracts → agenos) then Phase B (attendance xlsx → invoices + MIS + recon).

    Expected payload keys (all optional; missing pieces use ``settings`` or skip that phase):

    - ``contracts_root`` — local PDF directory, or Drive child folder name; omit when using
      ``O2C_GDRIVE_CONTRACTS_ALL_SITES`` (Phase A).
    - ``ignore_mtime_watermark`` — optional bool; ``false`` for incremental contract scan (same as
      nightly cron). Omit to use pipeline default (``true``: all PDFs, dedupe by sha256).
    - ``attendance_xlsx`` — optional path to OHC workbook (Phase B); if omitted, uses env local path or
      ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID`` export.
    - ``period_start`` / ``period_end`` — ``YYYY-MM-DD`` (Phase B).
    - ``invoice_out_dir`` — MIS + recon output directory (Phase B).

    Returns a dict consumed by ``run_automation_phase`` (``o2c_result``, ``graph_error``, …).
    """
    try:
        raw = dict(input_data or {})
        initial = _payload_to_graph_state(raw)
        out = run_o2c_full_graph(initial if initial else None)
        errs = list(out.get("errors") or [])
        return {
            "graph": "o2c_ohc.full_pipeline_v1",
            "thread_id": thread_id[:80],
            "o2c_result": dict(out),
            "error": "; ".join(errs) if errs else None,
        }
    except Exception as e:
        log.exception("O2C OHC agent failed")
        return {
            "graph": "o2c_ohc.full_pipeline_v1",
            "thread_id": thread_id[:80],
            "o2c_result": None,
            "error": str(e)[:2000],
        }
