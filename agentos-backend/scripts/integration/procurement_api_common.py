"""Shared helpers for live procurement PR integration scripts (UI-faithful API calls)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def load_env() -> None:
    from app.config.settings import settings  # noqa: F401 — triggers dotenv


def api_base() -> str:
    return (os.environ.get("AGENTOS_API_BASE_URL") or "http://localhost:8000").rstrip("/")


def resolve_access_token(*, email: str | None, password: str | None, token: str | None) -> str:
    if token and token.strip():
        return token.strip()
    if email and password:
        return login(email=email.strip(), password=password)
    mint_email = (os.environ.get("AGENTOS_INTEGRATION_EMAIL") or "login@1mg.com").strip()
    auto = os.environ.get("AGENTOS_INTEGRATION_AUTO_TOKEN", "1").lower() not in ("0", "false", "no")
    mint_flag = os.environ.get("AGENTOS_INTEGRATION_MINT_TOKEN", "").lower() in ("1", "true", "yes")
    if mint_flag or auto:
        return mint_dev_token(mint_email)
    raise SystemExit(
        "Auth required: set AGENTOS_ACCESS_TOKEN, or --email/--password, "
        "or use default auto JWT mint (AGENTOS_INTEGRATION_EMAIL, default login@1mg.com)."
    )


def mint_dev_token(email: str) -> str:
    """Mint JWT for an active user (local integration only — same secret as running API)."""
    import asyncio

    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import AsyncSessionLocal
    from app.security.tokens import create_access_token

    async def _run() -> str:
        async with AsyncSessionLocal() as s:
            res = await s.execute(select(User).where(User.email == email.lower().strip()))
            user = res.scalar_one_or_none()
            if not user or not user.is_active:
                raise SystemExit(f"No active user for AGENTOS_INTEGRATION_EMAIL={email!r}")
            return create_access_token(str(user.id), {"role": user.primary_role})

    return asyncio.run(_run())


def login(*, email: str, password: str) -> str:
    url = f"{api_base()}/api/auth/login"
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, json={"email": email, "password": password})
    if resp.status_code >= 400:
        raise SystemExit(f"Login failed HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    tok = data.get("access_token")
    if not isinstance(tok, str) or not tok:
        raise SystemExit("Login response missing access_token")
    return tok


def create_po_via_api(
    *,
    access_token: str,
    document_type: str,
    form: dict[str, Any],
    parent_pr_id: str | None = None,
    files: list[tuple[str, str, bytes]] | None = None,
) -> dict[str, Any]:
    """POST /api/procurement/tickets/po — same contract as the UI."""
    url = f"{api_base()}/api/procurement/tickets/po"
    body: dict[str, Any] = {"document_type": document_type, "form": form}
    if parent_pr_id:
        body["parent_pr_id"] = parent_pr_id
    payload_json = json.dumps(body)
    multipart: list[tuple[str, tuple[str | None, bytes, str | None]]] = [
        ("payload", (None, payload_json.encode("utf-8"), "application/json")),
    ]
    for name, ctype, data in files or []:
        multipart.append(("files", (name, data, ctype or "application/octet-stream")))

    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(url, headers=headers, files=multipart)

    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            detail = json.dumps(resp.json(), indent=2)[:2000]
        except Exception:
            pass
        raise SystemExit(f"Create PO failed HTTP {resp.status_code}:\n{detail}")

    data = resp.json()
    if not isinstance(data, dict):
        raise SystemExit("Create PO returned non-JSON body")
    return data


def create_po_via_api_result(
    *,
    access_token: str,
    document_type: str,
    form: dict[str, Any],
    parent_pr_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{api_base()}/api/procurement/tickets/po"
    body: dict[str, Any] = {"document_type": document_type, "form": form}
    if parent_pr_id:
        body["parent_pr_id"] = parent_pr_id
    payload_json = json.dumps(body)
    multipart: list[tuple[str, tuple[str | None, bytes, str | None]]] = [
        ("payload", (None, payload_json.encode("utf-8"), "application/json")),
    ]
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=300.0) as client:
            resp = client.post(url, headers=headers, files=multipart)
    except httpx.RequestError as e:
        return None, f"API connection error: {e}"
    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            detail = json.dumps(resp.json(), indent=2)[:2000]
        except Exception:
            pass
        return None, f"HTTP {resp.status_code}: {detail}"
    data = resp.json()
    if not isinstance(data, dict):
        return None, "Create PO returned non-JSON body"
    return data, None


def create_pr_via_api(
    *,
    access_token: str,
    document_type: str,
    form: dict[str, Any],
    files: list[tuple[str, str, bytes]] | None = None,
) -> dict[str, Any]:
    """POST /api/procurement/tickets/pr — same contract as the UI (multipart + JSON payload)."""
    url = f"{api_base()}/api/procurement/tickets/pr"
    payload_json = json.dumps({"document_type": document_type, "form": form})
    multipart: list[tuple[str, tuple[str | None, bytes, str | None]]] = [
        ("payload", (None, payload_json.encode("utf-8"), "application/json")),
    ]
    for name, ctype, data in files or []:
        multipart.append(("files", (name, data, ctype or "application/octet-stream")))

    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(url, headers=headers, files=multipart)

    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            detail = json.dumps(resp.json(), indent=2)[:2000]
        except Exception:
            pass
        raise SystemExit(f"Create PR failed HTTP {resp.status_code}:\n{detail}")

    data = resp.json()
    if not isinstance(data, dict):
        raise SystemExit("Create PR returned non-JSON body")
    return data


def poll_ticket_sap_sync(
    access_token: str,
    ticket_id: str,
    *,
    timeout_s: float = 240.0,
) -> tuple[dict[str, Any] | None, str]:
    """Poll GET ticket until numeric sap_id or terminal SAP error."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}"
    while time.monotonic() < deadline:
        with httpx.Client(timeout=120.0) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code >= 400:
            return None, f"GET HTTP {resp.status_code}: {resp.text[:500]}"
        last = resp.json()
        if not isinstance(last, dict):
            return None, "GET ticket returned non-object"
        sap_id = str(last.get("sap_id") or "").strip()
        sync = last.get("sap_sync") if isinstance(last.get("sap_sync"), dict) else {}
        pending = bool(sync.get("sync_pending"))
        err = str(sync.get("last_error") or "").strip()
        attempts = int(sync.get("attempt_count") or 0)
        if sap_id and not sap_id.startswith("#") and not pending:
            return last, ""
        if err and attempts >= 1 and not sap_id:
            return last, err
        if err and not pending:
            return last, err
        if sap_id and err and "VERTEX" in err.upper() and attempts >= 1:
            return last, err
        time.sleep(2.5)
    return last, "timeout waiting for sap_id"


