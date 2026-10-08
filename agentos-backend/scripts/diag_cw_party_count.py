"""Diagnostic: break down receivable rows -> Party Level distinct codes for a BU.

Reuses the real receivable_dashboard parser primitives so the counts match what
the Reply Tracker / Party Level details would show.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from app.services.receivable_dashboard import (
    COL_BUSINESS_UNIT,
    COL_CODE,
    COL_NET_DUE,
    COL_PARTY_NAME,
    _age_column_indices,
    _discover_receivable_header,
    _find_receivable_sheet,
    _iter_party_rows,
    _match_col,
    _sheet_max_col,
    is_excluded_payable_ledger_name,
    normalize_bu_key,
)

TARGET_BU = "corporatewellness"  # normalize_bu_key form (spaces stripped)


def main(path: str) -> None:
    wb = load_workbook(path, data_only=True, read_only=False)
    try:
        rname = _find_receivable_sheet(wb.sheetnames)
        print(f"receivable sheet: {rname!r}")
        ws = wb[rname]
        c_max = _sheet_max_col(ws, cap=200)
        header_excel_row, row4 = _discover_receivable_header(ws, c_max)
        c_code = _match_col(list(row4), COL_CODE)
        c_bu = _match_col(list(row4), COL_BUSINESS_UNIT)
        c_net = _match_col(list(row4), COL_NET_DUE)
        c_name = _match_col(list(row4), COL_PARTY_NAME)
        age_idx = _age_column_indices(list(row4), c_max) or [c_net]
        raw = _iter_party_rows(
            ws,
            start_row=header_excel_row + 1,
            c_max=c_max,
            c_code=c_code,
            c_bu=c_bu,
            c_net=c_net,
            c_name=c_name,
            age_idx=age_idx,
        )
    finally:
        wb.close()

    # All raw rows whose BU normalizes to Corporate Wellness (blank-code rows are
    # already dropped by _iter_party_rows, so report that separately below).
    cw_rows = [p for p in raw if normalize_bu_key(p.bu) == TARGET_BU]
    print(f"\n=== Corporate Wellness (normalized BU) ===")
    print(f"raw _Party rows (already excludes blank-HANA rows): {len(cw_rows)}")

    payable = [p for p in cw_rows if is_excluded_payable_ledger_name(p.name)]
    kept = [p for p in cw_rows if not is_excluded_payable_ledger_name(p.name)]
    print(f"  - excluded as Payable/contra ledger lines: {len(payable)}")
    print(f"  rows after payable exclusion: {len(kept)}")

    blank_name = [p for p in kept if not (p.name and p.name.strip())]
    blank_bu = [p for p in kept if not p.bu]
    print(f"  - rows with blank Name (dropped from party list): {len(blank_name)}")
    print(f"  - rows with blank BU string: {len(blank_bu)}")

    # Collapse to distinct (code, BU-norm) == what Party Level details keys on.
    by_code_bu = defaultdict(list)
    for p in kept:
        by_code_bu[(p.code.strip(), normalize_bu_key(p.bu))].append(p)
    distinct_codes = {c for (c, _b) in by_code_bu}
    print(f"\n  distinct (code, BU) groups: {len(by_code_bu)}")
    print(f"  distinct HANA codes (party_map keys): {len(distinct_codes)}")

    # How many codes had >1 raw row (the de-dup that explains most of the gap)
    multi = {k: v for k, v in by_code_bu.items() if len(v) > 1}
    dup_rows = sum(len(v) - 1 for v in multi.values())
    print(f"  codes appearing on >1 row: {len(multi)}  (extra duplicate rows: {dup_rows})")

    # Raw row count INCLUDING blank-HANA rows, to reconcile with the 311 the user sees
    print("\n=== reconciliation ===")
    print(f"  raw rows (blank-code already excluded) : {len(cw_rows)}")
    print(f"  minus payable                           : -{len(payable)}")
    print(f"  minus duplicate rows (same code+BU)     : -{dup_rows}")
    print(f"  minus blank-name rows                   : -{len(blank_name)}")
    print(f"  => distinct party codes                 : {len(distinct_codes)}")


if __name__ == "__main__":
    main(sys.argv[1])
