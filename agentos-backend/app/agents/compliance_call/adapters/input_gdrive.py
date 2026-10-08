"""Google Drive input adapter — recursive media listing + download to temp (no persistence)."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from app.integrations.gdrive_o2c import (
    build_drive_service,
    drive_id_from_url_or_id,
    drive_retry_call,
    fetch_pdf_bytes_from_drive,
    resolve_o2c_gdrive_credentials_path,
)

log = logging.getLogger(__name__)

_MIME_FOLDER = "application/vnd.google-apps.folder"
_MEDIA_EXTS = frozenset(
    {
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".mpeg",
        ".webm",
        ".ogg",
        ".mpga",
        ".aac",
        ".flac",
    }
)
_CONTRACT_WALK_MAX_DEPTH = 40


def _is_media_file(meta: dict[str, Any]) -> bool:
    mime = str(meta.get("mimeType") or "")
    name = str(meta.get("name") or "")
    if mime == _MIME_FOLDER:
        return False
    ml = mime.lower()
    if ml.startswith("audio/") or ml in ("video/mp4", "video/webm"):
        return True
    return Path(name).suffix.lower() in _MEDIA_EXTS


def _drive_list_page(
    svc: Any,
    *,
    q: str,
    corpora: str,
    page_size: int,
    page_token: str | None,
) -> tuple[list, str | None]:
    list_kw: dict[str, Any] = dict(
        q=q,
        pageSize=page_size,
        fields="nextPageToken, files(id,name,mimeType,modifiedTime,size,parents)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora=corpora,
    )
    req = svc.files().list(**list_kw)
    if page_token:
        req = req.pageToken(page_token)
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


def list_media_files_recursive(
    svc: Any,
    *,
    root_folder_id: str,
    max_files: int = 5000,
) -> list[dict[str, Any]]:
    """Non-trashed audio/video-ish files under folder (recursive), merged corpora."""
    folder_id = drive_id_from_url_or_id(root_folder_id)
    merged: dict[str, dict[str, Any]] = {}

    def walk(svc_inner: Any, fid_root: str, corpora: str) -> None:
        by_id: dict[str, dict[str, Any]] = {}
        visited: set[str] = set()

        def inner(folder_id_inner: str, depth: int, rel_prefix: str) -> None:
            if depth > _CONTRACT_WALK_MAX_DEPTH or len(by_id) >= max_files:
                return
            if folder_id_inner in visited:
                return
            visited.add(folder_id_inner)

            q_subfolders = f"'{folder_id_inner}' in parents and trashed = false and mimeType = '{_MIME_FOLDER}'"
            for fol in _drive_collect_query(svc_inner, q=q_subfolders, corpora=corpora, max_files=2000):
                if str(fol.get("mimeType") or "") != _MIME_FOLDER:
                    continue
                if not _parents_include(fol.get("parents"), folder_id_inner):
                    continue
                sfid = str(fol.get("id") or "")
                fname = str(fol.get("name") or "")
                child_prefix = _relative_path_from_root(rel_prefix=rel_prefix, file_name=fname)
                inner(sfid, depth + 1, child_prefix)

            q_any = f"'{folder_id_inner}' in parents and trashed = false"
            for f in _drive_collect_query(svc_inner, q=q_any, corpora=corpora, max_files=max(0, max_files - len(by_id))):
                if not _parents_include(f.get("parents"), folder_id_inner):
                    continue
                if not _is_media_file(f):
                    continue
                rid = str(f.get("id") or "")
                fname = str(f.get("name") or "")
                rel = _relative_path_from_root(rel_prefix=rel_prefix, file_name=fname)
                by_id[rid] = {**f, "relative_path": rel}

        inner(fid_root, 0, "")

        merged.update(by_id)

    for corp in ("allDrives", "user"):
        walk(svc, folder_id, corp)

    return sorted(merged.values(), key=lambda f: f.get("modifiedTime") or "", reverse=True)


@dataclass
class WorkItem:
    drive_file_id: str
    revision: str  # Drive modifiedTime (revision fingerprint)
    filename: str
    mime: str
    relative_path: str
    media_mode: Literal["file", "stream"] = "file"


@runtime_checkable
class InputSource(Protocol):
    def list_pending(self) -> list[WorkItem]: ...
    def open_media_path(self, item: WorkItem) -> Path: ...
class GDriveInputSource:
    def __init__(self, *, svc: Any, folder_id: str) -> None:
        self._svc = svc
        self._folder_id = folder_id

    def list_pending(self) -> list[WorkItem]:
        rows = list_media_files_recursive(self._svc, root_folder_id=self._folder_id)
        out: list[WorkItem] = []
        for r in rows:
            fid = str(r.get("id") or "")
            if not fid:
                continue
            rev = str(r.get("modifiedTime") or r.get("size") or "")
            out.append(
                WorkItem(
                    drive_file_id=fid,
                    revision=rev,
                    filename=str(r.get("name") or "recording"),
                    mime=str(r.get("mimeType") or "application/octet-stream"),
                    relative_path=str(r.get("relative_path") or r.get("name") or ""),
                )
            )
        return out

    def open_media_path(self, item: WorkItem) -> Path:
        data, meta = fetch_pdf_bytes_from_drive(self._svc, item.drive_file_id)
        mime = str(meta.get("mimeType") or item.mime or "")
        if mime in ("application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet"):
            raise ValueError("Expected a binary media file, not a native Google Doc/Sheet")
        suffix = Path(item.filename).suffix or ".bin"
        fd, name = tempfile.mkstemp(prefix="cc_media_", suffix=suffix)
        os.close(fd)
        path = Path(name)
        path.write_bytes(data)
        return path


def build_compliance_drive_service() -> Any:
    """Uses ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` / ``GOOGLE_APPLICATION_CREDENTIALS`` (see ``resolve_o2c_gdrive_credentials_path``)."""
    path = resolve_o2c_gdrive_credentials_path(settings_sa_json="")
    return build_drive_service(credentials_path=path)
