"""
Read OHC attendance when the Summary view is driven by PivotTable + Slicers (or similar).

Why this exists
---------------
Excel **slicers** and **pivot refresh** are UI/runtime behaviour. ``openpyxl`` loads cell values as
stored in the file; it does **not** recalculate pivots or apply slicers. However, the workbook
stores:

- **Pivot cache** ``worksheetSource`` → sheet name + range of the raw fact table
- **Slicer caches** (tabular) → which pivot field is filtered + per-item **selected** flags
- **cacheField / sharedItems** → canonical ordered values for each pivot field

We replicate Excel's filter logic in Python:

1. Load the source range as rows of dicts (header row = field names).
2. For slicers **other than** client-site: apply **AND** filters using **only items marked
   selected** in the slicer XML (so if the user left month ≠ «All» or roll ≠ «All», we respect
   that — no assumption that they chose «All»).
3. For the **client-site** field: iterate each distinct site value that appears in the **source
   data** (equivalent to choosing the second dropdown one-by-one while other slicers stay as
   saved in the file), and build ``client_site_key -> [records]``.

If the pivot uses an **external** cache or the source range is missing, we return ``None`` so
callers can fall back to flat Summary parsing. For files with **no** embedded source table, the
only faithful option is to automate Excel (e.g. **xlwings** + desktop Excel) to set slicers and
read calculated cells — not included here.

References: [MS-XLSX] Slicer Cache, Pivot Cache Definition (Office Open XML).
"""

from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from app.agents.o2c_ohc.attendance_summary import (
    AttendanceRecord,
    ClientSiteAttendanceMap,
    _HEADER_SYNONYMS,
    _map_headers,
    _norm_header,
    _parse_column_map_env,
)
from app.config.settings import settings


def _local(tag: str) -> str:
    if not tag:
        return ""
    return tag.split("}", 1)[-1] if tag.startswith("{") else tag


def _cell_matches_allowed(sv: str, allowed: set[str]) -> bool:
    if sv in allowed:
        return True
    low = sv.lower()
    return any(low == a.lower() for a in allowed)


def _parse_worksheet_source(cache_xml: bytes) -> tuple[str | None, str | None]:
    root = ET.fromstring(cache_xml)
    for el in root.iter():
        if _local(el.tag) != "cacheSource":
            continue
        if el.get("type") != "worksheet":
            continue
        for ch in el:
            if _local(ch.tag) != "worksheetSource":
                continue
            ref = ch.get("ref")
            sheet = ch.get("sheet") or ch.get("name")
            return sheet, ref
    return None, None


def _parse_cache_fields(cache_xml: bytes) -> list[tuple[str, list[str]]]:
    """Ordered cacheField names and shared string values (best-effort)."""
    root = ET.fromstring(cache_xml)
    out: list[tuple[str, list[str]]] = []
    for cf in root.iter():
        if _local(cf.tag) != "cacheField":
            continue
        name = cf.get("name") or ""
        values: list[str] = []
        for child in cf:
            if _local(child.tag) != "sharedItems":
                continue
            for si in child:
                tag = _local(si.tag)
                v = si.get("v")
                if v is not None:
                    values.append(str(v))
                elif tag == "s":
                    # string item without v sometimes uses child text
                    if si.text:
                        values.append(si.text.strip())
        out.append((name, values))
    return out


def _parse_slicer_cache_tabular(slicer_cache_xml: bytes) -> dict[str, Any] | None:
    root = ET.fromstring(slicer_cache_xml)
    source_name = root.get("sourceName") or root.get("name") or ""
    pivot_field_idx: int | None = None
    try:
        pf_root = root.get("pivotField")
        if pf_root is not None:
            pivot_field_idx = int(pf_root)
    except ValueError:
        pivot_field_idx = None

    items: list[tuple[int | None, bool]] = []
    for tab in root.iter():
        if _local(tab.tag) != "tabular":
            continue
        pf_tab = tab.get("pivotField")
        if pf_tab is not None and pivot_field_idx is None:
            try:
                pivot_field_idx = int(pf_tab)
            except ValueError:
                pass
        for items_el in tab:
            if _local(items_el.tag) != "items":
                continue
            for iel in items_el:
                if _local(iel.tag) != "i":
                    continue
                x_raw = iel.get("x")
                xi: int | None
                try:
                    xi = int(x_raw) if x_raw is not None else None
                except ValueError:
                    xi = None
                s_raw = iel.get("s")
                # Missing s → treat as selected; s="0" → not selected (tabular slicer OOXML).
                selected = s_raw is None or s_raw in ("1", "true", "True")
                items.append((xi, selected))
        break

    if not items and not source_name:
        return None
    return {
        "sourceName": source_name,
        "pivotFieldIndex": pivot_field_idx,
        "items": items,
    }


