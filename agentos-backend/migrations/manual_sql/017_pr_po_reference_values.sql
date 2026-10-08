-- Manual SQL equivalent to Alembic 017_pr_po_reference_applies_to_kind.py
-- Run against the same database as the app (PostgreSQL).

-- 1) Add column for PR/PO scoping of duplicate SAP purchasing doc types (and future use).
ALTER TABLE pr_po_reference_values
  ADD COLUMN IF NOT EXISTS applies_to_kind VARCHAR(2) NOT NULL DEFAULT '';

-- 2) Replace unique constraint (domain + document_type + code) with (..., applies_to_kind).
ALTER TABLE pr_po_reference_values
  DROP CONSTRAINT IF EXISTS uq_pr_po_ref_domain_type_code;

ALTER TABLE pr_po_reference_values
  ALTER COLUMN code TYPE VARCHAR(256);

ALTER TABLE pr_po_reference_values
  ADD CONSTRAINT uq_pr_po_ref_domain_type_code_kind
  UNIQUE (domain, document_type, code, applies_to_kind);

CREATE INDEX IF NOT EXISTS ix_pr_po_ref_domain_kind
  ON pr_po_reference_values (domain, applies_to_kind);

-- Example INSERTs (adjust UUIDs if you insert without gen_random_uuid())

-- SAP purchasing doc type: same code, different label for PR vs PO
-- INSERT INTO pr_po_reference_values (id, domain, document_type, code, label, sort_order, extra, applies_to_kind)
-- VALUES
--   (gen_random_uuid(), 'purchasing_doc_type', '', 'YUNB', 'PR Non Valuated', 10, NULL, 'PR'),
--   (gen_random_uuid(), 'purchasing_doc_type', '', 'YUNB', 'PO Non Valuated', 20, NULL, 'PO');

-- Vendor row: composite code vendor|cocd; extra holds searchable metadata (populate via import script).
-- INSERT INTO pr_po_reference_values (id, domain, document_type, code, label, sort_order, extra, applies_to_kind)
-- VALUES (
--   gen_random_uuid(),
--   'vendor',
--   '',
--   '2000000427|1MGH',
--   'Big Tree Resource Management Pvt Lt — Delhi',
--   0,
--   '{"vendor": "2000000427", "cocd": "1MGH", "name1": "Big Tree Resource Management Pvt Lt", "city": "Delhi"}'::jsonb,
--   ''
-- );
