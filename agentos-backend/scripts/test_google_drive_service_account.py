#!/usr/bin/env python3
"""
Smoke-test Google Drive with a **service account** JSON key (no CLI args).

Set credentials, then optionally set test IDs. Each step runs only if its env is set.

**Required (one of):**
  - ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` — path to JSON key
  - ``GOOGLE_APPLICATION_CREDENTIALS``

**Always runs:** connection check + one ``files.list`` sample.

**Optional steps** (skip if env empty):
  - ``GOOGLE_DRIVE_TEST_PDF_FILE_ID`` → download PDF (or export Google Doc) to ``GOOGLE_DRIVE_TEST_OUT_DIR/pdf``
  - **Google Sheet URL:** ``GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL`` — full ``https://docs.google.com/spreadsheets/d/...`` link (quote if the value contains ``#``). **Native Sheet only** (``files.export`` → ``attendance.xlsx``). Same id resolution as ``GOOGLE_DRIVE_TEST_EXCEL_FILE_ID``.
  - ``GOOGLE_DRIVE_TEST_EXCEL_FILE_ID`` → same as above (legacy name); **native Google Sheet only** — Drive ``files.export`` to ``GOOGLE_DRIVE_TEST_OUT_DIR/attendance.xlsx`` (uploaded ``.xlsx`` blobs on Drive are not supported for this step).
  - ``GOOGLE_DRIVE_TEST_UPLOAD_FOLDER_ID`` + ``GOOGLE_DRIVE_TEST_UPLOAD_LOCAL_XLSX`` → upload that file into the folder
  - **Attendance parse simulation:** ``GOOGLE_DRIVE_TEST_ATTENDANCE_PARSE_SIMULATE=1`` runs ``parse_ohc_summary_workbook``. **Source:** (1) ``GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH`` if the file exists, else (2) ``GOOGLE_DRIVE_TEST_OUT_DIR/attendance.xlsx`` from ``GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL`` / ``GOOGLE_DRIVE_TEST_EXCEL_FILE_ID`` (download step above, or export here if missing). Optional parse-only: ``GOOGLE_DRIVE_TEST_ATTENDANCE_SHEET_ID``. Engine: ``GOOGLE_DRIVE_TEST_ATTENDANCE_ENGINE`` (default ``auto``). Honors ``O2C_ATTENDANCE_COLUMN_MAP_JSON``.
  - **MIS layout upload** (folder create + upload + domain ACL): code remains in ``simulate_mis_excel_upload_layout`` but the **call in** ``main()`` is commented out; uncomment when using a Shared Drive parent. Requires Shared Drive; service accounts have no My Drive quota.
  - ``GOOGLE_DRIVE_TEST_LIST_SINCE`` — ISO UTC e.g. ``2026-03-01T00:00:00Z`` → list files **modified or created** after that (max 50). Optional ``GOOGLE_DRIVE_TEST_LIST_FOLDER_ID`` to scope to a folder.
  - **Contract folder (ingestion-style):** if ``GOOGLE_DRIVE_TEST_CONTRACT_FOLDER_ID`` is set and a cutoff is set (``GOOGLE_DRIVE_TEST_CONTRACT_SINCE`` or, if empty, ``GOOGLE_DRIVE_TEST_LIST_SINCE``), list **only** ``application/pdf`` **under that folder id** (recursive subfolders only via child **folders**; no shortcuts) with **modifiedTime > cutoff**. Each row includes **relative_path** (POSIX path under the folder root, same idea as ``folder_scan.PdfCandidate.relative_path`` / ``Path.relative_to(contracts_root)``).

    **Profile parity:** Ingestion picks **contract LLM profile** and **billing_profile** from the **first path segment** of ``relative_path`` (see ``app/agents/o2c_ohc/pipeline.infer_contract_prompt_profile`` and ``billing_profile.infer_billing_profile``). That requires **at least one** ``/`` in ``relative_path`` (e.g. ``taco/Annex.pdf``), except PDFs sitting **directly** under the root folder (filename only) which match **local** “PDF at contracts root” → **generic**. So set ``GOOGLE_DRIVE_TEST_CONTRACT_FOLDER_ID`` to the Drive folder that matches **local** ``O2C_CONTRACTS_ROOT`` (the parent of ``taco/``, ``tcs/``, etc.), **not** only the inner ``taco`` folder,     unless your files live in deeper subpaths that still start with ``taco/``.

  - **Ingestion simulation:** ``GOOGLE_DRIVE_TEST_INGEST_SIMULATE=1`` — after listing, for up to ``GOOGLE_DRIVE_TEST_INGEST_SIMULATE_MAX`` PDFs (default 3), print **DB / pipeline** steps matching ``folder_scanner.list_new_or_updated_pdfs`` + ``pipeline.process_one_contract_pdf`` (no agenos calls). ``GOOGLE_DRIVE_TEST_OPENAI_UPLOAD=1`` — also OpenAI ``files.create`` (``purpose=user_data``) as in ``llm_extract.extract_markdown_and_payload``, log ``file_id``, then delete the file. Needs ``OPENAI_API_KEY``.

**Default:** ``GOOGLE_DRIVE_TEST_OUT_DIR`` = ``/tmp/gdrive_sa_smoke``

**IDs or URLs:** For ``GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL``, ``*_FILE_ID``, ``*_FOLDER_ID``, and sheet ids you may paste either the **bare resource id** or a full browser link. The script extracts the id and logs when it does. Prefer ``GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL="https://..."`` for Sheets so the intent is obvious.

Share files/folders with the service account ``client_email`` from the JSON. Enable Drive API on the GCP project.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass


def _repair_dotenv_missing_equals_for_drive_urls() -> None:
    """
    If ``.env`` has a typo like ``GOOGLE_DRIVE_TEST_EXCEL_FILE_IDhttps://...`` or
    ``GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URLhttps://...`` (no ``=``), dotenv ignores it;
    recover the URL into the right env var.
    """
    path = _ROOT / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    specs: list = [
        (
            re.compile(r"^GOOGLE_DRIVE_TEST_(PDF|EXCEL)_FILE_ID(https?://\S+)$", re.IGNORECASE),
            lambda m: (f"GOOGLE_DRIVE_TEST_{m.group(1).upper()}_FILE_ID", m.group(2)),
        ),
        (
            re.compile(r"^GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL(https?://\S+)$", re.IGNORECASE),
            lambda m: ("GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL", m.group(1)),
        ),
        (
            re.compile(r"^GOOGLE_DRIVE_TEST_SHEET_URL(https?://\S+)$", re.IGNORECASE),
            lambda m: ("GOOGLE_DRIVE_TEST_SHEET_URL", m.group(1)),
        ),
    ]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split("#", 1)[0]:
            continue
        for pat, keyfn in specs:
            m = pat.match(line)
            if not m:
                continue
            key, url = keyfn(m)
            url = str(url).strip().strip('"').strip("'")
            if os.environ.get(key, "").strip():
                break
            os.environ[key] = url
            print(f"(repaired .env: {key} was missing '=' before URL — loaded URL from line anyway)")
            break


_repair_dotenv_missing_equals_for_drive_urls()

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

_SCOPES = ("https://www.googleapis.com/auth/drive",)

_MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
_MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_PDF = "application/pdf"

_LIST_MAX = 50
# Ingestion-style folder scan may return more PDFs than the generic list-since cap.
_CONTRACT_FOLDER_LIST_MAX = 500
_CONTRACT_WALK_MAX_DEPTH = 40
_MIME_FOLDER = "application/vnd.google-apps.folder"


def drive_id_from_url_or_id(raw: str) -> str:
    """
    Accept a bare Drive file/folder id or a browser URL (Sheets, Docs, Slides, file, folder, ``open?id=``).
    Returns the extracted id, or the trimmed string if no URL pattern matches.
    """
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    low = s.lower()
    if "http://" not in low and "https://" not in low and "drive.google.com" not in low and "docs.google.com" not in low:
        return s
    decoded = unquote(s)
    for pat in (
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/presentation/d/([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
    ):
        m = re.search(pat, decoded)
        if m:
            return m.group(1)
    qs = parse_qs(urlparse(decoded).query)
    for key in ("id", "folderId"):
        vals = qs.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return s


def _env_drive_id(name: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    out = drive_id_from_url_or_id(raw)
    if raw and out != raw and (
        "://" in raw or "drive.google.com" in raw.lower() or "docs.google.com" in raw.lower()
    ):
        print(f"  ({name}: resolved Drive id from URL → {out!r})")
    return out


def _env_drive_id_first(*names: str) -> str:
    """First non-empty resolved id among env keys (order matters)."""
    for n in names:
        v = _env_drive_id(n)
        if v:
            return v
    return ""


def _credentials_path() -> Path:
    for key in (os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"), os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")):
        if key and str(key).strip():
            p = Path(key).expanduser().resolve()
            if p.is_file():
                return p
    raise SystemExit(
        "Set GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS to a service account JSON path."
    )


def _out_dir() -> Path:
    raw = (os.environ.get("GOOGLE_DRIVE_TEST_OUT_DIR") or "/tmp/gdrive_sa_smoke").strip()
    return Path(raw).expanduser().resolve()


def _service_account_email(credentials_path: Path) -> str | None:
    import json

    try:
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
        return str(data.get("client_email") or "").strip() or None
    except Exception:
        return None


def drive_service(credentials_path: Path):
    creds = service_account.Credentials.from_service_account_file(str(credentials_path), scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def run_check(svc, *, sa_email: str | None) -> None:
    about = svc.about().get(fields="user,kind").execute()
    print("drive.about:", about.get("kind"), about.get("user", {}))
    res = svc.files().list(pageSize=1, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = res.get("files") or []
    print("files.list sample:", files[0] if files else "(none — share a folder with the service account)")
    if sa_email:
        print("service_account client_email:", sa_email)


def _download_media(svc, file_id: str) -> bytes:
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _export_bytes(svc, file_id: str, mime: str) -> bytes:
    return svc.files().export(fileId=file_id, mimeType=mime).execute()


def fetch_pdf_bytes_from_drive(svc, file_id: str) -> tuple[bytes, dict[str, Any]]:
    meta = svc.files().get(
        fileId=file_id, fields="id,name,mimeType,modifiedTime,size", supportsAllDrives=True
    ).execute()
    mime = meta.get("mimeType") or ""
    if mime == _MIME_GOOGLE_DOC:
        data = _export_bytes(svc, file_id, _MIME_PDF)
    else:
        data = _download_media(svc, file_id)
    return data, meta


def download_pdf(svc, file_id: str, output: Path) -> None:
    data, meta = fetch_pdf_bytes_from_drive(svc, file_id)
    name = meta.get("name") or file_id
    mime = meta.get("mimeType") or ""
    print(f"download pdf: {name!r} mimeType={mime!r}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(f"  wrote {len(data)} bytes -> {output}")


def download_excel(svc, file_id: str, output: Path) -> None:
    """
    **Native Google Sheet only:** Drive ``files.export`` → ``.xlsx`` (server-side conversion).

    Export fidelity vs **File → Download → Microsoft Excel** is not guaranteed; Sheets-only functions
    may become values or ``#NAME?`` in Excel. For ingestion, parsers often use ``data_only=True``.

    A Drive file that is only an uploaded ``.xlsx`` (not ``application/vnd.google-apps.spreadsheet``)
    is rejected — use a Google Sheet id/url, or set ``GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH`` to a local path.
    """
    meta = svc.files().get(fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True).execute()
    mime = meta.get("mimeType") or ""
    name = meta.get("name") or ""
    print(f"download excel: {name!r} mimeType={mime!r}")
    if mime != _MIME_GOOGLE_SHEET:
        raise SystemExit(
            "download_excel expects a native Google Sheet (application/vnd.google-apps.spreadsheet). "
            f"Got mimeType={mime!r}. Point GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL / EXCEL_FILE_ID at a Sheet, "
            "or use GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH for a local .xlsx."
        )
    print("  path: files.export → application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    data = _export_bytes(svc, file_id, _MIME_XLSX)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(f"  wrote {len(data)} bytes -> {output}")


def upload_excel(svc, local: Path, folder_id: str) -> dict[str, Any]:
    if not local.is_file():
        raise SystemExit(f"not a file: {local}")
    body = {"name": local.name, "parents": [folder_id]}
    media = MediaFileUpload(str(local), mimetype=_MIME_XLSX, resumable=True)
    created = svc.files().create(
        body=body,
        media_body=media,
        fields="id,name,mimeType,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()
    print("upload excel:", created)
    return created


_MIS_MONTHS_LOWER = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _mis_month_label_utc(now_utc: datetime | None = None) -> str:
    dt = now_utc or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return _MIS_MONTHS_LOWER[dt.month - 1]


def _drive_escape_query_literal(s: str) -> str:
    """Drive query ``name = '...'`` escaping (backslash and single quote)."""
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")


def _mis_site_folder_name_from_relative_path(relative_path: str) -> str:
    """
    First path segment of a contract ``relative_path`` (e.g. ``taco/foo.pdf`` → ``taco``).
    PDFs directly under the contracts root (no ``/``) use ``GOOGLE_DRIVE_TEST_MIS_SITE_FOLDER`` if set,
    else ``_contracts_root`` with a printed note.
    """
    rel = (relative_path or "").strip().replace("\\", "/")
    if "/" in rel:
        return rel.split("/", 1)[0].strip() or "_contracts_root"
    fallback = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_SITE_FOLDER") or "").strip()
    if fallback:
        return fallback
    return "_contracts_root"


def _drive_list_children_folders_named(
    svc, *, parent_id: str, folder_name: str, max_hits: int = 20
) -> list[dict[str, Any]]:
    esc = _drive_escape_query_literal(folder_name)
    q = (
        f"'{parent_id}' in parents and trashed = false and "
        f"mimeType = '{_MIME_FOLDER}' and name = '{esc}'"
    )
    merged: dict[str, dict[str, Any]] = {}
    for corp in ("allDrives", "user"):
        for f in _drive_collect_query(svc, q=q, corpora=corp, max_files=max_hits):
            fid = str(f.get("id") or "")
            if fid and fid not in merged:
                merged[fid] = f
    return list(merged.values())


def _drive_create_folder(svc, *, parent_id: str, name: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "mimeType": _MIME_FOLDER,
        "parents": [parent_id],
    }
    return svc.files().create(
        body=body,
        fields="id,name,parents,mimeType",
        supportsAllDrives=True,
    ).execute()


def _drive_ensure_child_folder(
    svc, *, parent_id: str, name: str, kind: str
) -> tuple[str, bool]:
    """Return ``(folder_id, created)``."""
    hits = _drive_list_children_folders_named(svc, parent_id=parent_id, folder_name=name)
    if len(hits) > 1:
        print(
            f"  [{kind}] warning: {len(hits)} folders named {name!r} under parent — using first id={hits[0].get('id')!r}"
        )
    if hits:
        fid = str(hits[0].get("id") or "")
        print(f"  [{kind}] reuse folder name={name!r} id={fid}")
        return fid, False
    created = _drive_create_folder(svc, parent_id=parent_id, name=name)
    fid = str(created.get("id") or "")
    print(f"  [{kind}] created folder name={name!r} id={fid}")
    return fid, True


def _drive_apply_domain_permission(
    svc,
    *,
    file_id: str,
    domain: str,
    role: str,
    label: str,
) -> None:
    """
    Grant access to everyone in ``domain`` (Google Workspace **domain** permission).
    Fails if the file is not in a context that allows domain permissions (e.g. consumer Gmail).
    """
    body = {"type": "domain", "role": role, "domain": domain}
    try:
        svc.permissions().create(
            fileId=file_id,
            body=body,
            supportsAllDrives=True,
            fields="id,type,domain,role",
        ).execute()
        print(f"  [perm:{label}] domain={domain!r} role={role!r} file_id={file_id!r}")
    except HttpError as e:
        detail = getattr(e, "content", b"")[:400]
        if e.resp.status == 400 and b"only be added once" in detail:
            print(f"  [perm:{label}] already present for domain={domain!r} (skip)")
            return
        print(
            f"  [perm:{label}] failed {e.resp.status} {e.reason!r} — "
            f"domain ACLs need Workspace/Shared Drive; body≈{detail!r}"
        )


def _drive_remove_anyone_permissions(svc, *, file_id: str, label: str) -> int:
    """
    Remove ``type=anyone`` permissions (public / anyone-with-link style access on that item).
    Returns how many permission rows were deleted.
    """
    to_drop: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        req = svc.permissions().list(
            fileId=file_id,
            supportsAllDrives=True,
            fields="nextPageToken, permissions(id,type,role,allowFileDiscovery)",
        )
        if page_token:
            req = req.pageToken(page_token)
        try:
            res = req.execute()
        except HttpError as e:
            print(
                f"  [perm:{label}] permissions.list failed {e.resp.status} {e.reason!r} "
                f"for file_id={file_id!r}"
            )
            return 0
        for p in res.get("permissions") or []:
            if str(p.get("type") or "") == "anyone":
                to_drop.append(p)
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    removed = 0
    for p in to_drop:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        try:
            svc.permissions().delete(
                fileId=file_id,
                permissionId=pid,
                supportsAllDrives=True,
            ).execute()
            removed += 1
            role = str(p.get("role") or "")
            afd = p.get("allowFileDiscovery")
            print(
                f"  [perm:{label}] removed type=anyone id={pid!r} role={role!r} "
                f"allowFileDiscovery={afd!r}"
            )
        except HttpError as e:
            print(
                f"  [perm:{label}] permissions.delete anyone id={pid!r} failed: "
                f"{e.resp.status} {e.reason!r}"
            )
    return removed


def simulate_mis_excel_upload_layout(
    svc,
    *,
    mis_parent_folder_id: str,
    contract_style_relative_path: str,
    local_xlsx: Path,
    domain: str,
    domain_role: str,
) -> None:
    """
    Production-shaped layout: ``<mis_parent>/<site_segment>/<month>/attendance.xlsx``.

    - **Site** folder name = first segment of ``contract_style_relative_path`` (same idea as contract PDF ``relative_path``).
    - **Month** folder = English three-letter month in lower case (``jan`` … ``dec``), UTC unless overridden by env.
    - **Domain permission** on site folder, month folder, and uploaded file (Workspace domain share; not a substitute for DLP).
    - **Removes** ``type=anyone`` permissions on those items (anyone-with-link / public) before adding domain ACLs.
    """
    month_override = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_MONTH") or "").strip().lower()
    if month_override:
        if month_override not in _MIS_MONTHS_LOWER:
            raise SystemExit(
                f"GOOGLE_DRIVE_TEST_MIS_MONTH must be one of {list(_MIS_MONTHS_LOWER)}, got {month_override!r}"
            )
        month_name = month_override
    else:
        month_name = _mis_month_label_utc()

    site = _mis_site_folder_name_from_relative_path(contract_style_relative_path)
    print("--- MIS layout upload (site / month / excel + domain perm) ---")
    print(f"  mis_parent_folder_id={mis_parent_folder_id!r}")
    print(f"  contract_style_relative_path={contract_style_relative_path!r} → site_folder={site!r}")
    print(f"  month_folder={month_name!r} (UTC label; override with GOOGLE_DRIVE_TEST_MIS_MONTH)")
    print(f"  local_xlsx={local_xlsx}")
    print(f"  domain_permission domain={domain!r} role={domain_role!r}")

    if site == "_contracts_root" and "/" not in (contract_style_relative_path or "").strip().replace("\\", "/"):
        print(
            "  note: path has no '/' — using folder name _contracts_root. "
            "Set GOOGLE_DRIVE_TEST_MIS_SITE_FOLDER to override."
        )

    site_id, _site_created = _drive_ensure_child_folder(
        svc, parent_id=mis_parent_folder_id, name=site, kind="site(contract_document)"
    )
    month_id, _month_created = _drive_ensure_child_folder(
        svc, parent_id=site_id, name=month_name, kind="month(mis)"
    )

    created_file = upload_excel(svc, local_xlsx, month_id)
    file_id = str(created_file.get("id") or "")
    if not file_id:
        raise SystemExit("upload returned no file id")

    for lbl, fid in (
        ("site", site_id),
        ("month", month_id),
        ("file", file_id),
    ):
        _drive_remove_anyone_permissions(svc, file_id=fid, label=lbl)
        _drive_apply_domain_permission(
            svc, file_id=fid, domain=domain, role=domain_role, label=lbl
        )

    print(
        f"  done: excel at logical path {site!r}/{month_name!r}/{local_xlsx.name!r} "
        f"(Drive file id={file_id!r})"
    )


def _attendance_column_map_from_settings() -> dict[str, str] | None:
    from app.config.settings import settings

    raw = (settings.o2c_attendance_column_map_json or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _print_attendance_map_summary(amap: dict[str, list[dict[str, Any]]], *, max_sites: int = 30) -> None:
    sites = sorted(amap.keys())
    total_rows = sum(len(v) for v in amap.values())
    print(f"  client_site keys: {len(sites)}  total records: {total_rows}")
    for i, site in enumerate(sites[:max_sites], start=1):
        rows = amap[site]
        sample = rows[0] if rows else {}
        keys_preview = sorted(sample.keys())[:12]
        print(f"  [{i}] {site!r}: n={len(rows)} sample_keys={keys_preview}")
    if len(sites) > max_sites:
        print(f"  … truncated; {len(sites) - max_sites} more site(s)")


def simulate_attendance_parse_from_google_sheet(
    svc,
    *,
    excel_file_id: str,
    out_dir: Path,
) -> None:
    """
    Export Google Sheet (or native xlsx) via Drive, then parse with ``parse_ohc_summary_workbook``
    (same path as O2C pipeline ``engine=auto`` / roll / google).
    """
    from app.agents.o2c_ohc.attendance_summary import parse_ohc_summary_workbook

    excel_file_id = drive_id_from_url_or_id(excel_file_id)

    path_override = (os.environ.get("GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH") or "").strip()
    engine = (os.environ.get("GOOGLE_DRIVE_TEST_ATTENDANCE_ENGINE") or "auto").strip() or "auto"

    if path_override:
        xlsx_path = Path(path_override).expanduser().resolve()
        if not xlsx_path.is_file():
            print(
                f"--- attendance parse simulation (skipped: GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH "
                f"not a file: {xlsx_path}) ---"
            )
            return
        print("--- attendance parse simulation (local file, no Drive export) ---")
        print(f"  path={xlsx_path} engine={engine!r}")
    else:
        xlsx_path = out_dir / "attendance.xlsx"
        if not xlsx_path.is_file():
            if not excel_file_id:
                print(
                    "--- attendance parse simulation (skipped: set GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL "
                    "or GOOGLE_DRIVE_TEST_EXCEL_FILE_ID for export, or GOOGLE_DRIVE_TEST_ATTENDANCE_XLSX_PATH "
                    "for a local xlsx) ---"
                )
                return
            print(f"  exporting Drive file {excel_file_id!r} → {xlsx_path}")
            download_excel(svc, excel_file_id, xlsx_path)
        print(f"--- attendance parse simulation (Drive → xlsx, then parse) ---")
        print(f"  path={xlsx_path} engine={engine!r}")

    cmap = _attendance_column_map_from_settings()
    if cmap:
        print(f"  column_map from settings: {cmap!r}")

    try:
        amap = parse_ohc_summary_workbook(xlsx_path, column_map=cmap, engine=engine)
    except Exception as e:
        print(f"  parse_ohc_summary_workbook failed: {type(e).__name__}: {e}")
        raise

    _print_attendance_map_summary(amap)
    print("  reference: app.agents.o2c_ohc.attendance_summary.parse_ohc_summary_workbook")


def _parse_since(s: str) -> str:
    s = s.strip()
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    elif "+" in s[10:] or (s.count("-") > 2 and "T" in s):
        dt = datetime.fromisoformat(s)
    else:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def list_since(svc, *, since: str, folder_id: str | None) -> None:
    ts = _parse_since(since)
    parts = ["trashed = false"]
    if folder_id:
        parts.append(f"'{folder_id}' in parents")
    parts.append(f"(modifiedTime > '{ts}' or createdTime > '{ts}')")
    q = " and ".join(parts)
    print("list-since query:", q)
    out = []
    page_token = None
    while len(out) < _LIST_MAX:
        list_kw: dict = dict(
            q=q,
            pageSize=min(100, _LIST_MAX - len(out)),
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,createdTime,size)",
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives" if folder_id else "user",
        )
        req = svc.files().list(**list_kw)
        if page_token:
            req = req.pageToken(page_token)
        res = req.execute()
        batch = res.get("files") or []
        out.extend(batch)
        page_token = res.get("nextPageToken")
        if not page_token or not batch:
            break
    for f in out[:_LIST_MAX]:
        print(
            f"{f.get('id')}\t{f.get('modifiedTime')}\t{f.get('createdTime')}\t{f.get('mimeType')}\t{f.get('name')}"
        )
    print(f"total listed: {len(out[:_LIST_MAX])}")


def _drive_list_page(
    svc,
    *,
    q: str,
    corpora: str,
    page_size: int,
    page_token: str | None,
    fields: str = "nextPageToken, files(id,name,mimeType,modifiedTime,size,parents)",
) -> tuple[list, str | None]:
    list_kw: dict = dict(
        q=q,
        pageSize=page_size,
        fields=fields,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora=corpora,
    )
    req = svc.files().list(**list_kw)
    if page_token:
        req = req.pageToken(page_token)
    res = req.execute()
    return res.get("files") or [], res.get("nextPageToken")


def _drive_collect_query(svc, *, q: str, corpora: str, max_files: int) -> list:
    out: list = []
    page_token = None
    while len(out) < max_files:
        batch, page_token = _drive_list_page(
            svc,
            q=q,
            corpora=corpora,
            page_size=min(100, max_files - len(out)),
            page_token=page_token,
        )
        out.extend(batch)
        if not page_token or not batch:
            break
    return out


def _relative_path_from_root(*, rel_prefix: str, file_name: str) -> str:
    """Match ``folder_scan`` / ``PdfCandidate.relative_path``: POSIX path under contract root."""
    name = (file_name or "").strip().replace("\\", "/")
    if not rel_prefix:
        return name
    return f"{rel_prefix}/{name}"


def _parents_include(parents: object, folder_id: str) -> bool:
    if not parents:
        return False
    if isinstance(parents, str):
        return parents == folder_id
    return folder_id in (parents if isinstance(parents, (list, tuple)) else [])


def _contract_pdfs_recursive(
    svc,
    *,
    root_folder_id: str,
    ts: str,
    corpora: str,
    max_pdfs: int,
) -> dict[str, dict]:
    """
    DFS under ``root_folder_id`` only: each step lists **direct children** of the current folder
    (``'folderId' in parents``). Recurses only into ``application/vnd.google-apps.folder`` — no
    shortcuts, so traversal cannot jump outside the subtree rooted at ``root_folder_id``.
    """
    by_id: dict[str, dict] = {}
    visited: set[str] = set()

    def walk(folder_id: str, depth: int, rel_prefix: str) -> None:
        if depth > _CONTRACT_WALK_MAX_DEPTH or len(by_id) >= max_pdfs:
            return
        if folder_id in visited:
            return
        visited.add(folder_id)

        q_pdf = (
            f"'{folder_id}' in parents and trashed = false and "
            f"mimeType = 'application/pdf' and modifiedTime > '{ts}'"
        )
        for f in _drive_collect_query(svc, q=q_pdf, corpora=corpora, max_files=max(0, max_pdfs - len(by_id))):
            if not _parents_include(f.get("parents"), folder_id):
                continue
            fid = str(f.get("id") or "")
            fname = str(f.get("name") or "")
            rel = _relative_path_from_root(rel_prefix=rel_prefix, file_name=fname)
            by_id[fid] = {**f, "relative_path": rel}

        q_sub = f"'{folder_id}' in parents and trashed = false and mimeType = '{_MIME_FOLDER}'"
        subfolders = _drive_collect_query(svc, q=q_sub, corpora=corpora, max_files=2000)
        for fol in subfolders:
            if str(fol.get("mimeType") or "") != _MIME_FOLDER:
                continue
            if not _parents_include(fol.get("parents"), folder_id):
                continue
            fid = str(fol.get("id") or "")
            fname = str(fol.get("name") or "")
            child_prefix = _relative_path_from_root(rel_prefix=rel_prefix, file_name=fname)
            walk(fid, depth + 1, child_prefix)

    walk(root_folder_id, 0, "")
    return by_id


def _env_truthy(name: str, *, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _drive_ingestion_root_token(drive_folder_id: str) -> str:
    """Synthetic key for logs only; production ingest uses filesystem ``contracts_root`` string."""
    return f"gdrive://folder/{drive_folder_id}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_drive_modified_at(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _print_db_ingestion_precheck_stubs(*, ingestion_root: str, relative_path: str, sha256_hex: str) -> None:
    """What ``list_new_or_updated_pdfs`` / ``ingest_contract_payload`` touch in agenos (not executed)."""
    print("  [db:skipped] list_new_or_updated_pdfs — would query agenos:")
    print(
        "      SELECT sha256 FROM contract_document WHERE ingestion_root = %s  "
        f"(ingestion_root={ingestion_root!r})"
    )
    print("      → skip candidate if sha256 already in set (dedupe).")
    print(
        "      SELECT relative_path FROM failed_contract_parsing WHERE folder_root = %s  "
        "→ retry paths eligible even if mtime ≤ watermark."
    )
    print(
        "      SELECT MAX(source_file_modified_at) FROM contract_document WHERE ingestion_root = %s  "
        "→ watermark when ignore_mtime_watermark=False."
    )
    print(f"  [db:skipped] candidate relative_path={relative_path!r} sha256={sha256_hex[:20]}…")


def _print_pipeline_stubs_after_precheck() -> None:
    print("  [pipeline:skipped] extract_markdown_and_payload → OpenAI Responses (payload JSON + markdown)")
    print("  [pipeline:skipped] validate_contract_payload → jsonschema / repair loop")
    print("  [pipeline:skipped] ingest_contract_payload + index_contract_markdown (transactional)")


def _openai_upload_pdf_smoke(pdf_path: Path) -> tuple[str, Any]:
    """Mirror ``llm_extract.extract_markdown_and_payload`` upload step: ``files.create`` only."""
    import httpx
    from openai import OpenAI

    from app.config.settings import settings

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when GOOGLE_DRIVE_TEST_OPENAI_UPLOAD=1")
    _to = max(30.0, float(settings.o2c_openai_http_timeout_seconds))
    client = OpenAI(api_key=api_key, timeout=httpx.Timeout(_to, connect=30.0))
    t0 = time.monotonic()
    with pdf_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    elapsed = time.monotonic() - t0
    file_id = getattr(uploaded, "id", None)
    if not file_id:
        raise RuntimeError("openai_file_upload_error: missing file id")
    print(f"  [openai] files.create purpose=user_data id={file_id!r} ({elapsed:.1f}s)")
    return str(file_id), client


def _openai_delete_file(client: Any, file_id: str) -> None:
    try:
        client.files.delete(file_id)
        print(f"  [openai] files.delete id={file_id!r} (cleanup, same as llm_extract finally)")
    except Exception as e:
        print(f"  [openai] files.delete failed: {e}")


def simulate_drive_contract_ingestion(
    svc,
    rows: list[dict[str, Any]],
    *,
    drive_root_folder_id: str,
    since_user: str,
) -> None:
    """
    After Drive PDF list: print ingestion-equivalent **DB** checks (not run) and local **PDF probes**;
    optionally **upload** each temp PDF to OpenAI like production extract.
    """
    from app.agents.o2c_ohc.billing_profile import infer_billing_profile
    from app.agents.o2c_ohc.pdf_probe import infer_ingestion_class, pdf_page_count
    from app.agents.o2c_ohc.pipeline import infer_contract_prompt_profile

    max_n = max(0, int(os.environ.get("GOOGLE_DRIVE_TEST_INGEST_SIMULATE_MAX") or "3"))
    do_openai = _env_truthy("GOOGLE_DRIVE_TEST_OPENAI_UPLOAD")
    root_tok = _drive_ingestion_root_token(drive_root_folder_id)

    print("--- ingestion simulation (agenos not contacted) ---")
    print(f"synthetic ingestion_root token (log only): {root_tok}")
    print(f"Drive list cutoff (user env): {since_user!r} → RFC3339 {_parse_since(since_user)}")
    if not rows:
        print("no PDF rows to simulate")
        return
    print(f"processing first {max_n} of {len(rows)} row(s); OPENAI upload={'on' if do_openai else 'off'}")

    for i, row in enumerate(rows[:max_n], start=1):
        fid = str(row.get("id") or "")
        rel = str(row.get("relative_path") or "")
        name = str(row.get("name") or "contract.pdf")
        mt_raw = row.get("modifiedTime")
        print(f"\n--- simulate [{i}/{min(max_n, len(rows))}] drive_file_id={fid} ---")
        print(f"  relative_path={rel!r} name={name!r} drive.modifiedTime={mt_raw!r}")

        try:
            data, meta = fetch_pdf_bytes_from_drive(svc, fid)
        except HttpError as e:
            print(f"  [drive] download failed: {e.resp.status} {e.reason}")
            continue

        digest = _sha256_bytes(data)
        mtime = _parse_drive_modified_at(str(meta.get("modifiedTime") or mt_raw or ""))
        print(f"  [local] bytes={len(data)} sha256={digest[:16]}… mtime_utc={mtime}")

        safe_name = name.replace("/", "_") or "contract.pdf"
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"

        tmp: Path | None = None
        try:
            fd, tmp_s = tempfile.mkstemp(prefix="gdrive_ingest_", suffix=".pdf")
            os.close(fd)
            tmp = Path(tmp_s)
            tmp.write_bytes(data)

            _print_db_ingestion_precheck_stubs(ingestion_root=root_tok, relative_path=rel, sha256_hex=digest)

            pages = pdf_page_count(tmp)
            iclass = infer_ingestion_class(tmp)
            cpp = infer_contract_prompt_profile(rel)
            bp = infer_billing_profile(rel, cpp)
            print(f"  [probe] pdf_page_count={pages} infer_ingestion_class={iclass!r}")
            print(f"  [profile] contract_prompt_profile={cpp!r} billing_profile={bp!r}")

            _print_pipeline_stubs_after_precheck()

            if do_openai:
                file_id, client = _openai_upload_pdf_smoke(tmp)
                _openai_delete_file(client, file_id)
        except Exception as e:
            print(f"  [simulate] error: {e}")
        finally:
            if tmp is not None and tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def list_contract_pdfs_after_mtime(svc, *, folder_id: str, since: str) -> list[dict[str, Any]]:
    """
    Mirror local contract ingest: ``rglob('*.pdf')`` + ``mtime > since``.

    Traversal stays inside the tree rooted at ``folder_id``: only ``'currentId' in parents`` lists
    and only recurse into ``application/vnd.google-apps.folder`` children. Each PDF row gets
    ``relative_path`` (slash-separated names from root folder down to the file).

    ``relative_path`` is what ingestion uses with ``contracts_root`` = this folder; the **first
    segment** (before ``/``) drives ``infer_contract_prompt_profile`` / ``infer_billing_profile``.

    Merges ``corpora=allDrives`` and ``corpora=user`` (dedupe by file id).
    """
    from app.agents.o2c_ohc.billing_profile import infer_billing_profile
    from app.agents.o2c_ohc.pipeline import infer_contract_prompt_profile

    ts = _parse_since(since)
    print("contract-folder (ingestion-style) modifiedTime >", ts)
    print("reference: app/o2c/folder_scan.py iter_pdf_candidates (recursive + mtime)")

    merged: dict[str, dict[str, Any]] = {}
    for corp in ("allDrives", "user"):
        part = _contract_pdfs_recursive(
            svc,
            root_folder_id=folder_id,
            ts=ts,
            corpora=corp,
            max_pdfs=_CONTRACT_FOLDER_LIST_MAX,
        )
        merged.update(part)
        print(f"  corpus={corp!r}: {len(part)} pdf(s) in tree")

    if not merged:
        for corp in ("allDrives", "user"):
            q_any = f"'{folder_id}' in parents and trashed = false"
            kids = _drive_collect_query(svc, q=q_any, corpora=corp, max_files=30)
            mimes: dict[str, int] = {}
            for k in kids:
                m = str(k.get("mimeType") or "?")
                mimes[m] = mimes.get(m, 0) + 1
            print(f"  debug root children corpus={corp!r}: count={len(kids)} by_mime={mimes}")
        print(
            "  hint: contracts may be Google Docs (mime application/vnd.google-apps.document), "
            "not application/pdf; or folder id / share / corpus mismatch"
        )

    rows = sorted(merged.values(), key=lambda f: f.get("modifiedTime") or "", reverse=True)
    no_folder_segment = sum(1 for f in rows if "/" not in str(f.get("relative_path") or ""))
    if no_folder_segment:
        print(
            f"  profile warning: {no_folder_segment} row(s) have relative_path without '/' — "
            "ingestion uses generic contract prompt / billing for those (same as PDF directly under "
            "local contracts_root). Put profile folders (taco/, tcs/) under the Drive root id."
        )

    print(
        "columns: id\tmodifiedTime\trelative_path\tcontract_prompt_profile\tbilling_profile\tmimeType\tname"
    )
    for f in rows[:_CONTRACT_FOLDER_LIST_MAX]:
        rel = str(f.get("relative_path") or "")
        cpp = infer_contract_prompt_profile(rel)
        bp = infer_billing_profile(rel, cpp)
        print(
            f"{f.get('id')}\t{f.get('modifiedTime')}\t{rel}\t{cpp}\t{bp}\t"
            f"{f.get('mimeType')}\t{f.get('name')}"
        )
    print(f"total pdfs (recursive under folder root, modified > since, deduped): {len(rows)}")
    return rows


def main() -> None:
    cpath = _credentials_path()
    sa_email = _service_account_email(cpath)
    out = _out_dir()
    try:
        svc = drive_service(cpath)
    except Exception as e:
        raise SystemExit(f"Failed to build Drive client: {e}") from e

    try:
        print("--- check ---")
        run_check(svc, sa_email=sa_email)

        pdf_id = _env_drive_id("GOOGLE_DRIVE_TEST_PDF_FILE_ID")
        if pdf_id:
            print("--- download pdf ---")
            download_pdf(svc, pdf_id, out / "contract.pdf")

        xlsx_id = _env_drive_id_first(
            "GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL",
            "GOOGLE_DRIVE_TEST_SHEET_URL",
            "GOOGLE_DRIVE_TEST_EXCEL_FILE_ID",
        )
        if xlsx_id:
            print("--- download excel ---")
            download_excel(svc, xlsx_id, out / "attendance.xlsx")

        if _env_truthy("GOOGLE_DRIVE_TEST_ATTENDANCE_PARSE_SIMULATE"):
            sheet_for_parse = xlsx_id or _env_drive_id_first(
                "GOOGLE_DRIVE_TEST_ATTENDANCE_SHEET_ID",
                "GOOGLE_DRIVE_TEST_GOOGLE_SHEET_URL",
                "GOOGLE_DRIVE_TEST_SHEET_URL",
            )
            simulate_attendance_parse_from_google_sheet(svc, excel_file_id=sheet_for_parse, out_dir=out)

        folder = _env_drive_id("GOOGLE_DRIVE_TEST_UPLOAD_FOLDER_ID")
        local_up = (os.environ.get("GOOGLE_DRIVE_TEST_UPLOAD_LOCAL_XLSX") or "").strip()
        if folder and local_up:
            print("--- upload excel ---")
            upload_excel(svc, Path(local_up).expanduser().resolve(), folder)
        elif folder or local_up:
            print("skip upload: set both GOOGLE_DRIVE_TEST_UPLOAD_FOLDER_ID and GOOGLE_DRIVE_TEST_UPLOAD_LOCAL_XLSX")

        # Disabled: MIS folder creation + upload + domain / anyone ACL (Shared Drive only; re-enable in main when ready).
        # mis_parent = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_UPLOAD_PARENT_ID") or "").strip()
        # mis_rel = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_RELATIVE_PATH") or "").strip()
        # mis_xlsx = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_UPLOAD_LOCAL_XLSX") or "").strip()
        # mis_domain = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_DOMAIN") or "1mg.com").strip() or "1mg.com"
        # mis_dom_role = (os.environ.get("GOOGLE_DRIVE_TEST_MIS_DOMAIN_ROLE") or "writer").strip() or "writer"
        # if _env_truthy("GOOGLE_DRIVE_TEST_MIS_LAYOUT_UPLOAD"):
        #     if mis_parent and mis_rel and mis_xlsx:
        #         simulate_mis_excel_upload_layout(
        #             svc,
        #             mis_parent_folder_id=mis_parent,
        #             contract_style_relative_path=mis_rel,
        #             local_xlsx=Path(mis_xlsx).expanduser().resolve(),
        #             domain=mis_domain,
        #             domain_role=mis_dom_role,
        #         )
        #     else:
        #         print(
        #             "skip MIS layout: set GOOGLE_DRIVE_TEST_MIS_UPLOAD_PARENT_ID, "
        #             "GOOGLE_DRIVE_TEST_MIS_RELATIVE_PATH, and GOOGLE_DRIVE_TEST_MIS_UPLOAD_LOCAL_XLSX"
        #         )

        # Disabled: contract-folder Drive PDF list + ingestion / OpenAI simulation;
        #           generic list-since (re-enable when needed).
        # list_since_cutoff = (os.environ.get("GOOGLE_DRIVE_TEST_LIST_SINCE") or "").strip()
        # contract_dir = (os.environ.get("GOOGLE_DRIVE_TEST_CONTRACT_FOLDER_ID") or "").strip()
        # contract_since = (os.environ.get("GOOGLE_DRIVE_TEST_CONTRACT_SINCE") or "").strip()
        # if contract_dir:
        #     cutoff = contract_since or list_since_cutoff
        #     if cutoff:
        #         print("--- list contract folder PDFs (ingestion-style) ---")
        #         rows = list_contract_pdfs_after_mtime(svc, folder_id=contract_dir, since=cutoff)
        #         if _env_truthy("GOOGLE_DRIVE_TEST_INGEST_SIMULATE"):
        #             simulate_drive_contract_ingestion(
        #                 svc,
        #                 rows,
        #                 drive_root_folder_id=contract_dir,
        #                 since_user=cutoff,
        #             )
        #     else:
        #         print(
        #             "skip contract-folder list: set GOOGLE_DRIVE_TEST_CONTRACT_SINCE "
        #             "or GOOGLE_DRIVE_TEST_LIST_SINCE"
        #         )

        # Disabled: generic ``files.list`` since cutoff (re-enable when needed).
        # since = list_since_cutoff
        # if since:
        #     print("--- list since ---")
        #     list_folder = (os.environ.get("GOOGLE_DRIVE_TEST_LIST_FOLDER_ID") or "").strip() or None
        #     list_since(svc, since=since, folder_id=list_folder)

    except HttpError as e:
        raise SystemExit(f"Drive API error: {e.resp.status} {e.reason}\n{e.content!r}") from e


if __name__ == "__main__":
    main()
