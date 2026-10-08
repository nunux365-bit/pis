-- Optional SQL supplements after JSON fixed-master load.
-- Org default plants and labels live in reference_fixed_master.json; keep this file for
-- one-off data patches that are awkward in JSON (run manually when needed).

-- Example (commented):
-- UPDATE pr_po_reference_values SET label = '1mg Healthcare'
-- WHERE domain = 'company_code' AND code = '1MGH';
