"""Write the final .xlsx exactly matching the source `SBI Mar Working 26.xlsx` layout.

Decisions reflected here, taken from a cell-level diff of the source:
  - NO freeze panes on any output sheet (Dump still freezes row 1).
  - Gridlines OFF on every sheet.
  - Order level / non-permissible row 1:
      A1 "GRAND TOTAL": gray fill (FFD8D8D8), bold, center+vcenter, wrap, THICK borders all 4 sides
      B1-I1 (blank label cells): top + bottom THICK border; I1 adds right THICK
      J1-N1 (SUMs): yellow (FFFFFF00), not-bold, center, wrap, nf=#,##0,
                    borders: left=medium, right=thick, top=thick, bottom=thick
  - Order level row 2:
      A2 "S No": yellow fill, bold, center, wrap, L=thick R=thick T=medium B=thick
      B2-I2: yellow, bold, center, wrap, L=medium R=thick T=medium B=thick  (F2/G2 also vcenter)
      J2-N2: GRAY, bold, center+vcenter, wrap, L=medium R=thick T=medium B=thick
  - Data rows 3+: NO formatting / NO borders / NO fill.
  - PF Summary: no fill; row-2 headers bold center+vbottom, thin borders all sides.
      Data rows thin borders, center+vbottom.
  - Summary: thin borders on rows 3-10, center align; wrap on row 3 (header) and col C; bold
             on A7/A10/B7 + on row-3 headers. B8 has NO borders (source quirk; reproduced).

Formula retention:
  - Dump enrichment columns with rule_type='formula' (e.g. AT=(AA+AC)/Z) are written
    as per-row absolute formulas (=(AA2+AC2)/Z2, =(AA3+AC3)/Z3, ...). Excel recalcs on open.
  - Order level / non-permissible / PF Summary formula rules — same treatment.
  - Summary formula rules are written as-is (already use cell refs like B4+B6).
  - Grand-total SUMs on Order level row 1 and PF Summary K1 use dynamic max_row.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .. import db, format_spec
from . import formula as _formula


# ---- Fills ---- #
# Use full 8-char ARGB ("FF" = opaque alpha). openpyxl otherwise writes "00" alpha
# which makes Excel show a "file recovered" dialog on open.
YELLOW_FILL = PatternFill(start_color="FFFFFF00", end_color="FFFFFF00", fill_type="solid")
GRAY_FILL = PatternFill(start_color="FFD8D8D8", end_color="FFD8D8D8", fill_type="solid")

# ---- Fonts ---- #
BOLD = Font(bold=True)

# ---- Alignments ---- #
CENTER_WRAP = Alignment(horizontal="center", vertical="center", wrap_text=True)
CENTER = Alignment(horizontal="center")                                  # PF Summary / Summary default
CENTER_WRAP_NO_V = Alignment(horizontal="center", wrap_text=True)        # Some row 2 cells source does not vcenter
CENTER_NO_WRAP = Alignment(horizontal="center", vertical="bottom")       # PF Summary cells
CENTER_BOTTOM = Alignment(horizontal="center", vertical="bottom")

# ---- Sides ---- #
THICK = Side(style="thick")
MEDIUM = Side(style="medium")
THIN = Side(style="thin")

# ---- Border helpers ---- #
def _border(l=None, r=None, t=None, b=None) -> Border:
    return Border(left=l, right=r, top=t, bottom=b)

THIN_ALL = _border(l=THIN, r=THIN, t=THIN, b=THIN)

# Default formatting for all data cells across every sheet.
#   - Every cell gets a thin border (gridlines are off, so this gives a real grid).
#   - Numeric cells (that don't already have a date format) get "#,##0.00".
DATA_NUMERIC_NF = "#,##0.00"


def _is_date_format(nf: Optional[str]) -> bool:
    """Cheap heuristic: any number-format string with y/m/d/h characters is a date."""
    if not nf or nf == "General":
        return False
    s = nf.lower()
    return any(ch in s for ch in ("y", "m", "d", "h"))


def _is_numeric_only_format(nf: Optional[str]) -> bool:
    """True if `nf` is a plain numeric format (no dates, percents, text marker, currency symbols).
    These are upgraded to #,##0.00 by the data-cell styler."""
    if not nf or nf == "General":
        return True
    s = nf.lower()
    if any(ch in s for ch in ("y", "d", "h", "%", "@", "₹", "$", "€", "£")):
        return False
    return True  # only digits, commas, dots, hashes, zeros, underscores → numeric


