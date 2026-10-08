"""
Canonical O2C contract ingest design — single source for docstrings and docs.

Implementation order in ``pipeline.process_one_contract_pdf``:
1. LLM: PDF + embedded JSON Schema → ``document_markdown`` + ``contract_payload`` (candidate).
2. Normalize: ``_apply_file_truth`` overwrites file-derived fields in ``extraction_metadata``.
3. Validate: ``json_validation.validate_contract_payload`` (repair loop on failure).
4. DB: ``ingest_contract_payload`` (relational inserts; not a raw JSON dump).
"""

from __future__ import annotations

# Product / PM one-liner (matches code order: normalize before validate).
CONTRACT_INGEST_PRODUCT_FLOW = (
    "PDF + schema → markdown + candidate JSON → normalized (file truth), validated payload → DB inserts"
)

# (stage_id, title, implementation hint)
CONTRACT_INGEST_STAGES: tuple[tuple[str, str, str], ...] = (
    ("extract", "PDF + schema → markdown + candidate JSON", "llm_extract.extract_markdown_and_payload_async"),
    ("normalize", "File-derived extraction_metadata", "pipeline._apply_file_truth"),
    ("validate", "JSON Schema + repair loop", "json_validation.validate_contract_payload"),
    ("db", "Relational ingest", "ingest.ingest_contract_payload"),
)
