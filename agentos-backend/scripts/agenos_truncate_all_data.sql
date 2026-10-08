-- =============================================================================
-- DESTRUCTIVE: remove every row from Agenos / O2C billing tables (schema kept).
-- Covers contracts (all kinds: TACO, TCS-style MSAs, etc.), sites, aliases, MIS,
-- invoices, attendance, extraction runs, and failed parses so you can reingest from scratch.
-- If your DB has ``o2c_ingestion_state`` (folder-scan watermarks), truncate it in the same
-- session before or after this script, or add it to the list below.
--
-- Run:
--   psql "$AGENOS_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/agenos_truncate_all_data.sql
-- Or: python scripts/agenos_truncate_all_data.py --yes
-- =============================================================================

BEGIN;

TRUNCATE TABLE
    o2c_mis_summary_row,
    invoice_line,
    invoice_run,
    package_usage_period,
    attendance_row,
    attendance_import_batch,
    o2c_mis_run,
    o2c_attendance_site_recon,
    contract_rate_line,
    payment_terms,
    contract_party,
    contract_terms_document,
    contract_extraction_run,
    site_alias,
    contract_document,
    contract_terms_version,
    service_site,
    failed_contract_parsing,
    billing_client
RESTART IDENTITY CASCADE;

COMMIT;
