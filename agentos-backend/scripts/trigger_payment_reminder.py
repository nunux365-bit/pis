#!/usr/bin/env python3
"""Trigger the PAYMENT_REMINDER_WEEKLY pipeline for a chosen variant.

Builds per-client send plans from a receivable workbook (Drive file or local
.xlsx) and a Google Sheets master tracker, then optionally dispatches emails.

Supported variants
------------------
  diagnostics_aggregator  (DIAGNOSTICS_AGGREGATOR_VARIANT)  — Invoice details-H&T
  platform_aggregator     (PLATFORM_AGGREGATOR_VARIANT)     — Invoice wise Agg.
  epharma                 (EPHARMA_VARIANT)
  chw                     (CHW_VARIANT)

Both aggregator variants share the diagnostics (Lab) master tracker, so the
same ``--master-sheet-id`` / ``EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID``
applies to each.

Pipeline stages
---------------
  1. Receivable .xlsx download from Google Drive  OR  use a local file
  2. Master tracker load from Google Sheets
  3. Sheet read + DSL filter + projection + aggregation
  4. Lookup sheets (unaccounted revenue)
  5. Post-lookup decision gates (overpayment / insufficient_outstanding)
  6. Recipient resolution via master tracker
  7. Subject + HTML body rendering
  8. (optional) Email dispatch — two sub-modes:
       a. ``--mode send`` — legacy in-process send via Gmail SA.
       b. ``--mode send --persist`` — persist rows to DB as
          ``EmailAutomationSend`` (same upsert path as the production
          graph), then invoke the graph's dispatch node so sends get a
          full DB audit trail (status, provider_message_id, etc.).

Usage
-----
  cd agentos-backend
  set -a; source .env; set +a
  export GOOGLE_APPLICATION_CREDENTIALS=…   # service account JSON

  # Dry-run — build and print plans, no emails:
  python scripts/trigger_payment_reminder.py \\
      --variant diagnostics_aggregator \\
      --receivable-file-id <DRIVE_FILE_ID> \\
      --master-sheet-id   <SHEET_ID>

  # Dry-run with a local .xlsx:
  python scripts/trigger_payment_reminder.py \\
      --variant epharma \\
      --receivable-local-xlsx ~/Downloads/Receivables.xlsx \\
      --master-sheet-id   <SHEET_ID>

  # Send emails via legacy in-process sender (no DB persistence):
  python scripts/trigger_payment_reminder.py \\
      --variant diagnostics_aggregator \\
      --receivable-file-id <DRIVE_FILE_ID> \\
      --master-sheet-id   <SHEET_ID> \\
      --mode send \\
      --limit 5

  # Persist to DB and dispatch via graph node (recommended — full audit trail):
  python scripts/trigger_payment_reminder.py \\
      --variant diagnostics_aggregator \\
      --receivable-file-id <DRIVE_FILE_ID> \\
      --master-sheet-id   <SHEET_ID> \\
      --mode send \\
      --persist \\
      --limit 5

  # Persist only (queue rows for human approval / later dispatch):
  python scripts/trigger_payment_reminder.py \\
      --variant diagnostics_aggregator \\
      --receivable-file-id <DRIVE_FILE_ID> \\
      --master-sheet-id   <SHEET_ID> \\
      --mode send \\
      --persist \\
      --no-dispatch-after-persist

  # Send for specific business keys only:
  python scripts/trigger_payment_reminder.py \\
      --variant epharma \\
      --receivable-local-xlsx ~/Downloads/Receivables.xlsx \\
      --master-sheet-id   <SHEET_ID> \\
      --mode send \\
      --business-keys 1000001489,1000001490 \\
      --limit 3

Behind a corporate proxy (TLS-intercepting)
-------------------------------------------
Google Sheets (tracker) and Gmail (send) go through Python's ``httplib2``, which
needs THREE things to work behind the proxy — the flag alone is not enough:

  1. PySocks in the venv (httplib2's proxy backend; not a project dependency)::

         uv pip install --native-tls pysocks     # installs into .venv only

  2. LOWERCASE proxy env — httplib2 ignores the uppercase HTTP(S)_PROXY vars::

         export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"

  3. ``--insecure-local-tls`` — relax strict X.509 for the proxy's re-signed CA.

  Full example::

      uv pip install --native-tls pysocks
      export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"
      python scripts/trigger_payment_reminder.py --variant platform_aggregator \\
          --receivable-local-xlsx Receivable-08-07.2026.xlsx \\
          --mode send --business-keys 1000017221 --limit 1 --insecure-local-tls

Env-var fallbacks (used when the corresponding CLI flag is omitted)
-------------------------------------------------------------------
  RECEIVABLE_DRIVE_FILE_ID
      Drive file ID for the receivable workbook.
  RECEIVABLE_LOCAL_XLSX
      Local .xlsx path (lowest-priority receivable source).
  EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID
      Master sheet ID for the diagnostics_aggregator variant.
  EMAIL_AUTOMATION_EPHARMA_MASTER_SHEET_ID
      Master sheet ID for the epharma variant.
  GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON  or  GOOGLE_APPLICATION_CREDENTIALS
      Service account for Drive download + Sheets read.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
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


from app.email_automation.pipeline.dispatch import DEFAULT_DISPATCH_SEND_BATCH

# ── Variant registry ──────────────────────────────────────────────────────────

# Maps CLI name → (VariantConfig attribute name, env-var for master sheet ID)
_VARIANT_REGISTRY: dict[str, tuple[str, str]] = {
    "diagnostics_aggregator": (
        "DIAGNOSTICS_AGGREGATOR_VARIANT",
        "EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID",
    ),
    "platform_aggregator": (
        "PLATFORM_AGGREGATOR_VARIANT",
        # Shares the diagnostics (Lab) master tracker — same sheet id + tab.
        "EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID",
    ),
    "epharma": (
        "EPHARMA_VARIANT",
        "EMAIL_AUTOMATION_EPHARMA_MASTER_SHEET_ID",
    ),
    "chw": (
        "CHW_VARIANT",
        "EMAIL_AUTOMATION_CHW_MASTER_SHEET_ID",
    ),
}


def _get_variant(variant_name: str):
    """Return the VariantConfig object for *variant_name*."""
    attr, _ = _VARIANT_REGISTRY[variant_name]
    from app.email_automation.workflow_packs import payment_reminder as _pr

    return getattr(_pr, attr)


# ── Env bootstrap ─────────────────────────────────────────────────────────────


def _load_env() -> None:
    for p in (_ROOT / ".env", _ROOT.parent / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)
            return
    load_dotenv()


# ── Google Drive helpers ──────────────────────────────────────────────────────


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


# ── Tracker loader ────────────────────────────────────────────────────────────


async def _load_tracker_rows(sheet_id: str, tab: str, header_row_index: int) -> list[dict]:
    """Read the master tracker from Google Sheets."""
    from app.email_automation import sheets_sa

    logging.info(
        "Loading master tracker: sheet_id=%s  tab=%r  header_row=%d",
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


# ── Plan builder ──────────────────────────────────────────────────────────────


async def _build_plans(
    xlsx_path: Path,
    tracker_rows: list[dict],
    variant_name: str,
    period: str,
) -> list[dict[str, Any]]:
    """Run the variant pipeline and return per-client send plans (pure — no IO)."""
    from app.email_automation.pipeline.process import build_variant_plans
    from app.email_automation.workflow_packs.payment_reminder import PAYMENT_REMINDER_WEEKLY

    variant = _get_variant(variant_name)
    logging.info(
        "Building plans: variant=%s  workbook=%s  period=%s",
        variant_name, xlsx_path.name, period,
    )
    plans = await build_variant_plans(
        pack=PAYMENT_REMINDER_WEEKLY,
        variant=variant,
        xlsx_path=xlsx_path,
        tracker_rows=tracker_rows,
        period=period,
    )
    logging.info("build_variant_plans → %d plan(s)", len(plans))
    return plans


# ── Email dispatch ────────────────────────────────────────────────────────────


def _dispatch_plans(
    plans: list[dict[str, Any]],
    *,
    variant_name: str,
    limit: int,
    business_keys: set[str] | None,
    send_skipped_ar_notifications: bool,
) -> list[dict[str, Any]]:
    """Send emails for the given plans.

    * Non-skipped plans  → send to ``resolved_to`` / ``resolved_cc``.
    * Skipped plans with ``static_to`` → notify the AR desk only (when
      ``send_skipped_ar_notifications=True``).

    ``settings.email_automation_test_mode`` is respected automatically by
    :func:`~app.email_automation.engine.sender.send` at the wire layer.
    Resolved recipients persisted on the row remain the business-intended
    addresses so previews / audit trails still show the real audience.
    """
    from app.email_automation.engine import sender as _sender
    from app.email_automation.pipeline.process import _skip_reason_line  # type: ignore[attr-defined]

    variant = _get_variant(variant_name)

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
            static_to = list(variant.resolver.static_to or ())
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


# ── Display helpers ───────────────────────────────────────────────────────────

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

        print(fmt.format(bk, name, net_str, rows_str, skip_str, review_str, to_str), file=sys.stderr)


def _print_send_results(results: list[dict[str, Any]]) -> None:
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
            print(f"  ↵ SKIP  {bk!s:<22}  {detail}", file=sys.stderr)
        else:
            err = r.get("error", "unknown")
            print(f"  ✗ FAIL  {bk!s:<22}  {err}", file=sys.stderr)


# ── DB-persist + graph-dispatch helpers ──────────────────────────────────────


async def _persist_plans(
    plans: list[dict[str, Any]],
    *,
    variant_name: str,
    period: str,
    business_keys: set[str] | None,
) -> dict[str, Any]:
    """Persist send plans as ``EmailAutomationSend`` rows via the same
    idempotent upsert used by the production graph.

    Returns a summary dict with counts of inserted / skipped (dedupe) rows.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.config.settings import settings
    from app.db.models import EmailAutomationSend
    from app.db.session import AsyncSessionLocal
    from app.email_automation.pipeline._shared import dedupe_key, jsonable, now_utc
    from app.email_automation.pipeline.process import (
        _NON_BLOCKING_REVIEW_REASON_CODES,  # type: ignore[attr-defined]
        _is_skip_gate,  # type: ignore[attr-defined]
        _shape_recipients_for_persist,  # type: ignore[attr-defined]
    )
    from app.email_automation.workflow_packs.payment_reminder import PAYMENT_REMINDER_WEEKLY

    workflow_type = PAYMENT_REMINDER_WEEKLY.workflow_type
    variant = _get_variant(variant_name)

    inserted_keys: list[str] = []
    skipped_dedupe: list[str] = []
    skipped_filter: list[str] = []

    async with AsyncSessionLocal() as db:
        for plan in plans:
            bk: str = plan.get("business_key") or ""

            # ── Business-key allowlist filter ─────────────────────────────────
            if business_keys and bk not in business_keys:
                skipped_filter.append(bk)
                continue

            skip_gate = plan.get("skip_gate") or None
            review_reasons: list[dict] = list(plan.get("review_reasons") or [])

            blocking_reasons = [
                r for r in review_reasons
                if r.get("code") not in _NON_BLOCKING_REVIEW_REASON_CODES
            ]
            if skip_gate or blocking_reasons:
                status = "skipped"
            elif settings.email_automation_require_approval:
                status = "rendered"
            else:
                status = "approved"

            stored_reasons = (
                review_reasons + ([skip_gate] if skip_gate else [])
                if (review_reasons or skip_gate)
                else None
            )

            to_addrs, cc_addrs, body_html = _shape_recipients_for_persist(
                plan=plan,
                variant=variant,
                status=status,
                stored_reasons=stored_reasons,
            )

            dedupe = dedupe_key(workflow_type, variant.name, bk, period)

            aggregated_snapshot = jsonable(
                {
                    "variant": variant.name,
                    "business_key_parts": plan.get("business_key_parts") or [],
                    "row_count": plan.get("row_count") or 0,
                    "totals": plan.get("totals") or {},
                    "source_sheets": plan.get("source_sheets") or [],
                    "period_key": period,
                    "sample_rows": plan.get("sample_rows") or [],
                    "client_recipient_missing": bool(plan.get("client_recipient_missing")),
                    "client_not_in_tracker": bool(plan.get("client_not_in_tracker")),
                    "script_triggered": True,
                }
            )

            stmt = (
                pg_insert(EmailAutomationSend)
                .values(
                    source_message_id=None,
                    workflow_type=workflow_type,
                    variant=variant.name,
                    business_key=bk,
                    period_key=period,
                    dedupe_key=dedupe,
                    status=status,
                    review_reasons=jsonable(stored_reasons) if stored_reasons else None,
                    resolved_to_addrs=to_addrs,
                    resolved_cc_addrs=cc_addrs,
                    rendered_subject=plan.get("rendered_subject") or "",
                    rendered_body_html=body_html,
                    aggregated_data=aggregated_snapshot,
                    test_mode=bool(settings.email_automation_test_mode),
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(EmailAutomationSend.id)
            )
            result = await db.execute(stmt)
            inserted = result.scalar_one_or_none()
            if inserted is not None:
                inserted_keys.append(bk)
                logging.info(
                    "persist: inserted EmailAutomationSend id=%s bk=%s status=%s",
                    inserted, bk, status,
                )
            else:
                skipped_dedupe.append(bk)
                logging.info(
                    "persist: dedupe-skipped bk=%s (period=%s already persisted)",
                    bk, period,
                )

        await db.commit()

    return {
        "inserted": len(inserted_keys),
        "dedupe_skipped": len(skipped_dedupe),
        "filter_skipped": len(skipped_filter),
        "inserted_keys": inserted_keys,
        "dedupe_skipped_keys": skipped_dedupe,
    }


async def _dispatch_via_graph(
    limit: int, allow_keys: list[str] | None = None
) -> dict[str, Any]:
    """Invoke the email-automation graph's dispatch node directly.

    Triggers the same code path used by the production dispatch cron:
    reclaim stuck rows → claim approved/rendered rows under SKIP LOCKED
    → send via Gmail → update DB status + provider_message_id.

    The graph dispatch node claims eligible rows **globally** (every variant,
    every prior scan) up to ``limit`` — it is NOT scoped to what this run just
    persisted. To keep a manual trigger from draining unrelated queued rows
    (e.g. an old ePharma ``rendered`` row from a previous scan), we temporarily
    pin the dispatch business-key allowlist to ``allow_keys`` (the keys this run
    inserted) for the duration of the call, then restore it.

    Caveat: the allowlist filters by ``business_key`` only. If the same key also
    has an older eligible row (different period / older code), that row could
    still be picked. For fully isolated testing of one variant + workbook, use
    ``--mode send`` WITHOUT ``--persist`` (the in-process sender only touches the
    plans built from your file for the chosen variant).
    """
    from app.agents.email_automation.graph import run_dispatch_async
    from app.config.settings import settings

    prev_allowlist = settings.email_automation_dispatch_allowlist
    scoped = sorted({k for k in (allow_keys or []) if k})
    if scoped:
        settings.email_automation_dispatch_allowlist = ",".join(scoped)
        logging.info(
            "dispatch_via_graph: scoping dispatch to %d persisted business_key(s) "
            "via allowlist (prevents draining unrelated queued rows)",
            len(scoped),
        )
    logging.info("dispatch_via_graph: invoking graph dispatch node (limit=%d)", limit)
    try:
        result = await run_dispatch_async(limit=limit)
    finally:
        settings.email_automation_dispatch_allowlist = prev_allowlist
    logging.info(
        "dispatch_via_graph: sent=%d failed=%d reclaim=%s",
        len(result.get("sent_ids") or []),
        len(result.get("failed") or []),
        result.get("reclaim"),
    )
    return result


# ── Async entrypoint ──────────────────────────────────────────────────────────


async def run(
    *,
    variant_name: str,
    xlsx_path: Path,
    master_sheet_id: str,
    period: str,
    mode: str,
    limit: int,
    business_keys: set[str] | None,
    send_skipped_ar_notifications: bool,
    persist: bool = False,
    dispatch_after_persist: bool = True,
) -> dict[str, Any]:
    """Full pipeline run — returns a summary dict (printed as JSON at exit).

    When ``persist=True`` the plans are upserted into ``EmailAutomationSend``
    via the same idempotent path used by the production graph, and (when
    ``dispatch_after_persist=True``) the graph's dispatch node is invoked so
    the rows are sent with a full DB audit trail. This is the recommended path
    for manual triggers of DIAGNOSTICS_AGGREGATOR_VARIANT and other variants.
    """

    from app.config.settings import settings

    variant = _get_variant(variant_name)

    tab_name: str = (
        getattr(settings, variant.master_tab_setting, "") or "Master"
    )

    tracker_rows = await _load_tracker_rows(
        master_sheet_id,
        tab_name,
        variant.master_header_row_index,
    )

    plans = await _build_plans(xlsx_path, tracker_rows, variant_name, period)

    # ── Summary banner ────────────────────────────────────────────────────────
    skipped_plans = [p for p in plans if p.get("skip_gate")]
    active_plans  = [p for p in plans if not p.get("skip_gate")]
    review_plans  = [p for p in active_plans if p.get("review_reasons")]

    divider = "─" * 72
    print(f"\n{divider}", file=sys.stderr)
    print(f"  {variant_name.upper()} — PAYMENT_REMINDER_WEEKLY", file=sys.stderr)
    print(f"  Period       : {period}", file=sys.stderr)
    print(f"  Workbook     : {xlsx_path.name}", file=sys.stderr)
    print(f"  Master sheet : {master_sheet_id}", file=sys.stderr)
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

    # ── Persist + graph dispatch ───────────────────────────────────────────────
    persist_result: dict[str, Any] | None = None
    graph_dispatch_result: dict[str, Any] | None = None
    send_results: list[dict[str, Any]] | None = None

    if mode == "send" and persist:
        print(
            f"\n  Persisting plans to DB (EmailAutomationSend) …\n",
            file=sys.stderr,
        )
        persist_result = await _persist_plans(
            plans,
            variant_name=variant_name,
            period=period,
            business_keys=business_keys,
        )
        print(
            f"  Persist summary: inserted={persist_result['inserted']}  "
            f"dedupe_skipped={persist_result['dedupe_skipped']}  "
            f"filter_skipped={persist_result['filter_skipped']}",
            file=sys.stderr,
        )
        for bk in persist_result.get("inserted_keys") or []:
            print(f"    ✓ PERSISTED  {bk}", file=sys.stderr)
        for bk in persist_result.get("dedupe_skipped_keys") or []:
            print(f"    ↵ DEDUPE-SKIP {bk}", file=sys.stderr)

        # Dispatch every key this run resolved (freshly inserted OR dedupe-skipped
        # because they were persisted on a previous run). Scoping the graph
        # dispatch to these keys means a re-run still sends rows that are
        # persisted-but-not-yet-sent (status approved/rendered), while the claim
        # query naturally skips anything already 'sent'. Without this, re-running
        # the same period would insert nothing and never dispatch the queued rows.
        dispatch_keys = list(
            dict.fromkeys(
                (persist_result.get("inserted_keys") or [])
                + (persist_result.get("dedupe_skipped_keys") or [])
            )
        )
        if dispatch_after_persist and dispatch_keys:
            print(
                f"\n  Dispatching via graph node (limit={limit}, scoped to "
                f"{len(dispatch_keys)} key(s): {persist_result['inserted']} new + "
                f"{persist_result['dedupe_skipped']} already-persisted; "
                f"already-'sent' rows are skipped by the claim query) …\n",
                file=sys.stderr,
            )
            graph_dispatch_result = await _dispatch_via_graph(
                limit, allow_keys=dispatch_keys
            )
            sent_ids = graph_dispatch_result.get("sent_ids") or []
            failed   = graph_dispatch_result.get("failed") or []
            reclaim  = graph_dispatch_result.get("reclaim") or {}
            print(
                f"  Graph dispatch summary: sent={len(sent_ids)}  "
                f"failed={len(failed)}  reclaim={reclaim}",
                file=sys.stderr,
            )
            if not sent_ids and not failed:
                print(
                    "    (nothing sent — the scoped keys have no approved/rendered "
                    "rows left; they may already be 'sent', or need approval if "
                    "EMAIL_AUTOMATION_REQUIRE_APPROVAL=true)",
                    file=sys.stderr,
                )
            for sid in sent_ids:
                print(f"    ✓ SENT (graph)  send_id={sid}", file=sys.stderr)
            for f in failed:
                print(
                    f"    ✗ FAIL (graph)  send_id={f.get('id')}  err={f.get('error')!s:.80}",
                    file=sys.stderr,
                )
        elif dispatch_after_persist:
            print(
                "\n  No plans resolved for dispatch (nothing matched the filters).",
                file=sys.stderr,
            )

    # ── Legacy in-process send (no persist) ───────────────────────────────────
    elif mode == "send" and not persist:
        print(f"\n  Dispatching emails (limit={limit}) …\n", file=sys.stderr)
        send_results = _dispatch_plans(
            plans,
            variant_name=variant_name,
            limit=limit,
            business_keys=business_keys,
            send_skipped_ar_notifications=send_skipped_ar_notifications,
        )
        print("", file=sys.stderr)
        _print_send_results(send_results)

        sent    = sum(1 for r in send_results if r["status"] == "sent")
        failed  = sum(1 for r in send_results if r["status"] == "failed")
        skipped = sum(1 for r in send_results if r["status"].startswith("skip"))
        print(
            f"\n  Dispatch summary: sent={sent}  failed={failed}  skipped={skipped}",
            file=sys.stderr,
        )

    return {
        "ok": True,
        "variant": variant_name,
        "period": period,
        "workbook": xlsx_path.name,
        "master_sheet_id": master_sheet_id,
        "tracker_rows": len(tracker_rows),
        "plans_total": len(plans),
        "plans_active": len(active_plans),
        "plans_skipped": len(skipped_plans),
        "plans_with_reviews": len(review_plans),
        "mode": mode,
        "persist": persist,
        "persist_result": persist_result,
        "graph_dispatch_result": graph_dispatch_result,
        "send_results": send_results,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse
    from datetime import date as _date

    parser = argparse.ArgumentParser(
        description=(
            "Trigger the PAYMENT_REMINDER_WEEKLY pipeline for a chosen variant.\n\n"
            "Builds per-client send plans from a receivable workbook and the\n"
            "variant's master tracker, then optionally dispatches the emails."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Variant ───────────────────────────────────────────────────────────────
    parser.add_argument(
        "--variant",
        "-V",
        choices=list(_VARIANT_REGISTRY),
        required=True,
        help=(
            "Which payment-reminder variant to run.\n"
            "  diagnostics_aggregator  — DIAGNOSTICS_AGGREGATOR_VARIANT (Invoice details-H&T)\n"
            "  platform_aggregator     — PLATFORM_AGGREGATOR_VARIANT (Invoice wise Agg.)\n"
            "  epharma                 — EPHARMA_VARIANT\n"
            "  chw                     — CHW_VARIANT"
        ),
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

    # ── Input: master sheet ───────────────────────────────────────────────────
    parser.add_argument(
        "--master-sheet-id",
        "-m",
        metavar="SHEET_ID",
        default="",
        help=(
            "Google Sheets spreadsheet ID for the variant's master tracker.\n"
            "Falls back to EMAIL_AUTOMATION_DIAGNOSTICS_MASTER_SHEET_ID or\n"
            "EMAIL_AUTOMATION_EPHARMA_MASTER_SHEET_ID depending on --variant,\n"
            "then to the matching settings key in .env."
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
            "Comma-separated business keys (HANA / BP codes) to restrict sending.\n"
            "Empty (default) = process all eligible plans up to --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_DISPATCH_SEND_BATCH,
        help=(
            f"Maximum number of emails to send per run (safety cap; default "
            f"{DEFAULT_DISPATCH_SEND_BATCH}). "
            "Only applies in --mode send."
        ),
    )
    parser.add_argument(
        "--send-skipped-ar-notifications",
        action="store_true",
        default=False,
        help=(
            "In --mode send (legacy, no --persist), also dispatch AR-desk "
            "notifications for skipped plans (mirrors production behaviour for "
            "potential_overpayment / insufficient_outstanding)."
        ),
    )

    # ── DB-persist + graph-dispatch ───────────────────────────────────────────
    parser.add_argument(
        "--persist",
        action="store_true",
        default=False,
        help=(
            "In --mode send: persist plans to the DB as EmailAutomationSend "
            "rows (idempotent — uses the same ON CONFLICT DO NOTHING upsert "
            "as the production graph, keyed on dedupe_key). "
            "Recommended for DIAGNOSTICS_AGGREGATOR_VARIANT manual triggers "
            "so sends get a full audit trail."
        ),
    )
    parser.add_argument(
        "--no-dispatch-after-persist",
        dest="dispatch_after_persist",
        action="store_false",
        default=True,
        help=(
            "When --persist is set, skip the graph dispatch step after "
            "persisting. Rows stay in 'approved' / 'rendered' status and "
            "will be picked up by the normal dispatch cron, or can be "
            "dispatched manually via the /api/email-automation/dispatch "
            "endpoint."
        ),
    )
    parser.add_argument(
        "--insecure-local-tls",
        action="store_true",
        default=False,
        help=(
            "DEV ONLY: relax strict X.509 verification (VERIFY_X509_STRICT) so "
            "outbound HTTPS (Google Sheets tracker + Gmail send) works behind a "
            "corporate TLS-intercepting proxy whose CA cert isn't marked "
            "critical. Cert verification stays ON. Never pass this in "
            "production. Default: off. NOTE: routing through the proxy also "
            "requires PySocks in the venv and lowercase http_proxy/https_proxy "
            "env vars — see the module docstring."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Relax TLS strictness BEFORE any HTTPS client is built (Sheets/Gmail). Same
    # dev-only shim as scripts/run_email_automation_local.py; opt-in via the flag
    # so production / cron behaviour is unchanged.
    if args.insecure_local_tls:
        from _tls_local import relax_strict_tls_verification

        relax_strict_tls_verification()
    _load_env()

    # Settings are imported AFTER _load_env() so env overrides are visible.
    from app.config.settings import settings

    # ── Resolve receivable workbook ───────────────────────────────────────────
    cli_local_xlsx = (args.receivable_local_xlsx or "").strip()
    env_local_xlsx = os.environ.get("RECEIVABLE_LOCAL_XLSX", "").strip()
    cli_file_id    = (args.receivable_file_id or "").strip()
    env_file_id    = os.environ.get("RECEIVABLE_DRIVE_FILE_ID", "").strip()

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
            prefix="payment_reminder_recv_", suffix=".xlsx", delete=False
        ) as fh:
            fh.write(raw)
            tmp_path = fh.name
        path = Path(tmp_path)
        print(f"  → saved to {path}", file=sys.stderr)

    elif env_file_id:
        print(f"Downloading receivable workbook from Drive (file_id={env_file_id}) …", file=sys.stderr)
        meta = _drive_meta(env_file_id)
        print(
            f"  Name    : {meta.get('name')}\n"
            f"  Size    : {meta.get('size', '?')} bytes\n"
            f"  Modified: {meta.get('modifiedTime')}",
            file=sys.stderr,
        )
        raw = _drive_download_xlsx(env_file_id)
        with tempfile.NamedTemporaryFile(
            prefix="payment_reminder_recv_", suffix=".xlsx", delete=False
        ) as fh:
            fh.write(raw)
            tmp_path = fh.name
        path = Path(tmp_path)
        print(f"  → saved to {path}", file=sys.stderr)

    elif env_local_xlsx:
        path = Path(env_local_xlsx).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(
                f"Local XLSX not found from RECEIVABLE_LOCAL_XLSX: {path}\n"
                "Either unset RECEIVABLE_LOCAL_XLSX or pass "
                "--receivable-file-id / --receivable-local-xlsx explicitly."
            )
        if path.suffix.lower() != ".xlsx":
            logging.warning("Expected .xlsx extension; continuing anyway: %s", path)
        print(f"Using local workbook (RECEIVABLE_LOCAL_XLSX): {path}", file=sys.stderr)

    else:
        raise SystemExit(
            "Provide a receivable workbook via --receivable-file-id / "
            "RECEIVABLE_DRIVE_FILE_ID  OR  --receivable-local-xlsx / RECEIVABLE_LOCAL_XLSX."
        )

    # ── Resolve master sheet ID ───────────────────────────────────────────────
    _, env_var_name = _VARIANT_REGISTRY[args.variant]
    variant_obj = _get_variant(args.variant)

    master_sheet_id = (
        args.master_sheet_id
        or os.environ.get(env_var_name, "")
        or getattr(settings, variant_obj.master_sheet_id_setting, "")
        or ""
    ).strip()

    if not master_sheet_id:
        raise SystemExit(
            f"Provide a master sheet ID via --master-sheet-id, "
            f"{env_var_name}, or {variant_obj.master_sheet_id_setting} in .env."
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

    # ── Validate --persist flag usage ─────────────────────────────────────────
    if args.persist and args.mode != "send":
        raise SystemExit(
            "--persist requires --mode send. "
            "Use --mode send --persist to persist plans to DB."
        )

    if args.persist:
        print(
            "  ℹ  --persist mode: plans will be written to EmailAutomationSend "
            "(source_message_id=NULL, script_triggered=True in aggregated_data).",
            file=sys.stderr,
        )
        if args.dispatch_after_persist:
            print(
                "  ℹ  Graph dispatch node will be invoked after persist "
                f"(limit={args.limit}).",
                file=sys.stderr,
            )
        else:
            print(
                "  ℹ  --no-dispatch-after-persist: rows will be queued but NOT "
                "dispatched in this run.",
                file=sys.stderr,
            )

    # ── Execute ───────────────────────────────────────────────────────────────
    result: dict[str, Any] = {}
    try:
        result = asyncio.run(
            run(
                variant_name=args.variant,
                xlsx_path=path,
                master_sheet_id=master_sheet_id,
                period=period,
                mode=args.mode,
                limit=args.limit,
                business_keys=bk_filter,
                send_skipped_ar_notifications=args.send_skipped_ar_notifications,
                persist=args.persist,
                dispatch_after_persist=args.dispatch_after_persist,
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