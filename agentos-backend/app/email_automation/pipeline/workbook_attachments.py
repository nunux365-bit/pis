"""Resolve a downloaded Gmail attachment to a receivables workbook path.

Supports direct ``.xlsx`` / ``.xlsb`` attachments and ``.zip`` archives that
contain one matching workbook (same filename rules as :mod:`payment_reminder`
classifier). ``.xlsb`` files are converted to ``.xlsx`` in the same staging
directory via ``python-calamine`` before the openpyxl-based pipeline runs.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from openpyxl import Workbook
from python_calamine import CalamineWorkbook

# Keep in sync with ``PAYMENT_REMINDER_WEEKLY`` classifier + snapshot ingest gate.
RECEIVABLE_WORKBOOK_EXT = r"(?:xlsx|xlsb)"
RECEIVABLE_ATTACHMENT_REGEX = (
    rf".*recei?vabl[e]?s?.*\.(?:{RECEIVABLE_WORKBOOK_EXT}|zip)$"
)
RECEIVABLE_WORKBOOK_FILENAME_RE = re.compile(
    rf".*recei?vabl[e]?s?.*\.{RECEIVABLE_WORKBOOK_EXT}$",
    re.IGNORECASE,
)
RECEIVABLE_WORKBOOK_INGEST_RE = re.compile(
    rf"recei?vabl[e]?s?.*\.{RECEIVABLE_WORKBOOK_EXT}$",
    re.IGNORECASE,
)

_EXCEL_SHEET_TITLE_MAX = 31


class WorkbookResolveError(ValueError):
    """Attachment could not be resolved to a single receivables workbook."""


def _is_receivable_workbook_name(name: str) -> bool:
    return bool(RECEIVABLE_WORKBOOK_FILENAME_RE.search((name or "").strip()))


def _unique_sheet_title(name: str, used: set[str]) -> str:
    title = (name or "Sheet")[:_EXCEL_SHEET_TITLE_MAX]
    if title not in used:
        used.add(title)
        return title
    base = title
    n = 1
    while True:
        suffix = f"_{n}"
        title = f"{base[: _EXCEL_SHEET_TITLE_MAX - len(suffix)]}{suffix}"
        if title not in used:
            used.add(title)
            return title
        n += 1


def convert_xlsb_to_xlsx(src: Path, dest: Path | None = None) -> Path:
    """Read ``.xlsb`` with calamine and write a values-only ``.xlsx`` for openpyxl."""

    source = src.expanduser().resolve()
    if source.suffix.lower() != ".xlsb":
        raise WorkbookResolveError(f"expected .xlsb input, got {source.name!r}")
    target = (dest or source.with_suffix(".xlsx")).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    wb_in = CalamineWorkbook.from_path(str(source))
    wb_out = Workbook(write_only=True)
    used_titles: set[str] = set()
    for sheet_name in wb_in.sheet_names:
        ws_out = wb_out.create_sheet(title=_unique_sheet_title(sheet_name, used_titles))
        sheet = wb_in.get_sheet_by_name(sheet_name)
        for row in sheet.iter_rows():
            ws_out.append(list(row))
    wb_out.save(target)
    return target


def normalize_receivable_workbook(path: Path) -> Path:
    """Return an ``.xlsx`` path for downstream openpyxl readers."""

    resolved = path.expanduser().resolve()
    suffix = resolved.suffix.lower()
    if suffix == ".xlsx":
        if not _is_receivable_workbook_name(resolved.name):
            raise WorkbookResolveError(
                f"attachment {resolved.name!r} does not match receivables workbook naming"
            )
        return resolved
    if suffix == ".xlsb":
        if not _is_receivable_workbook_name(resolved.name):
            raise WorkbookResolveError(
                f"attachment {resolved.name!r} does not match receivables workbook naming"
            )
        return convert_xlsb_to_xlsx(resolved)
    raise WorkbookResolveError(f"unsupported workbook type: {resolved.name!r}")


def _flat_zip_name(member: str) -> str:
    """Basename only; reject path traversal."""
    parts = Path(member.replace("\\", "/")).parts
    if ".." in parts:
        raise WorkbookResolveError(f"zip path traversal rejected: {member!r}")
    return parts[-1] if parts else ""


def _extract_zip_members(zip_path: Path, dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw_name = info.filename or ""
            if raw_name.startswith("__MACOSX/"):
                continue
            base = _flat_zip_name(raw_name)
            if not base:
                continue
            target = dest_dir / base
            if target.exists() and target not in written:
                raise WorkbookResolveError(
                    f"zip contains duplicate filename {base!r}; use a single workbook"
                )
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(target)
    return written


def _pick_workbook(candidates: list[Path]) -> Path:
    matches = [p for p in candidates if _is_receivable_workbook_name(p.name)]
    if not matches:
        names = ", ".join(p.name for p in candidates[:8])
        raise WorkbookResolveError(
            "no receivables workbook in zip "
            f"(expected filename containing 'receivable' and ending in .xlsx or .xlsb; got: {names})"
        )
    if len(matches) == 1:
        return matches[0]
    # Prefer the largest file when multiple receivable workbooks are present.
    matches.sort(key=lambda p: p.stat().st_size, reverse=True)
    if matches[0].stat().st_size > matches[1].stat().st_size:
        return matches[0]
    names = ", ".join(p.name for p in matches)
    raise WorkbookResolveError(
        f"zip contains multiple receivables workbooks; cannot choose: {names}"
    )


def resolve_downloaded_workbook(downloaded: Path) -> Path:
    """Return a local ``.xlsx`` path for variant processing and snapshot ingest."""

    path = downloaded.resolve()
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsb"}:
        return normalize_receivable_workbook(path)
    if suffix != ".zip":
        raise WorkbookResolveError(f"unsupported attachment type: {path.name!r}")

    extracted = _extract_zip_members(path, path.parent)
    if not extracted:
        raise WorkbookResolveError(f"zip archive is empty: {path.name!r}")
    return normalize_receivable_workbook(_pick_workbook(extracted))


def cleanup_attachment_staging_dir(dest_dir: Path) -> None:
    """Remove downloaded zip, extracted/converter workbooks, and staging directory."""

    if not dest_dir.exists():
        return
    try:
        shutil.rmtree(dest_dir)
    except OSError:
        pass
