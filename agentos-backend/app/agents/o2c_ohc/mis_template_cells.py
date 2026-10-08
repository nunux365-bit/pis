"""
Fixed-cell map for `Standardized MIS Format - OHC.xlsx` (no {{placeholders}}).

Maps semantic fields to worksheet coordinates for template-driven fills (legacy layout; live path builds MIS from ``o2c_mis_run`` / ``o2c_mis_summary_row`` in code).
Extend as product owners confirm additional cells (GST block, bank, etc.).
"""

from __future__ import annotations

from typing import Any

# Sheet "OHC Summary " (note trailing space in template)
SHEET_SUMMARY = "OHC Summary "

# Header row 1 is labels; row 2+ are line blocks. Template uses sample client in A2.
CELL_CLIENT_NAME = f"{SHEET_SUMMARY}!A2"
# Template has no dedicated invoice # cell in the audited sample — add when finance confirms.
# Line-oriented block starts row 2; multiple summary rows fill from this row downward
FIRST_LINE_ROW = 2
COL_SERVICE = "B"
COL_RATE = "C"
COL_TOTAL_DAYS = "D"
COL_ATTENDANCE = "E"
COL_FINAL = "F"


def describe_standardized_mis_coverage() -> dict[str, Any]:
    """What invoice DB fields map to this template today vs gaps."""
    return {
        "template": "Standardized MIS Format - OHC.xlsx",
        "covered_from_invoice_tables": [
            "billing_client.name → OHC Summary col A (client block)",
            "invoice_line.description → col B (service/role line)",
            "invoice_line.unit_rate / quantity semantics → cols C–F (needs business rules)",
        ],
        "gaps": [
            "No invoice_number / date / address / GST / bank cells in sample rows 1–7",
            "OHC Detailed sheet is a second matrix — separate mapping required",
            "ArrayFormula in B2 — overwriting may require clearing template formulas",
        ],
    }
