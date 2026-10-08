#!/usr/bin/env python3
"""Live integration: create PR via AgentOS API — workflow YSER (Service).

Mimics UI: POST /api/procurement/tickets/pr → server normalize → SAP Z_PURCHASE_REQUISITION_SRV (YSER).

Usage (from agentos-backend, API running on :8000):

  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_pr_create_yser.py
  AGENTOS_INTEGRATION_MINT_TOKEN=1 python scripts/integration/run_pr_create_yser.py --preview-sap
  python scripts/integration/run_pr_create_yser.py --email YOU@co.com --password '...'

Override SAP master data (ask SAP team for valid QAS codes):

  SAP_SERVICE=10000000006 SAP_SERVICE_2=10000000007 SAP_SERVICE_GROUP=S089-0001 SAP_COST_CENTER=HCO91001H0 python scripts/integration/run_pr_create_yser.py --mint-token
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import (
    add_common_args,
    create_pr_via_api,
    load_env,
    preview_sap_payload,
    print_ticket_result,
    resolve_token_from_args,
)

DOCUMENT_TYPE = "YSER"


def simple_yser_form(*, header_note: str) -> dict:
    """One service line, one cost center."""
    return {
        "header": {
            "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": os.environ.get("SAP_PUR_GROUP", "S0Z"),
            "plant": os.environ.get("SAP_PLANT", ""),
            "storage_location": os.environ.get("SAP_SLOC", "1001"),
            "service_group": (os.environ.get("SAP_SERVICE_GROUP") or "").strip(),
            "header_note": header_note or "Integration YSER",
        },
        "lines": [
            {
                "service": (os.environ.get("SAP_SERVICE") or "").strip(),
                "short_text": "Service line simple",
                "delivery_date": "2026-07-01",
                "unit_price": "100",
                "valuation_price": "100",
                "allocations": [
                    {
                        "cost_center": (os.environ.get("SAP_COST_CENTER") or "HBM13291G0").strip(),
                        "qty": "1",
                    }
                ],
            }
        ],
    }


def complex_yser_form(*, header_note: str) -> dict:
    """Two service blocks; multiple cost-center allocations (fans out to many SAP lines)."""
    service_1 = (os.environ.get("SAP_SERVICE") or "").strip()
    service_2 = (os.environ.get("SAP_SERVICE_2") or service_1).strip()
    cc_a = (os.environ.get("SAP_COST_CENTER") or "HBM13291G0").strip()
    cc_b = (os.environ.get("SAP_COST_CENTER_2") or "HBM13291H0").strip()
    sg = (os.environ.get("SAP_SERVICE_GROUP") or "S111-0001").strip()

    return {
        "header": {
            "purchasing_org": os.environ.get("SAP_PUR_ORG", "1MGH"),
            "purchasing_group": os.environ.get("SAP_PUR_GROUP", "S0Z"),
            "plant": os.environ.get("SAP_PLANT", ""),
            "storage_location": os.environ.get("SAP_SLOC", "1001"),
            "service_group": sg,
            "header_note": header_note or "Integration YSER",
        },
        "lines": [
            {
                "service": service_1,
                "short_text": "Service line A integration",
                "delivery_date": "2026-07-01",
                "unit_price": "150",
                "valuation_price": "450",
                "allocations": [
                    {"cost_center": cc_a, "qty": "1"},
                    {"cost_center": cc_b, "qty": "2"},
                ],
            },
            {
                "service": service_2,
                "short_text": "Service line B integration",
                "delivery_date": "2026-07-15",
                "unit_price": "75",
                "valuation_price": "75",
                "allocations": [{"cost_center": cc_a, "qty": "1"}],
            },
        ],
    }


def main() -> int:
    load_env()
    applied = apply_integration_reference_defaults()
    print(integration_defaults_summary(applied))
    if not os.environ.get("SAP_SERVICE", "").strip():
        raise SystemExit("SAP_SERVICE missing — import pr_po reference master or set SAP_SERVICE")
    p = argparse.ArgumentParser(description="Create PR (YSER) through AgentOS API → SAP")
    add_common_args(p)
    p.add_argument("--complex", action="store_true", help="Two service blocks + multi CC")
    args = p.parse_args()
    form = (
        complex_yser_form(header_note=args.note)
        if args.complex
        else simple_yser_form(header_note=args.note)
    )

    if args.preview_sap or args.dry_run:
        preview_sap_payload(DOCUMENT_TYPE, form)

    if args.dry_run:
        print("Dry-run: no API call.")
        return 0

    token = resolve_token_from_args(args)
    ticket = create_pr_via_api(
        access_token=token,
        document_type=DOCUMENT_TYPE,
        form=form,
    )
    return print_ticket_result(ticket, label="YSER create PR")


if __name__ == "__main__":
    raise SystemExit(main())
