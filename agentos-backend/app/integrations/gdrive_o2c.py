"""
Google Drive helpers for O2C: contracts (PDF listing + download), Sheets export, MIS xlsx upload.

Mirrors behaviour tested in ``scripts/test_google_drive_service_account.py`` (profile-safe
``relative_path``, merged corpora, native Sheet export only).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, unquote, urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from app.config.settings import settings

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# Retry these HTTP statuses (rate limit + typical Google backend transients).
_DRIVE_TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


def drive_retry_call(fn: Callable[[], _T]) -> _T:
    """
    Run a Drive API callable with retries on rate limits, 5xx, and common transport errors.

    Uses ``settings.o2c_gdrive_api_max_retries`` and ``o2c_gdrive_api_retry_base_seconds``.
    """
    n = max(0, int(settings.o2c_gdrive_api_max_retries))
    base = float(settings.o2c_gdrive_api_retry_base_seconds)
    for attempt in range(n + 1):
        try:
            return fn()
        except HttpError as e:
            status = int(getattr(getattr(e, "resp", None), "status", 0) or 0)
            if status not in _DRIVE_TRANSIENT_HTTP_STATUS or attempt >= n:
                raise
            delay = base * (2**attempt)
            log.warning(
                "Drive API HttpError status=%s (attempt %s/%s), retry in %.2fs",
                status,
                attempt + 1,
                n + 1,
                delay,
            )
            time.sleep(delay)
        except (TimeoutError, ConnectionError, OSError) as e:
            if attempt >= n:
                raise
            delay = base * (2**attempt)
            log.warning(
                "Drive API transport %s (attempt %s/%s), retry in %.2fs",
                type(e).__name__,
                attempt + 1,
                n + 1,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("drive_retry_call: unexpected fall-through")

_SCOPES = ("https://www.googleapis.com/auth/drive",)
_MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
_MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_PDF = "application/pdf"
_MIME_FOLDER = "application/vnd.google-apps.folder"
_CONTRACT_WALK_MAX_DEPTH = 40
_CONTRACT_FOLDER_LIST_MAX = 50000
_MIS_MONTHS_LOWER = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def drive_id_from_url_or_id(raw: str) -> str:
    """Bare Drive id or extract id from Docs/Drive URL."""
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


def resolve_o2c_gdrive_credentials_path(*, settings_sa_json: str = "") -> Path:
    """First existing path among O2C setting, GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON, GOOGLE_APPLICATION_CREDENTIALS."""
    for raw in (
        (settings_sa_json or "").strip(),
        (os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON") or "").strip(),
        (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip(),
    ):
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        "Set O2C_GDRIVE_SERVICE_ACCOUNT_JSON, GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON, or "
        "GOOGLE_APPLICATION_CREDENTIALS to a service account JSON file."
    )


def build_drive_service(credentials_path: Path | None = None, *, settings_sa_json: str = "") -> Any:
    path = credentials_path or resolve_o2c_gdrive_credentials_path(settings_sa_json=settings_sa_json)
    creds = service_account.Credentials.from_service_account_file(str(path), scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_rfc3339_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_drive_modified_time(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drive_escape_query_literal(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")


def _drive_list_page(
    svc: Any,
    *,
    q: str,
    corpora: str,
    page_size: int,
    page_token: str | None,
    fields: str = "nextPageToken, files(id,name,mimeType,modifiedTime,size,parents)",
) -> tuple[list, str | None]:
    list_kw: dict[str, Any] = dict(
        q=q,
        pageSize=page_size,
        fields=fields,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora=corpora,
    )
    if page_token:
        list_kw["pageToken"] = page_token
    req = svc.files().list(**list_kw)
    res = drive_retry_call(req.execute)
    return res.get("files") or [], res.get("nextPageToken")


def _drive_collect_query(svc: Any, *, q: str, corpora: str, max_files: int) -> list:
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


def _parents_include(parents: object, folder_id: str) -> bool:
    if not parents:
        return False
    if isinstance(parents, str):
        return parents == folder_id
    return folder_id in (parents if isinstance(parents, (list, tuple)) else [])


def _relative_path_from_root(*, rel_prefix: str, file_name: str) -> str:
    name = (file_name or "").strip().replace("\\", "/")
    if not rel_prefix:
        return name
    return f"{rel_prefix}/{name}"


def _use_walk_contract_discovery() -> bool:
    """Legacy recursive folder walk when ``O2C_GDRIVE_CONTRACTS_DISCOVERY_WALK=true``."""
    raw = (os.environ.get("O2C_GDRIVE_CONTRACTS_DISCOVERY_WALK") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _primary_parent_id(parents: object) -> str | None:
    if not parents:
        return None
    if isinstance(parents, str):
        s = parents.strip()
        return s or None
    if isinstance(parents, (list, tuple)):
        for p in parents:
            s = str(p or "").strip()
            if s:
                return s
    return None


def _drive_get_meta(svc: Any, file_id: str, *, fields: str) -> dict[str, Any] | None:
    fid = (file_id or "").strip()
    if not fid:
        return None
    try:
        meta = drive_retry_call(
            svc.files().get(fileId=fid, fields=fields, supportsAllDrives=True).execute
        )
    except HttpError:
        return None
    return meta if isinstance(meta, dict) else None


def _load_folder_map_user(svc: Any, *, max_folders: int) -> dict[str, dict[str, Any]]:
    """All non-trashed folders visible to the SA (``corpora=user``) for path resolution."""
    q = f"trashed = false and mimeType = '{_MIME_FOLDER}'"
    out: dict[str, dict[str, Any]] = {}
    for f in _drive_collect_query(svc, q=q, corpora="user", max_files=max(0, max_folders)):
        fid = str(f.get("id") or "").strip()
        if fid:
            out[fid] = f
    return out


def relative_path_under_folder_root(
    *,
    file_meta: dict[str, Any],
    root_folder_id: str,
    folder_map: dict[str, dict[str, Any]],
    svc: Any | None = None,
) -> str | None:
    """
    Build the same ``relative_path`` string as ``_contract_pdfs_recursive`` for a PDF under ``root_folder_id``.

    Returns ``None`` when the file is outside the subtree rooted at ``root_folder_id``.
    """
    root = (root_folder_id or "").strip()
    if not root:
        return None
    fname = str(file_meta.get("name") or "").strip().replace("\\", "/")
    if not fname:
        return None
    parent_id = _primary_parent_id(file_meta.get("parents"))
    if not parent_id:
        return None
    if parent_id == root:
        return fname

    segments: list[str] = []
    current = parent_id
    for _ in range(_CONTRACT_WALK_MAX_DEPTH):
        fol = folder_map.get(current)
        if fol is None and svc is not None:
            fetched = _drive_get_meta(svc, current, fields="id,name,mimeType,parents")
            if fetched and str(fetched.get("mimeType") or "") == _MIME_FOLDER:
                fol = fetched
                folder_map[current] = fetched
        if fol is None:
            return None
        seg = str(fol.get("name") or "").strip().replace("\\", "/")
        if not seg:
            return None
        segments.append(seg)
        fol_parent = _primary_parent_id(fol.get("parents"))
        if not fol_parent:
            return None
        if fol_parent == root:
            segments.reverse()
            return "/".join(segments + [fname])
        current = fol_parent
    return None


def _contract_pdfs_global_query(
    svc: Any,
    *,
    root_folder_id: str,
    modified_after_utc: datetime | None,
    max_pdfs: int,
) -> dict[str, dict[str, Any]]:
    """
    List PDFs via one Drive query (``corpora=user``), scoped to ``root_folder_id`` by parent chain.

    Matches recursive-walk ``relative_path`` layout for the same tree; avoids per-folder list calls.
    """
    root = drive_id_from_url_or_id(root_folder_id)
    if not root:
        return {}

    mtime_clause = ""
    if modified_after_utc is not None:
        mtime_clause = f" and modifiedTime > '{drive_rfc3339_utc(modified_after_utc)}'"
    q_pdf = f"trashed = false and mimeType = '{_MIME_PDF}'{mtime_clause}"

    # Full scan: one folder map for path resolution. Incremental: lazy parent lookups (few PDFs/night).
    folder_map: dict[str, dict[str, Any]] = {}
    if modified_after_utc is None:
        folder_map = _load_folder_map_user(svc, max_folders=_CONTRACT_FOLDER_LIST_MAX)

    by_id: dict[str, dict[str, Any]] = {}
    for f in _drive_collect_query(svc, q=q_pdf, corpora="user", max_files=max(0, max_pdfs)):
        fid = str(f.get("id") or "").strip()
        if not fid or fid in by_id:
            continue
        rel = relative_path_under_folder_root(
            file_meta=f,
            root_folder_id=root,
            folder_map=folder_map,
            svc=svc,
        )
        if rel is None:
            continue
        by_id[fid] = {**f, "relative_path": rel}
    return by_id


def _list_contract_pdfs_walk_merged(
    svc: Any,
    *,
    root_folder_id: str,
    modified_after_utc: datetime | None,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for corp in ("allDrives", "user"):
        part = _contract_pdfs_recursive(
            svc,
            root_folder_id=root_folder_id,
            modified_after_utc=modified_after_utc,
            corpora=corp,
            max_pdfs=_CONTRACT_FOLDER_LIST_MAX,
        )
        merged.update(part)
    return merged


def _contract_pdfs_recursive(
    svc: Any,
    *,
    root_folder_id: str,
    modified_after_utc: datetime | None,
    corpora: str,
    max_pdfs: int,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    visited: set[str] = set()
    mtime_clause = ""
    if modified_after_utc is not None:
        mtime_clause = f" and modifiedTime > '{drive_rfc3339_utc(modified_after_utc)}'"

    def walk(folder_id: str, depth: int, rel_prefix: str) -> None:
        if depth > _CONTRACT_WALK_MAX_DEPTH or len(by_id) >= max_pdfs:
            return
        if folder_id in visited:
            return
        visited.add(folder_id)

        q_pdf = f"'{folder_id}' in parents and trashed = false and mimeType = 'application/pdf'{mtime_clause}"
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


def list_contract_pdfs_merged_corpora(
    svc: Any,
    *,
    root_folder_id: str,
    modified_after_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    PDF rows under ``root_folder_id`` with ``relative_path`` (POSIX path from that root).

    Default: global ``files.list`` on PDFs (``corpora=user``) + parent-chain path resolution —
    O(changed PDFs) instead of walking every subfolder. Set ``O2C_GDRIVE_CONTRACTS_DISCOVERY_WALK=true``
    to restore the legacy recursive walk (``allDrives`` + ``user`` merge).
    """
    folder_id = drive_id_from_url_or_id(root_folder_id)
    if not folder_id:
        return []
    if _use_walk_contract_discovery():
        merged = _list_contract_pdfs_walk_merged(
            svc,
            root_folder_id=folder_id,
            modified_after_utc=modified_after_utc,
        )
    else:
        merged = _contract_pdfs_global_query(
            svc,
            root_folder_id=folder_id,
            modified_after_utc=modified_after_utc,
            max_pdfs=_CONTRACT_FOLDER_LIST_MAX,
        )
    return sorted(merged.values(), key=lambda f: f.get("modifiedTime") or "", reverse=True)


