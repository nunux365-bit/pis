"""
Canonical seed rows for `catalog_*` tables — inserted at bootstrap only.
Runtime APIs read from the database, not from this module.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# (name, status, ring, description, tasks_today, uptime_pct, sort_order)
AGENT_SEED_ROWS: list[tuple[str, str, int, str, int, Decimal | None, int]] = [
    (
        "Orchestrator",
        "active",
        2,
        "Cross-department decomposition, LangGraph supervisor pattern.",
        12,
        Decimal("99.950"),
        0,
    ),
    (
        "Finance Agent",
        "active",
        2,
        "Invoices, GST, rate cards, vendor compliance, payment runs.",
        47,
        Decimal("99.900"),
        1,
    ),
    (
        "HR Agent",
        "idle",
        2,
        "Onboarding, payroll reconciliation, leave, policy Q&A.",
        18,
        Decimal("99.700"),
        2,
    ),
    (
        "Supply Chain Agent",
        "idle",
        2,
        "Expiry, cold chain, multi-warehouse transfer, PO generation.",
        31,
        Decimal("99.600"),
        3,
    ),
    (
        "IT Agent",
        "idle",
        2,
        "Ticket triage, access provisioning/revocation, SaaS license audit.",
        9,
        Decimal("99.800"),
        4,
    ),
    (
        "Admin Agent",
        "idle",
        2,
        "Expenses, maintenance, contracts, travel policy.",
        6,
        Decimal("99.500"),
        5,
    ),
    (
        "Compliance Agent",
        "idle",
        2,
        "Cross-cutting regulatory checks — FSSAI, DCGI, GST compliance.",
        4,
        Decimal("99.900"),
        6,
    ),
    (
        "Cold Outreach Agent",
        "active",
        2,
        "Weekly cold email dispatch across all active campaigns, with daily GSheet sync.",
        0,
        Decimal("99.900"),
        7,
    ),
    (
        "Optimus Agent",
        "active",
        2,
        "Intelligence platform — SmartQnA (RAG), Prosight (analytics), QuickML (no-code ML), TextToWorkflow.",
        0,
        Decimal("99.900"),
        8,
    ),
]


def _slug(s: str) -> str:
    return (
        s.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("(", "")
        .replace(")", "")
    )


def agent_seed_dicts() -> list[dict[str, Any]]:
    out = []
    for name, status, ring, desc, tasks, uptime, order in AGENT_SEED_ROWS:
        out.append(
            {
                "slug": _slug(name),
                "name": name,
                "status": status,
                "ring": ring,
                "description": desc,
                "tasks_today": tasks,
                "uptime_pct": uptime,
                "sort_order": order,
            }
        )
    return out


# category_name -> list of (skill_slug, title, version, sort_order)
SKILL_SEED_BY_CATEGORY: list[tuple[str, list[tuple[str, str, str, int]]]] = [
    (
        "Finance",
        [
            ("invoice-3way-match", "Invoice 3-Way Match", "2.1", 0),
            ("rate-card-comparison", "Rate Card Comparison", "1.0", 1),
            ("gst-computation", "GST Computation", "1.0", 2),
            ("gst-return-prep", "GST Return Preparation", "1.0", 3),
            ("vendor-compliance-score", "Vendor Compliance Score", "1.0", 4),
            ("payment-run-validation", "Payment Run Validation", "1.0", 5),
            ("itc-reconciliation", "ITC Reconciliation", "1.0", 6),
        ],
    ),
    (
        "HR",
        [
            ("onboarding-sequence", "Onboarding Sequence", "1.0", 0),
            ("offboarding-sequence", "Offboarding Sequence", "1.0", 1),
            ("payroll-reconciliation", "Payroll Reconciliation", "1.0", 2),
            ("leave-policy-check", "Leave Policy Check", "1.0", 3),
            ("policy-qa", "Policy Q&A", "1.0", 4),
            ("fnf-settlement", "F&F Settlement Calculation", "1.0", 5),
            ("attendance-discrepancy", "Attendance Discrepancy", "1.0", 6),
        ],
    ),
    (
        "Supply Chain",
        [
            ("expiry-risk", "Expiry Risk Assessment", "1.0", 0),
            ("demand-forecast", "Demand Forecast", "1.0", 1),
            ("cold-chain-alert", "Cold Chain Alert", "1.0", 2),
            ("multi-wh-transfer", "Multi-Warehouse Transfer", "1.0", 3),
            ("inventory-reconciliation", "Inventory Reconciliation", "1.0", 4),
            ("supplier-performance", "Supplier Performance Score", "1.0", 5),
            ("po-generation", "PO Generation", "1.0", 6),
        ],
    ),
    (
        "IT",
        [
            ("ticket-classification", "Ticket Classification", "1.0", 0),
            ("access-provisioning", "Access Provisioning", "1.0", 1),
            ("access-revocation", "Access Revocation", "1.0", 2),
            ("known-issue-resolution", "Known Issue Resolution", "1.0", 3),
            ("saas-license-audit", "SaaS License Audit", "1.0", 4),
            ("infra-alert-triage", "Infrastructure Alert Triage", "1.0", 5),
        ],
    ),
    (
        "Admin",
        [
            ("expense-validation", "Expense Validation", "1.0", 0),
            ("maintenance-scheduling", "Maintenance Scheduling", "1.0", 1),
            ("contract-renewal-check", "Contract Renewal Check", "1.0", 2),
            ("travel-policy-compliance", "Travel Policy Compliance", "1.0", 3),
        ],
    ),
    (
        "Cross-Cutting",
        [
            ("document-parsing", "Document Parsing", "1.0", 0),
            ("notification-dispatch", "Notification Dispatch", "1.0", 1),
            ("anomaly-detection", "Anomaly Detection", "1.0", 2),
            ("report-generation", "Report Generation", "1.0", 3),
            ("data-freshness-check", "Data Freshness Check", "1.0", 4),
            ("schema-drift-detection", "Schema Drift Detection", "1.0", 5),
            ("employee-context-build", "Employee Context Build", "1.0", 6),
            ("morning-briefing", "Morning Briefing", "1.0", 7),
            ("pattern-learning", "Pattern Learning", "1.0", 8),
        ],
    ),
    (
        "Optimus",
        [
            ("smartqna-rag", "SmartQnA RAG", "1.0", 0),
            ("smartqna-crag", "SmartQnA CRAG Pipeline", "1.0", 1),
            ("document-ingestion", "Document Ingestion", "1.0", 2),
            ("adaptive-retrieval", "Adaptive Retrieval", "1.0", 3),
            ("query-classification", "Query Classification", "1.0", 4),
            ("prosight-analytics", "Prosight Analytics", "0.1", 5),
            ("quickml-builder", "QuickML Builder", "0.1", 6),
            ("text-to-workflow", "Text to Workflow", "0.1", 7),
        ],
    ),
]
