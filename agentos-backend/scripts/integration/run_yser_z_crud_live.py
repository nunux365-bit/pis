#!/usr/bin/env python3
"""Live YSER: Z create → standard GET → update → delete.

Usage (from agentos-backend):

  python scripts/integration/run_yser_z_crud_live.py
  python scripts/integration/run_yser_z_crud_live.py --skip-delete

Requires PROCUREMENT_SAP_* in .env. Override master data via SAP_SERVICE, SAP_SERVICE_GROUP, etc.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults
from scripts.integration.run_pr_create_yser import simple_yser_form


async def main() -> int:
    from app.config.settings import settings
    from app.procurement import sap_pr_client
    from app.procurement.sap_pr_z_client import delete_yser_pr
    from app.procurement.sap_pr_z_payload import build_z_yser_pr_payload
    from app.procurement.sap_pr_z_client import get_yser_pr
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read
    from app.procurement.sap_ticket_form_read import load_form_from_sap

    p = argparse.ArgumentParser(description="YSER Z create + standard GET live test")
    p.add_argument("--skip-delete", action="store_true", help="Leave PR in SAP after test")
    p.add_argument(
        "--pr-number",
        metavar="PR",
        help="Skip create; run read/update/delete on an existing PR (update/delete only)",
    )
    args = p.parse_args()

    if not sap_pr_client.sap_pr_configured():
        print("SAP not configured (PROCUREMENT_SAP_BASE_URL / credentials).", file=sys.stderr)
        return 1

    from scripts.integration.integration_reference_defaults import integration_defaults_summary

    applied = apply_integration_reference_defaults()
    print(integration_defaults_summary(applied))
    ticket_id = str(uuid.uuid4())
    form = simple_yser_form(header_note=f"Z CRUD {ticket_id[:8]}")
    print(f"SAP: {settings.procurement_sap_base_url}")
    print("Form summary:", json.dumps(form, indent=2)[:800])

    if args.pr_number:
        pr_no = str(args.pr_number).strip()
        print(f"\nUsing existing PR: {pr_no}")
    else:
        payload = build_z_yser_pr_payload(form=form, document_type="YSER", ticket_id=ticket_id)
        print("\n--- Z create payload (trimmed) ---")
        print(json.dumps(payload, indent=2)[:1500])

        pr_no, err = await sap_pr_client.create_pr(
            ticket_id=ticket_id, form=form, document_type="YSER"
        )
        if err or not pr_no:
            print(f"\nCREATE FAILED: {err}")
            return 2
        print(f"\nCREATE OK: PRNumber={pr_no}")

    z_body, z_err = await get_yser_pr(pr_number=pr_no, ticket_id="read-z")
    if z_body:
        z_form = form_from_z_pr_read(z_body, document_type="YSER", seed_form=form)
        print(f"GET Z API: OK, lines={len(z_form.get('lines') or [])}")
        print(json.dumps(z_form, indent=2)[:1200])
    else:
        print(f"GET Z API: failed ({z_err})")
        return 3

    hydrated, source, hydr_err = await load_form_from_sap(
        kind="PR",
        document_type="YSER",
        sap_id=pr_no,
        ticket_id=ticket_id,
        seed_form=form,
    )
    print(f"load_form_from_sap: source={source} err={hydr_err} lines={len((hydrated or {}).get('lines') or [])}")

    new_short = f"Z CRUD upd {ticket_id[:8]}"
    new_delivery = "2026-11-20"
    if form.get("lines"):
        form["lines"][0]["short_text"] = new_short
        form["lines"][0]["delivery_date"] = new_delivery
    pr2, upd_err = await sap_pr_client.update_pr(
        sap_id=pr_no,
        ticket_id=ticket_id,
        form=form,
        document_type="YSER",
    )
    if upd_err:
        print(f"\nUPDATE FAILED: {upd_err}")
        return 4
    print(f"\nUPDATE OK: PRNumber={pr2}")

    after_body, after_err = await get_yser_pr(pr_number=pr_no, ticket_id="read-after-upd")
    if after_err or not after_body:
        print(f"POST-UPDATE READ FAILED: {after_err}")
        return 4
    after_form = form_from_z_pr_read(after_body, document_type="YSER", seed_form=form)
    line0 = (after_form.get("lines") or [{}])[0]
    if line0.get("short_text") != new_short:
        print(f"VERIFY FAIL short_text: SAP {line0.get('short_text')!r} != {new_short!r}")
        return 4
    if line0.get("delivery_date") != new_delivery:
        print(f"VERIFY FAIL delivery_date: SAP {line0.get('delivery_date')!r} != {new_delivery!r}")
        return 4
    hdr_note = (after_form.get("header") or {}).get("header_note", "")
    print(f"VERIFY OK (short_text, delivery_date). header_note unchanged: {hdr_note!r}")

    if args.skip_delete:
        print("\n--skip-delete: PR left in SAP:", pr_no)
        return 0

    ok, del_err = await delete_yser_pr(sap_id=pr_no, ticket_id=ticket_id)
    if not ok:
        print(f"\nDELETE FAILED: {del_err}")
        print("(PR may remain in SAP:", pr_no, ")")
        return 5
    print(f"\nDELETE OK: PRNumber={pr_no}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