def fetch_pdf_bytes_from_drive(svc: Any, file_id: str) -> tuple[bytes, dict[str, Any]]:
    meta = drive_retry_call(
        svc.files()
        .get(fileId=file_id, fields="id,name,mimeType,modifiedTime,size", supportsAllDrives=True)
        .execute
    )
    mime = meta.get("mimeType") or ""
    if mime == _MIME_GOOGLE_DOC:
        data = drive_retry_call(
            svc.files().export(fileId=file_id, mimeType=_MIME_PDF).execute
        )
    else:
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = drive_retry_call(dl.next_chunk)
        data = buf.getvalue()
    return data, meta


def download_pdf_to_temp_file(svc: Any, file_id: str) -> Path:
    """Download or export PDF bytes to a named temp file; caller must unlink."""
    data, meta = fetch_pdf_bytes_from_drive(svc, file_id)
    suffix = Path(str(meta.get("name") or "contract")).suffix
    if suffix.lower() != ".pdf":
        suffix = ".pdf"
    fd, name = tempfile.mkstemp(prefix="o2c_gdrive_", suffix=suffix)
    os.close(fd)
    path = Path(name)
    try:
        path.write_bytes(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def export_google_sheet_to_xlsx(svc: Any, sheet_id: str, dest: Path) -> None:
    """Native Google Sheet only: ``files.export`` → xlsx."""
    sheet_id = drive_id_from_url_or_id(sheet_id)
    meta = drive_retry_call(
        svc.files().get(fileId=sheet_id, fields="id,name,mimeType", supportsAllDrives=True).execute
    )
    mime = meta.get("mimeType") or ""
    if mime != _MIME_GOOGLE_SHEET:
        raise ValueError(
            "Expected a native Google Sheet (application/vnd.google-apps.spreadsheet); "
            f"got mimeType={mime!r}. Use a Sheet id/URL or a local .xlsx path."
        )
    data = drive_retry_call(svc.files().export(fileId=sheet_id, mimeType=_MIME_XLSX).execute)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def list_child_folders_named(svc: Any, *, parent_id: str, folder_name: str, max_hits: int = 20) -> list[dict[str, Any]]:
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


def list_child_xlsx_named(
    svc: Any, *, parent_id: str, file_name: str, max_hits: int = 20
) -> list[dict[str, Any]]:
    """Non-trashed .xlsx files directly under ``parent_id`` with exact ``file_name`` (merged corpora)."""
    esc = _drive_escape_query_literal(file_name)
    q = (
        f"'{parent_id}' in parents and trashed = false and "
        f"mimeType = '{_MIME_XLSX}' and name = '{esc}'"
    )
    merged: dict[str, dict[str, Any]] = {}
    for corp in ("allDrives", "user"):
        for f in _drive_collect_query(svc, q=q, corpora=corp, max_files=max_hits):
            fid = str(f.get("id") or "")
            if fid and fid not in merged:
                merged[fid] = f
    return list(merged.values())


def find_child_folder_id(svc: Any, *, parent_id: str, folder_name: str) -> str | None:
    hits = list_child_folders_named(svc, parent_id=parent_id, folder_name=folder_name)
    if not hits:
        return None
    if len(hits) > 1:
        log.warning(
            "Multiple Drive folders named %r under parent %s — using first id=%s",
            folder_name,
            parent_id,
            hits[0].get("id"),
        )
    return str(hits[0].get("id") or "") or None


def ensure_child_folder(svc: Any, *, parent_id: str, name: str) -> str:
    """Return existing child folder id or create one."""
    existing = find_child_folder_id(svc, parent_id=parent_id, folder_name=name)
    if existing:
        return existing
    body: dict[str, Any] = {
        "name": name,
        "mimeType": _MIME_FOLDER,
        "parents": [parent_id],
    }
    created = drive_retry_call(
        svc.files()
        .create(body=body, fields="id,name,parents,mimeType", supportsAllDrives=True)
        .execute
    )
    fid = str(created.get("id") or "")
    if not fid:
        raise RuntimeError(f"Drive folder create returned no id for {name!r}")
    return fid


def mis_month_folder_name(period_start: date) -> str:
    return _MIS_MONTHS_LOWER[period_start.month - 1]


def contract_folder_segment_for_mis(relative_path: str) -> str:
    """First path segment of contract ``folder_path`` / ``relative_path`` (matches smoke script)."""
    rel = (relative_path or "").strip().replace("\\", "/")
    if "/" in rel:
        seg = rel.split("/", 1)[0].strip()
        return seg or "_contracts_root"
    return "_contracts_root"


def upload_xlsx_to_folder(svc: Any, local: Path, folder_id: str) -> dict[str, Any]:
    """
    Put ``local`` into ``folder_id``. If a non-trashed .xlsx with the same file name already exists
    in that folder, **replace its content** (``files.update``); otherwise ``files.create``.
    """
    if not local.is_file():
        raise FileNotFoundError(str(local))
    media = MediaFileUpload(str(local), mimetype=_MIME_XLSX, resumable=True)
    hits = list_child_xlsx_named(svc, parent_id=folder_id, file_name=local.name)
    if len(hits) > 1:
        hits.sort(key=lambda f: str(f.get("modifiedTime") or ""), reverse=True)
        log.warning(
            "Multiple Drive .xlsx named %r under folder %s — overwriting newest id=%s",
            local.name,
            folder_id,
            hits[0].get("id"),
        )
    if hits:
        fid = str(hits[0].get("id") or "")
        if not fid:
            raise RuntimeError(f"Drive xlsx query returned empty id for {local.name!r}")
        return drive_retry_call(
            svc.files()
            .update(
                fileId=fid,
                media_body=media,
                fields="id,name,mimeType,webViewLink,parents",
                supportsAllDrives=True,
            )
            .execute
        )
    body = {"name": local.name, "parents": [folder_id]}
    return drive_retry_call(
        svc.files()
        .create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,webViewLink,parents",
            supportsAllDrives=True,
        )
        .execute
    )


def upload_mis_xlsx_layout(
    svc: Any,
    *,
    mis_parent_folder_id: str,
    contract_folder_segment: str,
    period_start: date,
    local_xlsx: Path,
) -> str | None:
    """
    ``<mis_parent>/<segment>/<month>/filename.xlsx`` (create folders as needed).
    Returns file id (updates content in place when same-named .xlsx already exists in the month folder).
    """
    parent_id = drive_id_from_url_or_id(mis_parent_folder_id)
    site_id = ensure_child_folder(svc, parent_id=parent_id, name=contract_folder_segment)
    month_id = ensure_child_folder(svc, parent_id=site_id, name=mis_month_folder_name(period_start))
    created = upload_xlsx_to_folder(svc, local_xlsx, month_id)
    return str(created.get("id") or "") or None
