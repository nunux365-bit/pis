"""Multipart file handling for procurement ticket routes."""

from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import UploadFile

# Limits shared with ``app.procurement.service`` validation after read.
MAX_PROCUREMENT_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PROCUREMENT_ATTACHMENTS = 10
# Upper bound on the sum of *attachment* payload bytes per request — avoids a client
# slipping 10 × 25 MB (250 MB) of data in when only one request slot is expected.
MAX_PROCUREMENT_TOTAL_BYTES = 100 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

# Canonical MIMEs accepted by AgentOS + SAP QAS AttachmentSet.
_ALLOWED_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)

# Client/legacy aliases → canonical MIME (extension may refine xls vs xlsx).
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "application/excel": "application/vnd.ms-excel",
    "application/x-excel": "application/vnd.ms-excel",
    "application/x-msexcel": "application/vnd.ms-excel",
    "application/msexcel": "application/vnd.ms-excel",
    "application/haansoftxlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Reject ``malware.exe.pdf`` style names on extension fallback (and always as hard deny).
_DANGEROUS_INNER_EXTS: frozenset[str] = frozenset(
    {".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".js", ".vbs", ".ps1", ".dll"}
)


def _normalize_mime(mime: str) -> str:
    return (mime or "").strip().lower().split(";", 1)[0].strip()


def _filename_has_dangerous_inner_ext(filename: str) -> bool:
    """True for names like ``evil.exe.pdf`` (dangerous segment before final extension)."""
    name = PurePosixPath((filename or "").strip()).name.lower()
    parts = name.split(".")
    if len(parts) < 3:
        return False
    for part in parts[1:-1]:
        if f".{part}" in _DANGEROUS_INNER_EXTS:
            return True
    return False


def _canonical_mime_from_client(*, filename: str, mime: str) -> str | None:
    """Map advertised MIME (+ optional alias) to a canonical allowed type, or None."""
    m = _normalize_mime(mime)
    if m in _MIME_ALIASES:
        m = _MIME_ALIASES[m]
    # Ambiguous legacy Excel labels: prefer extension when present.
    if m in (
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        ext = PurePosixPath((filename or "").strip()).suffix.lower()
        if ext in (".xls", ".xlsx"):
            return _EXT_TO_MIME[ext]
    if m in _ALLOWED_MIMES:
        return m
    return None


def procurement_upload_mime_allowed(mime: str) -> bool:
    """True if the advertised MIME type is allowed (including known aliases)."""
    m = _normalize_mime(mime)
    if m in _MIME_ALIASES:
        m = _MIME_ALIASES[m]
    return m in _ALLOWED_MIMES


def content_matches_mime(*, mime: str, data: bytes) -> bool:
    """Lightweight magic-byte check for the resolved MIME (not a full parser)."""
    if not data:
        return False
    if mime == "application/pdf":
        head = data[:1024].lstrip()
        return head.startswith(b"%PDF")
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "application/vnd.ms-excel":
        # OLE Compound File (classic .xls) or ZIP (some clients mislabel .xlsx).
        return data.startswith(b"\xd0\xcf\x11\xe0") or data[:2] == b"PK"
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return data[:2] == b"PK"
    return False


def resolve_procurement_upload_mime(*, filename: str, mime: str) -> str | None:
    """Return a canonical allowed MIME, or None if the upload is not allowed.

    Prefer a recognized advertised MIME (incl. aliases); otherwise fall back to extension
    when the browser sends empty / octet-stream. Dangerous double-extensions are rejected.
    """
    if _filename_has_dangerous_inner_ext(filename):
        return None
    canonical = _canonical_mime_from_client(filename=filename, mime=mime)
    if canonical:
        return canonical
    m = _normalize_mime(mime)
    if m in ("", "application/octet-stream"):
        ext = PurePosixPath((filename or "").strip()).suffix.lower()
        return _EXT_TO_MIME.get(ext)
    return None


async def read_upload_files(files: list[UploadFile] | None) -> list[tuple[str, str, bytes]]:
    """Read uploads with per-file and per-request caps **during** read so huge bodies never
    land fully in memory. MIME (or extension fallback) is checked on the UploadFile before
    any bytes are read; content magic is checked after read.
    """
    raw_files = [f for f in (files or []) if f.filename]
    if len(raw_files) > MAX_PROCUREMENT_ATTACHMENTS:
        raise ValueError(f"At most {MAX_PROCUREMENT_ATTACHMENTS} files allowed.")
    out: list[tuple[str, str, bytes]] = []
    total_request_bytes = 0
    for f in raw_files:
        ctype = f.content_type or "application/octet-stream"
        resolved = resolve_procurement_upload_mime(filename=f.filename or "", mime=ctype)
        if not resolved:
            raise ValueError(f"File type not allowed for {f.filename}: {ctype}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await f.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            total_request_bytes += len(chunk)
            if total > MAX_PROCUREMENT_ATTACHMENT_BYTES:
                mb = MAX_PROCUREMENT_ATTACHMENT_BYTES // (1024 * 1024)
                raise ValueError(f"File {f.filename} exceeds {mb} MB.")
            if total_request_bytes > MAX_PROCUREMENT_TOTAL_BYTES:
                mb = MAX_PROCUREMENT_TOTAL_BYTES // (1024 * 1024)
                raise ValueError(f"Total upload size exceeds {mb} MB for this request.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not content_matches_mime(mime=resolved, data=data):
            raise ValueError(
                f"File content does not match declared type for {f.filename}: {resolved}"
            )
        out.append((f.filename, resolved, data))
    return out
