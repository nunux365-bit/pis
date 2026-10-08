"""Google Sheets client — read-only master trackers, SA-as-itself (no DWD).

Minimal surface because the engine only needs a single call:
:func:`read_table` — return a sheet tab as ``(headers, rows)`` where rows are lists
aligned to ``headers``. A thin in-process TTL cache keeps weekly scans from spamming
the API while still picking up manual edits within minutes.

**Access model**: the service account reads sheets using **its own identity**,
not via Workspace-user impersonation. Operationally that means each master
tracker must be shared (Viewer is enough) with the SA's ``client_email`` — the
one in the JSON key file. The Gmail side still uses DWD + impersonation (see
:mod:`.gmail_sa`); Sheets is deliberately kept off DWD so Admin only has to
authorize the two Gmail scopes and the sharing review happens per-sheet.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.email_automation import gmail_sa

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SheetTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    def dicts(self) -> list[dict[str, Any]]:
        """Row-of-dicts view; trailing cells are padded to header length."""

        out: list[dict[str, Any]] = []
        n = len(self.headers)
        for r in self.rows:
            padded = list(r) + [None] * max(0, n - len(r))
            out.append({self.headers[i]: padded[i] for i in range(n)})
        return out


_tls = threading.local()


def _build_sheets_credentials():
    """Return SA credentials for Sheets — **no impersonation**.

    The SA authenticates as itself and requests ``spreadsheets.readonly``. Each
    tracker sheet must be shared with the SA's ``client_email`` for the request
    to succeed; a 403 here means sharing is missing (not a scope / DWD issue).
    """

    from google.oauth2 import service_account

    sa_path = gmail_sa._resolve_sa_json_path()
    return service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[gmail_sa.SHEETS_READONLY_SCOPE]
    )


def _build_sheets_service():
    """Return a per-thread Sheets v4 client using the non-impersonating SA creds."""

    svc = getattr(_tls, "sheets", None)
    if svc is not None:
        return svc
    from googleapiclient.discovery import build

    creds = _build_sheets_credentials()
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    _tls.sheets = svc
    return svc


class _Cache:
    """Tiny TTL cache: ``(sheet_id, tab) -> (expires_at, SheetTable)``.

    We deliberately avoid Redis here — payloads are small, a single process handles
    scanning, and restarts invalidate the cache which is fine.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._data: dict[tuple[str, str], tuple[float, SheetTable]] = {}

    def get(self, key: tuple[str, str]) -> SheetTable | None:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            exp, tbl = entry
            if exp < time.monotonic():
                self._data.pop(key, None)
                return None
            return tbl

    def put(self, key: tuple[str, str], value: SheetTable) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = _Cache(ttl_seconds=300)


def read_table(
    spreadsheet_id: str,
    tab: str,
    *,
    header_row_index: int = 0,
    use_cache: bool = True,
) -> SheetTable:
    """Read a sheet tab. ``header_row_index`` is 0-based inside the returned range."""

    if not spreadsheet_id:
        raise ValueError("spreadsheet_id is required")
    if not tab:
        raise ValueError("tab is required")

    key = (spreadsheet_id, tab)
    if use_cache:
        cached = _cache.get(key)
        if cached is not None:
            return cached

    svc = _build_sheets_service()
    # Pull the whole tab as operator-formatted strings (the trackers are spreadsheets
    # humans edit; raw numbers/dates would lose the formatting they rely on).
    resp = gmail_sa.call_with_retry(
        "spreadsheets.values.get",
        lambda: svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=tab,
            valueRenderOption="FORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )
        .execute(),
    )
    values = resp.get("values") or []
    if header_row_index >= len(values):
        headers: tuple[str, ...] = ()
        rows: tuple[tuple[Any, ...], ...] = ()
    else:
        raw_headers = values[header_row_index]
        headers = tuple((h or "").strip() for h in raw_headers)
        rows = tuple(tuple(r) for r in values[header_row_index + 1 :])
    tbl = SheetTable(headers=headers, rows=rows)
    if use_cache:
        _cache.put(key, tbl)
    return tbl
