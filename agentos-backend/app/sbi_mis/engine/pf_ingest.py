"""Ingest a PF Summary xlsx into the pf_historicals table.

Expected layout (matches the SBI working template):
  Row 1: column headers OR blank (followed by sum formulas in some cols)
  Row 2: headers like 'PF', 'Type', 'Wallet Balance', 'Jan', 'Jan AHC', 'Feb', 'Feb AHC', 'Mar', 'Mar AHC', ...
  Row 3+: data

We detect the header row by scanning for a row whose cells match expected labels, then
extract one (pf, month, pharma_total, ahc_total, pf_type, wallet_limit) record per cell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook


# Canonical short-month → month key builder requires a base year; for v1 we map known labels.
_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_pf_summary(path: Path, default_year: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read the PF summary xlsx and return (records, info).

    records: list of dicts suitable for pf_historicals upsert:
        {pf, month, pharma_total, ahc_total, pf_type, wallet_limit}
    info: diagnostic metadata (sheet_name, header_row, month_columns, row_count)
    """
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet_name = next(
            (s for s in wb.sheetnames if s.strip().lower() == "pf summary"),
            wb.sheetnames[0],
        )
        ws = wb[sheet_name]

        # Find the header row. Scan first 5 rows for one containing "PF" and a month-name.
        header_row_idx: Optional[int] = None
        headers: List[str] = []
        max_probe = 5
        for r, row in enumerate(
            ws.iter_rows(min_row=1, max_row=max_probe, values_only=True),
            start=1,
        ):
            row_vals = [str(v).strip() if v is not None else "" for v in (row or ())]
            lower = [v.lower() for v in row_vals]
            if any(v == "pf" or v == "pf id" for v in lower) and any(v in _MONTH_MAP for v in lower):
                header_row_idx = r
                headers = row_vals
                break
        if header_row_idx is None:
            raise ValueError(
                "Could not find a header row in the PF Summary sheet "
                "(looking for 'PF' + a month name like 'Jan/Feb/…')."
            )

        # Identify column roles
        col_map: Dict[str, int] = {}  # role → 1-based column index
        month_cols: List[Tuple[str, int]] = []  # (month_key 'YYYY-MM', col_idx) for pharma
        ahc_cols: List[Tuple[str, int]] = []  # (month_key, col_idx) for AHC
        for i, h in enumerate(headers, start=1):
            hl = h.strip().lower()
            if hl in ("pf", "pf id", "corporate_identifier"):
                col_map["pf"] = i
            elif hl in ("type", "user type", "user_type"):
                col_map["type"] = i
            elif hl in ("wallet balance", "wallet", "wallet_balance", "wallet limit"):
                col_map["wallet"] = i
            elif hl in _MONTH_MAP:
                m = _MONTH_MAP[hl]
                month_cols.append((f"{default_year:04d}-{m:02d}", i))
            elif hl.endswith(" ahc") or hl.endswith("_ahc"):
                name = hl.replace(" ahc", "").replace("_ahc", "").strip()
                if name in _MONTH_MAP:
                    m = _MONTH_MAP[name]
                    ahc_cols.append((f"{default_year:04d}-{m:02d}", i))

        if "pf" not in col_map or not month_cols:
            raise ValueError(f"PF summary missing essential columns. Headers seen: {headers}")

        max_c = max(
            col_map["pf"],
            max((col_map[k] for k in ("type", "wallet") if k in col_map), default=0),
            max((c for _, c in month_cols), default=0),
            max((c for _, c in ahc_cols), default=0),
        )

        records: List[Dict[str, Any]] = []
        first_data_row = header_row_idx + 1
        row_count = 0
        pf_i = col_map["pf"] - 1
        type_i = (col_map["type"] - 1) if "type" in col_map else None
        wallet_i = (col_map["wallet"] - 1) if "wallet" in col_map else None
        month_idx_pairs = [(mk, c - 1) for mk, c in month_cols]
        ahc_idx_pairs = [(mk, c - 1) for mk, c in ahc_cols]

        def _cell(row_vals: Tuple[Any, ...], idx: Optional[int]) -> Any:
            if idx is None or idx < 0:
                return None
            if idx >= len(row_vals):
                return None
            return row_vals[idx]

        max_row = ws.max_row or 0
        for row in ws.iter_rows(
            min_row=first_data_row,
            max_row=max_row,
            min_col=1,
            max_col=max_c,
            values_only=True,
        ):
            row_t = tuple(row) if row is not None else ()
            pf_cell = _cell(row_t, pf_i)
            if pf_cell is None:
                continue
            pf_str = (
                str(int(pf_cell)) if isinstance(pf_cell, float) and pf_cell.is_integer() else str(pf_cell).strip()
            )
            if not pf_str or pf_str.lower() == "grand total":
                continue
            pf_type = _cell(row_t, type_i)
            wallet = _cell(row_t, wallet_i)
            row_count += 1
            months_seen: Dict[str, Dict[str, Any]] = {}
            for month_key, cidx in month_idx_pairs:
                v = _cell(row_t, cidx)
                months_seen.setdefault(month_key, {})["pharma_total"] = (
                    float(v) if isinstance(v, (int, float)) else None
                )
            for month_key, cidx in ahc_idx_pairs:
                v = _cell(row_t, cidx)
                months_seen.setdefault(month_key, {})["ahc_total"] = (
                    float(v) if isinstance(v, (int, float)) else None
                )
            for month_key, vals in months_seen.items():
                if vals.get("pharma_total") is None and vals.get("ahc_total") is None:
                    continue
                records.append(
                    {
                        "pf": pf_str,
                        "month": month_key,
                        "pharma_total": vals.get("pharma_total"),
                        "ahc_total": vals.get("ahc_total"),
                        "pf_type": pf_type,
                        "wallet_limit": float(wallet) if isinstance(wallet, (int, float)) else None,
                    }
                )

        return records, {
            "sheet_name": sheet_name,
            "header_row": header_row_idx,
            "row_count": row_count,
            "months_detected": sorted(set(m for m, _ in month_cols)),
        }
    finally:
        wb.close()
