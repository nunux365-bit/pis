#!/usr/bin/env python3
"""Resubmit with multiple form field changes — reports what persisted vs SAP error.

Usage (after a successful simple create):

  python scripts/integration/run_pr_merge_field_test.py --ticket-id UUID --version N --document-type YUNB
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from scripts.integration.integration_reference_defaults import (
    apply_integration_reference_defaults,
    integration_defaults_summary,
)
from scripts.integration.procurement_api_common import (
    api_base,
    load_env,
    print_ticket_result,
    resolve_access_token,
)


def get_ticket(token: str, tid: str) -> dict:
    r = httpx.get(
        f"{api_base()}/api/procurement/tickets/{tid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def patch(token: str, tid: str, *, version: int, form: dict, resync: bool) -> dict:
    r = httpx.patch(
        f"{api_base()}/api/procurement/tickets/{tid}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"form": form, "version": version, "resync_sap": resync},
        timeout=300,
    )
    if r.status_code >= 400:
        raise SystemExit(f"PATCH failed {r.status_code}: {r.text[:2500]}")
    return r.json()


def main() -> int:
    load_env()
    print(integration_defaults_summary(apply_integration_reference_defaults()))
    p = argparse.ArgumentParser()
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--document-type", choices=["YUNB", "YAST", "YSER"], required=True)
    args = p.parse_args()
    token = resolve_access_token(email=None, password=None, token=None)

    before = get_ticket(token, args.ticket_id)
    form = copy.deepcopy(before["form"])
    hdr = form.setdefault("header", {})
    line = form["lines"][0]
    allocs = line.setdefault("allocations", [{}])

    hdr["header_note"] = "Merge test — header note"
    line["short_text"] = (line.get("short_text") or "Line") + " [merged]"
    line["unit_price"] = "99"
    line["valuation_price"] = "99"
    line["delivery_date"] = "2026-08-15"
    cc = os.environ.get("SAP_COST_CENTER", "").strip()
    if cc and isinstance(allocs[0], dict):
        allocs[0]["cost_center"] = cc
        allocs[0]["qty"] = "2"

    print("\n--- Fields changed (DB + resync payload) ---")
    print("  header_note, short_text, unit_price, valuation_price, delivery_date")
    if cc:
        print(f"  cost_center={cc!r}, alloc qty=2")

    saved = patch(token, args.ticket_id, version=args.version, form=form, resync=False)
    print(f"\nDB save: version {args.version} -> {saved['version']}")

    resynced = patch(token, args.ticket_id, version=saved["version"], form=form, resync=True)
    code = print_ticket_result(resynced, label="Multi-field resync")
    after = get_ticket(token, args.ticket_id)
    print(f"\nPersisted header_note: {after['form']['header'].get('header_note')!r}")
    print(f"Persisted valuation_price: {after['form']['lines'][0].get('valuation_price')!r}")
    sync = after.get("sap_sync") or {}
    print(f"sap_sync.last_error: {sync.get('last_error')!r}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
