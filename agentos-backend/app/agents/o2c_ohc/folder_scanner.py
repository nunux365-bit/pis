"""Discover PDFs under a root newer than DB watermark (contract_document.source_file_modified_at)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async

log = logging.getLogger(__name__)

_UNDEFINED_COLUMN = "42703"  # PostgreSQL undefined_column


def canonical_ingestion_root_key(ingestion_root: str) -> str:
    """
    DB key for ``ingestion_root`` / ``folder_root`` watermark queries.

    If ``ingestion_root`` is an existing directory, use ``Path.resolve()`` (local parity).
    Otherwise use the trimmed string (stable synthetic key for Drive, e.g. ``gdrive://...``).
    """
    raw = (ingestion_root or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    try:
        if p.exists() and p.is_dir():
            return str(p.resolve())
    except OSError:
        pass
    return raw


@dataclass
class PdfCandidate:
    absolute_path: Path
    relative_path: str
    sha256: str
    mtime: datetime
    drive_file_id: str | None = None


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


async def _watermark_for_root_async(ingestion_root: str) -> datetime | None:
    root = canonical_ingestion_root_key(ingestion_root)
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r = await session.execute(
                    text("""
                    SELECT MAX(source_file_modified_at) AS m
                    FROM contract_document
                    WHERE ingestion_root = :root
                    """),
                    {"root": root},
                )
                row = r.mappings().first()
                m = dict(row).get("m") if row else None
                if m is None:
                    return None
                if isinstance(m, datetime) and m.tzinfo is None:
                    return m.replace(tzinfo=UTC)
                return m
    except ProgrammingError as e:
        orig = getattr(e, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) if orig is not None else None
        if sqlstate == _UNDEFINED_COLUMN:
            return None
        raise


def watermark_for_root(ingestion_root: str) -> datetime | None:
    """Latest processed file mtime for this root (requires DDL patch columns)."""
    return run_agenos_async(_watermark_for_root_async(ingestion_root))


async def _load_hashes_and_failed_paths_async(root_s: str) -> tuple[set[str], set[str]]:
    known_hashes: set[str] = set()
    failed_paths: set[str] = set()
    try:
        async with AgenosAsyncSessionLocal() as session:
            async with session.begin():
                r1 = await session.execute(
                    text("SELECT sha256 FROM contract_document WHERE ingestion_root = :r"),
                    {"r": root_s},
                )
                known_hashes = {str(row["sha256"]) for row in r1.mappings().all()}
                r2 = await session.execute(
                    text("""
                    SELECT relative_path
                    FROM failed_contract_parsing
                    WHERE folder_root = :r
                    """),
                    {"r": root_s},
                )
                failed_paths = {
                    str(row["relative_path"])
                    for row in r2.mappings().all()
                    if row.get("relative_path")
                }
    except ProgrammingError as e:
        orig = getattr(e, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) if orig is not None else None
        if sqlstate != _UNDEFINED_COLUMN:
            raise
    return known_hashes, failed_paths


def _join_drive_relative_prefix(prefix: str, rel_under_site: str) -> str:
    """Prepend site folder so ``taco/Annex.pdf`` matches local ``contracts_root/taco/...`` profile rules."""
    pfx = (prefix or "").strip().replace("\\", "/").strip("/")
    rel = (rel_under_site or "").strip().replace("\\", "/")
    if pfx and rel:
        return f"{pfx}/{rel}"
    if pfx:
        return pfx
    return rel


def list_new_or_updated_pdfs(
    root: Path,
    *,
    since: datetime | None = None,
    ignore_mtime_watermark: bool = True,
    drive_svc: Any | None = None,
    drive_folder_id: str | None = None,
    ingestion_root_key: str | None = None,
    drive_relative_path_prefix: str | None = None,
) -> list[PdfCandidate]:
    """
    Recursive PDFs under root. If `since` is set, only include files with mtime > since.
    When `since` is omitted, uses DB watermark ``MAX(source_file_modified_at)`` for this root.

    Set ``ignore_mtime_watermark=True`` to skip the mtime filter (e.g. copy/unzip preserved old
    ``st_mtime``). Same-sha256 rows for this ``ingestion_root`` are still excluded; paths in
    ``failed_contract_parsing`` are still eligible for retry when the watermark would otherwise skip them.

    **Drive mode:** pass ``drive_svc``, ``drive_folder_id`` (site folder id), and ``ingestion_root_key``
    (stable DB string). Optional ``drive_relative_path_prefix`` is the site segment (e.g. ``taco``)
    prepended to each file's path under that folder so ``infer_contract_prompt_profile`` matches local layout.
    """
    from app.integrations.gdrive_o2c import (
        fetch_pdf_bytes_from_drive,
        list_contract_pdfs_merged_corpora,
        parse_drive_modified_time,
        sha256_bytes,
    )

    if drive_folder_id and ingestion_root_key and drive_svc is not None:
        root_s = canonical_ingestion_root_key((ingestion_root_key or "").strip())
        if not root_s:
            return []

        if ignore_mtime_watermark:
            effective_since = None
        elif since is None:
            effective_since = watermark_for_root(root_s)
        else:
            effective_since = since

        known_hashes, failed_paths = run_agenos_async(_load_hashes_and_failed_paths_async(root_s))

        # If we must retry failed paths with old Drive modifiedTime, do not pre-filter in the API
        # (same idea as local rglob: see every PDF, then apply mtime + failed_paths in Python).
        api_mtime = effective_since if (effective_since is None or not failed_paths) else None
        rows = list_contract_pdfs_merged_corpora(
            drive_svc, root_folder_id=drive_folder_id, modified_after_utc=api_mtime
        )
        prefix = (drive_relative_path_prefix or "").strip()
        out_drive: list[PdfCandidate] = []
        for f in rows:
            fid = str(f.get("id") or "").strip()
            if not fid:
                continue
            rel_under = str(f.get("relative_path") or "").replace("\\", "/")
            rel = _join_drive_relative_prefix(prefix, rel_under)
            mtime = parse_drive_modified_time(str(f.get("modifiedTime") or "")) or datetime.now(tz=UTC)
            if effective_since is not None and mtime <= effective_since and rel not in failed_paths:
                continue
            # Incremental cron: trust Drive modifiedTime > DB watermark — skip a discovery
            # download+hash; pipeline downloads each candidate once for LLM ingest.
            if effective_since is not None and mtime > effective_since and rel not in failed_paths:
                out_drive.append(
                    PdfCandidate(
                        absolute_path=Path("/dev/null"),
                        relative_path=rel,
                        sha256="",
                        mtime=mtime,
                        drive_file_id=fid,
                    )
                )
                continue
            try:
                data, _meta = fetch_pdf_bytes_from_drive(drive_svc, fid)
            except Exception as e:
                log.warning("Drive PDF hash fetch failed id=%s: %s", fid, e)
                continue
            digest = sha256_bytes(data)
            if digest in known_hashes:
                continue
            out_drive.append(
                PdfCandidate(
                    absolute_path=Path("/dev/null"),
                    relative_path=rel,
                    sha256=digest,
                    mtime=mtime,
                    drive_file_id=fid,
                )
            )
        return sorted(out_drive, key=lambda c: c.relative_path)

    root = root.resolve()
    root_s = str(root)
    if not root.is_dir():
        return []

    if ignore_mtime_watermark:
        effective_since: datetime | None = None
    elif since is None:
        effective_since = watermark_for_root(root_s)
    else:
        effective_since = since

    out: list[PdfCandidate] = []
    known_hashes, failed_paths = run_agenos_async(_load_hashes_and_failed_paths_async(root_s))

    for p in sorted(root.rglob("*.pdf")):
        if not p.is_file():
            continue
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        rel = str(p.relative_to(root)).replace("\\", "/")
        if effective_since is not None and mtime <= effective_since and rel not in failed_paths:
            continue
        digest = _file_sha256(p)
        if digest in known_hashes:
            continue
        out.append(PdfCandidate(absolute_path=p, relative_path=rel, sha256=digest, mtime=mtime))

    return out
