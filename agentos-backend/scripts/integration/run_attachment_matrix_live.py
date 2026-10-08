#!/usr/bin/env python3
"""Live test: multi-file attach, SAP list, download for existing PR and PO."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.procurement_api_common import api_base, load_env, resolve_access_token


def _make_pdfs(tmp: Path, n: int) -> list[Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i in range(1, n + 1):
        p = tmp / f"attach-test-{i}.pdf"
        p.write_bytes(f"%PDF-1.4\n% attach {i}\n%%EOF\n".encode())
        out.append(p)
    return out


def _find_ticket(token: str, *, kind: str, sap_id: str | None) -> dict:
    with httpx.Client(timeout=60) as c:
        r = c.get(
            f"{api_base()}/api/procurement/tickets",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        raise SystemExit(f"list tickets HTTP {r.status_code}: {r.text[:500]}")
    rows = r.json()
    if sap_id:
        for t in rows:
            if str(t.get("kind")).upper() == kind.upper() and str(t.get("sap_id") or "").strip() == sap_id:
                return t
        raise SystemExit(f"No {kind} ticket with sap_id={sap_id!r}")
    for t in rows:
        if str(t.get("kind")).upper() == kind.upper() and (t.get("sap_id") or "").strip():
            return t
    raise SystemExit(f"No {kind} ticket with sap_id in list")


def _patch_files(token: str, ticket_id: str, version: int, pdfs: list[Path]) -> dict:
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}"
    files = [
        ("files", (p.name, p.read_bytes(), "application/pdf"))
        for p in pdfs
    ]
    data = {"version": str(version), "payload": json.dumps({"resync_sap": False})}
    with httpx.Client(timeout=300) as c:
        r = c.patch(url, headers={"Authorization": f"Bearer {token}"}, data=data, files=files)
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH HTTP {r.status_code}: {r.text[:2000]}")
    return r.json()


def _download(token: str, ticket_id: str, attachment_id: str) -> tuple[int, str, int]:
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}/attachments/{attachment_id}/download"
    with httpx.Client(timeout=120) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
    return r.status_code, (r.headers.get("content-type") or ""), len(r.content)


def _run_case(token: str, *, label: str, kind: str, sap_id: str | None, pdfs: list[Path]) -> int:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    t = _find_ticket(token, kind=kind, sap_id=sap_id)
    tid = str(t["id"])
    sid = str(t.get("sap_id") or "")
    ver = int(t["version"])
    print(f"ticket_id={tid} sap_id={sid} version={ver}")

    try:
        updated = _patch_files(token, tid, ver, pdfs)
    except RuntimeError as e:
        print(f"UPLOAD ERROR: {e}")
        return 1

    ver = int(updated["version"])
    atts = updated.get("attachments") or []
    print(f"UPLOAD OK — {len(atts)} attachment row(s) on ticket")
    for a in atts:
        print(
            f"  {a.get('name')}: id={a.get('id')!r} sap_document_id={a.get('sap_document_id')!r} "
            f"error={a.get('sap_sync_error')!r}"
        )
    sync = updated.get("sap_sync") or {}
    print(
        f"sync_pending={sync.get('sync_pending')} attachments_pending={sync.get('attachments_pending')} "
        f"attachment_last_error={sync.get('attachment_last_error')!r}"
    )

    print("\nDOWNLOAD via API (poll until SAP ids appear — background upload)")
    rc = 0
    for a in atts:
        ref = (a.get("sap_document_id") or a.get("id") or "").strip()
        if not ref:
            print(f"  SKIP {a.get('name')}: no ref")
            rc = 1
            continue
        if not a.get("sap_document_id"):
            print(f"  SKIP {a.get('name')}: not in SAP yet ({a.get('sap_sync_error')})")
            rc = 1
            continue
        code, ctype, n = _download(token, tid, ref)
        if code != 200:
            print(f"  FAIL {a.get('name')}: HTTP {code}")
            rc = 1
        else:
            print(f"  OK {a.get('name')}: HTTP {code} {ctype} {n} bytes")
    return rc


def main() -> int:
    load_env()
    token = resolve_access_token(email=None, password=None, token=None)
    tmp = Path("/tmp/agentos-attach-matrix")
    pdfs = _make_pdfs(tmp, 3)

    worst = 0
    worst |= _run_case(token, label="PR (3 files)", kind="PR", sap_id="1040000077", pdfs=pdfs)
    worst |= _run_case(token, label="PO (3 files)", kind="PO", sap_id=None, pdfs=pdfs)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
