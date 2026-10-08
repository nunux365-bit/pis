"""FastAPI application — production middleware, lifespan, routes."""

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.routes import (
    admin_erp_outbox,
    agents,
    analytics,
    approvals,
    dashboard,
    audit_api,
    auth,
    automations_api,
    chat,
    compliance_call as compliance_call_routes,
    order_rca as order_rca_routes,
    responder_eval as responder_eval_routes,
    whatsapp_jit_hold as whatsapp_jit_hold_routes,
    whatsapp_webhook as whatsapp_webhook_routes,
    email_automation as email_automation_routes,
    google_workspace,
    integrations,
    notifications,
    o2c,
    optimus as optimus_routes,
    outreach as outreach_routes,
    prosight as prosight_routes,
    procurement,
    search,
    sessions,
    skills,
    system_health,
    tools_api,
    users_admin,
    workflows,
    payroll,
)
from app.bootstrap import ensure_bootstrap_data
from app.config.settings import settings
from app.agents.o2c_ohc.agenos_async_session import dispose_agenos_async_engine
from app.agents.responder_eval.chat_db import dispose_chat_eval_async_engine
from app.db.session import AsyncSessionLocal, engine, init_db_schema
from app.infra.qdrant_store import ensure_policy_collection_async
from app.infra.redis_client import close_redis, get_redis
from app.agents.whatsapp_jit_hold.workers import start_workers as start_whatsapp_jit_hold_workers
from app.agents.whatsapp_jit_hold.workers import stop_workers as stop_whatsapp_jit_hold_workers
from app.kernel.scheduler import shutdown_scheduler, start_scheduler
from app.procurement.multipart import MAX_PROCUREMENT_TOTAL_BYTES


def _configure_logging() -> None:
    """
    Ensure ``app.*`` loggers emit to stderr.

    Uvicorn/gunicorn configure their own loggers; without this, WhatsApp/Kafka
    ``log.info`` lines (e.g. ``kafka consumer started``) are dropped while
    ``log.exception`` still appears.
    """
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    # Unmapped spaCy NER labels are ignored in presidio_engine; suppress any residual library WARNINGs.
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)


_configure_logging()
log = logging.getLogger(__name__)


async def _restore_sbi_cache() -> None:
    """On startup, reload the last active SBI MIS run into the in-memory pipeline cache.

    Fast path: if the raw file + rules haven't changed since the last run, results
    are loaded from the disk pickle (sub-second).  Slow path: the pipeline is re-run
    in the background exactly as if the user had clicked Activate.
    """
    try:
        from app.sbi_mis import db as _sbi_db, state as _sbi_state
        from app.sbi_mis.engine import ingest as _ingest

        async with AsyncSessionLocal() as session:
            current = await _sbi_db.get_current_run(session)

        if not current or not current.get("month"):
            return  # No previous run — nothing to restore.

        month = current["month"]
        raw_path = _sbi_db.resolve_stored_path(current.get("raw_file_path"))
        if raw_path is None or not raw_path.exists():
            log.warning("SBI MIS restore: raw file not found for month %s — skipping", month)
            return

        # Set active_month BEFORE any awaited I/O so the /status endpoint returns
        # the correct month even while the pickle / pipeline is still loading in
        # the background.  Without this, the dashboard's auto-activate useEffect
        # fires during startup because it sees active_month = null.
        _sbi_state._cache.active_month = month

        import asyncio as _asyncio
        from app.infra.thread_pools import cpu_executor

        loop = _asyncio.get_event_loop()

        # ── Fast path: disk cache hit ────────────────────────────────────────
        # try_restore_from_disk computes the fingerprint (file hash + rules hash)
        # and loads the pickled results if nothing has changed since last run.
        restored = await loop.run_in_executor(
            cpu_executor(), lambda: _sbi_state.try_restore_from_disk(month, raw_path)
        )
        if restored:
            log.info(
                "SBI MIS restore: cache hit for %s — skipped pipeline rerun ✓", month
            )
            return

        # ── Slow path: run pipeline ──────────────────────────────────────────
        log.info("SBI MIS restore: loading %s for month %s…", raw_path.name, month)
        df = await loop.run_in_executor(cpu_executor(), lambda: _ingest.load_raw(raw_path))
        _sbi_state._cache.dump_df = df
        _sbi_state._cache.active_month = month
        _sbi_state._cache.raw_file_path = raw_path
        log.info("SBI MIS restore: %d rows loaded — running pipeline…", len(df))
        await _sbi_state.do_one_rerun()
        log.info("SBI MIS restore: pipeline complete for month %s", month)
    except Exception:
        log.exception("SBI MIS cache restore failed (non-fatal)")

