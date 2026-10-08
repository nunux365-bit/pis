#!/usr/bin/env python3
"""One entry point for SAP workshop: simple / complex PR create via AgentOS API.

Auth: auto-mints JWT for login@1mg.com from Postgres (no --mint-token needed).
Override: AGENTOS_INTEGRATION_EMAIL, AGENTOS_ACCESS_TOKEN, or --email/--password.

Usage (from agentos-backend/):

  python scripts/integration/run_sap_test.py YAST simple
  python scripts/integration/run_sap_test.py YAST complex
  python scripts/integration/run_sap_test.py YSER simple
  python scripts/integration/run_sap_test.py all
  python scripts/integration/run_sap_test.py YAST simple --dry-run --preview-sap

See docs/procurement_sap_integration_playbook.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import integration_defaults_summary
from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults
from scripts.integration.procurement_api_common import (
    create_pr_via_api,
    load_env,
    preview_sap_payload,
    print_ticket_result,
    resolve_access_token,
)
from scripts.integration.run_pr_create_yast import (
    simple_yast_form,
    yast_multi_cc_form,
    yast_two_material_form,
)
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form
from scripts.integration.run_pr_create_yunb import (
    YUNB_SHAPES,
    simple_yunb_form,
    yunb_multi_cc_form,
    yunb_two_material_form,
)

# Default integration user (must exist and be active in Postgres)
DEFAULT_EMAIL = "login@1mg.com"

FormBuilder = Callable[..., dict[str, Any]]

CASES: dict[str, dict[str, tuple[FormBuilder, str]]] = {
    "YAST": {
        "simple": (simple_yast_form, "YAST"),
        "multi-cc": (yast_multi_cc_form, "YAST"),
        "two-material": (yast_two_material_form, "YAST"),
        "complex": (yast_two_material_form, "YAST"),
    },
    "YSER": {
        "simple": (simple_yser_form, "YSER"),
        "complex": (complex_yser_form, "YSER"),
    },
    "YUNB": {
        "simple": (simple_yunb_form, "YUNB"),
        "multi-cc": (yunb_multi_cc_form, "YUNB"),
        "two-material": (yunb_two_material_form, "YUNB"),
        # Deprecated alias
        "complex": (yunb_two_material_form, "YUNB"),
    },
}

ALL_ORDER = [
    ("YAST", "simple"),
    ("YAST", "multi-cc"),
    ("YAST", "two-material"),
    ("YSER", "simple"),
    ("YSER", "complex"),
    ("YUNB", "simple"),
    ("YUNB", "multi-cc"),
    ("YUNB", "two-material"),
]

YAST_ALL_ORDER = [
    ("YAST", "simple"),
    ("YAST", "multi-cc"),
    ("YAST", "two-material"),
]

YUNB_ALL_ORDER = [
    ("YUNB", "simple"),
    ("YUNB", "multi-cc"),
    ("YUNB", "two-material"),
]


def run_case(
    *,
    workflow: str,
    mode: str,
    token: str,
    note: str,
    dry_run: bool,
    preview_sap: bool,
) -> int:
    wf = workflow.upper()
    if wf in ("YUNB", "YAST"):
        _apply_workflow_fixtures(wf)
    builders = CASES.get(wf)
    if not builders or mode not in builders:
        raise SystemExit(f"Unknown case {workflow!r} {mode!r}")
    builder, doc_type = builders[mode]
    form = builder(header_note=note)
    label = f"{wf} {mode}"

    if preview_sap or dry_run:
        preview_sap_payload(doc_type, form)
    if dry_run:
        print(f"[dry-run] {label} — no API call.")
        return 0

    ticket = create_pr_via_api(
        access_token=token,
        document_type=doc_type,
        form=form,
    )
    return print_ticket_result(ticket, label=label)


def _apply_workflow_fixtures(workflow: str) -> dict[str, str]:
    return apply_integration_reference_defaults()


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(
        description="SAP workshop: create PR (simple/complex) via AgentOS API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python scripts/integration/run_sap_test.py YAST simple\n"
        "  python scripts/integration/run_sap_test.py all\n",
    )
    p.add_argument(
        "workflow",
        choices=["YAST", "YSER", "YUNB", "all", "yast", "yser", "yunb", "All"],
        help="Workflow or 'all' for six scenarios",
    )
    p.add_argument(
        "mode",
        nargs="?",
        default="simple",
        choices=["simple", "complex", "multi-cc", "two-material", "all"],
        help="Form shape; YUNB also supports multi-cc and two-material; 'all' runs full matrix",
    )
    p.add_argument("--email", help="Login email (default: auto-mint login@1mg.com)")
    p.add_argument("--password", help="Login password")
    p.add_argument("--token", help="Bearer token (skips mint)")
    p.add_argument("--note", default="", help="header_note override")
    p.add_argument("--dry-run", action="store_true", help="Preview only, no POST")
    p.add_argument("--preview-sap", action="store_true", help="Print SAP JSON shape before POST")
    args = p.parse_args()

    token = resolve_access_token(
        email=args.email,
        password=args.password,
        token=args.token,
    )

    wf = args.workflow.upper()
    if wf in ("YUNB", "YAST"):
        applied = _apply_workflow_fixtures(wf)
        print(f"{wf} QAS fixtures: " + ", ".join(f"{k}={v!r}" for k, v in sorted(applied.items())))

    if wf == "ALL":
        worst = 0
        for w, m in ALL_ORDER:
            print(f"\n{'=' * 60}\n>>> {w} {m}\n{'=' * 60}")
            code = run_case(
                workflow=w,
                mode=m,
                token=token,
                note=args.note,
                dry_run=args.dry_run,
                preview_sap=args.preview_sap,
            )
            worst = max(worst, code)
        return worst

    if wf == "YUNB" and args.mode == "all":
        worst = 0
        for w, m in YUNB_ALL_ORDER:
            print(f"\n{'=' * 60}\n>>> {w} {m}\n{'=' * 60}")
            code = run_case(
                workflow=w,
                mode=m,
                token=token,
                note=args.note,
                dry_run=args.dry_run,
                preview_sap=args.preview_sap,
            )
            worst = max(worst, code)
        return worst

    if wf == "YAST" and args.mode == "all":
        worst = 0
        for w, m in YAST_ALL_ORDER:
            print(f"\n{'=' * 60}\n>>> {w} {m}\n{'=' * 60}")
            code = run_case(
                workflow=w,
                mode=m,
                token=token,
                note=args.note,
                dry_run=args.dry_run,
                preview_sap=args.preview_sap,
            )
            worst = max(worst, code)
        return worst

    return run_case(
        workflow=wf,
        mode=args.mode,
        token=token,
        note=args.note,
        dry_run=args.dry_run,
        preview_sap=args.preview_sap,
    )


if __name__ == "__main__":
    raise SystemExit(main())
