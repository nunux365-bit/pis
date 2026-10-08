#!/usr/bin/env python3
"""Live integration: resubmit existing PR to SAP (PATCH resync_sap=true → MERGE).

Requires a ticket that already has sap_id (from a prior successful create).

Usage:

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_pr_resubmit.py \\
    --ticket-id <uuid> --version 1

Optional: pass --document-type YSER|YUNB|YAST to build an updated form (header note change).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import (
    add_common_args,
    load_env,
    patch_resync_via_api,
    print_ticket_result,
    resolve_token_from_args,
)

from scripts.integration.run_pr_create_yast import complex_yast_form
from scripts.integration.run_pr_create_yser import complex_yser_form
from scripts.integration.run_pr_create_yunb import complex_yunb_form

BUILDERS = {
    "YSER": complex_yser_form,
    "YUNB": complex_yunb_form,
    "YAST": complex_yast_form,
}


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="PATCH PR resync_sap → SAP MERGE")
    add_common_args(p)
    p.add_argument("--ticket-id", required=True, help="Procurement ticket UUID")
    p.add_argument("--version", type=int, required=True, help="Optimistic-lock version from create GET")
    p.add_argument(
        "--document-type",
        choices=["YSER", "YUNB", "YAST"],
        default="YAST",
        help="Form builder for updated payload",
    )
    args = p.parse_args()

    if args.dry_run:
        print("Resubmit requires ticket-id; dry-run not supported.")
        return 0

    builder = BUILDERS[args.document_type.upper()]
    form = builder(header_note=args.note or "Integration resubmit note")

    token = resolve_token_from_args(args)
    ticket = patch_resync_via_api(
        access_token=token,
        ticket_id=args.ticket_id,
        form=form,
        version=args.version,
    )
    return print_ticket_result(ticket, label="PR resubmit (MERGE)")


if __name__ == "__main__":
    raise SystemExit(main())
