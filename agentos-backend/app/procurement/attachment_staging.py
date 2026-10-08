"""Local disk staging for procurement attachments until SAP AttachmentSet upload succeeds."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from app.config.settings import settings

_UNSAFE = re.compile(r"[^\w.\-]+")


def staging_root() -> Path:
    raw = (settings.procurement_attachment_staging_dir or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path("/tmp/agentos-procurement").resolve() / "attachment_staging"


def _safe_name(filename: str) -> str:
    base = os.path.basename((filename or "file.pdf").strip()) or "file.pdf"
    return _UNSAFE.sub("_", base)[:180]


def ticket_staging_dir(ticket_id: str) -> Path:
    return staging_root() / str(ticket_id)


def resolve_staging_path(*, ticket_id: str, path: str) -> Path:
    """Resolve ``path`` and ensure it stays under ``{staging_root}/{ticket_id}/``."""
    root = ticket_staging_dir(ticket_id).resolve()
    p = Path(path).expanduser().resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise PermissionError(f"staging path outside ticket folder: {path}") from e
    if not p.is_file():
        raise FileNotFoundError(path)
    return p


def write_staging_file(
    *,
    ticket_id: str,
    filename: str,
    data: bytes,
) -> str:
    """Write bytes under ``{root}/{ticket_id}/{uuid}_{name}``. Returns absolute path string."""
    if not data:
        raise ValueError("empty attachment")
    root = ticket_staging_dir(ticket_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid.uuid4().hex}_{_safe_name(filename)}"
    path.write_bytes(data)
    # Canonical path (macOS /tmp → /private/tmp) so reads match after resolve().
    return str(path.resolve())


def read_staging_file(*, ticket_id: str, path: str) -> bytes:
    return resolve_staging_path(ticket_id=ticket_id, path=path).read_bytes()


def delete_staging_file(path: str | None, *, ticket_id: str | None = None) -> None:
    if not path:
        return
    try:
        if ticket_id:
            p = resolve_staging_path(ticket_id=ticket_id, path=path)
        else:
            p = Path(path)
            if not p.is_file():
                return
        p.unlink()
    except OSError:
        pass


def delete_ticket_staging_dir(ticket_id: str) -> None:
    root = ticket_staging_dir(ticket_id)
    if not root.is_dir():
        return
    for child in root.iterdir():
        try:
            if child.is_file():
                child.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