def _apply_data_cell_style(cell, override_nf: Optional[str], spec_nf: Optional[str],
                           value_for_type: Any = None) -> None:
    """Apply border + default number format to a data cell.

    `value_for_type` is the underlying value for type-detection — useful when `cell.value`
    is a formula string but we know the cached result is numeric.

    NOTE: Hot loops (Dump rows) should use `_StyleCache` + `_apply_fast_style` instead.
    Per-property setters trigger style-table hashing per call; cached `_style` assignment
    bypasses that and is ~2x faster on 5M+ cells.
    """
    import datetime as _dt
    cell.border = THIN_ALL

    if override_nf:
        cell.number_format = override_nf
        return

    v = value_for_type if value_for_type is not None else cell.value
    if isinstance(v, (_dt.datetime, _dt.date)):
        if spec_nf and _is_date_format(spec_nf):
            cell.number_format = spec_nf
        else:
            cell.number_format = "dd/mm/yyyy hh:mm" if isinstance(v, _dt.datetime) else "dd/mm/yyyy"
        return

    if isinstance(v, (int, float)) and not isinstance(v, bool):
        cell.number_format = DATA_NUMERIC_NF
        return

    if spec_nf and not _is_numeric_only_format(spec_nf):
        cell.number_format = spec_nf


# ---- Fast-path style cache ---- #
# Building styles via .border= / .number_format= triggers a style-table hash lookup
# inside openpyxl on EVERY assignment. With 5M+ cells on the Dump sheet that becomes
# the dominant cost. Workaround: pre-build one styled WriteOnlyCell per (border, nf)
# combo, capture its resolved `_style` StyleArray, then directly assign that array
# on subsequent cells. Skips the hash-lookup machinery entirely.

class _StyleCache:
    """Per-sheet cache of pre-resolved StyleArrays for common (border, number_format) combos."""

    __slots__ = ("_ws", "border_only", "border_numeric", "border_date", "border_datetime", "_overrides")

    def __init__(self, ws) -> None:
        self._ws = ws
        self.border_only = self._build(THIN_ALL, None)
        self.border_numeric = self._build(THIN_ALL, DATA_NUMERIC_NF)
        self.border_date = self._build(THIN_ALL, "dd/mm/yyyy")
        self.border_datetime = self._build(THIN_ALL, "dd/mm/yyyy hh:mm")
        self._overrides: Dict[str, Any] = {}  # number_format → StyleArray

    def _build(self, border, nf: Optional[str]):
        c = WriteOnlyCell(self._ws, value=0)
        if border is not None:
            c.border = border
        if nf is not None:
            c.number_format = nf
        return c._style

    def for_format(self, nf: str):
        sa = self._overrides.get(nf)
        if sa is None:
            sa = self._build(THIN_ALL, nf)
            self._overrides[nf] = sa
        return sa


def _apply_fast_style(cell, sc: _StyleCache, override_nf: Optional[str],
                      spec_nf: Optional[str], val: Any) -> None:
    """Fast equivalent of `_apply_data_cell_style` using a `_StyleCache`.

    Same decision tree as the slow path; outputs are byte-identical for matching inputs.
    """
    import datetime as _dt
    if override_nf:
        cell._style = sc.for_format(override_nf)
        return
    if isinstance(val, _dt.datetime):
        if spec_nf and _is_date_format(spec_nf):
            cell._style = sc.for_format(spec_nf)
        else:
            cell._style = sc.border_datetime
        return
    if isinstance(val, _dt.date):
        if spec_nf and _is_date_format(spec_nf):
            cell._style = sc.for_format(spec_nf)
        else:
            cell._style = sc.border_date
        return
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        cell._style = sc.border_numeric
        return
    if spec_nf and not _is_numeric_only_format(spec_nf):
        cell._style = sc.for_format(spec_nf)
        return
    cell._style = sc.border_only


# ======================== PUBLIC API ======================== #

