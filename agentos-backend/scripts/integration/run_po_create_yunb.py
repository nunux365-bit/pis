#!/usr/bin/env python3
"""Live integration: create YUNB PO via AgentOS API.

Thin wrapper around ``run_po_live.py`` for backward compatibility.

Usage (agentos-backend/):

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_create_yunb.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_create_yunb.py --parent-pr-sap 1040000063
  python scripts/integration/run_po_create_yunb.py --dry-run --preview-sap
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.run_po_live import main


if __name__ == "__main__":
    if "--doc-type" not in sys.argv:
        sys.argv[1:1] = ["--doc-type", "YUNB"]
    raise SystemExit(main())
