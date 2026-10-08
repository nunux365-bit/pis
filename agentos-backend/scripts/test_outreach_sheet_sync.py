#!/usr/bin/env python3
"""Test script: read Google Sheet → write to outreach_leads DB. NO email dispatch.

Usage (from agentos-backend/):
    python scripts/test_outreach_sheet_sync.py

Env vars required (same as email automation):
    GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON   — path to SA key JSON
    EMAIL_AUTOMATION_IMPERSONATED_USER  — e.g. automation.agents@1mg.com
    ASYNC_DATABASE_URL                  — Postgres connection string

Optional overrides:
    --sheet-id      Google Sheet ID (default: the CHW test sheet)
    --tab           Sheet tab name to read (default: Sheet1)
    --campaign      Campaign name slug written to DB (default: chw_outreach)
    --dry-run       Print what would be written; skip DB commit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Safety: ensure the dispatch/engine module is never imported in this process.
# ---------------------------------------------------------------------------
import importlib

_BLOCKED_MODULES = {
    "app.email_automation.outreach.engine",
    "app.agents.outreach.graph",
}


class _BlockDispatch:
    """Module finder that raises if anything tries to import the send path."""
    def find_module(self, name: str, path=None):
        if name in _BLOCKED_MODULES:
            raise ImportError(
                f"[SAFETY] Import of {name!r} is blocked in this test script. "
                "No email dispatch allowed."
            )
        return None


sys.meta_path.insert(0, _BlockDispatch())  # type: ignore[arg-type]


_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _read_sheet_direct(spreadsheet_id: str, tab_name: str) -> list[dict]:
    """Read a sheet using the SA directly (no DWD impersonation).

    Requires the sheet to be shared with the SA email.
    """
    from pathlib import Path

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_path = Path(
        __import__("os").environ.get(
            "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON",
            str(Path(__file__).resolve().parents[1] / "app/config/service_accounts.json"),
        )
    )
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_SHEETS_SCOPE]
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=tab_name,
            valueRenderOption="FORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )
        .execute()
    )
    values = resp.get("values") or []
    if len(values) < 2:
        return []
    headers = [str(h).strip() for h in values[1]]
    rows = []
    for offset, raw_row in enumerate(values[2:], start=0):
        padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
        row = {headers[i]: padded[i] for i in range(len(headers))}
        row["_row_index"] = offset + 3
        rows.append(row)
    return rows


async def _main(args: argparse.Namespace) -> None:
    from app.db.session import AsyncSessionLocal
    from app.email_automation.outreach.sync import reconcile_campaign_db

    print(f"\n{'='*60}")
    print("OUTREACH SHEET SYNC — TEST RUN")
    print(f"  Sheet ID  : {args.sheet_id}")
    print(f"  Tab       : {args.tab}")
    print(f"  Campaign  : {args.campaign}")
    print(f"  Dry run   : {args.dry_run}")
    print(f"{'='*60}\n")

    # -----------------------------------------------------------------------
    # Step 1: Read from Google Sheet (SA direct — no impersonation)
    # -----------------------------------------------------------------------
    print("Step 1: Reading from Google Sheet (direct SA, no impersonation) …")

    try:
        rows = await asyncio.to_thread(_read_sheet_direct, args.sheet_id, args.tab)
    except Exception as exc:
        print(f"  ERROR reading sheet: {exc}")
        print(
            "\n  Make sure the sheet is shared (Viewer) with:\n"
            "    service-automation@service-automation-491510.iam.gserviceaccount.com"
        )
        sys.exit(1)

    print(f"  Read {len(rows)} data row(s).")
    if not rows:
        print("  Sheet is empty or headers not found — nothing to sync.")
        sys.exit(0)

    # Print detected headers + first row for verification
    first = rows[0]
    headers = [k for k in first.keys() if k != "_row_index"]
    print(f"  Detected headers: {headers}")
    print(f"\n  First row (row index {first.get('_row_index')}):")
    for h in headers:
        val = first.get(h, "")
        if val:
            print(f"    {h}: {val!r}")

    # -----------------------------------------------------------------------
    # Step 2: Write to DB (reconcile only — no email, no dispatch)
    # -----------------------------------------------------------------------
    print(f"\nStep 2: Reconciling {len(rows)} rows into DB …")

    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    cfg = next((c for c in ACTIVE_CAMPAIGNS if c.campaign_name == args.campaign), None)
    if not cfg:
        print(f"  ERROR: campaign {args.campaign!r} not found in campaigns.py")
        sys.exit(1)

    ingest_data = {
        "campaign": args.campaign,
        "tab_name": args.tab,
        "rows": rows,
        "error": None,
    }

    if args.dry_run:
        from app.email_automation.outreach.sync import reconcile_rows
        from datetime import datetime, timezone

        plan = reconcile_rows(
            sheet_rows=rows,
            existing_leads={},
            campaign_name=args.campaign,
            tab_name=args.tab,
            now=datetime.now(timezone.utc),
        )
        print(f"  [DRY RUN] Would insert: {len(plan['to_insert'])} rows")
        print(f"  [DRY RUN] Would update: {len(plan['to_update'])} rows")
        print("\n  Sample inserts:")
        for r in plan["to_insert"][:3]:
            print(f"    email={r.get('primary_email')!r}  status={r.get('status')!r}  issues={r.get('issues')}")
        print("\n[DRY RUN] No DB writes made.")
        return

    async with AsyncSessionLocal() as db:
        stats = await reconcile_campaign_db(db, cfg, ingest_data)

    print(f"\n{'='*60}")
    print("DONE")
    print(f"  Inserted : {stats['inserted']}")
    print(f"  Updated  : {stats['updated']}")
    print(f"  Emails sent: 0  (dispatch never called)")
    print(f"{'='*60}\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Sheet→DB sync test (no email dispatch)")
    from app.email_automation.outreach.campaigns import ACTIVE_CAMPAIGNS
    from datetime import datetime, timezone
    default_cfg = ACTIVE_CAMPAIGNS[0]
    default_tab = f"{default_cfg.campaign_name}_{datetime.now(timezone.utc).strftime('%Y_%m')}"

    p.add_argument("--sheet-id", default=default_cfg.gsheet_id, help="Google Sheet ID")
    p.add_argument("--tab", default=default_tab, help="Tab name to read")
    p.add_argument("--campaign", default="chw_outreach", help="Campaign slug for DB rows")
    p.add_argument("--dry-run", action="store_true", help="Skip DB commit; print plan only")
    args = p.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