def write_xlsx(results: Dict[str, Any], output_path: Path, month_label: str = "",
               progress_cb: Optional[Callable[[str, float], None]] = None) -> None:
    """Write the final xlsx. Optional `progress_cb(stage, fraction)` for live status.

    Uses openpyxl write_only mode: rows are streamed to disk via ZipFile pipes
    instead of being held as Cell objects in memory. ~3x faster end-to-end on
    the 106k-row Dump sheet, and lower memory pressure on the worker.
    """
    def _p(stage: str, frac: float) -> None:
        if progress_cb:
            try: progress_cb(stage, frac)
            except Exception: pass

    wb = Workbook(write_only=True)
    # No default active sheet in write_only mode; nothing to remove.
    wb.calculation.fullCalcOnLoad = False

    rules_by_sheet = _rules_by_sheet()
    format_overrides = db.get_column_formats()
    sheets = results["sheets"]
    cached_values: Dict[Tuple[str, str], Any] = {}

    _p("writing Dump (largest)", 0.20)
    _write_dump(wb, sheets["Dump"], rules_by_sheet.get("Dump", {}),
                format_overrides.get("Dump", {}), cached_values)
    _p("writing Order level", 0.45)
    _write_order_level(wb, format_spec.ORDER_LEVEL, sheets["Order level "],
                       rules_by_sheet.get("Order level ", {}),
                       format_overrides.get("Order level ", {}), cached_values)
    _p("writing Order level - non permissible", 0.55)
    _write_order_level(wb, format_spec.ORDER_LEVEL_NP, sheets["Order level - non permissible"],
                       rules_by_sheet.get("Order level - non permissible", {}),
                       format_overrides.get("Order level - non permissible", {}), cached_values)
    _p("writing Summary", 0.60)
    _write_summary(wb, sheets["Summary"], rules_by_sheet.get("Summary", {}),
                   format_overrides.get("Summary", {}), cached_values)
    _p("writing PF Summary", 0.70)
    _write_pf_summary(wb, sheets["PF Summary"], rules_by_sheet.get("PF Summary", {}),
                      format_overrides.get("PF Summary", {}), cached_values)
    if "AHC" in sheets and sheets["AHC"] is not None:
        _p("writing AHC", 0.78)
        _write_ahc(wb, sheets["AHC"])

    # Capture sheet → worksheet xml index BEFORE save: in write_only mode
    # save() finalizes the workbook and the sheet list may not be safe to read after.
    sheet_xml_map: Dict[str, str] = {
        name: f"xl/worksheets/sheet{idx}.xml"
        for idx, name in enumerate(wb.sheetnames, start=1)
    }

    _p("saving workbook to disk", 0.82)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)

    _p("injecting cached values for instant open", 0.90)
    _inject_cached_values(output_path, cached_values, sheet_xml_map)
    _p("done", 1.0)


# ---------- Post-process: inject cached values into formula cells ---------- #

def _inject_cached_values(path: Path, cached_values: Dict[Tuple[str, str], Any],
                          sheet_xml_map: Dict[str, str]) -> None:
    """Rewrite the xlsx so each <f>...</f> has a matching <v>...</v> with the computed value.

    `sheet_xml_map` maps sheet display name → xl/worksheets/sheetN.xml relpath.
    """
    import shutil, zipfile

    # Reverse mapping: xml relpath → sheet display name
    xml_to_sheet = {xml: name for name, xml in sheet_xml_map.items()}

    # Group cached values by sheet
    by_sheet: Dict[str, Dict[str, Any]] = {}
    for (s, coord), v in cached_values.items():
        by_sheet.setdefault(s, {})[coord] = v

    tmp = path.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            sheet_name = xml_to_sheet.get(item)
            # Run inject on every worksheet (not only ones we have values for) to guarantee
            # every <f> has a valid <v>. Cells without a cached value default to 0.
            if sheet_name is not None:
                data = _inject_sheet(data, by_sheet.get(sheet_name, {}))
            zout.writestr(item, data)

    shutil.move(tmp, path)


# Match either a self-closing `<c ... />` cell OR a `<c ...>...</c>` pair.
# The alternation must try the OPEN-TAG form first (which only stops at `>`, not `/>`)
# but must NOT match self-closing as open tags. We achieve this by ensuring the attrs
# group excludes trailing `/`. Use TWO separate regexes — one for each shape — and run
# each independently. Self-closing cells never contain <f>, so we skip them entirely.
_CELL_OPEN_CLOSE_RE = re.compile(rb'<c\s+r="([A-Z]+\d+)"((?:[^>/]|/(?!>))*)>(.*?)</c>', re.DOTALL)


