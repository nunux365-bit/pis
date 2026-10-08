#!/usr/bin/env python3
"""Pre-merge live gate: PR create/read (+ attachments). YSER update smoke on created PR.

Usage (from agentos-backend/, API on :8000):

  python scripts/integration/run_pre_merge_live.py
  python scripts/integration/run_pre_merge_live.py --sap-only   # no API; direct SAP client
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.integration.integration_reference_defaults import apply_integration_reference_defaults
from app.procurement.field_schema import normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults
from scripts.integration.procurement_api_common import (
    api_base,
    create_pr_via_api_result,
    load_env,
    patch_resync_via_api_result,
)
from scripts.integration.sap_po_doc_fixtures import (
    apply_doc_master_to_po_form,
    apply_yast_pr_doc_env,
)
from scripts.integration.run_pr_create_yast import (
    simple_yast_form,
    yast_multi_cc_form,
    yast_two_material_form,
)
from scripts.integration.run_pr_create_yser import complex_yser_form, simple_yser_form
from scripts.integration.run_pr_create_yunb import (
    simple_yunb_form,
    yunb_multi_cc_form,
    yunb_two_material_form,
)
from scripts.integration.sap_qas_fixtures import (
    apply_yast_sap_qas_fixtures,
    apply_yser_sap_qas_fixtures,
    apply_yunb_sap_qas_fixtures,
)

import httpx


@dataclass
class Row:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        if skip:
            self.rows.append(Row(name, "SKIP", detail))
        else:
            self.rows.append(Row(name, "PASS" if ok else "FAIL", detail))

    def exit_code(self) -> int:
        return 1 if any(r.status == "FAIL" for r in self.rows) else 0

    def print_summary(self) -> None:
        w = max(len(r.name) for r in self.rows) if self.rows else 10
        print("\n" + "=" * 72)
        print("PRE-MERGE LIVE SUMMARY")
        print("=" * 72)
        for r in self.rows:
            print(f"  {r.status:4}  {r.name:<{w}}  {r.detail[:120]}")
        n_pass = sum(1 for r in self.rows if r.status == "PASS")
        n_fail = sum(1 for r in self.rows if r.status == "FAIL")
        n_skip = sum(1 for r in self.rows if r.status == "SKIP")
        print("=" * 72)
        print(f"PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
        print("=" * 72)


def _yser_multi_cc_form(header_note: str) -> dict[str, Any]:
    base = simple_yser_form(header_note=header_note)
    base["lines"][0]["allocations"] = [
        {"cost_center": base["lines"][0]["allocations"][0]["cost_center"], "qty": "1"},
        {
            "cost_center": (
                __import__("os").environ.get("SAP_COST_CENTER_2") or "HCO91001A0"
            ).strip(),
            "qty": "3",
        },
    ]
    return base


async def _verify_yser_z_read(*, pr: str, seed: dict, expect_lines: int) -> tuple[bool, str]:
    from app.procurement.sap_pr_z_client import get_yser_pr
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read
    from app.procurement.sap_ticket_form_read import load_form_from_sap

    body, err = await get_yser_pr(pr_number=pr, ticket_id="pre-merge")
    if err or not body:
        return False, f"get_yser_pr: {err}"
    z_form = form_from_z_pr_read(body, document_type="YSER", seed_form=seed)
    n = len(z_form.get("lines") or [])
    if n < expect_lines:
        return False, f"Z read lines={n} expected>={expect_lines}"
    hydrated, source, hydr_err = await load_form_from_sap(
        kind="PR",
        document_type="YSER",
        sap_id=pr,
        ticket_id="pre-merge",
        seed_form=seed,
    )
    if hydr_err or not hydrated:
        return False, f"hydrate: {hydr_err}"
    hn = len(hydrated.get("lines") or [])
    if hn < expect_lines:
        return False, f"hydrate lines={hn} source={source}"
    return True, f"lines={n} hydrate={hn}"


def _z_item_short_text(body: dict[str, Any]) -> str:
    """Item ``ShortText`` from Z read (not service master ``ShortText``)."""
    from app.procurement.sap_odata_utils import odata_results_list, odata_text

    root = body.get("d") if isinstance(body.get("d"), dict) else body
    if not isinstance(root, dict):
        return ""
    for entry in odata_results_list(root.get("to_Items")):
        if isinstance(entry, dict):
            st = odata_text(entry.get("ShortText"))
            if st:
                return st
    return ""


async def _sap_yser_update_smoke(report: Report, *, pr: str) -> None:
    from app.procurement import sap_pr_client
    from app.procurement.sap_pr_z_client import get_yser_pr
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    raw, err = await get_yser_pr(pr_number=pr, ticket_id="pm-upd-read")
    if err or not raw:
        report.add("YSER update", False, f"read: {err}")
        return
    form = form_from_z_pr_read(raw, document_type="YSER")
    marker = "pre-merge-upd"
    if not form.get("lines"):
        report.add("YSER update", False, "no lines to update")
        return
    form["lines"][0]["short_text"] = marker
    form["lines"][0]["delivery_date"] = "2026-11-20"
    _, upd_err = await sap_pr_client.update_pr(
        sap_id=pr, ticket_id="pm-upd", form=form, document_type="YSER"
    )
    if upd_err:
        report.add("YSER update", False, upd_err)
        return
    raw2, err2 = await get_yser_pr(pr_number=pr, ticket_id="pm-upd-verify")
    if err2 or not raw2:
        report.add("YSER update", False, f"verify read: {err2}")
        return
    got = _z_item_short_text(raw2)
    ok = got == marker
    report.add("YSER update", ok, f"PR {pr} item ShortText={got!r}")


async def _sap_yser_delete_service_smoke(report: Report) -> None:
    """Remove one UI service line — known gap if SAP keeps both services."""
    from app.procurement import sap_pr_client
    from app.procurement.sap_pr_z_client import get_yser_pr
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    tag = uuid.uuid4().hex[:8]
    form = complex_yser_form(header_note=f"pm-del-svc-{tag}")
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    pr, err = await sap_pr_client.create_pr(
        ticket_id=f"pm-del-{tag}", form=form, document_type="YSER"
    )
    if err or not pr:
        report.add("YSER delete service", False, err or "create failed")
        return
    one_line = copy.deepcopy(form)
    one_line["lines"] = [one_line["lines"][0]]
    _, upd_err = await sap_pr_client.update_pr(
        sap_id=pr, ticket_id=f"pm-del-u-{tag}", form=one_line, document_type="YSER"
    )
    if upd_err:
        report.add("YSER delete service", False, upd_err[:120])
        return
    raw, read_err = await get_yser_pr(pr_number=pr, ticket_id="pm-del-v")
    if read_err or not raw:
        report.add("YSER delete service", False, read_err or "read failed")
        return
    n = len(form_from_z_pr_read(raw, document_type="YSER").get("lines") or [])
    ok = n == 1
    report.add(
        "YSER delete service",
        ok,
        f"PR {pr} lines={n} (need 1 after removing service B)",
    )


async def _sap_direct_yser(report: Report) -> dict[str, str]:
    from app.procurement import sap_pr_client

    created: dict[str, str] = {}
    cases = [
        ("YSER SAP simple", simple_yser_form, 1),
        ("YSER SAP multi-cc", _yser_multi_cc_form, 1),
        ("YSER SAP complex", complex_yser_form, 2),
    ]
    tag = uuid.uuid4().hex[:8]
    for name, builder, expect_lines in cases:
        form = builder(header_note=f"pre-merge-{tag}")
        apply_procurement_defaults(form, document_type="YSER", kind="PR")
        tid = f"pm-{tag}-{name.split()[-1]}"
        pr, err = await sap_pr_client.create_pr(
            ticket_id=tid, form=form, document_type="YSER"
        )
        if err or not pr:
            report.add(name, False, err or "no pr")
            continue
        ok, detail = await _verify_yser_z_read(pr=pr, seed=form, expect_lines=expect_lines)
        report.add(name, ok, f"PR {pr} — {detail}")
        if ok:
            created[name] = pr
    return created


async def _mint_integration_token() -> str:
    import os

    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import AsyncSessionLocal
    from app.security.tokens import create_access_token

    email = (os.environ.get("AGENTOS_INTEGRATION_EMAIL") or "login@1mg.com").strip()
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(User).where(User.email == email.lower()))
        user = res.scalar_one_or_none()
        if not user or not user.is_active:
            raise SystemExit(f"No active user for {email!r}")
        return create_access_token(str(user.id), {"role": user.primary_role})


def _api_health() -> bool:
    try:
        with httpx.Client(timeout=5) as c:
            for path in ("/api/health", "/health", "/docs"):
                r = c.get(f"{api_base()}{path}")
                if r.status_code < 500:
                    return True
    except Exception:
        pass
    return False


def _run_pytest(report: Report) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_procurement_sap_pr.py", "tests/test_sap_odata_deferred.py", "-q"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:]
    report.add("unit tests (YSER/YUNB payloads)", proc.returncode == 0, " | ".join(tail))


def _poll_ticket_sap(
    token: str, ticket_id: str, *, timeout_s: float = 180.0
) -> tuple[dict[str, Any] | None, str]:
    import time

    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        with httpx.Client(timeout=60) as client:
            r = client.get(
                f"{api_base()}/api/procurement/tickets/{ticket_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code >= 400:
            return None, f"GET HTTP {r.status_code}"
        last = r.json()
        sap_id = str(last.get("sap_id") or "").strip()
        sync = last.get("sap_sync") if isinstance(last.get("sap_sync"), dict) else {}
        pending = bool(sync.get("sync_pending"))
        err = str(sync.get("last_error") or "").strip()
        if sap_id and not sap_id.startswith("#") and not pending:
            return last, ""
        if err and not pending:
            return last, err
        import time as _t

        _t.sleep(2.5)
    return last, "timeout waiting for sap_id"


def _prep_api_form(dt: str, builder: Any, *, header_note: str) -> dict[str, Any]:
    form = builder(header_note=header_note)
    form = normalize_form(dt, form)
    if dt == "YAST":
        apply_yast_pr_doc_env()
        form = apply_doc_master_to_po_form(form, "YAST")
    apply_procurement_defaults(form, document_type=dt, kind="PR")
    return form


def _whole_alloc_qty(form: dict[str, Any]) -> None:
    """API validation requires positive whole-number allocation qty."""
    for block in form.get("lines") or []:
        if not isinstance(block, dict):
            continue
        for alloc in block.get("allocations") or []:
            if not isinstance(alloc, dict):
                continue
            raw = str(alloc.get("qty") or "1").strip().replace(",", ".")
            try:
                q = max(1, int(round(float(raw))))
            except ValueError:
                q = 1
            alloc["qty"] = str(q)


def _api_update_form(label: str, dt: str, form: dict[str, Any]) -> dict[str, Any]:
    upd = copy.deepcopy(form)
    _whole_alloc_qty(upd)
    lines = upd.get("lines") or []
    if not lines or not isinstance(lines[0], dict):
        return upd
    if dt == "YSER":
        if "multi-cc" in label:
            lines[0]["unit_price"] = "120"
            lines[0]["valuation_price"] = "240"
        elif "complex" in label:
            if isinstance(lines[0], dict):
                lines[0]["unit_price"] = "160"
                lines[0]["valuation_price"] = "480"
            if len(lines) > 1 and isinstance(lines[1], dict):
                lines[1]["unit_price"] = "90"
                lines[1]["valuation_price"] = "90"
        else:
            lines[0]["short_text"] = (str(lines[0].get("short_text") or "Svc") + " api-upd")[:40]
            lines[0]["delivery_date"] = "2026-11-20"
    else:
        lines[0]["unit_price"] = "99"
        lines[0]["valuation_price"] = "99"
    return upd


def _api_create_matrix(report: Report, token: str) -> dict[str, dict[str, Any]]:
    apply_yser_sap_qas_fixtures()
    apply_yunb_sap_qas_fixtures()
    apply_yast_sap_qas_fixtures()
    tag = uuid.uuid4().hex[:8]
    matrix: list[tuple[str, str, Any]] = [
        ("YUNB simple", "YUNB", simple_yunb_form),
        ("YUNB multi-cc", "YUNB", yunb_multi_cc_form),
        ("YUNB two-material", "YUNB", yunb_two_material_form),
        ("YAST simple", "YAST", simple_yast_form),
        ("YAST multi-cc", "YAST", yast_multi_cc_form),
        ("YAST two-material", "YAST", yast_two_material_form),
        ("YSER simple", "YSER", simple_yser_form),
        ("YSER multi-cc", "YSER", _yser_multi_cc_form),
        ("YSER complex", "YSER", complex_yser_form),
    ]
    tickets: dict[str, dict[str, Any]] = {}
    for label, dt, builder in matrix:
        form = _prep_api_form(dt, builder, header_note=f"api-pm-{tag}")
        ticket, err = create_pr_via_api_result(
            access_token=token, document_type=dt, form=form
        )
        if err or not ticket:
            report.add(f"API create {label}", False, err or "no ticket")
            continue
        tid = str(ticket.get("id"))
        ticket, poll_err = _poll_ticket_sap(token, tid)
        if not ticket:
            report.add(f"API create {label}", False, poll_err)
            continue
        sap_id = str(ticket.get("sap_id") or "").strip()
        sync_err = poll_err or ""
        if isinstance(ticket.get("sap_sync"), dict) and not sync_err:
            sync_err = str(ticket["sap_sync"].get("last_error") or "")
        ok = bool(sap_id) and not sap_id.startswith("#") and not sync_err
        report.add(
            f"API create {label}",
            ok,
            f"PR {sap_id} ticket={tid} err={sync_err!r}",
        )
        if ok:
            tickets[label] = ticket
    return tickets


def _api_update_matrix(report: Report, token: str, tickets: dict[str, dict[str, Any]]) -> None:
    for label, ticket in tickets.items():
        tid = str(ticket.get("id") or "")
        version = int(ticket.get("version") or 1)
        form = ticket.get("form") if isinstance(ticket.get("form"), dict) else {}
        dt = label.split()[0]
        upd = _api_update_form(label, dt, form)
        patched, err = patch_resync_via_api_result(
            access_token=token,
            ticket_id=tid,
            form=upd,
            version=version,
        )
        if err or not patched:
            report.add(f"API update {label}", False, (err or "no ticket")[:120])
            continue
        synced, poll_err = _poll_ticket_sap(token, tid)
        if not synced:
            report.add(f"API update {label}", False, poll_err[:120])
            continue
        sync = synced.get("sap_sync") if isinstance(synced.get("sap_sync"), dict) else {}
        last_err = str(sync.get("last_error") or poll_err or "").strip()
        pending = bool(sync.get("sync_pending"))
        ok = not pending and not last_err
        report.add(f"API update {label}", ok, f"PR {synced.get('sap_id')} err={last_err!r}"[:120])


async def _api_yser_read_after_create(report: Report, tickets: dict[str, dict[str, Any]]) -> None:
    import asyncio

    for label in ("YSER simple", "YSER multi-cc", "YSER complex"):
        t = tickets.get(label)
        if not t:
            report.add(f"API read {label}", False, "no ticket from create", skip=False)
            continue
        sap_id = str(t.get("sap_id") or "").strip()
        expect = 2 if "complex" in label else 1
        seed = t.get("form") if isinstance(t.get("form"), dict) else {}
        ok, detail = await _verify_yser_z_read(pr=sap_id, seed=seed, expect_lines=expect)
        report.add(f"API Z read {label}", ok, detail)


def _api_attachment(report: Report, token: str, ticket: dict[str, Any] | None) -> None:
    if not ticket:
        report.add("API YSER attachment", False, "no YSER ticket", skip=True)
        return
    tid = str(ticket.get("id"))
    version = int(ticket.get("version") or 1)
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf)
        path = Path(tmp.name)
    url = f"{api_base()}/api/procurement/tickets/{tid}"
    try:
        with httpx.Client(timeout=300) as client:
            resp = client.patch(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"version": str(version), "payload": '{"resync_sap": false}'},
                files=[("files", ("pre-merge-test.pdf", pdf, "application/pdf"))],
            )
        if resp.status_code >= 400:
            report.add("API YSER attachment", False, f"HTTP {resp.status_code}")
            return
        tid = str(ticket.get("id"))
        import time

        deadline = time.monotonic() + 120.0
        data: dict[str, Any] | None = None
        poll_err = ""
        while time.monotonic() < deadline:
            with httpx.Client(timeout=60) as client:
                r = client.get(
                    f"{api_base()}/api/procurement/tickets/{tid}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code >= 400:
                poll_err = f"GET HTTP {r.status_code}"
                break
            data = r.json()
            sync = data.get("sap_sync") if isinstance(data.get("sap_sync"), dict) else {}
            pending = sync.get("attachments_pending")
            att_err = str(sync.get("attachment_last_error") or "").strip()
            synced = [
                a
                for a in (data.get("attachments") or [])
                if isinstance(a, dict) and a.get("sap_document_id")
            ]
            if synced and not pending:
                poll_err = ""
                break
            if att_err and not pending:
                poll_err = att_err
                break
            time.sleep(2.5)
        else:
            poll_err = poll_err or "attachment sync timeout"
        if not data:
            report.add("API YSER attachment", False, poll_err or "no ticket")
            return
        sync = data.get("sap_sync") if isinstance(data.get("sap_sync"), dict) else {}
        pending = sync.get("attachments_pending")
        att_err = poll_err or sync.get("attachment_last_error")
        synced = [
            a
            for a in (data.get("attachments") or [])
            if isinstance(a, dict) and a.get("sap_document_id")
        ]
        ok = not pending and bool(synced) and not att_err
        report.add(
            "API YSER attachment",
            ok,
            f"synced={len(synced)} pending={pending} err={att_err!r}",
        )
    finally:
        path.unlink(missing_ok=True)


def _api_get_ticket_read(report: Report, token: str, tickets: dict[str, dict[str, Any]]) -> None:
    for label in ("YUNB simple", "YSER simple"):
        t = tickets.get(label)
        if not t:
            continue
        tid = t["id"]
        with httpx.Client(timeout=120) as client:
            r = client.get(
                f"{api_base()}/api/procurement/tickets/{tid}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code >= 400:
            report.add(f"API GET ticket {label}", False, f"HTTP {r.status_code}")
            continue
        body = r.json()
        fs = body.get("form_source")
        lines = len((body.get("form") or {}).get("lines") or [])
        ok = fs in ("sap", "db", None) and lines >= 1
        report.add(f"API GET ticket {label}", ok, f"form_source={fs} lines={lines}")


async def _async_main(args: argparse.Namespace) -> int:
    load_env()
    apply_integration_reference_defaults()
    apply_yser_sap_qas_fixtures()
    report = Report()

    report.add("YSER delete all", True, "use run_yser_z_crud_live — not product API", skip=True)

    _run_pytest(report)

    from app.procurement import sap_pr_client

    if not sap_pr_client.sap_pr_configured():
        report.add("SAP configured", False, "PROCUREMENT_SAP_* missing")
        report.print_summary()
        return 1

    yser_prs = await _sap_direct_yser(report)
    simple_pr = yser_prs.get("YSER SAP simple")
    if simple_pr:
        await _sap_yser_update_smoke(report, pr=simple_pr)
    else:
        report.add("YSER update", False, "no YSER SAP simple PR from create")
    await _sap_yser_delete_service_smoke(report)

    if args.sap_only:
        report.print_summary()
        return report.exit_code()

    if not _api_health():
        report.add(
            "API E2E",
            False,
            f"API not reachable at {api_base()} — start uvicorn app.main:app",
        )
        report.print_summary()
        return report.exit_code()

    token = await _mint_integration_token()
    tickets = _api_create_matrix(report, token)
    _api_update_matrix(report, token, tickets)
    await _api_yser_read_after_create(report, tickets)
    _api_get_ticket_read(report, token, tickets)
    _api_attachment(report, token, tickets.get("YSER simple"))

    report.print_summary()
    return report.exit_code()


def main() -> int:
    p = argparse.ArgumentParser(description="Pre-merge procurement live tests")
    p.add_argument(
        "--sap-only",
        action="store_true",
        help="SAP client tests only (no AgentOS API)",
    )
    args = p.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
