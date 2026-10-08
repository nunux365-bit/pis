#!/usr/bin/env python3
"""Live PO API runner — repeat POST /api/procurement/tickets/po → SAP (workflow types only).

Uses PurchaseOrderType = document_type (YUNB / YAST / YSER). Does not map to NB.

Prerequisites:
  - AgentOS API running (default http://localhost:8000)
  - PROCUREMENT_SAP_* in .env
  - SAP PO create authorization on API_PURCHASEORDER_PROCESS_SRV

Quick start (agentos-backend/):

  # List PRs you can link
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py --list-parent-prs --doc-type YUNB

  # Preview OData body (no API POST)
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py --dry-run --preview-sap

  # Standalone PO
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py --doc-type YUNB

  # PO from SAP PR 1040000063
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py --doc-type YUNB --parent-pr-sap 1040000063

  # PO from AgentOS PR ticket UUID
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_po_live.py --doc-type YUNB --parent-pr-id <uuid>

Env overrides: SAP_PO_DELIVERY_DATE, SAP_PARENT_PR, SAP_MATERIAL, SAP_VENDOR, AGENTOS_API_BASE_URL
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import integration_defaults_summary
from scripts.integration.po_live_common import (
    apply_po_live_fixtures,
    default_delivery_date,
    list_parent_prs,
    print_po_live_result,
    print_run_command_hint,
    resolve_parent_pr_id,
    simple_po_form,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    api_base,
    create_po_via_api,
    load_env,
    preview_po_payload,
    resolve_token_from_args,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Live PO create via AgentOS API (repeatable)")
    add_common_args(p)
    p.add_argument(
        "--doc-type",
        choices=["YUNB", "YAST", "YSER"],
        default="YUNB",
        help="Workflow document type → SAP PurchaseOrderType (default YUNB)",
    )
    p.add_argument("--parent-pr-id", help="AgentOS parent PR ticket UUID")
    p.add_argument(
        "--parent-pr-sap",
        help="SAP PR number (resolves ticket UUID from API or built-in map)",
    )
    p.add_argument(
        "--delivery-date",
        default="",
        help=f"Line delivery date (default {default_delivery_date()!r} or SAP_PO_DELIVERY_DATE)",
    )
    p.add_argument(
        "--list-parent-prs",
        action="store_true",
        help="Print parent PR tickets with sap_id and exit",
    )
    args = p.parse_args()
    load_env()

    dt = args.doc_type.upper()
    applied = apply_po_live_fixtures(dt)
    print(integration_defaults_summary(applied))
    print(f"API base:         {api_base()}")
    print(f"delivery_date:    {args.delivery_date or default_delivery_date()}")

    delivery = args.delivery_date or default_delivery_date()
    form = simple_po_form(dt, header_note=args.note, delivery_date=delivery)
    parent_pr_sap = (args.parent_pr_sap or "").strip() or None
    preview_parent_sap = parent_pr_sap

    # Preview uses asyncio.run(DB); mint JWT uses another asyncio.run — run preview first.
    if not args.list_parent_prs and (args.dry_run or args.preview_sap):
        preview_po_payload(
            dt,
            form,
            parent_pr_number=preview_parent_sap,
            ticket_id="po-live-preview",
        )
        if args.dry_run:
            print("Dry-run: no API call.")
            print_run_command_hint(document_type=dt, parent_pr_sap=parent_pr_sap)
            return 0

    token = resolve_token_from_args(args)

    if args.list_parent_prs:
        list_parent_prs(access_token=token, document_type=dt)
        return 0

    parent_id = resolve_parent_pr_id(
        parent_pr_id=args.parent_pr_id,
        parent_pr_sap=parent_pr_sap,
        access_token=token,
    )
    if parent_id and not parent_pr_sap:
        from scripts.integration.po_live_common import DEFAULT_PARENT_PR_BY_SAP

        for sap, uid in DEFAULT_PARENT_PR_BY_SAP.items():
            if uid == parent_id:
                parent_pr_sap = sap
                break

    label = f"Live PO {dt}"
    if parent_pr_sap:
        label += f" ← PR {parent_pr_sap}"
    elif parent_id:
        label += f" ← parent_pr_id {parent_id}"

    ticket = create_po_via_api(
        access_token=token,
        document_type=dt,
        form=form,
        parent_pr_id=parent_id,
    )
    code = print_po_live_result(ticket, label=label)
    print_run_command_hint(document_type=dt, parent_pr_sap=parent_pr_sap)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