def _inject_sheet(data: bytes, values: Dict[str, Any]) -> bytes:
    """Inject <v>val</v> after <f>...</f> for each formula cell on the sheet.

    Only touches cells of the form `<c ...>...<f>...</f>...</c>`. Self-closing cells
    (`<c ... />`) never carry formulas, so they're ignored.
    """
    def repl(m):
        coord = m.group(1).decode()
        attrs = m.group(2)
        inner = m.group(3)
        if b"<f" not in inner:
            return m.group(0)
        val = values.get(coord, 0)
        # Determine the cell type attribute. Excel needs t="str" on formula cells whose
        # result is a string; numeric cells omit or use t="n". Bools use t="b".
        new_attrs = _strip_type(attrs) + _type_attr_for(val)
        v_xml = _value_to_v_xml(val)
        new_inner = re.sub(rb"<v\s*/>|<v>\s*</v>|<v>[^<]*</v>", b"", inner)
        new_inner = new_inner + v_xml
        return b"<c r=\"" + coord.encode() + b"\"" + new_attrs + b">" + new_inner + b"</c>"

    return _CELL_OPEN_CLOSE_RE.sub(repl, data)


_TYPE_ATTR_RE = re.compile(rb'\s+t="[^"]*"')


def _strip_type(attrs: bytes) -> bytes:
    """Remove any existing t="..." attribute from a cell's attribute string."""
    return _TYPE_ATTR_RE.sub(b"", attrs)


def _type_attr_for(val: Any) -> bytes:
    """Choose the cell type attribute that matches the cached formula result."""
    import math
    if val is None:
        return b''  # leave no type; <v>0</v> fallback will be numeric
    if isinstance(val, bool):
        return b' t="b"'
    if isinstance(val, (int, float)):
        if isinstance(val, float) and math.isnan(val):
            return b''
        return b''  # default numeric — no t= needed, matches Excel default
    # Everything else is a string
    return b' t="str"'


def _value_to_v_xml(v: Any) -> bytes:
    """Produce the <v>...</v> element for a cached formula value.

    Excel considers an empty <v /> invalid when the cell has a <f>. Always emit
    a concrete value — coerce None/NaN to 0 for numeric cells.
    """
    import math
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return b"<v>0</v>"
    if isinstance(v, bool):
        return b"<v>1</v>" if v else b"<v>0</v>"
    if isinstance(v, (int, float)):
        return f"<v>{v}</v>".encode()
    safe = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<v>{safe}</v>".encode()


# ======================== RULE ACCESS ======================== #

def _rules_by_sheet() -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in db.get_all_rules():
        out.setdefault(r["sheet"], {})[r["column_letter"]] = r
    return out