def get_prefill_po_via_api_result(
    *,
    access_token: str,
    pr_ticket_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """GET /api/procurement/tickets/{pr_id}/prefill-po — same as UI PO draft."""
    url = f"{api_base()}/api/procurement/tickets/{pr_ticket_id}/prefill-po"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.get(url, headers=headers)
    except httpx.RequestError as e:
        return None, f"API connection error: {e}"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}: {resp.text[:2000]}"
    data = resp.json()
    if not isinstance(data, dict):
        return None, "prefill-po returned non-JSON object"
    return data, None


def get_ticket_via_api(*, access_token: str, ticket_id: str) -> dict[str, Any]:
    """GET /api/procurement/tickets/{ticket_id}."""
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise SystemExit(f"GET ticket failed HTTP {resp.status_code}: {resp.text[:2000]}")
    data = resp.json()
    if not isinstance(data, dict):
        raise SystemExit("GET ticket returned non-JSON object")
    return data


def patch_resync_via_api(
    *,
    access_token: str,
    ticket_id: str,
    form: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    """PATCH ticket with resync_sap=true (JSON), same as UI resubmit."""
    ticket, err = patch_resync_via_api_result(
        access_token=access_token,
        ticket_id=ticket_id,
        form=form,
        version=version,
    )
    if err:
        raise SystemExit(f"PATCH resync failed: {err}")
    return ticket


def create_pr_via_api_result(
    *,
    access_token: str,
    document_type: str,
    form: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Like ``create_pr_via_api`` but returns ``(ticket, error)`` for batch runners."""
    url = f"{api_base()}/api/procurement/tickets/pr"
    payload_json = json.dumps({"document_type": document_type, "form": form})
    multipart: list[tuple[str, tuple[str | None, bytes, str | None]]] = [
        ("payload", (None, payload_json.encode("utf-8"), "application/json")),
    ]
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with httpx.Client(timeout=300.0) as client:
            resp = client.post(url, headers=headers, files=multipart)
    except httpx.RequestError as e:
        return None, f"API connection error: {e}"
    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            detail = json.dumps(resp.json(), indent=2)[:2000]
        except Exception:
            pass
        return None, f"HTTP {resp.status_code}: {detail}"
    data = resp.json()
    if not isinstance(data, dict):
        return None, "Create PR returned non-JSON body"
    return data, None


def patch_resync_via_api_result(
    *,
    access_token: str,
    ticket_id: str,
    form: dict[str, Any],
    version: int,
) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{api_base()}/api/procurement/tickets/{ticket_id}"
    body = {"form": form, "version": version, "resync_sap": True}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=300.0) as client:
            resp = client.patch(url, headers=headers, json=body)
    except httpx.RequestError as e:
        return None, f"API connection error: {e}"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}: {resp.text[:2000]}"
    data = resp.json()
    if not isinstance(data, dict):
        return None, "PATCH returned non-JSON body"
    return data, None


def classify_sap_ticket(
    ticket: dict[str, Any],
) -> tuple[str, str, bool]:
    """
    Returns ``(status, detail, flag_sap_team)``.
    status: pass | fail | warn
    """
    sap_id = str(ticket.get("sap_id") or "").strip()
    sync = ticket.get("sap_sync") if isinstance(ticket.get("sap_sync"), dict) else {}
    last_error = str(sync.get("last_error") or "").strip()
    if sap_id and not sap_id.startswith("#"):
        if last_error:
            return "warn", last_error, True
        return "pass", sap_id, False
    if sap_id:
        return "warn", f"placeholder sap_id {sap_id!r}", True
    if last_error:
        return "fail", last_error, True
    return "fail", "no sap_id and no last_error", True


def print_ticket_result(ticket: dict[str, Any], *, label: str) -> int:
    sap_id = ticket.get("sap_id")
    sync = ticket.get("sap_sync") if isinstance(ticket.get("sap_sync"), dict) else {}
    last_error = sync.get("last_error")
    attempts = sync.get("attempt_count")
    print(f"\n=== {label} ===")
    print(f"ticket_id:      {ticket.get('id')}")
    print(f"document_type:  {ticket.get('document_type')}")
    print(f"sap_id:         {sap_id!r}")
    print(f"sap_sync:       attempts={attempts} last_error={last_error!r}")
    if sap_id and str(sap_id).strip() and not str(sap_id).strip().startswith("#"):
        print("RESULT: SAP create OK (numeric sap_id)")
        return 0
    if sap_id and str(sap_id).strip():
        print("RESULT: SAP returned sap_id but looks like placeholder — verify in SAP GUI")
        return 2
    print("RESULT: SAP sync failed or no sap_id")
    return 1


def preview_sap_payload(document_type: str, form: dict[str, Any]) -> None:
    """Show payload after the same normalize/defaults/build path the server uses."""
    import asyncio

    from app.db.session import AsyncSessionLocal
    from app.procurement.field_schema import normalize_form
    from app.procurement.sap_defaults import apply_procurement_defaults
    from app.procurement.sap_order_unit import apply_line_order_units_from_reference
    from app.procurement.sap_pr_payload import build_pr_payload

    dt = document_type.upper()
    norm = normalize_form(dt, form)

    async def _prepare() -> None:
        apply_procurement_defaults(norm, document_type=dt, kind="PR")
        async with AsyncSessionLocal() as session:
            await apply_line_order_units_from_reference(
                session, form=norm, document_type=dt, ticket_kind="PR"
            )

    asyncio.run(_prepare())
    payload = build_pr_payload(form=norm, document_type=dt, pr_number="")
    lines = payload.get("to_PurchaseReqnItem") or []
    print("\n--- SAP payload preview (API_PURCHASEREQ_PROCESS_SRV) ---")
    print(f"PurchaseRequisitionType: {payload.get('PurchaseRequisitionType')}")
    print(f"Line count:              {len(lines)}")
    for i, row in enumerate(lines):
        if not isinstance(row, dict):
            continue
        accts = row.get("to_PurchaseReqnAcctAssgmt") or []
        print(
            f"  [{i+1}] Item={row.get('PurchaseRequisitionItem')} "
            f"AcctCat={row.get('AccountAssignmentCategory')!r} "
            f"Material={row.get('Material')!r} "
            f"Qty={row.get('RequestedQuantity')!r} BaseUnit={row.get('BaseUnit')!r} "
            f"acct_lines={len(accts) if isinstance(accts, list) else 0}"
        )
    print("--- end preview ---\n")


def preview_po_payload(
    document_type: str,
    form: dict[str, Any],
    *,
    parent_pr_number: str | None = None,
    ticket_id: str | None = None,
) -> None:
    """Show PO payload after the same normalize/defaults path the server uses."""
    import asyncio

    from app.db.session import AsyncSessionLocal
    from app.procurement.field_schema import normalize_form
    from app.procurement.sap_defaults import apply_procurement_defaults
    from app.procurement.sap_order_unit import apply_line_order_units_from_reference
    from app.procurement.sap_po_payload import build_po_payload

    dt = document_type.upper()
    norm = normalize_form(dt, form)

    async def _prepare() -> None:
        apply_procurement_defaults(norm, document_type=dt, kind="PO")
        async with AsyncSessionLocal() as session:
            await apply_line_order_units_from_reference(
                session, form=norm, document_type=dt, ticket_kind="PO"
            )

    asyncio.run(_prepare())
    if dt == "YSER":
        from app.procurement.sap_po_z_payload import build_z_yser_po_post_body

        payload = build_z_yser_po_post_body(
            form=norm,
            po_number="",
            ticket_id=ticket_id or "preview-ticket",
            parent_pr_number=parent_pr_number,
        )
        svc_label = "ZAPI_PURCHASEORDER_PROCESS_SRV"
    else:
        payload = build_po_payload(
            form=norm,
            document_type=dt,
            po_number="",
            ticket_id=ticket_id or "preview-ticket",
            parent_pr_number=parent_pr_number,
            kind="PO",
        )
        svc_label = "API_PURCHASEORDER_PROCESS_SRV"
    lines = payload.get("d", {}).get("to_PurchaseOrderItem", {}).get("results") if dt == "YSER" else payload.get("to_PurchaseOrderItem") or []
    print(f"\n--- SAP PO payload preview ({svc_label}) ---")
    print(f"PurchaseOrderType: {payload.get('PurchaseOrderType')}")
    print(f"Supplier:          {payload.get('Supplier')}")
    print(f"DocumentCurrency:  {payload.get('DocumentCurrency')}")
    print(f"Line count:        {len(lines)}")
    for i, row in enumerate(lines):
        if not isinstance(row, dict):
            continue
        accts = row.get("to_PurchaseOrderAccountAssignment") or []
        print(
            f"  [{i+1}] Item={row.get('PurchaseOrderItem')} "
            f"Material={row.get('Material')!r} "
            f"Qty={row.get('OrderQuantity')!r} "
            f"NetPrice={row.get('NetPriceAmount')!r} "
            f"PR={row.get('PurchaseRequisition')!r} "
            f"acct_lines={len(accts) if isinstance(accts, list) else 0}"
        )
    print("--- end preview ---\n")


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--email", help="Login email (or use AGENTOS_ACCESS_TOKEN)")
    p.add_argument("--password", help="Login password")
    p.add_argument("--token", help="Bearer access token (skips login)")
    p.add_argument(
        "--mint-token",
        action="store_true",
        help="Mint JWT for AGENTOS_INTEGRATION_EMAIL (local dev; API must share JWT_SECRET)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print form + SAP payload preview only; do not call API",
    )
    p.add_argument(
        "--preview-sap",
        action="store_true",
        help="Print SAP payload preview before POST",
    )
    p.add_argument("--note", default="", help="Override header_note")


def resolve_token_from_args(args: argparse.Namespace) -> str:
    if args.mint_token:
        os.environ["AGENTOS_INTEGRATION_MINT_TOKEN"] = "1"
    return resolve_access_token(
        email=args.email,
        password=args.password,
        token=args.token or os.environ.get("AGENTOS_ACCESS_TOKEN"),
    )
