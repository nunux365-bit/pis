"""Startup data: admin user, integration health rows."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    AutomationRule,
    AutomationSuggestion,
    CatalogAgent,
    CatalogSkill,
    CatalogSkillCategory,
    IntegrationHealth,
    Notification,
    PrPoReferenceValue,
    User,
    UserRole,
    WorkflowDefinition,
)
from app.procurement.reference_fixed_loader import load_fixed_master
from app.seed.catalog_seed import SKILL_SEED_BY_CATEGORY, agent_seed_dicts
from app.seed.workflow_definitions_seed import workflow_definition_seed_dicts
from app.security.passwords import hash_password

log = logging.getLogger(__name__)

DEFAULT_INTEGRATIONS = [
    ("SAP S/4HANA", "ERP", "FI, MM, SD"),
    ("Darwinbox", "HRMS", "Employee master, leave, payroll inputs"),
    ("TrueIn", "Attendance", "210 sites daily export"),
    ("ODIN", "Warehouse", "Inventory, lots, temperature logs"),
    ("Google Workspace", "Productivity", "Gmail, Drive, Sheets, Calendar"),
    ("Qdrant", "Vector DB", "Policies, SOPs, rate cards (dept-filtered)"),
]


async def ensure_bootstrap_data(session: AsyncSession) -> None:
    n_users = await session.scalar(select(func.count()).select_from(User))
    if n_users == 0:
        email = (settings.bootstrap_admin_email or "").strip().lower()
        password = settings.bootstrap_admin_password or ""
        if email and password:
            u = User(
                email=email,
                hashed_password=hash_password(password),
                full_name="System Administrator",
                department="Engineering",
                roles=[UserRole.SYSTEM_ADMIN.value],
            )
            session.add(u)
            await session.flush()
            session.add(
                Approval(
                    assignee_user_id=u.id,
                    created_by_user_id=u.id,
                    title="Sample — Vendor payment review (seed)",
                    description="Remove after UAT. MedPlus Logistics — exceeds threshold.",
                    status=ApprovalStatus.PENDING.value,
                    amount=Decimal("1820000"),
                    confidence=78,
                    risk="medium",
                    agent_name="Finance Agent",
                )
            )
            session.add(
                Notification(
                    user_id=u.id,
                    category="approval",
                    title="Sample approval in your queue",
                    body="Seed data from bootstrap.",
                    link="/o2c/ohc-mis",
                )
            )
            session.add(
                AutomationRule(
                    user_id=u.id,
                    name="Trusted vendor invoices under ₹1L",
                    trigger_description="After Invoice 3-Way Match + GST OK",
                    enabled=True,
                    confidence_threshold=96,
                    execution_count=842,
                )
            )
            session.add(
                AutomationSuggestion(
                    user_id=u.id,
                    title="Auto-approve Cipla invoices under ₹1L",
                    pattern_summary="47/47 matching approvals detected",
                    est_savings="~6.2 hrs / month",
                    status="suggested",
                )
            )
            log.warning("Bootstrap admin created: %s (change password immediately)", email)
        else:
            if settings.allow_registration:
                log.info("No users yet — registration is open.")
            else:
                log.warning(
                    "No users in database. Set bootstrap_admin_email + "
                    "bootstrap_admin_password in .env or enable allow_registration."
                )

    n_int = await session.scalar(select(func.count()).select_from(IntegrationHealth))
    if n_int == 0:
        now = datetime.now(UTC)
        for name, stype, mods in DEFAULT_INTEGRATIONS:
            session.add(
                IntegrationHealth(
                    name=name,
                    system_type=stype,
                    status="connected",
                    health_pct=Decimal("99.0"),
                    modules=mods,
                    last_sync_at=now,
                    last_error=None,
                )
            )
        log.info("Seeded %s integration health rows.", len(DEFAULT_INTEGRATIONS))

    n_agents = await session.scalar(select(func.count()).select_from(CatalogAgent))
    if n_agents == 0:
        for row in agent_seed_dicts():
            session.add(CatalogAgent(**row))
        log.info("Seeded catalog_agents from reference data.")

    n_skill_cats = await session.scalar(select(func.count()).select_from(CatalogSkillCategory))
    if n_skill_cats == 0:
        for ci, (cat_name, skills) in enumerate(SKILL_SEED_BY_CATEGORY):
            cat = CatalogSkillCategory(name=cat_name, sort_order=ci)
            session.add(cat)
            await session.flush()
            for slug, title, version, so in skills:
                session.add(
                    CatalogSkill(
                        category_id=cat.id,
                        slug=slug,
                        title=title,
                        version=version,
                        sort_order=so,
                    )
                )
        log.info("Seeded catalog skill categories and skills.")

    n_wfd = await session.scalar(select(func.count()).select_from(WorkflowDefinition))
    if n_wfd == 0:
        for row in workflow_definition_seed_dicts():
            session.add(WorkflowDefinition(**row))
        log.info("Seeded %s composable workflow_definitions.", len(workflow_definition_seed_dicts()))

    try:
        n_prpo = await session.scalar(select(func.count()).select_from(PrPoReferenceValue))
        if n_prpo == 0:
            result = await load_fixed_master(dry_run=False)
            log.info(
                "Loaded fixed procurement reference master: %s JSON rows (%s inserted, %s updated). "
                "Run SAP sync for catalogue domains.",
                result["json_rows"],
                result["inserted"],
                result["updated"],
            )
    except Exception as e:
        log.debug("pr_po_reference_values fixed master load skipped: %s", e)

    # ─── Seed Payroll Configuration, Routing Matrix & Users ───
    # (Fully handled via Alembic migrations)

    # ── SBI MIS bootstrap ─────────────────────────────────────────────────
    if settings.sbi_mis_enabled:
        from app.sbi_mis import format_spec as _fs
        from app.sbi_mis import db as _sbi_db

        # Seed default client (SBI) if missing
        clients = await _sbi_db.list_clients(session)
        if not clients:
            from sqlalchemy.dialects.postgresql import insert as _pg_insert
            from app.sbi_mis.models import SbiClient as _SbiClient
            stmt = (
                _pg_insert(_SbiClient)
                .values(code="SBI", display_name="SBI (Pharmacy + AHC)", active=True)
                .on_conflict_do_nothing(constraint="uq_sbi_client_code")
            )
            await session.execute(stmt)

        # Seed default rules from format_spec (INSERT OR IGNORE pattern)
        for r in _fs.seed_rules():
            await _sbi_db.upsert_rule_if_missing(
                session, r["sheet"], r["column_letter"],
                r["rule_type"], r.get("config_json", {}), r.get("notes", "")
            )

        await session.commit()