def _read_range_as_dicts(ws: Worksheet, ref: str) -> tuple[list[str], list[list[Any]]]:
    """ref like Sheet1!$A$1:$Z$999 or A1:Z999."""
    ref_clean = ref.split("!")[-1].replace("$", "")
    c1, r1, c2, r2 = range_boundaries(ref_clean)
    headers: list[str] = []
    rows: list[list[Any]] = []
    for r in range(r1, r2 + 1):
        row_vals = [ws.cell(r, c).value for c in range(c1, c2 + 1)]
        if r == r1:
            headers = [str(x).strip() if x is not None else "" for x in row_vals]
            continue
        if not any(x is not None and str(x).strip() for x in row_vals):
            continue
        rows.append(row_vals)
    return headers, rows


def _header_to_field_col(headers: list[str], field_name: str) -> int | None:
    """Match pivot cacheField name to column index (0-based)."""
    fn = field_name.strip().lower()
    for i, h in enumerate(headers):
        if _norm_header(h) == fn:
            return i
        if fn and fn in _norm_header(h):
            return i
    return None


def _selected_values_for_slicer(
    cache_fields: list[tuple[str, list[str]]],
    slicer: dict[str, Any],
) -> tuple[str | None, set[str] | None, bool]:
    """
    Returns (matched_cache_field_name, selected_value_set_or_None, all_selected_flag).

    If all_selected_flag is True, do not filter on this field.
    """
    src = (slicer.get("sourceName") or "").strip()
    items: list[tuple[int | None, bool]] = slicer.get("items") or []
    if not items:
        return src, None, True

    field_idx: int | None = slicer.get("pivotFieldIndex")
    matched_name: str | None = None
    field_values: list[str] = []

    if field_idx is not None and 0 <= field_idx < len(cache_fields):
        matched_name, field_values = cache_fields[field_idx]
    else:
        for name, vals in cache_fields:
            if name.strip().lower() == src.lower():
                matched_name, field_values = name, vals
                break
            if src and src.lower() in name.lower():
                matched_name, field_values = name, vals
                break

    if matched_name is None:
        return src, None, True

    by_x: dict[int, bool] = {}
    positional_sel: list[bool] = []
    for x, sel in items:
        if x is not None:
            by_x[int(x)] = sel
        else:
            positional_sel.append(sel)

    values_pick: set[str] = set()
    if by_x:
        for idx, sel in by_x.items():
            if sel and 0 <= idx < len(field_values):
                values_pick.add(field_values[idx])
    elif positional_sel and field_values:
        for idx, sel in enumerate(positional_sel):
            if sel and idx < len(field_values):
                values_pick.add(field_values[idx])

    if not values_pick:
        return matched_name, None, True

    if field_values and len(values_pick) >= len(field_values):
        return matched_name, None, True
    return matched_name, values_pick, False


def _row_to_record(
    headers: list[str],
    row: list[Any],
    field_to_logical: dict[str, int],
    client_site_col: int,
    site_value: str,
    row_index: int,
) -> AttendanceRecord:
    def col(logical: str) -> Any:
        idx = field_to_logical.get(logical)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    emp = col("employee_id")
    name = col("employee_name")
    role = col("role_code")
    pres = col("present_days")
    ab = col("absent_days")
    roll = col("roll_type")

    def dec(x: Any) -> Decimal | None:
        if x is None or x == "":
            return None
        if isinstance(x, (int, float)):
            return Decimal(str(x))
        try:
            return Decimal(str(x).strip().replace(",", ""))
        except (InvalidOperation, ValueError):
            return None

    return {
        "client_site_key": site_value.strip(),
        "employee_external_id": (str(emp).strip() if emp is not None else f"row_{row_index}"),
        "employee_name": str(name).strip() if name is not None else None,
        "role_code": str(role).strip() if role is not None else None,
        "present_days": dec(pres),
        "absent_days": dec(ab),
        "roll_type": str(roll).strip() if roll is not None else None,
        "_sheet_row": row_index,
    }


