"""Load the uploaded raw Dump .xlsx into a pandas DataFrame.

Only sheet "Dump" is read. Headers are validated against format_spec.DUMP_RAW_HEADERS (cols A..AG).
Cols AH..AX (if present) are discarded — we recompute them from rules.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .. import format_spec


class SchemaError(Exception):
    pass


def col_letters(n: int) -> List[str]:
    return [get_column_letter(i) for i in range(1, n + 1)]


def _find_dump_sheet(wb) -> str:
    """Pick the sheet that looks like the raw dump.

    Strategy:
      1. If a sheet named 'Dump' exists, use it.
      2. Else find the first sheet whose row 1 matches the expected raw headers
         (first N headers match format_spec.DUMP_RAW_HEADERS).
      3. Else fall back to the first sheet and let the header check surface the mismatch.
    """
    if "Dump" in wb.sheetnames:
        return "Dump"
    expected_prefix = format_spec.DUMP_RAW_HEADERS[:8]  # first 8 headers — enough of a signature
    for name in wb.sheetnames:
        ws = wb[name]
        try:
            row1 = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
            actual = [str(v) if v is not None else "" for v in row1][:len(expected_prefix)]
            if actual == expected_prefix:
                return name
        except StopIteration:
            continue
    return wb.sheetnames[0] if wb.sheetnames else "Dump"


_DEDUP_SUFFIX_RE = __import__("re").compile(r"\.\d+$")


def _normalize_header(h: str) -> str:
    """Strip pandas/CSV dedup suffix ('.1', '.2', …) appended to duplicate column names.
    The source schema legitimately has duplicates (order_id, financial_bu, bu), so when
    a CSV is round-tripped through pandas/calamine the dupes pick up suffixes — we
    tolerate that on the validator level."""
    if not h:
        return h
    return _DEDUP_SUFFIX_RE.sub("", h)


def validate_headers(path: Path) -> Tuple[List[str], List[str], str]:
    """Return (actual_headers, expected_headers, source_label) for the raw dump.

    ``source_label`` is ``"csv"`` for CSV/TSV, or the **xlsx sheet name** chosen for
    the dump — pass this to :func:`load_raw` as ``sheet_hint`` to skip a second workbook
    scan on upload.

    Accepts xlsx OR csv. For xlsx, sheet need NOT be named 'Dump' — auto-detected by
    matching the header prefix. For CSV, headers come from row 1.
    Raises SchemaError if no header sequence matches the expected schema.
    """
    actual, source_label = _read_headers_only(path)
    expected = format_spec.DUMP_RAW_HEADERS
    n = len(expected)
    actual_trimmed = [_normalize_header(str(h)) for h in actual[:n]]

    if actual_trimmed != expected:
        diffs = []
        for i, (a, e) in enumerate(zip(actual_trimmed + [None] * max(0, n - len(actual_trimmed)), expected)):
            if a != e:
                diffs.append(f"col {get_column_letter(i + 1)}: expected '{e}' got '{a}'")
        raise SchemaError(
            f"Raw-dump schema mismatch on '{source_label}': " + "; ".join(diffs[:6])
            + (" (and more…)" if len(diffs) > 6 else "")
        )
    return actual, expected, source_label


def _is_csv(path: Path) -> bool:
    return path.suffix.lower() in (".csv", ".tsv", ".txt")


def _read_csv_fast(path: Path) -> pd.DataFrame:
    """Read CSV/TSV trying common encodings.

    TSV is unambiguous (always tab + C engine).

    For ``.csv`` / ``.txt``, we peek at the first line's delimiter counts so tab- or
    semicolon-separated files are not mis-parsed as a single column (same outcomes as
    the historical ``sep=None``, ``engine="python"`` path). If a clear delimiter still
    fails the C parser (quoted fields, etc.), we fall back to that python path.
    """
    is_tsv = path.suffix.lower() == ".tsv"
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    last_err: Optional[Exception] = None

    if is_tsv:
        for enc in encodings:
            try:
                return pd.read_csv(
                    path, encoding=enc, sep="\t", engine="c", dtype=object, low_memory=False
                )
            except UnicodeDecodeError as e:
                last_err = e
        raise ValueError(f"Could not decode TSV {path.name}: {last_err}")

    preferred_sep: Optional[str] = None
    try:
        with open(path, "rb") as fh:
            sample = fh.read(65_536)
        line = sample.splitlines()[0].decode("latin-1", errors="replace") if sample else ""
        nt, nc, ns = line.count("\t"), line.count(","), line.count(";")
        if nt > nc and nt >= ns and line.strip():
            preferred_sep = "\t"
        elif ns > nc and ns > 0:
            preferred_sep = ";"
        elif nc > 0:
            preferred_sep = ","
    except OSError:
        preferred_sep = None

    if preferred_sep is not None:
        for enc in encodings:
            try:
                return pd.read_csv(
                    path,
                    encoding=enc,
                    sep=preferred_sep,
                    engine="c",
                    dtype=object,
                    low_memory=False,
                )
            except UnicodeDecodeError as e:
                last_err = e
                continue
            except (pd.errors.ParserError, ValueError):
                continue

    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc, header=0, sep=None, engine="python", dtype=object)
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError(f"Could not read CSV {path.name}: {last_err}")


def _read_excel_fast(
    path: Path,
    sheet_name: str,
    usecols: Optional[List[int]] = None,
) -> pd.DataFrame:
    """Read xlsx using calamine (Rust-based, ~3-10x faster than openpyxl) with fallback."""
    opts: Dict[str, Any] = {
        "sheet_name": sheet_name,
        "header": 0,
        "dtype": object,
    }
    if usecols is not None:
        opts["usecols"] = usecols
    try:
        return pd.read_excel(path, engine="calamine", **opts)
    except Exception:
        return pd.read_excel(path, engine="openpyxl", **opts)


def _read_table(
    path: Path,
    sheet_hint: Optional[str] = None,
    max_data_cols: Optional[int] = None,
) -> pd.DataFrame:
    """Top-level read for any supported input format. Returns a DataFrame with header row.

    ``max_data_cols`` when set trims CSV/xlsx to the first N logical columns (faster, less RAM).
    """
    if _is_csv(path):
        df = _read_csv_fast(path)
        if max_data_cols is not None:
            df = df.iloc[:, :max_data_cols]
        return df
    usecols = list(range(max_data_cols)) if max_data_cols is not None else None
    # xlsx — reuse sheet name from validate_headers when provided (avoids 2nd openpyxl open)
    if sheet_hint:
        return _read_excel_fast(path, sheet_hint, usecols=usecols)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = _find_dump_sheet(wb)
    finally:
        wb.close()
    return _read_excel_fast(path, sheet, usecols=usecols)


def _read_headers_only(path: Path) -> Tuple[List[str], str]:
    """Cheap header peek for either CSV or xlsx. Returns (headers, sheet_name_or_marker)."""
    if _is_csv(path):
        # Read row 1 RAW (header=None, nrows=1) so pandas doesn't dedup duplicate header
        # names by appending '.1'. Real Metabase exports rarely have duplicate headers,
        # but xlsx-derived CSVs do (the source SBI format has order_id, financial_bu, bu
        # appearing twice).
        last_err = None
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                df = pd.read_csv(path, encoding=enc, header=None, nrows=1,
                                  sep=None, engine="python", dtype=object)
                row = df.iloc[0].tolist()
                return [str(v) if v is not None else "" for v in row], "csv"
            except UnicodeDecodeError as e:
                last_err = e
                continue
        raise ValueError(f"Could not decode CSV {path.name}: {last_err}")
    wb = load_workbook(path, read_only=True, data_only=True)
    sheet_name = _find_dump_sheet(wb)
    ws = wb[sheet_name]
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    actual = [str(h) if h is not None else "" for h in header_row]
    wb.close()
    return actual, sheet_name


# Columns in Dump that are low-cardinality strings — promoting to category dtype
# saves ~30-50% RAM on a 100k-row frame and speeds up filter/groupby operations.
_DUMP_CATEGORICAL_COLS = (
    "A",   # corporate_partner — ~2 values
    "B",   # financial_bu      — ~2 values
    "C",   # bu                — ~2 values
    "F",   # channel_gateway   — handful
    "I",   # sku_sub_category  — ~10 values
    "J",   # sku_sub_category_l2
    "L",   # order_status
    "O",   # pack_form
    "W",   # tags
    "X",   # payment_method
    "Y",   # payment_method_tag
    "AH",  # checker
    "AP",  # billing_flag
    "AR",  # financial_bu (dupe)
    "AS",  # bu (dupe)
)


def load_raw(path: Path, sheet_hint: Optional[str] = None) -> pd.DataFrame:
    """Read the raw dump (xlsx OR csv). Returns a DataFrame A..<end> labeled by column letters.

    For xlsx uploads, pass ``sheet_hint`` from :func:`validate_headers` (third return value
    when not ``"csv"``) to avoid re-scanning the workbook for the dump sheet.
    """
    n_expected = len(format_spec.DUMP_RAW_HEADERS)
    raw = _read_table(path, sheet_hint=sheet_hint, max_data_cols=n_expected)
    raw = raw.iloc[:, :n_expected].copy()
    raw.columns = col_letters(n_expected)

    # CSV: dates come in as strings — coerce known date columns to datetime so the
    # downstream pipeline + xlsx export treat them correctly. xlsx already gives datetimes.
    if _is_csv(path):
        for letter in ("K", "M", "N"):  # order_placed_date, order_completed_date, delivered_month
            if letter in raw.columns:
                raw[letter] = pd.to_datetime(raw[letter], errors="coerce")

    # Memory: convert low-cardinality strings to category dtype.
    for c in _DUMP_CATEGORICAL_COLS:
        if c in raw.columns:
            try:
                raw[c] = raw[c].astype("category")
            except Exception:
                pass
    return raw


def row_count(path: Path) -> int:
    wb = load_workbook(path, read_only=True, data_only=True)
    sheet_name = _find_dump_sheet(wb)
    ws = wb[sheet_name]
    count = ws.max_row - 1 if ws.max_row else 0
    wb.close()
    return max(0, count)


def load_ahc(path: Path) -> pd.DataFrame:
    """Load an uploaded AHC file (xlsx OR csv). Preserves original header names.
    For xlsx, prefers a sheet named 'AHC' if present, else first sheet.
    """
    if _is_csv(path):
        df = _read_csv_fast(path)
    else:
        sheet_name = "AHC"
        try:
            xf = pd.ExcelFile(path, engine="calamine")
            try:
                names = xf.sheet_names
                if names:
                    sheet_name = "AHC" if "AHC" in names else names[0]
            finally:
                xf.close()
        except Exception:
            wb = load_workbook(path, read_only=True, data_only=True)
            try:
                sheet_name = (
                    "AHC"
                    if "AHC" in wb.sheetnames
                    else (wb.sheetnames[0] if wb.sheetnames else "AHC")
                )
            finally:
                wb.close()
        df = _read_excel_fast(path, sheet_name)
    df = df.dropna(how="all")
    return df


def anomalies(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Cheap sanity checks. Returns list of {level, message, count}."""
    out: List[Dict[str, Any]] = []
    # GL blank columns from the PRD — here the analog is CORPORATE_IDENTIFIER (col Q)
    blanks_q = df["Q"].isna().sum() if "Q" in df.columns else 0
    if blanks_q:
        out.append({"level": "warn", "message": f"{int(blanks_q)} rows with blank CORPORATE_IDENTIFIER"})

    blanks_z = df["Z"].isna().sum() if "Z" in df.columns else 0
    if blanks_z:
        out.append({"level": "warn", "message": f"{int(blanks_z)} rows with blank gmv_mrp"})

    return out