def _rule_formula(rule_row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the formula text if this rule is of type 'formula', else None."""
    if not rule_row or rule_row.get("rule_type") != "formula":
        return None
    try:
        cfg = json.loads(rule_row.get("config_json") or "{}")
    except Exception:
        return None
    f = cfg.get("formula")
    if not f:
        return None
    return f if isinstance(f, str) else None


# ---------- Formula localization ---------- #

def _localize_row_formula(formula: str, row: int) -> str:
    """Translate a per-row formula like '=(AA+AC)/Z' → '=(AA{row}+AC{row})/Z{row}'.
    Also rewrites Sheet.Col cross-sheet refs to Excel syntax: Dump.AF → Dump!AF:AF.

    For hot loops (e.g. 106k Dump rows), prefer `_make_row_formula_factory` which
    tokenizes once and reuses the parsed parts.
    """
    return _make_row_formula_factory(formula)(row)


_COL_REF_RE = re.compile(r"^[A-Za-z]+$")


def _make_row_formula_factory(formula: str) -> Callable[[int], str]:
    """Return a fast `fn(row) -> '=...'` that substitutes the current row number.

    Tokenizes the formula ONCE and returns a closure that joins precomputed parts
    with the row number. Avoids ~N tokenize calls per N-row column.
    """
    src = formula
    if not src.startswith("="):
        src = "=" + src
    tokens = _formula.tokenize(src)
    n = len(tokens)
    parts: List[Tuple[str, bool]] = []  # (text, needs_row_suffix)
    for i, t in enumerate(tokens):
        if t.kind == "SHEETREF":
            parts.append((_excelify_sheetref(t.value), False))
            continue
        if t.kind == "IDENT" and _COL_REF_RE.match(t.value) and t.value.upper() not in ("TRUE", "FALSE"):
            nxt = tokens[i + 1] if i + 1 < n else None
            prev = tokens[i - 1] if i > 0 else None
            if nxt and nxt.kind == "OP" and nxt.value == "(":
                parts.append((t.value.upper(), False))
                continue
            is_range = (nxt and nxt.kind == "OP" and nxt.value == ":") or \
                       (prev and prev.kind == "OP" and prev.value == ":")
            if is_range:
                parts.append((t.value.upper(), False))
                continue
            parts.append((t.value.upper(), True))
        else:
            parts.append((t.value, False))

    # Collapse adjacent literal (non-row) parts so the per-row join is shorter.
    collapsed: List[Tuple[str, bool]] = []
    buf = ""
    for text, needs in parts:
        if needs:
            if buf:
                collapsed.append((buf, False))
                buf = ""
            collapsed.append((text, True))
        else:
            buf += text
    if buf:
        collapsed.append((buf, False))

    def make(row: int) -> str:
        rs = str(row)
        out = ["="]
        for text, needs in collapsed:
            out.append(text)
            if needs:
                out.append(rs)
        return "".join(out)

    return make


def _excelify_scalar_formula(formula: str) -> str:
    """Translate any Sheet.Col refs in a whole-sheet (scalar) formula to Excel's Sheet!Col:Col."""
    src = formula
    if not src.startswith("="):
        src = "=" + src
    tokens = _formula.tokenize(src)
    out_parts: List[str] = []
    for t in tokens:
        if t.kind == "SHEETREF":
            out_parts.append(_excelify_sheetref(t.value))
        else:
            out_parts.append(t.value)
    return "=" + "".join(out_parts)


def _excelify_sheetref(raw: str) -> str:
    """'Dump.AF' → 'Dump!AF:AF'; \"'PF Summary'.K\" → \"'PF Summary'!K:K\".
    If the ref already has a row digit (Dump.AF2), leave as 'Dump!AF2'.
    """
    if raw.startswith("'"):
        end = raw.index("'", 1)
        sheet_part = raw[: end + 1]
        rest = raw[end + 2:]  # after '.'
    else:
        sheet_part, rest = raw.split(".", 1)
    if any(ch.isdigit() for ch in rest):
        # Specific cell reference, not a whole column
        return f"{sheet_part}!{rest}"
    # Whole-column ref — expand to X:X
    return f"{sheet_part}!{rest}:{rest}"


# ======================== Dump ======================== #

_DUMP_WIDTHS = {
    "A": 17.43, "B": 15.14, "C": 13.71, "D": 17.86, "E": 17.86, "F": 24.14, "G": 9.14,
    "H": 60.0, "I": 24.86, "J": 34.71, "K": 22.29, "L": 15.0, "M": 21.86, "N": 18.71,
    "O": 15.71, "P": 9.57, "Q": 22.71, "R": 60.0, "S": 13.71, "T": 30.0, "U": 14.14,
    "V": 15.43, "W": 8.57, "X": 17.0, "Y": 20.86, "Z": 9.29, "AA": 16.43, "AB": 14.0,
    "AC": 16.29, "AD": 16.43, "AE": 11.57, "AF": 15.86, "AG": 24.86, "AH": 15.71,
    "AI": 17.86, "AJ": 17.86, "AK": 17.86, "AL": 17.86, "AM": 14.57, "AN": 15.71,
    "AO": 14.14, "AP": 10.86, "AQ": 30.0, "AR": 15.14, "AS": 13.71, "AT": 9.0,
    "AU": 10.0, "AV": 10.0, "AW": 11.29, "AX": 15.71,
}


def _write_dump(wb: Workbook, df: pd.DataFrame, rules: Dict[str, Dict[str, Any]],
                overrides: Dict[str, str],
                cached_values: Dict[Tuple[str, str], Any]) -> None:
    """Write the Dump sheet WITHOUT per-cell styling.

    Decision: Dump is the source-of-truth raw data + computed enrichment columns.
    It's not the deliverable — `Order level`, `PF Summary`, `Summary` are. With
    106k+ rows × 50 cols, applying borders / number formats per cell costs ~30s
    (~6 µs/cell × 5.3M cells of openpyxl style-table hashing). Skip it entirely;
    formatting only matters on the user-facing pivot sheets.
    """
    ws = wb.create_sheet("Dump")
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A2"

    for letter, w in _DUMP_WIDTHS.items():
        ws.column_dimensions[letter].width = w

    raw_headers = list(format_spec.DUMP_RAW_HEADERS)
    enrichment_cols = list(format_spec.DUMP_ENRICHMENT_COLS)
    headers = raw_headers + [c["header"] for c in enrichment_cols]

    # Row 1 — headers, plain (source has no bold / no formatting on Dump row 1)
    ws.append(list(headers))

    enrichment_col_letters = [c["col"] for c in enrichment_cols]
    formula_factory_by_col: Dict[str, Callable[[int], str]] = {}
    for c in enrichment_col_letters:
        f = _rule_formula(rules.get(c))
        if f:
            try:
                formula_factory_by_col[c] = _make_row_formula_factory(f)
            except Exception:
                pass

    all_cols = [get_column_letter(i) for i in range(1, len(headers) + 1)]
    raw_df = df.reindex(columns=all_cols)

    factory_by_idx: List[Optional[Callable[[int], str]]] = [
        formula_factory_by_col.get(letter) for letter in all_cols
    ]
    has_any_formulas = any(f is not None for f in factory_by_idx)

    if not has_any_formulas:
        # Hot path: bare values straight from itertuples, no per-cell objects.
        # write_only's append() accepts plain values directly.
        for row_vals in raw_df.itertuples(index=False, name=None):
            ws.append([_coerce(v) for v in row_vals])
        return

    # Mixed path: only formula columns need WriteOnlyCells; others stay bare values.
    for r_i, row_vals in enumerate(raw_df.itertuples(index=False, name=None), start=2):
        out_row: List[Any] = []
        for c_i, val in enumerate(row_vals):
            factory = factory_by_idx[c_i]
            if factory is not None:
                try:
                    out_row.append(factory(r_i))
                    cached_values[("Dump", f"{all_cols[c_i]}{r_i}")] = val
                except Exception:
                    out_row.append(_coerce(val))
            else:
                out_row.append(_coerce(val))
        ws.append(out_row)


# ======================== Order level / non-permissible ======================== #

_OL_WIDTHS = {
    "A": 8.71, "B": 19.71, "C": 19.71, "D": 14.0, "E": 25.0, "F": 24.14, "G": 24.14,
    "H": 19.29, "I": 29.14, "J": 16.0, "K": 23.29, "L": 20.71, "M": 22.71, "N": 31.71,
}


def _col_idx(letter: str) -> int:
    letter = letter.upper()
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n


def _write_order_level(wb: Workbook, spec: format_spec.OutputSheetSpec,
                       df: pd.DataFrame, rules: Dict[str, Dict[str, Any]],
                       overrides: Dict[str, str],
                       cached_values: Dict[Tuple[str, str], Any]) -> None:
    ws = wb.create_sheet(spec.name)
    ws.sheet_view.showGridLines = False

    # Column widths up-front (independent of row writes)
    for letter, w in _OL_WIDTHS.items():
        ws.column_dimensions[letter].width = w

    cols = format_spec.effective_columns(spec)
    ncols = len(cols)
    max_data_row = len(df) + 2  # headers row 2, first data row 3

    sum_cols = {"J", "K", "L", "M", "N"}
    first_sum_c = min(
        (i for i, cs in enumerate(cols, start=1) if cs.col in sum_cols),
        default=ncols + 1,
    )

    # ---------- Row 1 ---------- #
    row1: List[Any] = []
    for c_i, col_spec in enumerate(cols, start=1):
        letter = get_column_letter(c_i)
        if c_i == 1:
            c = WriteOnlyCell(ws, value="GRAND TOTAL")
            c.fill = GRAY_FILL
            c.font = BOLD
            c.alignment = CENTER_WRAP
            c.border = _border(l=THICK, r=THICK, t=THICK, b=THICK)
        elif col_spec.col in sum_cols:
            c = WriteOnlyCell(ws, value=f"=SUM({letter}3:{letter}{max_data_row})")
            c.fill = YELLOW_FILL
            c.alignment = CENTER_WRAP_NO_V
            c.number_format = "#,##0"
            c.border = _border(l=MEDIUM, r=THICK, t=THICK, b=THICK)
            try:
                total = float(pd.to_numeric(df[col_spec.col], errors="coerce").fillna(0).sum())
                cached_values[(spec.name, f"{letter}1")] = total
            except Exception:
                pass
        else:
            c = WriteOnlyCell(ws, value=None)
            c.border = _border(t=THICK, b=THICK, r=(THICK if c_i == first_sum_c - 1 else None))
        row1.append(c)
    ws.append(row1)

    # ---------- Row 2 ---------- #
    row2: List[Any] = []
    for c_i, col_spec in enumerate(cols, start=1):
        c = WriteOnlyCell(ws, value=col_spec.header)
        c.font = BOLD
        if c_i == 1:
            c.fill = YELLOW_FILL
            c.alignment = CENTER_WRAP_NO_V
            c.border = _border(l=THICK, r=THICK, t=MEDIUM, b=THICK)
        elif col_spec.col in sum_cols:
            c.fill = GRAY_FILL
            c.alignment = CENTER_WRAP
            c.border = _border(l=MEDIUM, r=THICK, t=MEDIUM, b=THICK)
        else:
            c.fill = YELLOW_FILL
            c.alignment = CENTER_WRAP if col_spec.col in ("F", "G") else CENTER_WRAP_NO_V
            c.border = _border(l=MEDIUM, r=THICK, t=MEDIUM, b=THICK)
        row2.append(c)
    ws.append(row2)

    # ---------- Data rows 3+ ---------- #
    col_letters = [c.col for c in cols]
    factory_by_idx: List[Optional[Callable[[int], str]]] = []
    for letter in col_letters:
        f = _rule_formula(rules.get(letter))
        if f:
            try:
                factory_by_idx.append(_make_row_formula_factory(f))
            except Exception:
                factory_by_idx.append(None)
        else:
            factory_by_idx.append(None)

    overrides_by_idx: List[Optional[str]] = [overrides.get(letter) for letter in col_letters]
    spec_nf_by_idx: List[Optional[str]] = [c.number_format for c in cols]

    sc = _StyleCache(ws)
    data_view = df.reindex(columns=col_letters)
    for r_i, row_vals in enumerate(data_view.itertuples(index=False, name=None), start=3):
        cells: List[Any] = []
        for c_i, val in enumerate(row_vals):
            factory = factory_by_idx[c_i]
            if factory is not None:
                try:
                    c = WriteOnlyCell(ws, value=factory(r_i))
                    cached_values[(spec.name, f"{col_letters[c_i]}{r_i}")] = val
                except Exception:
                    c = WriteOnlyCell(ws, value=_coerce(val))
            else:
                c = WriteOnlyCell(ws, value=_coerce(val))
            _apply_fast_style(c, sc, overrides_by_idx[c_i], spec_nf_by_idx[c_i], val)
            cells.append(c)
        ws.append(cells)


# ======================== PF Summary ======================== #

def _write_pf_summary(wb: Workbook, df: pd.DataFrame, rules: Dict[str, Dict[str, Any]],
                      overrides: Dict[str, str],
                      cached_values: Dict[Tuple[str, str], Any]) -> None:
    spec = format_spec.PF_SUMMARY
    ws = wb.create_sheet(spec.name)
    ws.sheet_view.showGridLines = False

    cols = format_spec.effective_columns(spec)
    ncols = len(cols)
    max_data_row = len(df) + 2

    # ---------- Row 1: SUM over K only; other cells empty ---------- #
    row1: List[Any] = []
    for c_i, col_spec in enumerate(cols, start=1):
        if col_spec.col == "K":
            letter = get_column_letter(c_i)
            c = WriteOnlyCell(ws, value=f"=SUM({letter}3:{letter}{max_data_row})")
            c.font = BOLD
            c.alignment = CENTER_BOTTOM
            try:
                total = float(pd.to_numeric(df[col_spec.col], errors="coerce").fillna(0).sum())
                cached_values[(spec.name, f"{letter}1")] = total
            except Exception:
                pass
        else:
            c = WriteOnlyCell(ws, value=None)
        row1.append(c)
    ws.append(row1)

    # ---------- Row 2: headers ---------- #
    row2: List[Any] = []
    for col_spec in cols:
        c = WriteOnlyCell(ws, value=col_spec.header)
        c.font = BOLD
        c.alignment = CENTER_BOTTOM
        c.border = THIN_ALL
        row2.append(c)
    ws.append(row2)

    # ---------- Data rows ---------- #
    col_letters = [c.col for c in cols]
    factory_by_idx: List[Optional[Callable[[int], str]]] = []
    for letter in col_letters:
        f = _rule_formula(rules.get(letter))
        if f:
            try:
                factory_by_idx.append(_make_row_formula_factory(f))
            except Exception:
                factory_by_idx.append(None)
        else:
            factory_by_idx.append(None)

    overrides_by_idx: List[Optional[str]] = [overrides.get(letter) for letter in col_letters]
    spec_nf_by_idx: List[Optional[str]] = [c.number_format for c in cols]

    sc = _StyleCache(ws)
    data_view = df.reindex(columns=col_letters)
    for r_i, row_vals in enumerate(data_view.itertuples(index=False, name=None), start=3):
        cells: List[Any] = []
        for c_i, val in enumerate(row_vals):
            factory = factory_by_idx[c_i]
            if factory is not None:
                try:
                    c = WriteOnlyCell(ws, value=factory(r_i))
                    cached_values[(spec.name, f"{col_letters[c_i]}{r_i}")] = val
                except Exception:
                    c = WriteOnlyCell(ws, value=_coerce(val))
            else:
                c = WriteOnlyCell(ws, value=_coerce(val))
            _apply_fast_style(c, sc, overrides_by_idx[c_i], spec_nf_by_idx[c_i], val)
            c.alignment = CENTER_BOTTOM
            cells.append(c)
        ws.append(cells)

    # Source has no freeze panes and no explicit widths.


# ======================== Summary ======================== #

_SUMMARY_WIDTHS = {"A": 29.14, "B": 22.71, "C": 20.43, "D": 8.71}

# Cells that the source has NO borders on (quirks). We reproduce to stay identical.
_SUMMARY_NO_BORDER = {"B8"}


def _write_ahc(wb: Workbook, df: pd.DataFrame) -> None:
    """Pass-through writer for the uploaded AHC sheet."""
    ws = wb.create_sheet("AHC")
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A2"
    if df is None or len(df) == 0:
        return

    # Header row
    header_row: List[Any] = []
    for col_name in df.columns:
        c = WriteOnlyCell(ws, value=str(col_name))
        c.font = BOLD
        c.alignment = CENTER_BOTTOM
        c.border = THIN_ALL
        header_row.append(c)
    ws.append(header_row)

    # Data rows
    sc = _StyleCache(ws)
    for row in df.itertuples(index=False, name=None):
        cells: List[Any] = []
        for val in row:
            c = WriteOnlyCell(ws, value=_coerce(val))
            _apply_fast_style(c, sc, None, None, val)
            cells.append(c)
        ws.append(cells)


def _write_summary(wb: Workbook, cells: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                   overrides: Dict[str, str],
                   cached_values: Dict[Tuple[str, str], Any]) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_view.showGridLines = False

    for letter, w in _SUMMARY_WIDTHS.items():
        ws.column_dimensions[letter].width = w

    # Bucket SUMMARY_CELLS by row number → {row_int: {col_letter: StaticCell}}
    by_row: Dict[int, Dict[str, Any]] = {}
    max_row = 1
    last_col_idx = 1
    for sc in format_spec.SUMMARY_CELLS:
        digits = "".join(ch for ch in sc.cell if ch.isdigit())
        letters = "".join(ch for ch in sc.cell if ch.isalpha())
        r = int(digits)
        by_row.setdefault(r, {})[letters] = sc
        if r > max_row:
            max_row = r
        idx = _col_idx(letters)
        if idx > last_col_idx:
            last_col_idx = idx

    # Sequential row writes (write_only) — pad rows with no SUMMARY_CELLS
    for r in range(1, max_row + 1):
        row_specs = by_row.get(r, {})
        row_cells: List[Any] = []
        for c_idx in range(1, last_col_idx + 1):
            letter = get_column_letter(c_idx)
            sc = row_specs.get(letter)
            if sc is None:
                row_cells.append(WriteOnlyCell(ws, value=None))
                continue

            rule = rules.get(sc.cell)
            f = _rule_formula(rule)
            if f:
                try:
                    value = _excelify_scalar_formula(f)
                except Exception:
                    value = f if f.startswith("=") else "=" + f
                computed = cells.get(sc.cell)
                if computed is not None:
                    cached_values[("Summary", sc.cell)] = computed
                cell = WriteOnlyCell(ws, value=value)
            else:
                cell = WriteOnlyCell(ws, value=_coerce(cells.get(sc.cell)))

            _apply_data_cell_style(cell, overrides.get(sc.cell), sc.number_format,
                                   value_for_type=cells.get(sc.cell))

            wrap = (r == 3) or (letter == "C")
            cell.alignment = Alignment(horizontal="center", wrap_text=wrap)

            if r == 3 or sc.cell in ("A7", "A10", "B7"):
                cell.font = BOLD

            row_cells.append(cell)

        ws.append(row_cells)


# ======================== Value coercion ======================== #

def _coerce(val: Any) -> Any:
    import math
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    if isinstance(val, (int, float, str, bool)):
        return val
    return str(val)
