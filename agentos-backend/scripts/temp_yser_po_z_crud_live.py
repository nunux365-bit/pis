#!/usr/bin/env python3
"""Direct SAP live test for YSER PO (no AgentOS API). Temp integration probe."""

from __future__ import annotations

import asyncio
import copy
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings  # noqa: F401
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from app.procurement.sap_po_client import create_po, get_po, sap_po_configured, update_po
from app.procurement.sap_po_z_payload import form_from_z_po_read

from scripts.integration.integration_reference_defaults import load_po_db_context
from scripts.integration.po_live_common import apply_po_live_fixtures, build_po_matrix_cases
from scripts.integration.procurement_api_common import load_env


async def _run(label: str, form: dict, *, parent: str | None = None) -> bool:
    tid = str(uuid.uuid4())
    norm = normalize_form("YSER", form)
    apply_procurement_defaults(norm, document_type="YSER", kind="PO")
    print(f"\n=== {label} CREATE ===")
    po, err = await create_po(
        ticket_id=tid,
        form=norm,
        document_type="YSER",
        parent_sap_id=parent,
    )
    if err or not po:
        print(f"FAIL create: {err}")
        return False
    print(f"OK create PO {po}")

    body, gerr = await get_po(po_number=po, ticket_id=tid, document_type="YSER")
    if gerr or not body:
        print(f"FAIL get: {gerr}")
        return False
    read = form_from_z_po_read(body, seed_form=norm)
    print(f"OK get lines={len(read.get('lines') or [])}")

    upd = copy.deepcopy(read)
    upd["lines"][0]["unit_price"] = str(int(float(upd["lines"][0]["unit_price"])) + 5)
    upd["lines"][0]["net_price"] = upd["lines"][0]["unit_price"]
    print(f"=== {label} UPDATE ===")
    po2, uerr = await update_po(
        sap_id=po,
        ticket_id=f"{tid}-u",
        form=upd,
        document_type="YSER",
        parent_sap_id=parent,
    )
    if uerr or not po2:
        print(f"FAIL update: {uerr}")
        return False
    print(f"OK update PO {po2}")

    if len(read.get("lines") or []) >= 2:
        body_u, _ = await get_po(po_number=po, ticket_id=f"{tid}-uh", document_type="YSER")
        hydrated = form_from_z_po_read(body_u or {}, seed_form=upd)
        del_form = copy.deepcopy(hydrated)
        del_form["lines"] = del_form["lines"][:1]
        print(f"=== {label} DELETE LINE ===")
        po3, derr = await update_po(
            sap_id=po,
            ticket_id=f"{tid}-d",
            form=del_form,
            document_type="YSER",
            parent_sap_id=parent,
        )
        if derr or not po3:
            print(f"FAIL delete-line: {derr}")
            return False
        body2, _ = await get_po(po_number=po, ticket_id=f"{tid}-v", document_type="YSER")
        read2 = form_from_z_po_read(body2 or {})
        print(f"OK delete-line remaining lines={len(read2.get('lines') or [])}")
    return True


async def main() -> int:
    load_env()
    if not sap_po_configured():
        print("SAP not configured")
        return 2
    apply_po_live_fixtures("YSER")
    cases = build_po_matrix_cases(load_po_db_context(apply_env=True))

    ok = True
    ok &= await _run("standalone-simple", copy.deepcopy(cases["yser_simple"]["form"]))

    complex_form = copy.deepcopy(cases["yser_two_service"]["form"])
    ok &= await _run("standalone-complex", complex_form)

    # PR-linked if env provides released PR
    import os

    parent_pr = (os.environ.get("SAP_PARENT_PR") or "1010000365").strip()
    if parent_pr:
        linked = copy.deepcopy(cases["yser_simple"]["form"])
        linked["lines"][0]["purchase_requisition_item"] = "10"
        ok &= await _run(f"pr-linked ({parent_pr})", linked, parent=parent_pr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
