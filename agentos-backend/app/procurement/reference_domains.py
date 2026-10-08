"""Procurement reference `domain` keys and server-search policy."""

from __future__ import annotations

# Non-SAP policy master — JSON + SQL in ``app/procurement/data/``; never overwritten by SAP sync.
FIXED_MASTER_DOMAINS: frozenset[str] = frozenset(
    {
        "company_code",
        "purchasing_org",
        "currency",
        "payment_terms",
        "purchasing_doc_type",
        "order_unit",
        "account_assignment_category",
        "item_category",
    }
)

# Domains synced from SAP OData — ``scripts/sync_pr_po_reference_from_sap.py``.
SAP_MASTER_DOMAINS: frozenset[str] = frozenset(
    {
        "material",
        "service",
        "vendor",
        "plant",
        "storage_location",
        "cost_center",
        "asset",
        "tax_code",
        "material_group",
        "service_group",
        "purchasing_group",
    }
)

SEARCHABLE_REFERENCE_DOMAINS: frozenset[str] = frozenset(
    {"material", "vendor", "service", "cost_center", "asset"}
)

# Default / max page size for GET /reference-values/search (industry-style).
REFERENCE_SEARCH_DEFAULT_LIMIT: int = 25
REFERENCE_SEARCH_MAX_LIMIT: int = 100
