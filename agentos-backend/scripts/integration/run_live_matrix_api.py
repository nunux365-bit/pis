#!/usr/bin/env python3
"""Full live matrix: PR CRUD (API) → standalone PO (API) → PO-from-PR (API).

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_live_matrix_api.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_live_matrix_api.py --phase pr
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
    load_po_db_context,
)
from scripts.integration.procurement_api_common import add_common_args, load_env, resolve_token_from_args


def _run_signoff_script(script: str, layer: str = "api") -> int:
    cmd = [
        sys.executable,
        str(_ROOT / "scripts" / "integration" / script),
        "--layer",
        layer,
    ]
    print(f"\n>>> Running {script} --layer {layer}\n")
    return subprocess.call(cmd, cwd=str(_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description="Full API live matrix (PR → PO → PO-from-PR)")
    add_common_args(p)
    p.add_argument(
        "--phase",
        choices=["all", "pr", "po", "po-from-pr"],
        default="all",
        help="Run one phase or all (default: all)",
    )
    args = p.parse_args()
    load_env()
    os.environ["AGENTOS_INTEGRATION_MINT_TOKEN"] = "1"
    resolve_token_from_args(args)
    exit_code = 0

    if args.phase in ("all", "pr"):
        applied = apply_integration_reference_defaults()
        print(integration_defaults_summary(applied))
        code = _run_signoff_script("run_pr_standalone_signoff_live.py")
        if code:
            exit_code = code
        if args.phase == "pr":
            return exit_code

    if args.phase in ("all", "po"):
        if args.phase == "po":
            from scripts.integration.integration_reference_defaults import load_po_db_context

            ctx = load_po_db_context(apply_env=True)
            print(
                "PO DB context: "
                f"org={ctx.purchasing_org!r} vendor={ctx.vendor!r} "
                f"pay={ctx.payment_terms!r} mat={ctx.material!r}"
            )
        code = _run_signoff_script("run_po_standalone_signoff_live.py")
        if code:
            exit_code = code
        if args.phase == "po":
            return exit_code

    if args.phase in ("all", "po-from-pr"):
        code = _run_signoff_script("run_po_from_pr_signoff_live.py")
        if code:
            exit_code = code

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
