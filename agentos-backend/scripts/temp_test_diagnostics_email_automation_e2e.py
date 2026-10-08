#!/usr/bin/env python3
"""End-to-end test script for the DIAGNOSTICS_AGGREGATOR_VARIANT of PAYMENT_REMINDER_WEEKLY.

Downloads a receivable **.xlsx** from Google Drive (or uses a local file) and
runs the full diagnostics-aggregator pipeline — receivable filtering, aggregation,
recipient resolution, email rendering — optionally triggering real sends.

Pipeline stages exercised
-------------------------
  1. XLSX download from Google Drive (or local file)
  2. Diagnostics master tracker load from Google Sheets
  3. ``Receivable as on <date>`` sheet read + filter
     (Business Unit ∈ {e-Diagnostic-Aggregator, e-Pharmacy (Platform Aggregator)},
      Name of Business owner = Ashyin Thakral/Prateek verma, Net Due ≥ 1)
  4. ``Party wise Ageing-H&T`` lookup for Unaccounted Revenue
  5. Post-lookup decision gates (potential_overpayment, insufficient_outstanding)
  6. Recipient resolution: ``Code → Mail ID 1`` (TO) + ``Mail ID 2..11`` (CC)
  7. Subject + HTML body rendering
  8. *(optional)* Email dispatch via Gmail SA (``--mode send``)

Usage
-----
  cd agentos-backend
  set -a; source .env; set +a
  export GOOGLE_APPLICATION_CREDENTIALS=…   # or GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON

  # Dry-run — build plans and print table (no emails):
  python scripts/temp_test_diagnostics_email_automation_e2e.py \\
      --receivable-file-id 1s7Q9lXIHk_5q-6NTse-hQ0P4SyPihpcs \\
      --diagnostics-sheet-id 1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY

  # Dry-run with a local .xlsx:
  python scripts/temp_test_diagnostics_email_automation_e2e.py \\
      --receivable-local-xlsx ~/Downloads/Receivables.xlsx \\
      --diagnostics-sheet-id 1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY

  # Send emails (test_mode redirects to EMAIL_AUTOMATION_TEST_REDIRECT_TO):
  python scripts/temp_test_diagnostics_email_automation_e2e.py \\
      --receivable-file-id 1kwJTHVLBeUR_HghpE477T95zPX12k8Aq \\
      --diagnostics-sheet-id 1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY \\
      --mode send

  # Send to specific business keys only (targeted test):
  python scripts/temp_test_diagnostics_email_automation_e2e.py \\
      --receivable-local-xlsx ~/Downloads/Receivables.xlsx \\
      --diagnostics-sheet-id 1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY \\
      --mode send \\
      --business-keys 1000001489,1000001490 \\
      --limit 5

Env vars
--------
  RECEIVABLE_DRIVE_FILE_ID
      Drive file ID for the receivable workbook (fallback when --receivable-file-id is unset).
  EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID
      Diagnostics master Google Sheet ID (fallback when --diagnostics-sheet-id is unset).
  GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON  or  GOOGLE_APPLICATION_CREDENTIALS
      Service account for Drive download + Sheets read.
  RECEIVABLE_LOCAL_XLSX
      Skip Drive download; use this local .xlsx path (lowest priority vs --receivable-local-xlsx).
  EMAIL_AUTOMATION_TEST_MODE  (from .env / settings)
      When true (default), all emails are redirected to EMAIL_AUTOMATION_TEST_REDIRECT_TO.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(_p=None) -> None:  # type: ignore[misc]
        return None


# ── Env bootstrap ──────────────────────────────────────────────────────────────

def _load_env() -> None:
    for p in (_ROOT / ".env", _ROOT.parent / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)
            return
    load_dotenv()


# ── Google Drive helpers ───────────────────────────────────────────────────────

def _drive_v3():
    """Build a Drive API v3 client using the service-account credentials."""
    import httplib2
    from google.oauth2 import service_account
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    sa = os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if not sa:
        raise SystemExit(
            "Set GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON "
            "to the service account JSON path."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(sa), scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    http = httplib2.Http(timeout=900)
    authed = AuthorizedHttp(creds, http=http)
    return build("drive", "v3", http=authed, cache_discovery=False)


def _drive_download_xlsx(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    d = _drive_v3()
    buf = io.BytesIO()
    req = d.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, req, chunksize=10 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _drive_meta(file_id: str) -> dict:
    d = _drive_v3()
    return d.files().get(
        fileId=file_id, fields="id,name,mimeType,modifiedTime,size"
    ).execute()


# ── Tracker loader ─────────────────────────────────────────────────────────────

async def _load_tracker_rows(sheet_id: str, tab: str, header_row_index: int) -> list[dict]:
    """Read the diagnostics master tracker from Google Sheets."""
    from app.email_automation import sheets_sa

    logging.info(
        "Loading diagnostics master tracker: sheet_id=%s tab=%r header_row=%d",
        sheet_id, tab, header_row_index,
    )
    tbl = await asyncio.to_thread(
        sheets_sa.read_table,
        sheet_id,
        tab,
        header_row_index=header_row_index,
    )
    rows = tbl.dicts()
    logging.info("Tracker: %d row(s) loaded", len(rows))
    return rows


# ── Plan builder ───────────────────────────────────────────────────────────────

async def _build_plans(
    xlsx_path: Path,
    tracker_rows: list[dict],
    period: str,
) -> list[dict[str, Any]]:
    """Run the diagnostics-variant pipeline and return per-client send plans.

    Pure: no DB writes, no Gmail API calls.
    """
    from app.email_automation.pipeline.process import build_variant_plans
    from app.email_automation.workflow_packs.payment_reminder import (
        DIAGNOSTICS_AGGREGATOR_VARIANT,
        PAYMENT_REMINDER_WEEKLY,
    )

    logging.info(
        "Building diagnostics plans from %s (period=%s) …", xlsx_path.name, period
    )
    plans = await build_variant_plans(
        pack=PAYMENT_REMINDER_WEEKLY,
        variant=DIAGNOSTICS_AGGREGATOR_VARIANT,
        xlsx_path=xlsx_path,
        tracker_rows=tracker_rows,
        period=period,
    )
    logging.info("build_variant_plans → %d plan(s)", len(plans))
    return plans


# ── Email dispatch (send mode) ─────────────────────────────────────────────────

def _dispatch_plans(
    plans: list[dict[str, Any]],
    *,
    limit: int,
    business_keys: set[str] | None,
    send_skipped_ar_notifications: bool,
) -> list[dict[str, Any]]:
    """Send emails for the given plans.

    Mirrors the ``_shape_recipients_for_persist`` logic from the production
    pipeline to ensure exact behaviour parity:

    * Non-skipped plans  → send to ``resolved_to`` / ``resolved_cc``.
    * Skipped plans with ``static_to`` → notify the AR desk only (if
      ``send_skipped_ar_notifications=True``).

    ``settings.email_automation_test_mode`` is respected automatically by
    :func:`~app.email_automation.engine.sender.send` (redirects TO/CC to
    ``settings.email_automation_test_redirect_to``).
    """
    from app.email_automation.engine import sender as _sender
    from app.email_automation.pipeline.process import _skip_reason_line  # type: ignore[attr-defined]
    from app.email_automation.workflow_packs.payment_reminder import DIAGNOSTICS_AGGREGATOR_VARIANT

    results: list[dict[str, Any]] = []
    sent_count = 0

    for plan in plans:
        bk: str = plan.get("business_key") or ""

        # ── Business-key allowlist filter ─────────────────────────────────────
        if business_keys and bk not in business_keys:
            results.append({
                "business_key": bk,
                "status": "skipped_filter",
                "detail": "not in --business-keys allowlist",
            })
            continue

        skip_gate = plan.get("skip_gate")
        review_reasons: list[dict] = list(plan.get("review_reasons") or [])

        # ── Determine TO / CC / body ──────────────────────────────────────────
        if skip_gate:
            if not send_skipped_ar_notifications:
                results.append({
                    "business_key": bk,
                    "status": "skipped",
                    "detail": (
                        skip_gate.get("code")
                        if isinstance(skip_gate, dict)
                        else str(skip_gate)
                    ),
                })
                continue

            # AR-desk notification for skipped rows (mirrors production logic)
            static_to = list(DIAGNOSTICS_AGGREGATOR_VARIANT.resolver.static_to or ())
            if not static_to:
                results.append({
                    "business_key": bk,
                    "status": "skipped",
                    "detail": "no_static_to_for_ar_notification",
                })
                continue

            stored_reasons = review_reasons + [skip_gate] if (review_reasons or skip_gate) else None
            to_addrs = static_to
            cc_addrs: list[str] = []
            body_html = _skip_reason_line(stored_reasons) + (plan.get("rendered_body_html") or "")
            subject = plan.get("rendered_subject") or ""

        else:
            # Non-blocking review reasons log a warning but don't block sends
            blocking = [r for r in review_reasons if r.get("code") != "tracker_duplicate_keys"]
            if blocking:
                logging.warning(
                    "Plan %s has blocking review reasons: %s — skipping send",
                    bk, [r.get("code") for r in blocking],
                )
                results.append({
                    "business_key": bk,
                    "status": "skipped_review",
                    "detail": [r.get("code") for r in blocking],
                })
                continue

            to_addrs = list(plan.get("resolved_to") or [])
            cc_addrs = list(plan.get("resolved_cc") or [])
            subject = plan.get("rendered_subject") or ""
            body_html = plan.get("rendered_body_html") or ""

        if not to_addrs:
            results.append({
                "business_key": bk,
                "status": "skipped",
                "detail": "no_primary_recipient",
            })
            continue

        # ── Send-limit cap ────────────────────────────────────────────────────
        if sent_count >= limit:
            results.append({
                "business_key": bk,
                "status": "skipped_limit",
                "detail": f"send limit ({limit}) reached",
            })
            continue

        # ── Fire ──────────────────────────────────────────────────────────────
        try:
            outcome = _sender.send(
                _sender.OutboundEmail(
                    subject=subject,
                    body_html=body_html,
                    body_text=None,
                    resolved_to=tuple(to_addrs),
                    resolved_cc=tuple(cc_addrs),
                )
            )
            sent_count += 1
            results.append({
                "business_key": bk,
                "status": "sent",
                "was_skipped_plan": bool(skip_gate),
                "wire_to": list(outcome.wire_to),
                "wire_cc": list(outcome.wire_cc),
                "test_mode": outcome.test_mode,
                "provider_message_id": outcome.provider_message_id,
            })
        except Exception as exc:
            logging.exception("Send failed for business_key=%s", bk)
            results.append({
                "business_key": bk,
                "status": "failed",
                "error": str(exc),
            })

    return results


# ── Display helpers ────────────────────────────────────────────────────────────

_COL_WIDTHS = (20, 30, 15, 6, 18, 6, 36)
_HEADERS = (
    "Business Key",
    "Party Name",
    "Net Pending ₹",
    "Rows",
    "Skip Reason",
    "Review",
    "Resolved TO",
)


def _print_plans_table(plans: list[dict[str, Any]]) -> None:
    """Print a human-readable table of all plans to stderr."""
    if not plans:
        print("  (no plans — check invoice sheet filters / workbook content)", file=sys.stderr)
        return

    sep = "  ".join("─" * w for w in _COL_WIDTHS)
    fmt = "  ".join(f"{{:<{w}}}" for w in _COL_WIDTHS)

    print(fmt.format(*_HEADERS), file=sys.stderr)
    print(sep, file=sys.stderr)

    for p in plans:
        bk = str(p.get("business_key") or "")[: _COL_WIDTHS[0]]
        name = str(p.get("party_name") or "")[: _COL_WIDTHS[1]]

        totals = p.get("totals") or {}
        net_raw = totals.get("_net_pending") or totals.get("net_pending") or ""
        try:
            net_str = f"{float(net_raw):>12,.2f}" if net_raw else ""
        except (TypeError, ValueError):
            net_str = str(net_raw)[: _COL_WIDTHS[2]]

        rows_str = str(p.get("row_count") or "")

        skip_gate = p.get("skip_gate")
        skip_str = ""
        if skip_gate:
            skip_str = (
                skip_gate.get("code") if isinstance(skip_gate, dict) else str(skip_gate)
            )[: _COL_WIDTHS[4]]

        review_count = len(p.get("review_reasons") or [])
        review_str = str(review_count) if review_count else ""

        to_addrs = p.get("resolved_to") or []
        to_str = ", ".join(to_addrs)[: _COL_WIDTHS[6]]

        print(
            fmt.format(bk, name, net_str, rows_str, skip_str, review_str, to_str),
            file=sys.stderr,
        )


def _print_send_results(results: list[dict[str, Any]]) -> None:
    """Print send results to stderr."""
    for r in results:
        bk = r.get("business_key", "")
        status = r.get("status", "")

        if status == "sent":
            test_badge = "🧪 [TEST]" if r.get("test_mode") else "🚀 [LIVE]"
            wire_to = r.get("wire_to") or []
            wire_cc = r.get("wire_cc") or []
            ar_note = "  ← AR-desk notification" if r.get("was_skipped_plan") else ""
            msg_id = str(r.get("provider_message_id") or "")[:24]
            print(
                f"  ✓ SENT  {test_badge}  {bk:<22}  to={wire_to}  cc={wire_cc}"
                f"  msg={msg_id}{ar_note}",
                file=sys.stderr,
            )
        elif status in ("skipped", "skipped_filter", "skipped_limit", "skipped_review"):
            detail = r.get("detail") or ""
            print(f"  ⤵ SKIP  {bk!s:<22}  {detail}", file=sys.stderr)
        else:
            err = r.get("error", "unknown")
            print(f"  ✗ FAIL  {bk!s:<22}  {err}", file=sys.stderr)


# ── Async entrypoint ───────────────────────────────────────────────────────────

async def run(
    *,
    xlsx_path: Path,
    diagnostics_sheet_id: str,
    period: str,
    mode: str,
    limit: int,
    business_keys: set[str] | None,
    send_skipped_ar_notifications: bool,
) -> dict[str, Any]:
    """Full pipeline run — returns a summary dict (printed as JSON at exit)."""

    from app.email_automation.workflow_packs.payment_reminder import DIAGNOSTICS_AGGREGATOR_VARIANT
    from app.config.settings import settings

    # Resolve the tab name from settings (supports override via env)
    tab_name: str = (
        getattr(settings, DIAGNOSTICS_AGGREGATOR_VARIANT.master_tab_setting, "") or "Lab"
    )

    # Load tracker rows directly from the provided sheet ID so the script
    # can be used with any sheet without touching .env / settings.
    tracker_rows = await _load_tracker_rows(
        diagnostics_sheet_id,
        tab_name,
        DIAGNOSTICS_AGGREGATOR_VARIANT.master_header_row_index,
    )

    plans = await _build_plans(xlsx_path, tracker_rows, period)

    # ── Summary banner ────────────────────────────────────────────────────────
    skipped_plans = [p for p in plans if p.get("skip_gate")]
    active_plans = [p for p in plans if not p.get("skip_gate")]
    review_plans = [p for p in active_plans if p.get("review_reasons")]

    divider = "─" * 72
    print(f"\n{divider}", file=sys.stderr)
    print(f"  DIAGNOSTICS_AGGREGATOR_VARIANT — PAYMENT_REMINDER_WEEKLY", file=sys.stderr)
    print(f"  Period       : {period}", file=sys.stderr)
    print(f"  Workbook     : {xlsx_path.name}", file=sys.stderr)
    print(f"  Master sheet : {diagnostics_sheet_id}", file=sys.stderr)
    print(f"  Tracker rows : {len(tracker_rows)}", file=sys.stderr)
    print(f"{divider}", file=sys.stderr)
    print(f"  Total plans  : {len(plans)}", file=sys.stderr)
    print(f"  Active       : {len(active_plans)}", file=sys.stderr)
    print(f"  Skipped      : {len(skipped_plans)}", file=sys.stderr)
    print(f"  With reviews : {len(review_plans)}", file=sys.stderr)
    if settings.email_automation_test_mode:
        print(
            f"\n  ⚠  TEST MODE — all emails redirect to "
            f"{settings.email_automation_test_redirect_to!r}",
            file=sys.stderr,
        )
    else:
        print("\n  🚨 LIVE MODE — emails go to REAL recipients!", file=sys.stderr)
    print(f"{divider}\n", file=sys.stderr)

    _print_plans_table(plans)

    # ── Send (optional) ───────────────────────────────────────────────────────
    send_results: list[dict[str, Any]] | None = None
    if mode == "send":
        print(f"\n  Dispatching emails (limit={limit}) …\n", file=sys.stderr)
        send_results = _dispatch_plans(
            plans,
            limit=limit,
            business_keys=business_keys,
            send_skipped_ar_notifications=send_skipped_ar_notifications,
        )
        print("", file=sys.stderr)
        _print_send_results(send_results)

        sent = sum(1 for r in send_results if r["status"] == "sent")
        failed = sum(1 for r in send_results if r["status"] == "failed")
        skipped = sum(1 for r in send_results if r["status"].startswith("skip"))
        print(
            f"\n  Dispatch summary: sent={sent}  failed={failed}  skipped={skipped}",
            file=sys.stderr,
        )

    return {
        "ok": True,
        "period": period,
        "workbook": xlsx_path.name,
        "diagnostics_sheet_id": diagnostics_sheet_id,
        "tracker_rows": len(tracker_rows),
        "plans_total": len(plans),
        "plans_active": len(active_plans),
        "plans_skipped": len(skipped_plans),
        "plans_with_reviews": len(review_plans),
        "mode": mode,
        "send_results": send_results,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    from datetime import date as _date

    parser = argparse.ArgumentParser(
        description=(
            "End-to-end test for DIAGNOSTICS_AGGREGATOR_VARIANT — PAYMENT_REMINDER_WEEKLY.\n\n"
            "Builds per-client send plans from a receivable workbook and the\n"
            "diagnostics master tracker, then optionally dispatches the emails."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Input: receivable workbook ────────────────────────────────────────────
    src_grp = parser.add_mutually_exclusive_group()
    src_grp.add_argument(
        "--receivable-file-id",
        "-r",
        metavar="DRIVE_FILE_ID",
        default="",
        help=(
            "Google Drive file ID of the receivable .xlsx workbook.\n"
            "Falls back to the RECEIVABLE_DRIVE_FILE_ID environment variable."
        ),
    )
    src_grp.add_argument(
        "--receivable-local-xlsx",
        "-l",
        metavar="PATH",
        default="",
        help=(
            "Local .xlsx path to use instead of downloading from Drive.\n"
            "Also honoured via the RECEIVABLE_LOCAL_XLSX environment variable."
        ),
    )

    # ── Input: diagnostics master sheet ──────────────────────────────────────
    parser.add_argument(
        "--diagnostics-sheet-id",
        "-d",
        metavar="SHEET_ID",
        default="",
        help=(
            "Google Sheets spreadsheet ID for the diagnostics master tracker "
            "(e.g. 1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY).\n"
            "Falls back to EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID env var,\n"
            "then to settings.email_automation_diagnostics_master_sheet_id."
        ),
    )

    # ── Run control ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--mode",
        choices=["dry-run", "send"],
        default="dry-run",
        help=(
            "dry-run (default): build and display plans only — no emails sent.\n"
            "send: build plans and dispatch emails via Gmail SA."
        ),
    )
    parser.add_argument(
        "--period",
        metavar="YYYY-Www",
        default="",
        help=(
            "ISO-week period string, e.g. 2026-W22. "
            "Defaults to the current calendar week."
        ),
    )
    parser.add_argument(
        "--business-keys",
        metavar="KEY1,KEY2",
        default="",
        help=(
            "Comma-separated business keys (HANA codes) to restrict sending.\n"
            "Empty (default) = process all eligible plans up to --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help=(
            "Maximum number of emails to send per run (safety cap; default 10). "
            "Only applies in --mode send."
        ),
    )
    parser.add_argument(
        "--send-skipped-ar-notifications",
        action="store_true",
        default=False,
        help=(
            "In --mode send, also dispatch AR-desk notifications for skipped plans "
            "(mirrors production behaviour for potential_overpayment / "
            "insufficient_outstanding)."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _load_env()

    # Settings are imported AFTER _load_env() so env overrides are visible.
    from app.config.settings import settings

    # ── Resolve receivable workbook ───────────────────────────────────────────
    # Precedence must be explicit CLI flag > env var fallback.
    # In particular, --receivable-file-id should not be shadowed by
    # RECEIVABLE_LOCAL_XLSX from the shell/.env.
    cli_local_xlsx = (args.receivable_local_xlsx or "").strip()
    env_local_xlsx = os.environ.get("RECEIVABLE_LOCAL_XLSX", "").strip()
    cli_file_id = (args.receivable_file_id or "").strip()
    env_file_id = os.environ.get("RECEIVABLE_DRIVE_FILE_ID", "").strip()

    local_xlsx = cli_local_xlsx or env_local_xlsx
    file_id = cli_file_id or env_file_id

    path: Path
    tmp_path: str | None = None

    if cli_local_xlsx:
        path = Path(cli_local_xlsx).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Local XLSX not found: {path}")
        if path.suffix.lower() != ".xlsx":
            logging.warning("Expected .xlsx extension; continuing anyway: %s", path)
        print(f"Using local workbook (--receivable-local-xlsx): {path}", file=sys.stderr)

    elif cli_file_id:
        if env_local_xlsx:
            logging.info(
                "Ignoring RECEIVABLE_LOCAL_XLSX because --receivable-file-id was provided: %s",
                env_local_xlsx,
            )
        print(f"Downloading receivable workbook from Drive (file_id={cli_file_id}) …", file=sys.stderr)
        meta = _drive_meta(cli_file_id)
        print(
            f"  Name    : {meta.get('name')}\n"
            f"  Size    : {meta.get('size', '?')} bytes\n"
            f"  Modified: {meta.get('modifiedTime')}",
            file=sys.stderr,
        )
        raw = _drive_download_xlsx(cli_file_id)
        with tempfile.NamedTemporaryFile(
            prefix="diag_test_recv_", suffix=".xlsx", delete=False
        ) as fh:
            fh.write(raw)
            tmp_path = fh.name
        path = Path(tmp_path)
        print(f"  → saved to {path}", file=sys.stderr)

    elif file_id:
        print(f"Downloading receivable workbook from Drive (file_id={file_id}) …", file=sys.stderr)
        meta = _drive_meta(file_id)
        print(
            f"  Name    : {meta.get('name')}\n"
            f"  Size    : {meta.get('size', '?')} bytes\n"
            f"  Modified: {meta.get('modifiedTime')}",
            file=sys.stderr,
        )
        raw = _drive_download_xlsx(file_id)
        with tempfile.NamedTemporaryFile(
            prefix="diag_test_recv_", suffix=".xlsx", delete=False
        ) as fh:
            fh.write(raw)
            tmp_path = fh.name
        path = Path(tmp_path)
        print(f"  → saved to {path}", file=sys.stderr)

    elif local_xlsx:
        path = Path(local_xlsx).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(
                "Local XLSX not found from RECEIVABLE_LOCAL_XLSX: "
                f"{path}\n"
                "Either unset RECEIVABLE_LOCAL_XLSX or pass --receivable-file-id / "
                "--receivable-local-xlsx explicitly."
            )
        if path.suffix.lower() != ".xlsx":
            logging.warning("Expected .xlsx extension; continuing anyway: %s", path)
        print(f"Using local workbook (RECEIVABLE_LOCAL_XLSX): {path}", file=sys.stderr)

    else:
        raise SystemExit(
            "Provide a receivable workbook via --receivable-file-id / "
            "RECEIVABLE_DRIVE_FILE_ID  OR  --receivable-local-xlsx / RECEIVABLE_LOCAL_XLSX."
        )

    # ── Resolve diagnostics master sheet ID ───────────────────────────────────
    diag_sheet_id = (
        args.diagnostics_sheet_id
        or os.environ.get("EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID", "")
        or settings.email_automation_diagnostics_master_sheet_id
        or ""
    ).strip()
    if not diag_sheet_id:
        raise SystemExit(
            "Provide a diagnostics master sheet ID via --diagnostics-sheet-id or "
            "EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID / "
            "email_automation_diagnostics_master_sheet_id in .env."
        )

    # ── Resolve period ────────────────────────────────────────────────────────
    period = args.period.strip()
    if not period:
        iso = _date.today().isocalendar()
        period = f"{iso.year}-W{iso.week:02d}"

    # ── Resolve business-key filter ───────────────────────────────────────────
    bk_filter: set[str] | None = None
    if args.business_keys.strip():
        bk_filter = {k.strip() for k in args.business_keys.split(",") if k.strip()}
        print(f"Business-key filter: {sorted(bk_filter)}", file=sys.stderr)

    # ── Execute ───────────────────────────────────────────────────────────────
    result: dict[str, Any] = {}
    try:
        result = asyncio.run(
            run(
                xlsx_path=path,
                diagnostics_sheet_id=diag_sheet_id,
                period=period,
                mode=args.mode,
                limit=args.limit,
                business_keys=bk_filter,
                send_skipped_ar_notifications=args.send_skipped_ar_notifications,
            )
        )
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()