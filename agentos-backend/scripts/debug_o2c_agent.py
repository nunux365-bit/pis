#!/usr/bin/env python3
"""
Run **only** the O2C LangGraph **MIS** node locally (no contract PDF ingest, no shared-resource step).

Same behavior as the MIS step in the full ``o2c_ohc`` graph: ``.env`` / ``settings`` drive
attendance resolution (local path, ``O2C_ATTENDANCE_*``, or ``O2C_GDRIVE_ATTENDANCE_SHEET_URL_OR_ID``
export), MIS template/output dir defaults, optional MIS upload to Drive
(``O2C_GDRIVE_MIS_PARENT_FOLDER_ID``, service account JSON, etc.), and MIS auto-approve when
``O2C_MIS_AUTO_APPROVE_ENABLED`` is true (see ``attach_auto_approve_after_draft`` in the graph MIS node).

Debugger: cwd = ``agentos-backend``, run ``python scripts/debug_o2c_agent.py``.

For the **full** graph (contracts → MIS), use ``POST /api/workflows/trigger`` with ``workflow_key=o2c_ohc``
or call ``run_o2c_full_graph`` from ``app.agents.o2c_ohc.graph``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Edit here: billing period (required) and optional path overrides.
# Leave strings empty to use settings / .env (same as the graph MIS node).
# ---------------------------------------------------------------------------
DEBUG_MIS_STATE: dict[str, Any] = {
    "period_start": "2026-05-01",
    "period_end": "2026-05-31",
    # Optional overrides (omit key or use "" to rely on .env / settings — same as graph MIS node).
    "attendance_xlsx": "",
    "invoice_out_dir": "",
    "mis_out_dir": "",
    "mis_template_path": "",
}

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from app.agents.o2c_ohc.graph import run_o2c_mis_node_only

    cfg = dict(DEBUG_MIS_STATE)
    ps = (cfg.get("period_start") or "").strip()
    pe = (cfg.get("period_end") or "").strip()
    if not ps or not pe:
        print("Set period_start and period_end in DEBUG_MIS_STATE (YYYY-MM-DD).", file=sys.stderr)
        return 1

    state: dict[str, Any] = {"period_start": ps, "period_end": pe}
    for key in ("attendance_xlsx", "invoice_out_dir", "mis_out_dir", "mis_template_path"):
        v = (cfg.get(key) or "").strip()
        if v:
            state[key] = v

    fragment = run_o2c_mis_node_only(state)
    print(json.dumps(fragment, indent=2, default=str))
    errs = fragment.get("errors") or []
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