def try_parse_attendance_via_pivot_slicers(
    xlsx_path: Path,
    *,
    client_site_slicer_hint: str | None = None,
) -> ClientSiteAttendanceMap | None:
    """
    Attempt pivot+slicer aware parse. Returns None if workbook has no worksheet pivot source
    we can read.
    """
    path = xlsx_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        cache_files = sorted(n for n in names if n.startswith("xl/pivotCache/pivotCacheDefinition") and n.endswith(".xml"))
        if not cache_files:
            return None

        slicer_files = sorted(n for n in names if n.startswith("xl/slicerCaches/") and n.endswith(".xml"))
        if not slicer_files:
            return None

        # Use first worksheet-sourced pivot cache (typical OHC)
        sheet_name: str | None = None
        ref: str | None = None
        cache_fields: list[tuple[str, list[str]]] = []
        for cf_path in cache_files:
            xml = zf.read(cf_path)
            sh, rf = _parse_worksheet_source(xml)
            if sh and rf:
                sheet_name, ref = sh, rf
                cache_fields = _parse_cache_fields(xml)
                break

        if not sheet_name or not ref:
            return None

        slicers: list[dict[str, Any]] = []
        for sf in slicer_files:
            data = _parse_slicer_cache_tabular(zf.read(sf))
            if data:
                slicers.append(data)

        if not slicers:
            return None

    wb = load_workbook(path, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            # sheet might be quoted differently
            alt = [s for s in wb.sheetnames if s.strip().lower() == sheet_name.strip().lower()]
            if alt:
                sheet_name = alt[0]
            else:
                return None

        ws = wb[sheet_name]
        headers, data_rows = _read_range_as_dicts(ws, ref)
        if not headers:
            return None

        # Map logical columns (employee, role, …) using header synonyms on **source** headers
        header_tuple = tuple(headers)
        cmap = _parse_column_map_env()
        logical_to_idx: dict[str, int] = {}
        mapped = _map_headers(header_tuple, cmap)
        for logical, col_1based in mapped.items():
            logical_to_idx[logical] = col_1based - 1

        # Identify client-site column: env hint > synonym match on header names > slicer sourceName
        hint = (client_site_slicer_hint or settings.o2c_attendance_client_site_field or "").strip()
        client_site_header_candidates: list[str] = []
        if hint:
            client_site_header_candidates.append(hint)
        for syn in _HEADER_SYNONYMS["client_site"]:
            client_site_header_candidates.append(syn)

        client_site_col: int | None = None
        for cand in client_site_header_candidates:
            idx = _header_to_field_col(headers, cand)
            if idx is not None:
                client_site_col = idx
                break

        # Match slicer whose sourceName looks like client/site if column not found
        if client_site_col is None:
            for sl in slicers:
                sn = (sl.get("sourceName") or "").lower()
                if any(x in sn for x in ("site", "client", "location", "plant")):
                    idx = _header_to_field_col(headers, sl.get("sourceName") or "")
                    if idx is not None:
                        client_site_col = idx
                        break

        if client_site_col is None:
            return None

        site_slicer_index: int | None = settings.o2c_attendance_client_site_slicer_index
        if site_slicer_index is None:
            for si, sl in enumerate(slicers):
                sn = (sl.get("sourceName") or "").strip()
                idx = _header_to_field_col(headers, sn)
                if idx == client_site_col:
                    site_slicer_index = si
                    break
        if site_slicer_index is None:
            for si, sl in enumerate(slicers):
                sn = (sl.get("sourceName") or "").lower()
                if any(k in sn for k in ("site", "client", "location", "plant")):
                    site_slicer_index = si
                    break

        # Build filters for other slicers (respect non-«All» UI state saved in file)
        static_filters: list[tuple[int, set[str]]] = []
        for si, sl in enumerate(slicers):
            if site_slicer_index is not None and si == site_slicer_index:
                continue
            fname, vals, all_sel = _selected_values_for_slicer(cache_fields, sl)
            if fname is None or all_sel or not vals:
                continue
            col_idx = _header_to_field_col(headers, fname)
            if col_idx is None:
                sn = (sl.get("sourceName") or "").strip()
                col_idx = _header_to_field_col(headers, sn)
            if col_idx is None:
                continue
            static_filters.append((col_idx, vals))

        # Distinct client sites after static filters
        site_values: set[str] = set()
        for ridx, row in enumerate(data_rows, start=2):
            ok = True
            for col_i, allowed in static_filters:
                if col_i >= len(row):
                    ok = False
                    break
                cell = row[col_i]
                sv = str(cell).strip() if cell is not None else ""
                if not _cell_matches_allowed(sv, allowed):
                    ok = False
                    break
            if not ok:
                continue
            if client_site_col < len(row):
                v = row[client_site_col]
                if v is not None and str(v).strip():
                    site_values.add(str(v).strip())

        by_site: dict[str, list[AttendanceRecord]] = defaultdict(list)
        for site in sorted(site_values):
            for ridx, row in enumerate(data_rows, start=2):
                ok = True
                for col_i, allowed in static_filters:
                    if col_i >= len(row):
                        ok = False
                        break
                    cell = row[col_i]
                    sv = str(cell).strip() if cell is not None else ""
                    if not _cell_matches_allowed(sv, allowed):
                        ok = False
                        break
                if not ok:
                    continue
                if client_site_col >= len(row):
                    cv = row[client_site_col]
                    cvs = str(cv).strip() if cv is not None else ""
                    if cvs.lower() != site.lower():
                        continue
                rec = _row_to_record(
                    headers, row, logical_to_idx, client_site_col, site, ridx
                )
                by_site[site].append(rec)

        return dict(by_site)
    finally:
        wb.close()


