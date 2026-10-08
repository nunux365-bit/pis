"""In-memory cache for OHC attendance workbook: Drive export + parse (OpenPyXL / roll parser)."""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.integrations.gdrive_o2c import drive_id_from_url_or_id

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedOhcAttendanceWorkbook:
    """Resolved attendance workbook: site map + path used for optional Manpower enrichment."""

    amap: dict[str, list[dict[str, Any]]] | None
    resolved_path: Path | None


_LOCK = threading.Lock()
# cache_key -> (expires_at_monotonic, ParsedOhcAttendanceWorkbook)
_STORE: dict[str, tuple[float, ParsedOhcAttendanceWorkbook]] = {}


def clear_attendance_workbook_cache() -> None:
    """Clear all entries (e.g. tests)."""
    with _LOCK:
        _STORE.clear()


def _column_map_fingerprint() -> str:
    raw = (settings.o2c_attendance_column_map_json or "").strip()
    if not raw:
        return "0"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _workbook_cache_key(*, explicit: str | None, engine: str, col_fp: str) -> str | None:
    """
    Stable key for the same resolution path as ``resolve_attendance_xlsx_path``.
    Local files include mtime so edits invalidate without waiting for TTL.
    """
    eng = (engine or "auto").strip() or "auto"
    ex = (explicit or "").strip()
    if ex:
        p = Path(ex).expanduser()
        if not p.is_file():
            return None
        rp = p.resolve()
        try:
            mt = rp.stat().st_mtime_ns
        except OSError:
            return None
        return f"explicit:{rp}:{mt}:{eng}:{col_fp}"

    local = (settings.o2c_attendance_xlsx_path or settings.o2c_ohc_attendance_xlsx_path or "").strip()
    if local:
        p = Path(local).expanduser()
        if not p.is_file():
            return None
        rp = p.resolve()
        try:
            mt = rp.stat().st_mtime_ns
        except OSError:
            return None
        return f"settings_local:{rp}:{mt}:{eng}:{col_fp}"

    raw = (settings.o2c_gdrive_attendance_sheet_url_or_id or "").strip()
    if raw:
        sid = drive_id_from_url_or_id(raw)
        return f"gdrive:{sid}:{eng}:{col_fp}"
    return None


def _prune_expired_unlocked(now: float) -> None:
    dead = [k for k, (exp, _) in _STORE.items() if exp <= now]
    for k in dead:
        del _STORE[k]


def load_parsed_ohc_attendance_workbook_cached(
    *,
    explicit: str | None,
    cleanup_paths: list[Path],
    drive_svc: Any | None,
    engine: str = "auto",
) -> ParsedOhcAttendanceWorkbook:
    """
    Resolve workbook path (explicit → settings local → Drive export), parse to site map.

    When ``settings.o2c_attendance_workbook_cache_ttl_seconds`` > 0, returns a deep copy of a
    cached payload for the same key within the TTL (skips Drive download and parse). Otherwise
    behaviour matches uncached resolve + parse + temp cleanup.

    Returns :class:`ParsedOhcAttendanceWorkbook` with ``amap`` (possibly ``None``) and
    ``resolved_path`` when a local temp or settings file was used (for Manpower enrichment).
    """
    from app.agents.o2c_ohc.attendance_summary import parse_ohc_summary_workbook
    from app.agents.o2c_ohc.manpower_clinical_hint import enrich_amap_with_manpower_hints
    from app.agents.o2c_ohc.mis_gdrive_finalize import resolve_attendance_xlsx_path, unlink_o2c_temp_paths

    ttl = int(getattr(settings, "o2c_attendance_workbook_cache_ttl_seconds", 3600) or 0)
    col_fp = _column_map_fingerprint()
    cache_key = _workbook_cache_key(explicit=explicit, engine=engine, col_fp=col_fp)

    now = time.monotonic()
    if ttl > 0 and cache_key:
        with _LOCK:
            _prune_expired_unlocked(now)
            ent = _STORE.get(cache_key)
            if ent and ent[0] > now:
                log.debug("attendance workbook cache hit key=%s", cache_key[:80])
                wb = ent[1]
                amap = copy.deepcopy(wb.amap) if wb.amap is not None else None
                return ParsedOhcAttendanceWorkbook(amap=amap, resolved_path=wb.resolved_path)

    path = resolve_attendance_xlsx_path(
        explicit=(explicit or "").strip() or None,
        cleanup_paths=cleanup_paths,
        drive_svc=drive_svc,
    )
    if not path:
        return ParsedOhcAttendanceWorkbook(amap=None, resolved_path=None)

    path_obj = Path(path)
    amap = parse_ohc_summary_workbook(path_obj, engine=engine)
    if amap:
        enrich_amap_with_manpower_hints(amap, path_obj)

    unlink_o2c_temp_paths(cleanup_paths)

    resolved = path_obj.resolve() if path_obj.is_file() else None
    payload = ParsedOhcAttendanceWorkbook(amap=amap, resolved_path=resolved)

    if ttl > 0 and cache_key:
        with _LOCK:
            _prune_expired_unlocked(time.monotonic())
            _STORE[cache_key] = (
                time.monotonic() + ttl,
                ParsedOhcAttendanceWorkbook(
                    amap=copy.deepcopy(amap) if amap is not None else None,
                    resolved_path=resolved,
                ),
            )
            log.debug("attendance workbook cache store key=%s ttl_s=%s", cache_key[:80], ttl)

    return payload
