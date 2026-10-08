#!/usr/bin/env python3
"""Download the receivables **.xlsx** from Google Drive and refresh the local dashboard snapshot.

* **“Google sheet” vs implementation** — Finance often means the file opened in Google; it is
  still a **.xlsx** (Excel) binary on **Drive** (`get_media`), not a native **Google Sheet**
  (`spreadsheets` API). Same parser as a file from ``~/Downloads``.

* **Workbook** — A **.xlsx** on Google Drive. The default file id is the shared receivables book.

* **Email reminder counts** — Read from ``email_automation_sends`` (``status`` in
  ``approved`` / ``rendered`` / ``sent``, weekly workflow, non-test) on a DB you point at,
  usually **remote** (e.g. production). Set ``REMINDER_SENDS_DATABASE_URL`` (async URL).

* **Local dashboard DB** — ``DATABASE_URL`` in ``agentos-backend/.env`` (delete existing snapshot
  rows, insert one new row). Do **not** put production credentials for this — only your
  **local** Postgres for the app.

  cd agentos-backend
  set -a; source .env; set +a
  # optional: override remote (example shape only; use your own password via env, not in git)
  # export REMINDER_SENDS_DATABASE_URL="postgresql+asyncpg://user:pass@host:9799/agentos"
  export GOOGLE_APPLICATION_CREDENTIALS=…   # or GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON
  python scripts/temp_load_receivable_dashboard_from_drive.py

Env:

  RECEIVABLE_DRIVE_FILE_ID   (default) receivable workbook on Drive, e.g. 10hsZoH7Ca_8R3IW2ieuA-f6RvxPjWb8D
  GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON  or GOOGLE_APPLICATION_CREDENTIALS
  DATABASE_URL  — from ``.env``: **local** async Postgres (delete + insert snapshot)
  REMINDER_SENDS_DATABASE_URL  — **optional**; async URL for reminder rollup queries only.
     If unset, rollups use the same DB as ``DATABASE_URL``.
  RECEIVABLE_LOCAL_XLSX  — **optional**; absolute or relative path to a ``.xlsx`` on disk.
     If set, skips Drive download (no Google creds needed for that run).
  RECEIVABLE_USE_LOCAL_DB_FOR_REMINDERS=1  — after loading ``.env``, drop ``REMINDER_SENDS_DATABASE_URL``
     so reminder ``COUNT`` rollups use **DATABASE_URL** (e.g. remote DB in ``.env`` is unreachable).

Requires the Drive file to be shared with the service account ``client_email`` (Drive path only).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Flock webhook ─────────────────────────────────────────────────────────────
# URL is read from settings (RECEIVABLE_INGEST_FLOCK_WEBHOOK_URL env var) at
# runtime — not hardcoded here.


class _FlockCollector(logging.Handler):
    """Accumulate WARNING+ log records emitted anywhere during the ingest run.

    Attach this handler to the root logger before the run starts; after the run
    completes (success or error) call ``_send_flock_alert`` to POST one
    consolidated message.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _send_flock_alert(
    collector: _FlockCollector,
    *,
    webhook_url: str = "",
    fatal: Exception | None = None,
    file_name: str = "",
    snap_id: str = "",
    is_test_mode: bool = False,
) -> None:
    """POST a single consolidated Flock message; no-op in test mode or on clean runs."""
    if is_test_mode:
        logging.info("Flock alert suppressed: test mode is ON")
        return
    if not webhook_url:
        logging.info("Flock alert suppressed: RECEIVABLE_INGEST_FLOCK_WEBHOOK_URL not set")
        return

    lines: list[str] = []

    if fatal:
        lines.append("🚨 *Receivable Ingest FAILED*")
    elif collector.records:
        lines.append("⚠️ *Receivable Ingest completed with warnings*")
    else:
        return  # clean run — don't spam the group

    if file_name:
        lines.append(f"📄 File: `{file_name}`")
    if snap_id:
        lines.append(f"🆔 Snapshot: `{snap_id}`")

    if fatal:
        lines.append(f"\n*Fatal error:* {type(fatal).__name__}: {fatal}")

    if collector.records:
        # Deduplicate by rendered message so we don't repeat the same
        # BU-mismatch warning 5× for 5 BUs in different wording.
        seen: set[str] = set()
        unique: list[logging.LogRecord] = []
        for rec in collector.records:
            msg = rec.getMessage()
            if msg not in seen:
                seen.add(msg)
                unique.append(rec)

        lines.append(f"\n*Issues ({len(unique)} unique / {len(collector.records)} total):*")
        for rec in unique:
            icon = "🔴" if rec.levelno >= logging.ERROR else "🟡"
            lines.append(f"{icon} {rec.getMessage()}")

    text = "\n".join(lines)
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logging.info("Flock alert sent (HTTP %s)", resp.status)
    except Exception as exc:  # never let notification failure crash the script
        logging.warning("Failed to send Flock alert: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(_p=None) -> None:  # type: ignore[misc]
        return None


def _load_env() -> None:
    for p in (_ROOT / ".env", _ROOT.parent / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)
            return
    load_dotenv()


def _to_async_dsn(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("postgresql://"):
        u = u.replace("postgresql://", "postgresql+asyncpg://", 1)
    return u


def _drive_v3():
    """Drive API client with a long socket timeout (large .xlsx downloads)."""
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
    # Default httplib2 timeouts are too low for multi‑MB `get_media` on slow links.
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


async def _load_reminder_rollup(
    dsn: str,
) -> tuple[dict[str, int], dict]:
    from app.services.receivable_dashboard import load_reminder_send_rollup_for_receivable_dashboard

    engine = create_async_engine(dsn, pool_pre_ping=True)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with factory() as session:
            return await load_reminder_send_rollup_for_receivable_dashboard(session)
    finally:
        await engine.dispose()


async def _insert_snapshot(
    path: Path, *, drive_file_id: str | None, drive_meta: dict, replace: bool = True
) -> str:
    from app.config.settings import settings
    from app.db.models import ReceivableDashboardSnapshot
    from app.db.session import AsyncSessionLocal
    from app.services.receivable_dashboard import build_receivable_payload

    reminder = (os.environ.get("REMINDER_SENDS_DATABASE_URL") or "").strip()
    if not reminder:
        logging.warning(
            "REMINDER_SENDS_DATABASE_URL not set — using DATABASE_URL (local) for reminder rollups"
        )
        reminder = _to_async_dsn(settings.database_url)
    else:
        logging.info("Loading reminder send rollups from REMINDER_SENDS_DATABASE_URL")
    reminder = _to_async_dsn(reminder)

    send_counts, reminder_meta = await _load_reminder_rollup(reminder)
    logging.info(
        "Reminder rollup: %d business keys, row_count=%s",
        len(send_counts),
        reminder_meta.get("row_count"),
    )

    def _build():
        p = build_receivable_payload(
            path,
            send_counts,
            source_message_id=None,
            reminder_sends_meta=reminder_meta,
        )
        if p and isinstance(p.get("meta"), dict):
            m = p["meta"]
            m["loaded_by"] = "scripts/temp_load_receivable_dashboard_from_drive.py"
            m["reminder_rollup_datasource"] = (
                "remote"
                if (os.environ.get("REMINDER_SENDS_DATABASE_URL") or "").strip()
                else "same_as_local_database"
            )
        return p

    payload = await asyncio.to_thread(_build)
    if payload is None:
        raise RuntimeError(
            "build_receivable_payload returned None — no receivable tab or "
            "missing Code/BU/Net columns. Check sheet names and headers."
        )
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        if drive_file_id:
            meta["drive_file_id"] = drive_file_id
        meta["drive_name"] = drive_meta.get("name")
        meta["drive_mime_type"] = drive_meta.get("mimeType")
        meta["drive_modified_time"] = drive_meta.get("modifiedTime")
        if drive_meta.get("local_path"):
            meta["local_xlsx_path"] = drive_meta.get("local_path")

    async with AsyncSessionLocal() as db:
        if replace:
            res = await db.execute(delete(ReceivableDashboardSnapshot))
            logging.info(
                "Local DB: removed existing snapshot row(s) rowcount=%s", res.rowcount
            )
        else:
            logging.info("Local DB: appending new snapshot (existing rows kept)")
        row = ReceivableDashboardSnapshot(source_message_id=None, payload=payload)
        db.add(row)
        await db.commit()
        await db.refresh(row)

    return str(row.id)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Refresh the receivables dashboard snapshot from Drive or a local file."
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help=(
            "replace (default): delete all existing snapshots then insert a new one. "
            "append: insert a new snapshot alongside existing ones."
        ),
    )
    args = parser.parse_args()
    replace = args.mode == "replace"

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _load_env()

    # ── Flock collector — attach early so we catch every WARNING+ ─────────────
    from app.config.settings import settings as _settings

    _is_test_mode = _settings.email_automation_test_mode
    _flock_url = (_settings.receivable_ingest_flock_webhook_url or "").strip()
    _flock = _FlockCollector()
    logging.getLogger().addHandler(_flock)  # root logger → captures all modules
    # ─────────────────────────────────────────────────────────────────────────

    # `load_dotenv(override=True)` re-applies .env; unset is lost unless done here.
    if os.environ.get("RECEIVABLE_USE_LOCAL_DB_FOR_REMINDERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    ):
        os.environ.pop("REMINDER_SENDS_DATABASE_URL", None)
        logging.info(
            "RECEIVABLE_USE_LOCAL_DB_FOR_REMINDERS: reminder rollups use DATABASE_URL"
        )

    local = (os.environ.get("RECEIVABLE_LOCAL_XLSX") or "").strip()
    path: Path
    file_id: str | None = None
    meta: dict
    tmp_path: str | None = None

    if local:
        path = Path(local).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"RECEIVABLE_LOCAL_XLSX not a file: {path}")
        if path.suffix.lower() != ".xlsx":
            logging.warning("Expected .xlsx extension; continuing anyway: %s", path)
        meta = {
            "name": path.name,
            "local_path": str(path),
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        print("Using local workbook:", path, file=sys.stderr)
    else:
        # Default file ID — overridden by RECEIVABLE_DRIVE_FILE_ID in .env (load_dotenv override=True).
        file_id = os.environ.get(
            "RECEIVABLE_DRIVE_FILE_ID", "1s7Q9lXIHk_5q-6NTse-hQ0P4SyPihpcs"
        ).strip()
        print("Drive file id:", file_id, file=sys.stderr)
        meta = _drive_meta(file_id)
        print("Drive file:", json.dumps(meta, indent=2), file=sys.stderr)
        raw = _drive_download_xlsx(file_id)
        with tempfile.NamedTemporaryFile(
            prefix="receivable_drive_", suffix=".xlsx", delete=False
        ) as f:
            f.write(raw)
            tmp_path = f.name
        path = Path(tmp_path)

    _fatal: Exception | None = None
    snap_id: str = ""
    try:
        snap_id = asyncio.run(
            _insert_snapshot(path, drive_file_id=file_id, drive_meta=meta, replace=replace)
        )
    except Exception as _exc:
        _fatal = _exc
        raise
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
        # Remove collector from root logger so it doesn't linger
        logging.getLogger().removeHandler(_flock)
        _send_flock_alert(
            _flock,
            webhook_url=_flock_url,
            fatal=_fatal,
            file_name=meta.get("name", ""),
            snap_id=snap_id,
            is_test_mode=_is_test_mode,
        )

    print(json.dumps({"ok": True, "receivable_dashboard_snapshot_id": snap_id}, indent=2))


if __name__ == "__main__":
    main()
