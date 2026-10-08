"""Daily schema canary — required tables exist (sync SQLAlchemy)."""

import logging

from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)

# Keep aligned with Alembic migrations / core product tables.
REQUIRED_TABLES = (
    "users",
    "workflow_runs",
    "approvals",
    "audit_logs",
    "post_hitl_outbox",
    "user_oauth_tokens",
    "workflow_definitions",
    "catalog_agents",
    "catalog_skills",
    "integration_health",
)


def run_schema_canary_job() -> None:
    from app.config.settings import settings

    eng = create_engine(settings.database_url_sync, pool_pre_ping=True)
    missing: list[str] = []
    try:
        with eng.connect() as conn:
            for t in REQUIRED_TABLES:
                fq = f"public.{t}"
                reg = conn.execute(text("SELECT to_regclass(:n)"), {"n": fq}).scalar()
                if not reg:
                    missing.append(t)
    except Exception:
        log.exception("schema_canary: could not connect or query")
        return
    finally:
        eng.dispose()

    if missing:
        log.error("schema_canary: missing tables %s", missing)
    else:
        log.info("schema_canary: all %s tables present", len(REQUIRED_TABLES))
