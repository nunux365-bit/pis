#!/usr/bin/env python3
"""
Invoke the O2C OHC workflow agent locally (same as POST /api/workflows/trigger with workflow_key=o2c_ohc).

Debugger setup (Cursor / VS Code):
  - Working directory: ``agentos-backend`` (so ``.env`` loads and ``app`` is importable).
  - Run / debug: ``python scripts/debug_o2c_agent_full.py``
  - Edit ``DEBUG_PAYLOAD`` below (paths + ``mis_template_path``); MIS needs output dir + template
    (see ``_node_mis`` in ``app.agents.o2c_ohc.graph``).

Contract ingest design: ``app.agents.o2c_ohc.product_flow.CONTRACT_INGEST_PRODUCT_FLOW``.
More entrypoints: ``docs/agentos/O2C_AGENT_TEST_ENTRYPOINTS.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Edit these for local runs (no CLI).
# ---------------------------------------------------------------------------
DEBUG_THREAD_ID = "debug-script"

DEBUG_PAYLOAD: dict[str, Any] = {
    # Phase A: absolute path to PDF folder, or Drive child name when using O2C_GDRIVE_* in .env.
    # Omit contracts_root only when O2C_GDRIVE_CONTRACTS_ALL_SITES=true and parent folder id is set.
    "contracts_root": "",
    # false = incremental by DB watermark (same as nightly cron); omit = scan all PDFs (sha256 dedupe).
    "ignore_mtime_watermark": False,
    # Phase B: OHC workbook (Summary tab). Omit to use O2C_ATTENDANCE_* / O2C_GDRIVE_ATTENDANCE_SHEET_* from .env.
    "attendance_xlsx": "",
    "period_start": "2026-05-01",
    "period_end": "2026-05-31",
    # MIS requires both output dir and template (or set O2C_INVOICE_OUT_DIR + O2C_INVOICE_MIS_TEMPLATE_PATH in .env).
    "invoice_out_dir": "",
    "mis_template_path": "",
}

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from app.agents.o2c_ohc.agent import run_o2c_ohc_agent

    result = run_o2c_ohc_agent(DEBUG_PAYLOAD, thread_id=DEBUG_THREAD_ID)

    print(json.dumps(result, indent=2, default=str))
    if result.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
