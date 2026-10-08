"""Google Sheets client for outreach prospect tabs."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.email_automation import gmail_sa  # noqa: E402 — used by write_back_* methods

log = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[;&]")
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def parse_address_list(raw: str | None) -> list[str]:
    """Split &- or ;-delimited email strings into a clean list."""
    if not raw:
        return []
    return [a.strip() for a in _SPLIT_RE.split(raw) if a.strip()]


def _build_sheets_service():
    """Build a Sheets v4 client using the service account directly."""
    from app.email_automation import gmail_sa
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_path = gmail_sa._resolve_sa_json_path()
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_SHEETS_SCOPE]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


@dataclass
class OutreachSheetClient:
    """Read/write client for one prospect tab."""

    spreadsheet_id: str
    tab_name: str

    def read_prospect_rows(self) -> list[dict[str, Any]]:
        """Return all data rows as dicts.

        Row 1 is skipped (meta-annotations). Row 2 is used as headers.
        Each dict has the column headers as keys plus a synthetic ``_row_index``
        key (1-based sheet row number) for writeback targeting.
        """
        from app.email_automation import gmail_sa

        svc = _build_sheets_service()
        resp = gmail_sa.call_with_retry(
            "spreadsheets.values.get",
            lambda: svc.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=self.tab_name,
                valueRenderOption="FORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute(),
        )
        values = resp.get("values") or []
        if len(values) < 2:
            return []
        headers = [str(h).strip() for h in values[1]]
        rows: list[dict[str, Any]] = []
        for sheet_offset, raw_row in enumerate(values[2:], start=0):
            padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
            row = {headers[i]: padded[i] for i in range(len(headers))}
            row["_row_index"] = sheet_offset + 3
            rows.append(row)
        return rows

    def list_tab_names(self) -> list[str]:
        """Return names of all sheet tabs in the spreadsheet."""
        from app.email_automation import gmail_sa
        svc = _build_sheets_service()
        resp = gmail_sa.call_with_retry(
            "spreadsheets.get",
            lambda: svc.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title")
            .execute(),
        )
        return [s["properties"]["title"] for s in (resp.get("sheets") or [])]

    def write_back_status(self, updates: list[tuple[int, str, str]]) -> None:
        """Write status + timestamp back to Col N and Col O.

        ``updates`` is a list of (gsheet_row_index, status_value, timestamp_iso).
        """
        if not updates:
            return
        from app.email_automation import gmail_sa

        svc = _build_sheets_service()
        data = []
        for row_idx, status_val, ts_val in updates:
            data.append({
                "range": f"{self.tab_name}!N{row_idx}:O{row_idx}",
                "values": [[status_val, ts_val]],
            })
        body = {"valueInputOption": "USER_ENTERED", "data": data}
        gmail_sa.call_with_retry(
            "spreadsheets.values.batchUpdate",
            lambda: svc.spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=self.spreadsheet_id, body=body)
            .execute(),
        )
        log.info("outreach gsheet writeback: %d rows updated", len(updates))

    def write_back_reply_category(self, updates: list[tuple[int, str]]) -> None:
        """Write reply category to Col P for each row.

        ``updates`` is a list of (gsheet_row_index, category_value).
        """
        if not updates:
            return

        svc = _build_sheets_service()
        data = []
        for row_idx, category_val in updates:
            data.append({
                "range": f"{self.tab_name}!P{row_idx}",
                "values": [[category_val]],
            })
        body = {"valueInputOption": "USER_ENTERED", "data": data}
        gmail_sa.call_with_retry(
            "spreadsheets.values.batchUpdate",
            lambda: svc.spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=self.spreadsheet_id, body=body)
            .execute(),
        )
        log.info("outreach gsheet reply writeback: %d rows updated", len(updates))
