#!/usr/bin/env python3
"""
Download a file from Google Drive using a service account JSON key.

Either resolve by **parent folder + exact file name**, or by **file id**.

Usage:
  python scripts/download_drive_sql.py --folder FOLDER_ID_OR_URL --name FILE.sql CREDENTIALS.json
  python scripts/download_drive_sql.py --file-id FILE_ID_OR_URL CREDENTIALS.json

The Drive file (or folder) must be shared with the service account ``client_email`` from the JSON.
Shared drives are supported (same flags as other backend Drive helpers).

Examples:
  python scripts/download_drive_sql.py --folder https://drive.google.com/drive/folders/abc... --name dump.sql ./sa.json
  python scripts/download_drive_sql.py --file-id 1abc...xyz ./sa.json -o ./out.sql
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

_SCOPES = ("https://www.googleapis.com/auth/drive",)
_MIME_FOLDER = "application/vnd.google-apps.folder"


def drive_id_from_url_or_id(raw: str) -> str:
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


def _drive_escape_query_literal(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")


def _download_media(svc, file_id: str) -> bytes:
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _default_output_path(meta_name: str | None, file_id: str) -> Path:
    name = (meta_name or "").strip()
    if name:
        return Path(name).name
    return Path(f"{file_id}.sql")


def find_file_id_in_folder(svc, parent_id: str, file_name: str) -> tuple[str, dict]:
    """Return (file_id, metadata dict) for the single non-trashed child with exact ``file_name``."""
    esc = _drive_escape_query_literal(file_name)
    q = f"'{parent_id}' in parents and name = '{esc}' and trashed = false"
    res = (
        svc.files()
        .list(
            q=q,
            pageSize=25,
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = res.get("files") or []
    if not files:
        raise FileNotFoundError(f"no file named {file_name!r} in folder {parent_id!r}")
    non_folders = [f for f in files if (f.get("mimeType") or "") != _MIME_FOLDER]
    if len(non_folders) != 1:
        if len(non_folders) > 1:
            ids = ", ".join(f"{f.get('id')} ({f.get('mimeType')})" for f in non_folders)
            raise RuntimeError(f"multiple matches for {file_name!r}: {ids}")
        raise FileNotFoundError(f"only folder(s) match {file_name!r}, not a file")
    f0 = non_folders[0]
    return str(f0["id"]), f0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a file from Google Drive (service account).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--folder",
        metavar="FOLDER_ID",
        help="Parent folder id or URL; use with --name",
    )
    src.add_argument(
        "--file-id",
        metavar="FILE_ID",
        help="Drive file id or URL (skip folder/name lookup)",
    )
    parser.add_argument(
        "--name",
        help="Exact file name inside --folder (required with --folder)",
    )
    parser.add_argument("credentials_json", help="Path to Google service account JSON key file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: Drive file name in the current directory)",
    )
    args = parser.parse_args()

    if args.folder is not None:
        if not (args.name or "").strip():
            print("error: --name is required with --folder", file=sys.stderr)
            return 1
    elif args.name:
        print("error: --name only applies with --folder", file=sys.stderr)
        return 1

    cred_path = Path(args.credentials_json).expanduser().resolve()
    if not cred_path.is_file():
        print(f"error: credentials file not found: {cred_path}", file=sys.stderr)
        return 1

    creds = service_account.Credentials.from_service_account_file(str(cred_path), scopes=_SCOPES)
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    if args.folder is not None:
        parent = drive_id_from_url_or_id(args.folder)
        if not parent:
            print("error: empty folder id", file=sys.stderr)
            return 1
        if parent != str(args.folder).strip().strip('"').strip("'"):
            print(f"resolved folder id: {parent!r}")
        try:
            parent_meta = svc.files().get(
                fileId=parent,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            print(f"error: folder files.get failed: {e}", file=sys.stderr)
            return 1
        if (parent_meta.get("mimeType") or "") != _MIME_FOLDER:
            print(
                f"error: --folder must be a folder (got mimeType={parent_meta.get('mimeType')!r})",
                file=sys.stderr,
            )
            return 1
        try:
            fid, meta = find_file_id_in_folder(svc, parent, args.name.strip())
        except (FileNotFoundError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"resolved file id: {fid!r} name={meta.get('name')!r}")
    else:
        raw_id = args.file_id or ""
        fid = drive_id_from_url_or_id(raw_id)
        if not fid:
            print("error: empty file id", file=sys.stderr)
            return 1
        if fid != raw_id.strip().strip('"').strip("'"):
            print(f"resolved Drive id: {fid!r}")
        meta = {}
        try:
            meta = svc.files().get(
                fileId=fid,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            print(f"error: Drive files.get failed: {e}", file=sys.stderr)
            return 1

    mime = meta.get("mimeType") or ""
    if mime == _MIME_FOLDER:
        print("error: resolved id refers to a folder, not a file", file=sys.stderr)
        return 1
    if mime.startswith("application/vnd.google-apps."):
        print(
            f"error: native Google file type {mime!r} — use a uploaded .sql blob or add export logic; "
            "this script only downloads stored file content.",
            file=sys.stderr,
        )
        return 1

    try:
        data = _download_media(svc, fid)
    except HttpError as e:
        print(f"error: download failed: {e}", file=sys.stderr)
        return 1

    if args.output:
        out = Path(args.output).expanduser()
    else:
        out = Path.cwd() / _default_output_path(meta.get("name"), fid)

    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {len(data)} bytes → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
