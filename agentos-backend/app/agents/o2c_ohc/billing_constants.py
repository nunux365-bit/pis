"""Shared identifiers for contract billing rows (O2C OHC; MIS + rate lines)."""

from __future__ import annotations

# ``fixed_monthly`` rate line: % of period staffing subtotal (contract: admin on monthly invoice).
OHC_INVOICE_ADMIN_ROLE_CODE = "OHC_ADMIN_INVOICE_PCT"

# JSON key inside ``billing_rules`` on that line (percentage of staffing base subtotal).
INVOICE_ADMIN_PCT_BILLING_RULE_KEY = "invoice_admin_pct"

# Optional: which Phase A rows feed ``sum_B`` for the admin line (default = staffing only).
INVOICE_ADMIN_BASE_KEY = "invoice_admin_base"
INVOICE_ADMIN_BASE_STAFFING_ONLY = "staffing_only"
INVOICE_ADMIN_BASE_STAFFING_PLUS_FIXED_MONTHLY = "staffing_plus_fixed_monthly"

# ``source_ref["note"]`` on invoice-admin lines created by TACO ingest normalizer (MIS may key off this).
SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE = "server_taco_invoice_admin_line"
