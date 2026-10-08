-- Run concurrently on large production tables to avoid long ACCESS EXCLUSIVE locks.
-- After these complete, stamp the Alembic revision to 021_pr_po_ref_vendor_trgm
-- (``alembic stamp 021_pr_po_ref_vendor_trgm``) so the migration does not
-- attempt to re-create the same indexes without CONCURRENTLY.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_label_trgm
    ON pr_po_reference_values USING gin (label gin_trgm_ops)
    WHERE domain = 'vendor';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_code_trgm
    ON pr_po_reference_values USING gin (code gin_trgm_ops)
    WHERE domain = 'vendor';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_name1_trgm
    ON pr_po_reference_values USING gin ((coalesce(extra->>'name_1', '')) gin_trgm_ops)
    WHERE domain = 'vendor';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_name2_trgm
    ON pr_po_reference_values USING gin ((coalesce(extra->>'name_2', '')) gin_trgm_ops)
    WHERE domain = 'vendor';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_searchterm_trgm
    ON pr_po_reference_values USING gin ((coalesce(extra->>'searchterm', '')) gin_trgm_ops)
    WHERE domain = 'vendor';

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pr_po_ref_vendor_city_trgm
    ON pr_po_reference_values USING gin ((coalesce(extra->>'city', '')) gin_trgm_ops)
    WHERE domain = 'vendor';