# Cheap gate at the edge: reject obviously oversized uploads (headers only) before
# ASGI begins streaming the body. The 16 MB non-upload default keeps JSON PATCH /
# create bodies bounded; procurement multipart routes use the larger cap from
# :mod:`app.procurement.multipart` (see ``_UPLOAD_PATH_PREFIXES``).
_MAX_JSON_BODY_BYTES = 16 * 1024 * 1024
_UPLOAD_PATH_PREFIXES: tuple[str, ...] = (
    "/api/procurement/tickets/pr",
    "/api/procurement/tickets/po",
    "/api/procurement/tickets/",  # matches ``.../tickets/{id}/attachments/...`` download paths
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.skip_create_all_on_startup:
        # create_all is additive (no drops); new ORM tables get created on first boot.
        await init_db_schema()
    try:
        r = get_redis()
        await r.ping()
    except Exception as e:
        log.warning("Redis unavailable: %s — rate limiting degraded", e)
    try:
        await ensure_policy_collection_async()
    except Exception as e:
        log.warning("Qdrant ensure collection: %s", e)
    try:
        async with AsyncSessionLocal() as session:
            await ensure_bootstrap_data(session)
            await session.commit()
            
            # Sync matrix roles to users table on startup
            from app.api.routes.payroll import sync_matrix_roles_to_users
            await sync_matrix_roles_to_users(session)
    except Exception as e:
        log.exception("Bootstrap failed: %s", e)
    if settings.sbi_mis_enabled:
        from pathlib import Path as _Path
        _sbi_data = _Path(settings.sbi_mis_data_dir).expanduser().resolve()
        _sbi_data.mkdir(parents=True, exist_ok=True)
        (_sbi_data / "uploads").mkdir(exist_ok=True)
        (_sbi_data / "outputs").mkdir(exist_ok=True)
        # Restore the last active run from DB so the pipeline is warm after restart.
        # Runs in the background — server is immediately available while it loads.
        from app.infra.task_tracker import spawn

        spawn(_restore_sbi_cache(), name="sbi-cache-restore")
    # Initialize MLflow for LLM tracing
    if settings.llm_tracing_enabled:
        try:
            from app.observability import init_mlflow
            init_mlflow()
        except Exception as e:
            log.warning("MLflow init failed: %s — LLM tracing disabled", e)
    try:
        from app.agents.responder_eval.startup import validate_responder_eval_settings

        validate_responder_eval_settings()
    except Exception as e:
        log.exception("Responder eval settings invalid: %s", e)
        raise
    if settings.run_background_jobs:
        log.info(
            "background jobs starting scheduler=yes whatsapp_jit_hold_enabled=%s",
            settings.whatsapp_jit_hold_enabled,
        )
        start_scheduler()
        await start_whatsapp_jit_hold_workers()
    else:
        log.info("RUN_BACKGROUND_JOBS=false — scheduler and in-process workers not started")
    yield
    if settings.run_background_jobs:
        await stop_whatsapp_jit_hold_workers()
        shutdown_scheduler()
    try:
        from app.infra.task_tracker import shutdown_spawned_tasks

        await shutdown_spawned_tasks(grace_sec=5)
    except Exception:
        log.debug("spawned task shutdown failed", exc_info=True)
    try:
        from app.infra.thread_pools import shutdown_executors

        await shutdown_executors()
    except Exception:
        log.debug("executor shutdown failed", exc_info=True)
    await dispose_chat_eval_async_engine()
    await engine.dispose()
    await dispose_agenos_async_engine()
    await close_redis()
    try:
        from app.infra.httpx_clients import close_shared_http_clients

        await close_shared_http_clients()
    except Exception as e:
        log.debug("Shared httpx clients cleanup: %s", e)
    try:
        from app.infra.openai_async_client import close_shared_openai_client

        await close_shared_openai_client()
    except Exception as e:
        log.debug("Shared OpenAI client cleanup: %s", e)
    try:
        from app.agents.optimus.smartqna.openai_client import close_async_openai_client

        await close_async_openai_client()
    except Exception as e:
        log.debug("SmartQnA OpenAI client cleanup: %s", e)
    try:
        from app.agents.optimus.smartqna.retriever import close_async_qdrant_client

        await close_async_qdrant_client()
    except Exception as e:
        log.debug("SmartQnA Qdrant client cleanup: %s", e)
    try:
        from app.infra.qdrant_store import close_async_qdrant

        await close_async_qdrant()
    except Exception as e:
        log.debug("Policy Qdrant client cleanup: %s", e)
    try:
        from app.infra.mysql_compliance import dispose_mysql_compliance_engine

        await dispose_mysql_compliance_engine()
    except Exception as e:
        log.debug("MySQL compliance engine cleanup: %s", e)
    # Graceful shutdown for Optimus clients
    try:
        from app.agents.optimus.flock.api_client import close_http_client
        await close_http_client()
    except Exception as e:
        log.debug("Optimus Flock HTTP client cleanup: %s", e)


app = FastAPI(
    title=settings.app_name,
    description="Agent Operating System — LangGraph agents, skills, integrations",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def enforce_body_size(request: Request, call_next):
    """Reject requests whose advertised ``Content-Length`` is above the configured cap.

    This is a cheap pre-flight check before we begin reading multipart bodies — it does
    **not** replace per-file caps in :func:`app.procurement.multipart.read_upload_files`,
    which still enforce limits while streaming (in case ``Content-Length`` is absent or
    clients misreport it).
    """
    cl = request.headers.get("content-length")
    if cl:
        try:
            size = int(cl)
        except ValueError:
            size = -1
        if size >= 0:
            path = request.url.path or ""
            # Multipart ticket PATCH (form + files) uses the same path as JSON PATCH; allow large bodies.
            _patch_ticket_multipart = (
                request.method.upper() == "PATCH"
                and re.fullmatch(
                    r"/api/procurement/tickets/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                    path,
                )
                is not None
            )
            is_upload = _patch_ticket_multipart or any(
                path.startswith(p) and ("/attachments" in path or path.endswith("/pr") or path.endswith("/po"))
                for p in _UPLOAD_PATH_PREFIXES
            )
            limit = MAX_PROCUREMENT_TOTAL_BYTES if is_upload else _MAX_JSON_BODY_BYTES
            if size > limit:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={
                        "detail": f"Request body exceeds {limit // (1024 * 1024)} MB.",
                        "request_id": getattr(request.state, "request_id", None),
                    },
                )
    return await call_next(request)


@app.exception_handler(IntegrityError)
async def integrity_handler(request: Request, exc: IntegrityError):
    log.warning("integrity: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": "Data conflict (duplicate or FK violation)",
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # Pydantic v2 may put Exception instances in error ``ctx``; JSONResponse must not embed raw exceptions.
    payload = {
        "detail": exc.errors(),
        "request_id": getattr(request.state, "request_id", None),
    }
    safe = json.loads(json.dumps(payload, default=str))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=safe,
    )


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Unexpected errors — no stack traces to clients; request_id for log correlation."""
    rid = getattr(request.state, "request_id", None)
    log.exception("unhandled error request_id=%s", rid)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "request_id": rid,
        },
    )


_cors_allow_all = settings.cors_allow_all or settings.cors_origins == ["*"]
if _cors_allow_all:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Public / low-friction
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(system_health.router, prefix="/api/health", tags=["health"])

# Authenticated domain APIs
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
app.include_router(approvals.router, prefix="/api/approvals", tags=["approvals"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(skills.router, prefix="/api/skills", tags=["skills"])
app.include_router(integrations.router, prefix="/api/integrations", tags=["integrations"])
app.include_router(
    google_workspace.router,
    prefix="/api/integrations/google",
    tags=["integrations-google"],
)
app.include_router(tools_api.router, prefix="/api/tools", tags=["tools"])
app.include_router(automations_api.router, prefix="/api/automations", tags=["automations"])
app.include_router(audit_api.router, prefix="/api/audit", tags=["audit"])
app.include_router(users_admin.router, prefix="/api/admin/users", tags=["admin-users"])
app.include_router(admin_erp_outbox.router, prefix="/api/admin", tags=["admin"])
app.include_router(o2c.router, prefix="/api/o2c", tags=["o2c"])
app.include_router(procurement.router, prefix="/api/procurement", tags=["procurement"])
app.include_router(
    email_automation_routes.router,
    prefix="/api/email-automation",
    tags=["email-automation"],
)
app.include_router(
    compliance_call_routes.router,
    prefix="/api",
    tags=["compliance"],
)
app.include_router(order_rca_routes.router, prefix="/api")
app.include_router(order_rca_routes.internal_router, prefix="/api")
app.include_router(responder_eval_routes.router, prefix="/api")
app.include_router(whatsapp_jit_hold_routes.router, prefix="/api")
app.include_router(whatsapp_webhook_routes.router)
app.include_router(outreach_routes.router, prefix="/api/outreach", tags=["outreach"])
app.include_router(optimus_routes.router, prefix="/api/optimus", tags=["optimus"])
app.include_router(prosight_routes.router, tags=["prosight"])  # Already has /api/prosight prefix
app.include_router(payroll.router, prefix="/api/workflow", tags=["payroll"])

# SBI MIS — feature-flagged; only mount when SBI_MIS_ENABLED=true
if settings.sbi_mis_enabled:
    from app.api.routes import sbi_mis as _sbi_mis_router
    app.include_router(_sbi_mis_router.router,
                       prefix="/api/v1/sbi-mis",
                       tags=["SBI MIS"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentos-backend"}
