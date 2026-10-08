"""Recursive PDF discovery + mtime filter using o2c_ingestion_state watermark."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class PdfCandidate:
    absolute_path: Path
    relative_path: str
    sha256: str
    mtime: datetime


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def iter_pdf_candidates(
    root: Path,
    *,
    since_mtime: datetime | None,
    ignore_mtime_watermark: bool = False,
) -> list[PdfCandidate]:
    """
    If ``ignore_mtime_watermark`` is True, every PDF under root is considered (still hashed).
    Duplicates are dropped later via ``ContractDocument.sha256``. Use when files were copied or
    unzipped and ``st_mtime`` does not reflect “added to this folder”.
    """
    root = root.expanduser().resolve()
    out: list[PdfCandidate] = []
    for p in sorted(root.rglob("*.pdf")):
        if not p.is_file():
            continue
        st = p.stat()
        mt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if not ignore_mtime_watermark and since_mtime is not None and mt <= since_mtime:
            continue
        rel = str(p.relative_to(root))
        out.append(
            PdfCandidate(
                absolute_path=p,
                relative_path=rel,
                sha256=_sha256_file(p),
                mtime=mt,
            )
        )
    return out
