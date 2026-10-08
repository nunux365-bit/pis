-- Partial B-tree on vendor ``code`` (composite ``{vendor}|{cocd}``) for prefix / numeric search.
-- Pair with application logic: ``split_part(code, '|', 1)`` prefix match + ``company_code`` filter.
-- At ~200k rows, ``pg_trgm`` is optional if users type ≥2 characters and CoCd is usually set on PO.

CREATE INDEX IF NOT EXISTS ix_pr_po_ref_vendor_code
  ON pr_po_reference_values (code)
  WHERE domain = 'vendor';
