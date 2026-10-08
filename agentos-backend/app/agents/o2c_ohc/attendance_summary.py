"""
Parse OHC attendance workbook — Summary tab (flat table) or pivot **source** data + slicers.

**engine="auto"** (default): **OHC roll** workbook — dual tabs ``OHC Attendance On Roll`` +
``OHC Attendance Off Roll``, **or** a single unified ``OHC Attendance`` tab with the same wide layout
(string or Excel date headers in row 2). If roll parse fails, raises (no Summary fallback).

**engine="google"**: force **A1/E1 = All**, iterate **B1** over **Manpower!AB** via **xlwings** + Excel;
row **2** = role, row **3** = employee. If xlwings fails, falls back to snapshot + warnings.

**engine="xlwings"**: same as the xlwings branch of ``google`` (no snapshot fallback — raises on failure).

Override column letters via env `O2C_ATTENDANCE_COLUMN_MAP_JSON`, e.g.
`{"client_site":"D","roll_type":"C","employee_id":"A","role_code":"F","present_days":"H"}`.

For pivot files, set `O2C_ATTENDANCE_CLIENT_SITE_SLICER_INDEX` (0-based) if auto-detection of the
client–site slicer is wrong.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string
from openpyxl.worksheet.worksheet import Worksheet

from app.config.settings import settings

# Normalized in-memory row (no DB persistence).
AttendanceRecord = dict[str, Any]
ClientSiteAttendanceMap = dict[str, list[AttendanceRecord]]

_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "client_site": (
        "client_site",
        "client-site",
        "client site",
        "site",
        "client / site",
        "customer site",
        "location",
        "plant",
    ),
    "roll_type": (
        "roll",
        "onroll",
        "off roll",
        "offroll",
        "on roll",
        "employment type",
        "emp type",
        "category",
    ),
    "period": ("month", "period", "billing month", "attendance month"),
    "employee_id": ("emp id", "employee id", "emp code", "employee code", "ecode", "id"),
    "employee_name": ("name", "employee name", "emp name", "staff name"),
    "role_code": ("role", "role code", "designation", "category role", "position"),
    "present_days": ("present", "present days", "pd", "paid days", "days present"),
    "absent_days": ("absent", "absent days", "ad", "lop", "days absent"),
}


def _norm_header(cell: Any) -> str:
    if cell is None:
        return ""
    t = str(cell).strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def _find_summary_sheet(wb) -> Any:
    for name in wb.sheetnames:
        if name.strip().lower() == "summary":
            return wb[name]
    # Fallback: sheet name contains summary
    for name in wb.sheetnames:
        if "summary" in name.lower():
            return wb[name]
    return wb[wb.sheetnames[0]]


def _score_header_row(row: tuple[Any, ...]) -> int:
    texts = [_norm_header(c) for c in row if c is not None and str(c).strip()]
    return len(texts)


def _detect_header_row(ws: Worksheet, scan_max: int = 40) -> int:
    best_r = 1
    best_score = 0
    for r in range(1, min(ws.max_row, scan_max) + 1):
        row = tuple(ws.cell(r, c).value for c in range(1, min(ws.max_column, 80) + 1))
        sc = _score_header_row(row)
        if sc > best_score:
            best_score = sc
            best_r = r
    return best_r


def _map_headers(
    header_row: tuple[Any, ...],
    column_map_override: dict[str, str] | None,
) -> dict[str, int]:
    """
    Returns field_name -> 1-based column index.
    column_map_override: logical_name -> Excel column letter(s), e.g. {"client_site":"D"}
    """
    if column_map_override:
        out: dict[str, int] = {}
        for key, letters in column_map_override.items():
            if not letters:
                continue
            letters = str(letters).strip().upper()
            try:
                out[key] = column_index_from_string(letters)
            except ValueError:
                continue
        if out:
            return out

    idx_by_field: dict[str, int] = {}
    for col_idx, raw in enumerate(header_row, start=1):
        h = _norm_header(raw)
        if not h:
            continue
        for field, synonyms in _HEADER_SYNONYMS.items():
            if field in idx_by_field:
                continue
            if h == field or any(h == s or s in h or h in s for s in synonyms):
                idx_by_field[field] = col_idx
                break
    return idx_by_field


def _cell_decimal(ws: Worksheet, r: int, c: int | None) -> Decimal | None:
    if c is None:
        return None
    v = ws.cell(r, c).value
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    try:
        return Decimal(str(v).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _cell_str(ws: Worksheet, r: int, c: int | None) -> str | None:
    if c is None:
        return None
    v = ws.cell(r, c).value
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_ohc_summary_workbook(
    xlsx_path: Path,
    *,
    column_map: dict[str, str] | None = None,
    engine: str = "auto",
) -> ClientSiteAttendanceMap:
    """
    Build map: client_site_display_value -> list of attendance records.

    * ``engine="auto"`` / ``engine="roll_full"`` — require **OHC Attendance On Roll** + **Off Roll** tabs
      and parse them. No pivot/snapshot/flat fallback (prevents attendance drift).
    * ``engine="flat"`` — legacy row scan of Summary only (kept for tests / back-compat).
    """
    path = xlsx_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    if engine == "xlwings":
        from app.agents.o2c_ohc.summary_grid_attendance import try_attendance_all_sites_xlwings

        xw_map = try_attendance_all_sites_xlwings(path)
        if xw_map is not None:
            return xw_map
        raise ValueError(
            "engine='xlwings' failed: install xlwings, ensure Microsoft Excel is available, "
            "and the workbook opens without errors."
        )

    if engine == "google":
        from app.agents.o2c_ohc.summary_grid_attendance import parse_google_sheet_attendance

        m, meta = parse_google_sheet_attendance(path, prefer_xlwings=True)
        if m:
            return m
        raise ValueError(
            "engine='google' returned no data: "
            + "; ".join(meta.get("warnings") or ["unknown"])
        )

    if engine in ("auto", "roll_full"):
        from app.agents.o2c_ohc.ohc_roll_workbook_parser import try_parse_ohc_roll_workbook

        ds = try_parse_ohc_roll_workbook(path)
        if ds is not None:
            return ds.to_client_site_attendance_map()
        raise ValueError(
            f"engine={engine!r} requires roll layout: either tabs 'OHC Attendance On Roll' + "
            "'OHC Attendance Off Roll', or unified 'OHC Attendance', with detectable date columns; "
            "roll parse failed."
        )

    if engine != "flat":
        raise ValueError(
            f"engine={engine!r} is not supported for Summary parsing now. "
            "Use engine='auto' (roll tabs only) or engine='flat'."
        )

    wb = load_workbook(path, data_only=True)
    try:
        ws = _find_summary_sheet(wb)
        header_r = _detect_header_row(ws)
        header_row = tuple(ws.cell(header_r, c).value for c in range(1, min(ws.max_column, 80) + 1))
        cmap = column_map or _parse_column_map_env()
        field_to_col = _map_headers(header_row, cmap)

        c_site = field_to_col.get("client_site")
        if c_site is None:
            raise ValueError(
                "Could not detect client/site column on Summary tab. Set O2C_ATTENDANCE_COLUMN_MAP."
            )

        c_roll = field_to_col.get("roll_type")
        c_emp = field_to_col.get("employee_id")
        c_name = field_to_col.get("employee_name")
        c_role = field_to_col.get("role_code")
        c_pres = field_to_col.get("present_days")
        c_abs = field_to_col.get("absent_days")

        raw_rows: list[tuple[int, dict[str, Any]]] = []
        for r in range(header_r + 1, ws.max_row + 1):
            site_val = _cell_str(ws, r, c_site)
            if not site_val:
                continue
            # Roll = All → include any roll value
            rec: dict[str, Any] = {
                "client_site_key": site_val.strip(),
                "employee_external_id": (_cell_str(ws, r, c_emp) or f"row_{r}"),
                "employee_name": _cell_str(ws, r, c_name),
                "role_code": _cell_str(ws, r, c_role),
                "present_days": _cell_decimal(ws, r, c_pres),
                "absent_days": _cell_decimal(ws, r, c_abs),
                "roll_type": _cell_str(ws, r, c_roll),
                "_sheet_row": r,
            }
            raw_rows.append((r, rec))

        by_site: ClientSiteAttendanceMap = defaultdict(list)
        for _, rec in raw_rows:
            by_site[rec["client_site_key"]].append(rec)

        return dict(by_site)
    finally:
        wb.close()


def _parse_column_map_env() -> dict[str, str] | None:
    raw = (settings.o2c_attendance_column_map_json or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def load_attendance_from_settings() -> ClientSiteAttendanceMap:
    p = (settings.o2c_attendance_xlsx_path or settings.o2c_ohc_attendance_xlsx_path or "").strip()
    if not p:
        raise ValueError("Set O2C_ATTENDANCE_XLSX_PATH to the OHC attendance workbook.")
    return parse_ohc_summary_workbook(Path(p))
