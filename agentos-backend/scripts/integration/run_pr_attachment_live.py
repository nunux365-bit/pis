#!/usr/bin/env python3
"""Upload PDF attachment(s) to an existing PR/PO ticket (SAP AttachmentSet).

Usage::

  python scripts/integration/run_pr_attachment_live.py --ticket-id <uuid> --pdf /path/to/file.pdf
  python scripts/integration/run_pr_attachment_live.py --sap-id 1040000077 --kind PR --pdf test.pdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import load_env, print_ticket_result, resolve_access_token


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="Attach PDF to existing procurement ticket")
    p.add_argument("--ticket-id", help="AgentOS ticket UUID")
    p.add_argument("--sap-id", help="Resolve ticket by SAP id (latest matching row)")
    p.add_argument("--kind", choices=["PR", "PO"], default="PR")
    p.add_argument("--pdf", required=True, help="Path to PDF file")
    p.add_argument("--version", type=int, default=0, help="Ticket version (0 = fetch from API)")
    args = p.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")
    data = pdf_path.read_bytes()
    if not data.startswith(b"%PDF"):
        print("Warning: file does not start with %PDF — SAP may still reject it")

    token = resolve_access_token(email=None, password=None, token=None)

    ticket_id = (args.ticket_id or "").strip()
    version = args.version

    if not ticket_id:
        sap_id = (args.sap_id or "").strip()
        if not sap_id:
            raise SystemExit("Provide --ticket-id or --sap-id")
        import httpx
        from scripts.integration.procurement_api_common import api_base

        with httpx.Client(timeout=60) as client:
            r = client.get(
                f"{api_base()}/api/procurement/tickets",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code >= 400:
            raise SystemExit(f"list tickets failed: {r.status_code}")
        rows = r.json()
        match = [
            t
            for t in rows
            if str(t.get("sap_id") or "").strip() == sap_id
            and str(t.get("kind") or "").upper() == args.kind.upper()
        ]
        if not match:
            raise SystemExit(f"No {args.kind} ticket with sap_id={sap_id!r}")
        ticket_id = str(match[0]["id"])
        version = int(match[0]["version"])
        print(f"Resolved ticket_id={ticket_id} version={version}")

    import httpx
    from scripts.integration.procurement_api_common import api_base

    if version <= 0:
        with httpx.Client(timeout=60) as client:
            r = client.get(
                f"{api_base()}/api/procurement/tickets/{ticket_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code >= 400:
            raise SystemExit(f"get ticket failed: {r.status_code}")
        version = int(r.json()["version"])

    # PATCH multipart: files only, no form resync
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}"
    files = [("files", (pdf_path.name, data, "application/pdf"))]
    data_form = {
        "version": str(version),
        "payload": '{"resync_sap": false}',
    }
    import httpx

    with httpx.Client(timeout=300) as client:
        resp = client.patch(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data=data_form,
            files=files,
        )
    if resp.status_code >= 400:
        raise SystemExit(f"PATCH failed {resp.status_code}: {resp.text[:2000]}")
    ticket = resp.json()
    print_ticket_result(ticket, label="ATTACH")
    atts = ticket.get("attachments") or []
    for a in atts:
        print(
            f"  - {a.get('name')}: sap_document_id={a.get('sap_document_id')!r} "
            f"error={a.get('sap_sync_error')!r}"
        )
    sync = ticket.get("sap_sync") or {}
    print(
        f"attachments_pending={sync.get('attachments_pending')} "
        f"attachment_last_error={sync.get('attachment_last_error')!r}"
    )
    return 0 if not sync.get("attachments_pending") else 1


if __name__ == "__main__":
    raise SystemExit(main())
