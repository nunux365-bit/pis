"""Sanity checks for ``pr_po_reference_values`` (UI pickers, scoped search, SAP forms)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.field_schema import (
    DOCUMENT_TYPE_CODES,
    block_field_specs,
    header_field_specs,
)
from app.procurement.plant_org import plant_matches_purchasing_org
from app.procurement.reference_domains import SEARCHABLE_REFERENCE_DOMAINS
from app.procurement.reference_query import (
    _material_group_has_workflow_materials_predicate,
    _search_base_where,
    _storage_location_plant_predicate,
    workflow_material_product_types,
)
from app.procurement.reference_domains import FIXED_MASTER_DOMAINS

WORKSHOP_ORGS: tuple[str, ...] = ("1MGH", "1MGT", "1LFS")
WORKFLOW_DOC_TYPES: tuple[str, ...] = DOCUMENT_TYPE_CODES


@dataclass
class ValidationIssue:
    level: str  # error | warn
    check: str
    message: str


@dataclass
class ValidationReport:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def error(self, check: str, message: str) -> None:
        self.issues.append(ValidationIssue("error", check, message))
        self.ok = False

    def warn(self, check: str, message: str) -> None:
        self.issues.append(ValidationIssue("warn", check, message))


def _collect_reference_domains() -> set[str]:
    domains: set[str] = set()
    for dt in WORKFLOW_DOC_TYPES:
        for spec in header_field_specs(dt) + block_field_specs(dt):
            if spec.reference_domain:
                domains.add(spec.reference_domain)
        from app.procurement.field_schema import allocation_field_specs

        for spec in allocation_field_specs(dt):
            if spec.reference_domain:
                domains.add(spec.reference_domain)
    return domains


async def validate_reference_master(session: AsyncSession) -> ValidationReport:
    report = ValidationReport(ok=True)

    # --- domain counts ---
    counts: dict[str, int] = {}
    res = await session.execute(
        select(PrPoReferenceValue.domain, func.count())
        .group_by(PrPoReferenceValue.domain)
    )
    for dom, n in res:
        counts[str(dom)] = int(n)

    for dom in sorted(_collect_reference_domains()):
        n = counts.get(dom, 0)
        if n == 0:
            report.error("ui_domain", f"domain {dom!r} has 0 rows (required by field_schema)")

    for dom in sorted(FIXED_MASTER_DOMAINS):
        n = counts.get(dom, 0)
        if n == 0:
            report.error("fixed_master", f"fixed domain {dom!r} has 0 rows (load reference_fixed_master.json)")

    # --- orgs / currency ---
    for org in WORKSHOP_ORGS:
        for dom in ("company_code", "purchasing_org"):
            res = await session.execute(
                select(func.count()).where(
                    PrPoReferenceValue.domain == dom,
                    PrPoReferenceValue.code == org,
                )
            )
            if res.scalar_one() == 0:
                report.error("workshop_org", f"missing {dom} code {org!r}")

    res = await session.execute(
        select(func.count()).where(
            PrPoReferenceValue.domain == "currency",
            PrPoReferenceValue.code == "INR",
        )
    )
    if res.scalar_one() == 0:
        report.error("currency", "missing currency INR")

    # --- workflow purchasing_doc_type ---
    for dt in WORKFLOW_DOC_TYPES:
        for kind in ("PR", "PO"):
            res = await session.execute(
                select(func.count()).where(
                    PrPoReferenceValue.domain == "purchasing_doc_type",
                    PrPoReferenceValue.code == dt,
                    PrPoReferenceValue.applies_to_kind == kind,
                )
            )
            if res.scalar_one() == 0:
                report.error(
                    "workflow_doc_type",
                    f"missing purchasing_doc_type {dt!r} applies_to_kind={kind!r}",
                )

    # --- item categories ---
    for code, label in (("", "Standard"), ("D", "Service")):
        res = await session.execute(
            select(func.count()).where(
                PrPoReferenceValue.domain == "item_category",
                PrPoReferenceValue.code == code,
            )
        )
        if res.scalar_one() == 0:
            report.error("item_category", f"missing item_category code {code!r} ({label})")

    # --- SAP-synced catalogue minimums ---
    sap_mins: dict[str, int] = {
        "material": 100,
        "service": 50,
        "vendor": 1000,
        "plant": 10,
        "storage_location": 100,
        "cost_center": 1000,
        "material_group": 10,
        "service_group": 10,
        "tax_code": 10,
        "purchasing_group": 5,
    }
    for dom, min_n in sap_mins.items():
        if counts.get(dom, 0) < min_n:
            report.error("sap_sync", f"domain {dom!r} has {counts.get(dom, 0)} rows (need >={min_n}; run SAP sync)")

    # --- vendor composite + payt (PO payment terms) ---
    res = await session.execute(
        select(func.count()).where(
            PrPoReferenceValue.domain == "vendor",
            PrPoReferenceValue.code.like("%|%"),
        )
    )
    composite_n = res.scalar_one()
    if composite_n == 0:
        report.error("vendor_composite", "no vendor rows with composite code vendor|company")
    res = await session.execute(
        select(func.count()).where(
            PrPoReferenceValue.domain == "vendor",
            ~PrPoReferenceValue.code.like("%|%"),
        )
    )
    bare_n = res.scalar_one()
    if bare_n > 0:
        report.warn(
            "vendor_legacy_bare",
            f"{bare_n} vendor row(s) use bare supplier code (no |company); "
            "run scripts/cleanup_legacy_bare_vendors.py and periodic full vendor sync",
        )
    res = await session.execute(
        text(
            """
            SELECT count(*) FROM pr_po_reference_values
            WHERE domain = 'vendor' AND code LIKE '%|1MGH'
              AND COALESCE(extra->>'payt', '') <> ''
            """
        )
    )
    if res.scalar_one() == 0:
        report.warn("vendor_payt", "no vendor|1MGH rows with extra.payt (PO auto-fill may fail)")

    # --- storage_location composite ---
    res = await session.execute(
        select(func.count()).where(
            PrPoReferenceValue.domain == "storage_location",
            PrPoReferenceValue.code.like("%|%"),
        )
    )
    if res.scalar_one() == 0:
        report.error("storage_location", "no storage_location rows with plant|sloc composite code")

    # --- cost_center facets ---
    res = await session.execute(
        text(
            """
            SELECT count(*) FROM pr_po_reference_values
            WHERE domain = 'cost_center'
              AND COALESCE(extra->>'Entity', '') <> ''
            """
        )
    )
    cc_entity = res.scalar_one()
    if cc_entity == 0:
        report.error("cost_center_facets", "no cost_center rows with extra.Entity (facet search broken)")

    # --- scoped plant per org ---
    for org in WORKSHOP_ORGS:
        plants = (
            await session.execute(
                select(PrPoReferenceValue.code).where(PrPoReferenceValue.domain == "plant")
            )
        ).scalars().all()
        matching = [p for p in plants if plant_matches_purchasing_org(str(p), org)]
        if not matching:
            report.error("plant_scope", f"no plant rows match purchasing org {org!r}")

    # --- workflow materials (product type filter) ---
    ex = PrPoReferenceValue.extra
    upper_mt = func.upper(func.coalesce(ex["material_type"].as_string(), ""))
    for wf_dt, err_key in (("YUNB", "yunb_materials"), ("YAST", "yast_materials")):
        mtypes = workflow_material_product_types(wf_dt)
        if not mtypes:
            continue
        res = await session.execute(
            select(func.count()).where(
                PrPoReferenceValue.domain == "material",
                upper_mt.in_([m.upper() for m in mtypes]),
            )
        )
        if res.scalar_one() == 0:
            report.error(err_key, f"no materials with product type in {mtypes!r} for {wf_dt}")

    # --- material_group scoped per workflow ---
    for wf_dt, err_key in (
        ("YUNB", "material_group_scope_yunb"),
        ("YAST", "material_group_scope_yast"),
    ):
        res = await session.execute(
            select(func.count()).where(
                PrPoReferenceValue.domain == "material_group",
                _material_group_has_workflow_materials_predicate(wf_dt),
            )
        )
        if res.scalar_one() == 0:
            report.error(
                err_key,
                f"no material_group rows with {wf_dt} catalogue materials",
            )

    # --- scoped search smoke (DB predicates compile + return rows) ---
    for domain in SEARCHABLE_REFERENCE_DOMAINS:
        if domain == "vendor":
            w = _search_base_where(
                domain=domain,
                q="10",
                workflow_document_type="YUNB",
                ticket_kind="PO",
                company_code="1MGH",
            )
        elif domain == "asset":
            w = _search_base_where(
                domain=domain,
                q="00",
                workflow_document_type="YAST",
                ticket_kind="PR",
                company_code="1MGH",
            )
        elif domain == "cost_center":
            w = _search_base_where(
                domain=domain,
                q="",
                workflow_document_type="YSER",
                ticket_kind="PR",
                company_code="1MGH",
                cc_entity="1MGH",
            )
        elif domain == "material":
            w = _search_base_where(
                domain=domain,
                q="30",
                workflow_document_type="YUNB",
                ticket_kind="PR",
                company_code="1MGH",
            )
        else:  # service
            w = _search_base_where(
                domain=domain,
                q="lap",
                workflow_document_type="YSER",
                ticket_kind="PR",
                company_code="1MGH",
            )
        res = await session.execute(select(func.count()).select_from(PrPoReferenceValue).where(w))
        n = res.scalar_one()
        if n == 0:
            report.warn("scoped_search", f"scoped search for {domain!r} returned 0 rows (smoke query)")

    # --- storage_location plant scope ---
    for org in WORKSHOP_ORGS:
        plant_res = await session.execute(
            select(PrPoReferenceValue.code).where(PrPoReferenceValue.domain == "plant")
        )
        plants = [str(p) for p in plant_res.scalars() if plant_matches_purchasing_org(str(p), org)]
        if not plants:
            continue
        plant = sorted(plants)[0]
        w = and_storage = _storage_location_plant_predicate(plant)
        res = await session.execute(
            select(func.count()).where(
                PrPoReferenceValue.domain == "storage_location",
                and_storage,
            )
        )
        if res.scalar_one() == 0:
            report.warn("sloc_plant", f"no storage_location for plant {plant!r} (org {org})")

    return report


def format_report(report: ValidationReport) -> str:
    lines = ["=== Procurement reference master validation ===", f"OK: {report.ok}", ""]
    if not report.issues:
        lines.append("No issues.")
        return "\n".join(lines)
    for issue in report.issues:
        lines.append(f"[{issue.level.upper()}] {issue.check}: {issue.message}")
    return "\n".join(lines)
