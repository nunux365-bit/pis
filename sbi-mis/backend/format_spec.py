"""
Hard-coded SBI MIS output format.

This is the structural skeleton of the SBI "Working" xlsx:
  - which sheets exist
  - which columns each sheet has, their headers, number formats
  - DEFAULT rules for each derived column (can be overridden via UI and persisted in DB)

Editable pieces live in the `rules` table of SQLite. Structural pieces (sheet names,
column letters/headers, column widths, number formats) are NOT editable — they come
from SBI's contract and change rarely; when they do, edit this file and ship v2.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# -------------------- Dump (raw input) schema -------------------- #

# Columns A..AG come verbatim from the Metabase query #3180 export.
# We validate the uploaded file's header row against this list exactly.
DUMP_RAW_HEADERS: List[str] = [
    "corporate_partner",      # A
    "financial_bu",           # B
    "bu",                     # C
    "group_id",               # D
    "order_id",               # E
    "channel_gateway",        # F
    "sku_id",                 # G
    "sku_name",               # H
    "sku_sub_category",       # I
    "sku_sub_category_l2",    # J
    "order_placed_date",      # K
    "order_status",           # L
    "order_completed_date",   # M
    "delivered_month",        # N
    "pack_form",              # O
    "item_size",              # P
    "CORPORATE_IDENTIFIER",   # Q
    "rejection_reasons",      # R
    "cancel_reason",          # S
    "return_reason",          # T
    "erx_generated",          # U
    "mobile_number",          # V
    "tags",                   # W
    "payment_method",         # X
    "payment_method_tag",     # Y
    "gmv_mrp",                # Z
    "upfront_discount",       # AA
    "gmv_list_price",         # AB
    "coupon_discount",        # AC
    "shipping_charges",       # AD
    "vas_charges",            # AE
    "co_pay_discount",        # AF
    "net_payable_by_customer",# AG
    # --- Columns AH-AS also come directly from Metabase (user-confirmed Apr 2026) --- #
    "checker",                # AH
    "group_ids",              # AI
    "group_idss",             # AJ
    "return_group_ids",       # AK
    "order_id",               # AL  (dupe)
    "wallet_balance",         # AM
    "upfront_checker",        # AN
    "copay_checker",          # AO
    "billing_flag",           # AP
    "copay_check",            # AQ
    "financial_bu",           # AR  (dupe)
    "bu",                     # AS  (dupe)
]

# Dump enrichment columns AT..AX — per-row formulas + Next Step decision col.
# All A-AS come directly from Metabase and need no rules.
DUMP_ENRICHMENT_COLS: List[Dict[str, Any]] = [
    {"col": "AT", "header": "Disc %",
     "default_rule": {"type": "formula", "config": {"formula": "=(AA+AC)/Z"}},
     "notes": "(upfront_discount + coupon_discount) / gmv_mrp"},

    {"col": "AU", "header": "Co_Pay %",
     "default_rule": {"type": "formula", "config": {"formula": "=AF/Z"}},
     "notes": "co_pay_discount / gmv_mrp"},

    {"col": "AV", "header": "Net_Pay%",
     "default_rule": {"type": "formula", "config": {"formula": "=1-AT-AU"}},
     "notes": "1 - Disc% - Co_Pay%"},

    {"col": "AW", "header": "Ratio",
     "default_rule": {"type": "formula", "config": {"formula": "=AU/AV"}},
     "notes": "Co_Pay% / Net_Pay%"},

    {"col": "AX", "header": "Next Step",
     "default_rule": {"type": "formula", "config": {
         "formula": '=IF(OR(AH="non-permissible",AH="non permissible"),"Non-permissible",IF(AND(ROUND(AT+AV,4)=1,AU=0,AW=0),"Non cashless",IF(AND(AV>0.2,AU=0),"wallet exhausted","No issue")))'
     }},
     "notes": "Decision tree. Outputs canonical taxonomy: Non-permissible | Non cashless | wallet exhausted | No issue."},

    {"col": "AY", "header": "Order classification",
     "default_rule": {"type": "group_any", "config": {
         "group_by": ["D", "E"],  # group by (group_id, order_id)
         "source": "AX",
         "equals": "Non-permissible",
         "match_value": "Non-permissible",
         "else_value": "OK",
     }},
     "notes": "If ANY line in the order has AX = Non-permissible, whole order is tagged Non-permissible. Used by Order level filters to send mixed orders to OL-NP (matching source file)."},
]


# -------------------- Output sheet specs -------------------- #

@dataclass
class OutputCol:
    col: str                      # column letter 'A', 'B', ...
    header: str                   # row-2 header text (Order level) or row-1 (elsewhere)
    default_rule: Dict[str, Any]  # {type, config}
    number_format: Optional[str] = None  # openpyxl number_format string
    width: Optional[float] = None


@dataclass
class OutputSheetSpec:
    name: str                     # EXACT sheet name (note trailing space in "Order level ")
    kind: str                     # 'row_per_dump' | 'pivot' | 'static'
    header_row: int               # which row holds headers (2 for Order level, 2 for PF Summary, 1 for AHC-style)
    first_data_row: int           # first data row (3 for Order level, 3 for PF Summary)
    grand_total_row: Optional[int] = None  # row with =SUM(...) grand totals (1 for Order level)
    filter_rule_key: Optional[str] = None  # DB key for the row-filter rule (None if no filter)
    columns: List[OutputCol] = field(default_factory=list)
    notes: str = ""


# --- Order level (permissible) --- #
ORDER_LEVEL = OutputSheetSpec(
    name="Order level ",
    kind="pivot",
    header_row=2,
    first_data_row=3,
    grand_total_row=1,
    filter_rule_key="__filter__",
    notes="Pivot over permissible Dump line items, grouped by (group_id, order_id). "
          "Numeric columns SUM line items; categoricals take FIRST of group.",
    columns=[
        OutputCol("A", "S No", {"type": "counter", "config": {}}, number_format="0"),
        OutputCol("B", "group_id",       {"type": "pivot_group_key", "config": {"key": 0, "source": "D"}}),
        OutputCol("C", "order_id",       {"type": "pivot_group_key", "config": {"key": 1, "source": "E"}}),
        OutputCol("D", "USER_TYPE",      {"type": "pivot_first", "config": {"source": "A"}}),
        OutputCol("E", "PF ID",          {"type": "pivot_first", "config": {"source": "Q"}}),
        OutputCol("F", "Placed Date",    {"type": "pivot_first", "config": {"source": "K"}}, number_format="mm/dd/yyyy hh:mm"),
        OutputCol("G", "Completed Date", {"type": "pivot_first", "config": {"source": "M"}}, number_format="mm/dd/yyyy hh:mm"),
        OutputCol("H", "Payment_method", {"type": "pivot_first", "config": {"source": "F"}}),
        OutputCol("I", "Next Step",      {"type": "pivot_first", "config": {"source": "AX"}}),
        OutputCol("J", "GMV_MRP",        {"type": "pivot_agg", "config": {"agg": "sum", "agg_col": "Z"}}),
        OutputCol("K", "DISCOUNT",       {"type": "pivot_agg", "config": {"agg": "sum", "agg_col": "AA"}}),
        OutputCol("L", "GMV_LIST",       {"type": "pivot_agg", "config": {"agg": "sum", "agg_col": "AB"}}),
        OutputCol("M", "CO_PAY_DISCOUNT",{"type": "pivot_agg", "config": {"agg": "sum", "agg_col": "AF"}}),
        OutputCol("N", "USER_PAID",      {"type": "pivot_agg", "config": {"agg": "sum", "agg_col": "AG"}}),
    ],
)

# --- Order level - non permissible --- #
ORDER_LEVEL_NP = OutputSheetSpec(
    name="Order level - non permissible",
    kind="pivot",
    header_row=2,
    first_data_row=3,
    grand_total_row=1,
    filter_rule_key="__filter__",
    notes="Pivot over non-permissible Dump line items, grouped by (group_id, order_id).",
    columns=list(ORDER_LEVEL.columns),
)

# --- PF Summary --- #
PF_SUMMARY = OutputSheetSpec(
    name="PF Summary",
    kind="pivot",
    header_row=2,
    first_data_row=3,
    grand_total_row=1,
    notes="One row per unique CORPORATE_IDENTIFIER. v1 only fills current month's column.",
    columns=[
        OutputCol("A", "PF", {"type": "pivot_key", "config": {"source": "Q"}}),
        OutputCol("B", "Type", {"type": "pivot_first", "config": {"source": "A"}},
                  # corporate_partner per PF (assumes one per PF)
                  ),
        OutputCol("C", "Wallet Balance",
                  {"type": "lookup", "config": {"table": "pf_wallet_limits", "key_col": "Q"}}),
        OutputCol("D", "Jan",     {"type": "blank", "config": {}}),
        OutputCol("E", "Jan AHC", {"type": "blank", "config": {}}),
        OutputCol("F", "Feb",     {"type": "blank", "config": {}}),
        OutputCol("G", "Feb AHC", {"type": "blank", "config": {}}),
        OutputCol("H", "Mar",
                  {"type": "pivot_agg",
                   "config": {"source": "Q", "agg": "sum", "agg_col": "AF"}}),
        OutputCol("I", "Mar AHC", {"type": "blank", "config": {}}),
        OutputCol("J", "Grand Total",
                  {"type": "formula", "config": {"formula": "=D+E+F+G+H+I"}}),
        OutputCol("K", "Overutilised",
                  {"type": "formula", "config": {"formula": "=IF(C=\"\",0,MAX(0,J-C))"}}),
    ],
)


@dataclass
class StaticCell:
    cell: str                     # e.g. "A3"
    default_rule: Dict[str, Any]
    number_format: Optional[str] = None


# --- Summary --- #
# Matches the source file exactly (10 rows).
SUMMARY_CELLS: List[StaticCell] = [
    StaticCell("A3",  {"type": "static", "config": {"value": "Row Labels"}}),
    StaticCell("B3",  {"type": "static", "config": {"value": "Sum of co_pay_discount"}}),
    StaticCell("A4",  {"type": "static", "config": {"value": "No issue"}}),
    StaticCell("B4",  {"type": "formula", "config": {"formula": "=SUMIF(Dump.AX,\"No issue\",Dump.AF)"}}),
    StaticCell("C4",  {"type": "static", "config": {"value": "to be billed"}}),
    StaticCell("A5",  {"type": "static", "config": {"value": "Non-permissible"}}),
    StaticCell("B5",  {"type": "formula", "config": {"formula": "=SUMIF(Dump.AX,\"Non-permissible\",Dump.AF)"}}),
    StaticCell("C5",  {"type": "static", "config": {"value": "not billed"}}),
    StaticCell("A6",  {"type": "static", "config": {"value": "Partial copay, wallet exhausted"}}),
    StaticCell("B6",  {"type": "formula", "config": {"formula": "=SUMIF(Dump.AX,\"Partial copay, wallet exhausted\",Dump.AF)"}}),
    StaticCell("C6",  {"type": "static", "config": {"value": "to be billed"}}),
    StaticCell("A7",  {"type": "static", "config": {"value": "Grand Total Pharma"}}),
    StaticCell("B7",  {"type": "formula", "config": {"formula": "=SUM(B4+B6)"}}),
    StaticCell("A8",  {"type": "static", "config": {"value": "Overutilised"}}),
    StaticCell("B8",  {"type": "formula", "config": {"formula": "=SUM('PF Summary'.K)"}}),
    StaticCell("C8",  {"type": "static", "config": {"value": "Minus"}}),
    StaticCell("A9",  {"type": "static", "config": {"value": "Annual Health Checkup"}}),
    StaticCell("B9",  {"type": "blank", "config": {}}),
    StaticCell("C9",  {"type": "static", "config": {"value": "to be billed"}}),
    StaticCell("A10", {"type": "static", "config": {"value": "Final To be billed"}}),
    StaticCell("B10", {"type": "formula", "config": {"formula": "=(B7+B9)-B8"}}),
]


# -------------------- Public accessors -------------------- #

OUTPUT_SHEETS: List[OutputSheetSpec] = [ORDER_LEVEL, ORDER_LEVEL_NP, PF_SUMMARY]

SHEET_ORDER: List[str] = ["Dump", "Order level ", "Order level - non permissible", "Summary", "PF Summary", "AHC"]


def effective_columns(spec: "OutputSheetSpec") -> List[OutputCol]:
    """Return the LIVE column list for a pivot sheet.

    Reads from the `sheet_columns` DB table so users can add / delete columns;
    falls back to the hard-coded spec columns if the DB is empty (first-run guarantee).
    Rule configs live in the `rules` table and are looked up per column by letter.
    """
    try:
        from . import db as _db
        db_rows = _db.sheet_columns_for(spec.name)
    except Exception:
        db_rows = []
    if not db_rows:
        return list(spec.columns)

    # Build OutputCol list from DB rows. For rule defaults:
    #   - spec-defined cols reuse the original spec's default_rule
    #   - user-added cols default to 'blank' (the user will then edit via inspector)
    spec_by_col = {c.col: c for c in spec.columns}
    out: List[OutputCol] = []
    for r in db_rows:
        letter = r["col_letter"]
        header = r["header"]
        nf = r["number_format"]
        if letter in spec_by_col:
            base = spec_by_col[letter]
            out.append(OutputCol(
                col=letter,
                header=header or base.header,
                default_rule=base.default_rule,
                number_format=nf if nf is not None else base.number_format,
                width=base.width,
            ))
        else:
            out.append(OutputCol(
                col=letter,
                header=header,
                default_rule={"type": "blank", "config": {}},
                number_format=nf,
            ))
    return out


def seed_rules() -> List[Dict[str, Any]]:
    """Return a list of rule records (sheet, column_letter, rule_type, config_json) to seed on first DB init."""
    import json
    out: List[Dict[str, Any]] = []

    # Dump enrichment columns
    for col in DUMP_ENRICHMENT_COLS:
        r = col["default_rule"]
        out.append({
            "sheet": "Dump",
            "column_letter": col["col"],
            "rule_type": r["type"],
            "config_json": json.dumps(r["config"]),
            "notes": col.get("notes", ""),
        })

    # Order level + Order level - non permissible
    for sheet in (ORDER_LEVEL, ORDER_LEVEL_NP):
        # Filter on AY (order-level classification) so any order with ANY non-permissible line
        # goes entirely to OL-NP — matching the source working file's behavior.
        filter_expr = "AY != 'Non-permissible'" if sheet is ORDER_LEVEL else "AY == 'Non-permissible'"
        out.append({
            "sheet": sheet.name,
            "column_letter": "__filter__",
            "rule_type": "filter",
            "config_json": json.dumps({"expr": filter_expr, "source_col": "AY"}),
            "notes": "Which Dump rows flow into this sheet. Uses AY (order-level classification) so mixed orders end up on OL-NP entirely.",
        })
        for c in sheet.columns:
            out.append({
                "sheet": sheet.name,
                "column_letter": c.col,
                "rule_type": c.default_rule["type"],
                "config_json": json.dumps(c.default_rule["config"]),
                "notes": "",
            })

    # PF Summary
    for c in PF_SUMMARY.columns:
        out.append({
            "sheet": PF_SUMMARY.name,
            "column_letter": c.col,
            "rule_type": c.default_rule["type"],
            "config_json": json.dumps(c.default_rule["config"]),
            "notes": "",
        })

    # Summary (per-cell)
    for cell in SUMMARY_CELLS:
        out.append({
            "sheet": "Summary",
            "column_letter": cell.cell,
            "rule_type": cell.default_rule["type"],
            "config_json": json.dumps(cell.default_rule["config"]),
            "notes": "",
        })

    return out


def schema_json() -> Dict[str, Any]:
    """JSON for the frontend — describes sheet layouts so the grid can render."""
    import json as _json

    def serialize_cols(cols: List[OutputCol]) -> List[Dict[str, Any]]:
        return [
            {
                "col": c.col,
                "header": c.header,
                "number_format": c.number_format,
                "width": c.width,
                "default_rule_type": c.default_rule["type"],
            }
            for c in cols
        ]

    return {
        "dump": {
            "raw_headers": DUMP_RAW_HEADERS,
            "enrichment_cols": [
                {
                    "col": c["col"],
                    "header": c["header"],
                    "default_rule_type": c["default_rule"]["type"],
                    "notes": c.get("notes", ""),
                }
                for c in DUMP_ENRICHMENT_COLS
            ],
        },
        "output_sheets": [
            {
                "name": s.name,
                "kind": s.kind,
                "header_row": s.header_row,
                "first_data_row": s.first_data_row,
                "grand_total_row": s.grand_total_row,
                "has_filter": s.filter_rule_key is not None,
                "notes": s.notes,
                "columns": serialize_cols(s.columns),
            }
            for s in OUTPUT_SHEETS
        ],
        "summary_cells": [
            {"cell": cell.cell, "default_rule_type": cell.default_rule["type"], "number_format": cell.number_format}
            for cell in SUMMARY_CELLS
        ],
        "sheet_order": SHEET_ORDER,
    }
