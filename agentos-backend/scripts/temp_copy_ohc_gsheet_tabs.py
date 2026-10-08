#!/usr/bin/env python3
"""Pull **OHC Attendance** and **Manpower** from a Google Sheet and save a **local Excel file**.

Upload that ``.xlsx`` to Google Drive yourself → “Open with Google Sheets” if you want it as a Sheet.
No file is created in the service account’s Drive (avoids quota issues).

Uses the service-account JSON path from ``.env`` (same vars as other temp scripts).

  cd agentos-backend
  set -a; source .env; set +a
  python scripts/temp_copy_ohc_gsheet_tabs.py

Env:

  GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON  or  GOOGLE_APPLICATION_CREDENTIALS
  OHC_SOURCE_SPREADSHEET_ID  — optional (default below)
  OHC_COPY_LOCAL_XLSX  — optional output path (default: ``scripts/temp_ohc_attendance_manpower_<utc>.xlsx``)

The source spreadsheet must be shared with the SA ``client_email`` (Viewer is enough).

Scope: ``spreadsheets.readonly`` only.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(_p=None) -> None:  # type: ignore[misc]
        return None


SOURCE_ID_DEFAULT = "1Ouoo-yhDIhnZxghNFmD5UlrjiF2vxug1LM5P50_EkMA"
TABS = ("OHC Attendance", "Manpower")
_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _load_env() -> None:
    for p in (_ROOT / ".env", _ROOT.parent / ".env"):
        if p.is_file():
            load_dotenv(p, override=True)
            return
    load_dotenv()


def _sa_json_path() -> Path:
    p = (os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON") or "").strip() or (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    ).strip()
    if not p:
        raise SystemExit(
            "Set GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS to the SA JSON path."
        )
    path = Path(p).expanduser()
    if not path.is_file():
        raise SystemExit(f"Service account JSON not found: {path}")
    return path


def _a1_tab_range(tab: str) -> str:
    esc = tab.replace("'", "''")
    return f"'{esc}'!A:ZZ"


def main() -> None:
    _load_env()
    src_id = (os.environ.get("OHC_SOURCE_SPREADSHEET_ID") or "").strip() or SOURCE_ID_DEFAULT

    out_raw = (os.environ.get("OHC_COPY_LOCAL_XLSX") or "").strip()
    if out_raw:
        out_path = Path(out_raw).expanduser().resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        out_path = (_ROOT / "scripts" / f"temp_ohc_attendance_manpower_{stamp}.xlsx").resolve()

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from openpyxl import Workbook

    creds = service_account.Credentials.from_service_account_file(
        str(_sa_json_path()), scopes=[_READONLY]
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    ranges = [_a1_tab_range(t) for t in TABS]
    batch = (
        sheets.spreadsheets()
        .values()
        .batchGet(spreadsheetId=src_id, ranges=ranges, majorDimension="ROWS")
        .execute()
    )
    value_ranges = batch.get("valueRanges") or []

    wb = Workbook()
    wb.remove(wb.active)
    for tab, vr in zip(TABS, value_ranges, strict=True):
        ws = wb.create_sheet(title=tab[:31])
        rows = vr.get("values") if vr.get("values") is not None else []
        for row in rows:
            ws.append(list(row))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    print(f"Source spreadsheet: {src_id}")
    print(f"Wrote local workbook: {out_path}")
    print("Upload this file to Drive and open with Google Sheets if you need a cloud copy.")


if __name__ == "__main__":
    main()
