"""Application settings — production-oriented defaults."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse, urlunparse

from typing import Literal, Self

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# agentos-backend/app/config/settings.py → parents[2] == agentos-backend (repo backend root)
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _hydrate_environ_from_dotenv() -> None:
    """
    Load ``.env`` into ``os.environ`` *before* ``BaseSettings`` runs.

    Pydantic's built-in ``env_file`` can miss values depending on cwd and how paths are
    resolved; pushing vars into the environment matches the usual "docker / shell" model:
    **real process environment wins** (``override=False`` on the backend file), then an
    optional **second** ``.env`` in cwd overrides when it is a different file.

    Keys not present in any ``.env`` or the OS environment keep field **defaults** below.

    The **backend root** ``.env`` uses ``override=True`` so new keys (e.g. ``O2C_*``) are not
    shadowed by empty placeholders already in ``os.environ`` from the shell or IDE; production
    can still rely on real process env when no repo ``.env`` is deployed.
    """
    root_env = _BACKEND_ROOT / ".env"
    cwd_env = Path.cwd() / ".env"

    if root_env.is_file():
        load_dotenv(root_env, override=True)
    if cwd_env.is_file() and cwd_env.resolve() != root_env.resolve():
        load_dotenv(cwd_env, override=True)
    elif not root_env.is_file() and cwd_env.is_file():
        load_dotenv(cwd_env, override=False)


_hydrate_environ_from_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Dotenv already merged into os.environ above; read env only (no second env_file pass).
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # App
    app_name: str = "1mg AgentOS API"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # When true, allow any browser Origin (reflected; works with allow_credentials). Set false and
    # set CORS_ORIGINS in production if you want an explicit allowlist.
    cors_allow_all: bool = True

    # Security
    jwt_secret_key: str = "CHANGE_ME_IN_PRODUCTION_USE_OPENSSL_RAND_HEX_32"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    # Browser auth: HttpOnly cookies (set AUTH_COOKIE_SECURE=true on HTTPS).
    auth_cookie_secure: bool = False
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    bcrypt_rounds: int = 12
    allow_registration: bool = False
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    # When false (default), missing ORM tables are created on startup via create_all.
    # Set true if the database is managed only by external migrations / manual DDL.
    skip_create_all_on_startup: bool = False

    # Database (async SQLAlchemy)
    database_url: str = (
        "postgresql+asyncpg://pankaj:pankaj@localhost:5432/agentos"
    )
    # Alembic / sync scripts (psycopg2)
    database_url_sync: str = "postgresql://pankaj:pankaj@localhost:5432/agentos"

    # O2C_OHC agent — optional separate DB; when unset/empty, uses ``database_url_sync``.
    agenos_database_url_sync: str = ""
    agenos_connect_timeout_seconds: int = 15

    @model_validator(mode="after")
    def _coalesce_agenos_database_url_sync(self) -> Self:
        if not (self.agenos_database_url_sync or "").strip():
            self.agenos_database_url_sync = self.database_url_sync
        return self
    o2c_contracts_root: str = ""
    # Google Drive O2C (optional). Parent folder id/url; site folder name from O2C_CONTRACTS_ROOT.
    # When true, list PDFs under the parent recursively (all site subfolders); leave O2C_CONTRACTS_ROOT
    # empty unless you want single-site (non-empty root overrides and targets one child folder).
    o2c_gdrive_contracts_all_sites: bool = False
    o2c_gdrive_contracts_parent_folder_id: str = ""
    # Stable DB key for contract_document.ingestion_root when using Drive (not Path.resolve).
    o2c_contracts_ingestion_root_db: str = ""
    o2c_gdrive_service_account_json: str = ""  # optional; else GOOGLE_* env vars
    o2c_gdrive_attendance_sheet_url_or_id: str = ""  # native Sheet → temp xlsx when local path empty
    o2c_gdrive_mis_parent_folder_id: str = ""  # MIS xlsx upload: parent / site / month layout
    # After successful MIS upload to Drive, remove local xlsx and set DB xlsx_path to gdrive://file/{id}
    o2c_gdrive_keep_local_mis_xlsx: bool = False
    # Transient Drive API failures (429 / 5xx / transport): retry count and exponential backoff base (seconds).
    o2c_gdrive_api_max_retries: int = Field(default=3, ge=0)
    o2c_gdrive_api_retry_base_seconds: float = Field(default=0.75, ge=0.05)
    o2c_contract_schema_path: str = ""  # default in code paths: bundled schema if empty
    o2c_attendance_xlsx_path: str = ""
    # In-memory TTL for parsed attendance workbook (Drive export + parse). 0 disables.
    o2c_attendance_workbook_cache_ttl_seconds: int = Field(default=3600, ge=0)
    o2c_attendance_column_map_json: str = ""
    # Pivot + slicer parsing: column header or slicer sourceName for client/site (optional)
    o2c_attendance_client_site_field: str = ""
    # 0-based index of the slicer that is the client–site dropdown (second of three); optional
    o2c_attendance_client_site_slicer_index: int | None = None
    # Google-Sheet-style Summary: sheet name, and exact strings for “All” on 1st / 3rd dropdowns
    o2c_attendance_summary_sheet: str = "Summary"
    o2c_attendance_all_period_label: str = "All"
    o2c_attendance_all_roll_label: str = "All"
    # Manpower → attendance ``clinical_role_hint`` (Employee ID → Specialist); excel_reader parsing.
    o2c_manpower_clinical_hint_enabled: bool = True
    o2c_manpower_sheet_name: str = "Manpower"
    o2c_manpower_use_designation_for_clinical_hint: bool = True
    o2c_manpower_backfill_doj_lwd: bool = True
    o2c_manpower_add_missing_roll_rows: bool = True
    o2c_invoice_template_xlsx_path: str = ""
    # OpenAI O2C extract: full PDF only (Responses API input_file).
    # HTTP timeout for the OpenAI client (PDF upload bytes + long Responses). Upload time scales
    # with file size and uplink; raise this if proxies or large PDFs cause ReadTimeout/WriteTimeout.
    o2c_openai_http_timeout_seconds: float = Field(default=1200.0, ge=30.0)
    # Responses API token ceiling (includes reasoning tokens on o-series / gpt-5-class models).
    o2c_llm_max_output_tokens: int = 100000
    # When true, embed contract_ingestion_schema.json in the LLM prompt (stronger shape guidance; ~25k+ chars).
    o2c_llm_include_json_schema_in_prompt: bool = True
    # Vector indexing for contract markdown chunks (Qdrant collection: agentos).
    qdrant_timeout_seconds: int = 30
    qdrant_collection_agentos: str = "agentos"
    o2c_embedding_model: str = "text-embedding-3-small"
    o2c_embedding_dimensions: int = 1536
    o2c_embedding_batch_size: int = 64
    o2c_markdown_chunk_size: int = 2000
    # Match clinical-intelligence enhanced_ingestion RecursiveCharacterTextSplitter overlap.
    o2c_markdown_chunk_overlap: int = 400
    o2c_markdown_min_chunk_chars: int = 80
    # Transient failures (rate limits, Qdrant timeouts): retries for embed + upsert in contract indexing.
    o2c_vector_index_retries: int = 3
    o2c_vector_index_retry_base_seconds: float = 0.75
    # When attendance site label has no site_alias, call LLM to match service_site and insert alias
    o2c_llm_site_match_enabled: bool = True
    o2c_llm_repair_attempts: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "o2c_llm_repair_attempts",
            "O2C_LLM_REPAIR_ATTEMPTS",
            "o2c_max_llm_retries",
            "O2C_MAX_LLM_RETRIES",
        ),
    )
    # MIS LLM arithmetic via OpenAI Responses tool-calling (calculator + macros).
    o2c_mis_enable_calculator_tool: bool = True
    o2c_mis_max_tool_calls: int = Field(default=1200, ge=1, le=5000)
    # OpenAI reproducibility: Chat Completions fallback only (Responses API has no seed). Unset = omit.
    o2c_mis_openai_seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # MIS draft: when strict contract date overlap finds nothing, fall back to latest non-rejected
    # terms with rate lines for the site (same behavior as recon rerun with allow_expired_contract_terms).
    o2c_mis_allow_expired_contract_terms: bool = False
    # After MIS draft/rerun: auto-approve when prior month approved MIS matches headcount, contract, tier, ±tolerance.
    o2c_mis_auto_approve_enabled: bool = True
    o2c_mis_auto_approve_tolerance_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    o2c_mis_auto_approve_site_blocklist: str = ""
    # MIS draft: merge LWD leaver + DOJ joiner attendance on joiner row per contract line (conservative pairing).
    o2c_mis_handover_pairing_enabled: bool = True
    @field_validator("database_url", mode="before")
    @classmethod
    def coerce_async_pg(cls, v: str) -> str:
        if not isinstance(v, str):
            return v
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        # e.g. postgresql://user:pass@host:5432 → append default DB name
        v = cls._ensure_db_name(v)
        return v

    @field_validator("database_url_sync", mode="before")
    @classmethod
    def coerce_sync_pg(cls, v: str) -> str:
        if not isinstance(v, str):
            return v
        if v.startswith("postgresql+asyncpg://"):
            v = v.replace("postgresql+asyncpg://", "postgresql://", 1)
        v = cls._ensure_db_name(v)
        return v

    @staticmethod
    def _ensure_db_name(url: str) -> str:
        asyncpg = url.startswith("postgresql+asyncpg://")
        normalized = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        p = urlparse(normalized)
        if p.path in ("", "/"):
            fixed = urlunparse(
                (p.scheme, p.netloc, "/agentos", p.params, p.query, p.fragment)
            )
            return (
                fixed.replace("postgresql://", "postgresql+asyncpg://", 1)
                if asyncpg
                else fixed
            )
        return url

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Qdrant (set QDRANT_ENABLED=false to skip client init, collection ensure, and contract indexing)
    qdrant_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection_policies: str = "agentos_policies"

    # LLM (optional — chat works without keys using deterministic replies)
    llm_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: str = ""
    # O2C contract extract (Responses API + PDF) and other OpenAI chat paths use this model.
    openai_chat_model: str = "gpt-5.4"
    anthropic_api_key: str = ""
    anthropic_chat_model: str = "claude-3-5-sonnet-20241022"

    # MLflow / Observability (disabled - using optimus_llm_usage table instead)
    mlflow_tracking_uri: str = "http://localhost:5000"
    llm_tracing_enabled: bool = False  # Disabled - Optimus uses DB logging via optimus_llm_usage table

    # Optimus LLM usage logging (token tracking to DB)
    optimus_llm_usage_logging_enabled: bool = False  # Enable to log token usage to optimus_llm_usage table

    # Rate limits (per minute, per key)
    rate_limit_login_per_ip: int = 20
    rate_limit_api_per_user: int = 300

    # APScheduler: MemoryJobStore for CI/local without persisting cron; else SQLAlchemy on DATABASE_URL_SYNC
    scheduler_use_memory_jobstore: bool = False

    # When False, skip APScheduler + in-process workers (WhatsApp JIT). Use on multi-worker API
    # processes; run a dedicated single-worker jobs service with True. Prefer setting via systemd
    # Environment= — do not put this in shared ``.env`` (dotenv override=True would clobber units).
    run_background_jobs: bool = True

    # Public URL of this API (OAuth callbacks, links). Default local dev.
    api_public_base_url: str = "http://localhost:8000"

    # Generic outbound HTTP tool: if non-empty, only these hostnames (exact) are allowed.
    external_http_allowlist: list[str] = Field(default_factory=list)

    # Google OAuth (Workspace: Gmail, Drive, Sheets). Leave client_id empty to disable routes.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""  # default: {api_public_base_url}/api/integrations/google/callback

    # Composable pipelines from workflow_definitions table (migration 005 + seed)
    workflow_composable_enabled: bool = True

    procurement_sap_max_attempts: int = Field(default=6, ge=1, le=50)
    procurement_sap_retry_base_seconds: int = Field(default=60, ge=10, le=3600)
    procurement_sap_retry_batch_size: int = Field(default=100, ge=1, le=2000)
    # Live SAP PR/PO (S/4 OData). When base URL is set, create/update uses the real APIs.
    procurement_sap_base_url: str = ""  # e.g. https://10.1.98.30:20400 (no trailing slash)
    procurement_sap_username: str = ""
    procurement_sap_password: str = ""
    procurement_sap_verify_ssl: bool = True  # staging often needs false
    procurement_sap_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    procurement_sap_client: str = "200"  # sap-client query/header + sap-usercontext cookie
    procurement_sap_language: str = "EN"  # sap-language query/header
    procurement_sap_hydrate_on_read: bool = True  # GET ticket: form from SAP when sap_id set
    # Nightly SAP full master-data → pr_po_reference_values (see jobs.procurement_reference_sync).
    procurement_reference_sync_enabled: bool = True
    # Local PDF staging until SAP AttachmentSet upload succeeds (then file is deleted).
    procurement_attachment_staging_dir: str = ""

    # Browser app origin (OAuth redirect after Google SSO). Default matches local Next.js.
    frontend_public_base_url: str = "http://localhost:3000"
    # Google SSO (OIDC) for web login — separate from Workspace integration OAuth.
    google_sso_client_id: str = ""
    google_sso_client_secret: str = ""
    google_sso_redirect_uri: str = ""  # default: {api_public_base_url}/api/auth/google/callback
    # Comma-separated email domains allowed to sign in (empty = any verified Google email).
    google_sso_allowed_email_domains: str = ""
    # When true (default), first successful Google OIDC login for an unknown email creates a local
    # ``users`` row (employee, random password hash). Set false in locked-down orgs where only
    # pre-provisioned accounts may sign in.
    google_sso_auto_provision: bool = True
    # Procurement SAP alert email uses Gmail API (``app.email_automation.gmail_sa``):
    # ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` + ``EMAIL_AUTOMATION_IMPERSONATED_USER``.
    procurement_app_public_url: str = "http://localhost:3000"
    # SAP create/resubmit exhausted-retry alert to ticket owner (Gmail SA). Set false on local dev.
    procurement_sap_failure_email_enabled: bool = True
    # Payroll workflow transition email notifications. Set false during dev/testing.
    payroll_email_enabled: bool = False
    payroll_admin_email: str = "payroll@1mg.com"
    payroll_encryption_key: str = ""

    # Optional cron jobs (default off — enable per env when integrations exist)
    job_daily_briefing_enabled: bool = False
    job_expiry_scan_enabled: bool = False
    job_context_refresh_enabled: bool = False
    job_pattern_analysis_enabled: bool = False

    # Email Automation — SA + DWD Gmail; read + send from one service mailbox.
    # SA key file path must be exported as GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON.
    email_automation_enabled: bool = False
    # Workspace user the SA impersonates for Gmail read + send (must be the mailbox owner).
    email_automation_impersonated_user: str = "automation.agents@1mg.com"
    # Outbound ``From`` and ``Reply-To`` for email automation (single setting).
    # Empty = omit both; Gmail uses the impersonated user's default send-as and
    # no explicit Reply-To. When set (e.g. invoices@1mg.com), the value must
    # appear under Gmail "Send mail as" for that user.
    email_automation_send_from: str = ""
    # Inbox scan: Gmail search query applied on ``users.messages.list``.
    # This string has two jobs and the ``from:`` clauses drive both:
    #   1. Wire-level filter — Gmail only returns matching messages.
    #   2. Classifier allowlist — every ``from:<addr>`` token here is parsed
    #      out and handed to the in-code classifier as its sender allowlist
    #      (see :func:`app.email_automation.pipeline.process._senders_from_inbox_query`).
    # Edit once, both layers stay in lockstep. Supports ``from:a OR from:b``
    # and ``from:(a OR b)`` syntax; quoted display names are tolerated.
    # Broader subject gate at Gmail (keep in sync with
    # ``PAYMENT_REMINDER_WEEKLY.subject_regex`` in code), e.g.
    # ``from:ar@example.com subject:Receivable newer_than:14d``.
    # Unquoted ``subject:Receivable`` matches subjects containing that token;
    # use ``subject:"phrase"`` when you need an exact phrase.
    # We deliberately do NOT add ``is:unread``: the ingest loop terminates
    # the moment it sees an id that's already in our DB (Gmail returns ids
    # newest-first, so a known id means everything older is also persisted).
    # Staying off ``is:unread`` keeps Gmail scope at ``readonly`` \u2014 no
    # ``gmail.modify``, no DWD authorization changes, no label writes.
    email_automation_inbox_query: str = (
        'from:bhawna.gandhi@1mg.com '
        'subject:"Receivable / Overdue as on " '
        'newer_than:14d'
    )
    # Scan cadence — we poll the inbox on a short interval so any new receivable
    # dump lands in the DB within one cycle. 10 minutes is the AR team's
    # agreed ceiling between "Bhawna sent it" and "reminders should be drafted".
    # Dispatch runs on its own faster interval (see below).
    email_automation_scan_interval_minutes: int = 10
    # Dispatch sweeps the send queue much more often so an ``approved`` row
    # leaves the box within a minute or two of being rendered.
    email_automation_dispatch_interval_minutes: int = 2
    # Safety rails while integrating: NEVER send to real recipients; redirect to this mailbox.
    email_automation_test_mode: bool = True
    email_automation_test_redirect_to: str = "automation.agents@1mg.com"
    # Google Sheets master trackers for recipient resolution (per-variant).
    email_automation_epharma_master_sheet_id: str = ""  # e.g. "1PUqpxM_KE_mdeEKKKkyhQA0BCNJXI3iLm3mVd2XAsMY"
    email_automation_epharma_master_tab: str = "Master"
    email_automation_chw_master_sheet_id: str = ""  # e.g. "1l8JOhRGzGsTFEgZx_lTx-xk2fA0PO4gUddNBlV4W5e0"
    email_automation_chw_master_tab: str = "Mail Master"
    email_automation_diagnostics_master_sheet_id: str = "1eK849Gr1CqgiUrxPsbfBMNnnBiPqVB48mUq3Dw5ZxyY"  # e.g. Master Sheet For Recon (Lab tab)
    email_automation_diagnostics_master_tab: str = "Both"
    # Attachment staging dir (per run). Defaults to /tmp subtree; files are deleted after processing.
    email_automation_attachment_dir: str = "/tmp/email_automation"
    # No human-in-the-loop by design: rows that render cleanly auto-dispatch.
    # The only safety net during rollout is ``email_automation_test_mode``,
    # which redirects every outgoing ``To``/``Cc`` to the automation mailbox.
    # Rows with data-quality issues (tracker_not_found, potential_overpayment,
    # etc.) are marked ``skipped`` with a reason — never auto-sent. Flip this
    # to ``True`` only if a future use-case needs explicit operator approval.
    email_automation_require_approval: bool = False

    # ── SBI MIS ──────────────────────────────────────────────
    sbi_mis_enabled: bool = False
    # Base directory for SBI MIS uploads, outputs, and pipeline cache.
    # macOS local dev: ~/sbi_mis_data  (home root — avoids TCC on ~/Documents)
    # Ubuntu EC2:      /var/lib/sbi_mis
    # Docker/K8s:      mount a persistent volume at this path.
    # Default (~/.local/share/sbi_mis) works on Ubuntu; override in .env for macOS.
    sbi_mis_data_dir: str = "~/.local/share/sbi_mis"
    # F3: max dispatch attempts per send row before the retry endpoint refuses
    # further auto-retry. 5 covers transient Gmail 5xx + a few cooldowns;
    # anything beyond that is almost always a content / recipient issue that
    # won't self-heal, and we'd rather alert ops than wedge the queue.
    email_automation_send_max_attempts: int = 5
    # Canary allowlist (Option B) — comma-separated ``business_key`` values
    # (KAM codes, e.g. ``ABC123,XYZ789``). When NON-EMPTY, dispatch only
    # sends rows whose ``business_key`` is on this list; every other
    # ``approved``/``rendered`` row stays untouched until the allowlist
    # is widened or cleared. When empty (default) dispatch sends
    # everything approved — same behaviour as before the flag existed.
    # Case-sensitive match against ``EmailAutomationSend.business_key``
    # exactly as persisted by the pipeline (so the value you put here
    # must match what shows up in the UI / DB, not a raw tracker code).
    # Flip-to-prod runbook: start with 1–2 friendly KAMs here, flip
    # ``email_automation_test_mode=false``, observe one dispatch tick,
    # then widen. Clear the variable to go fleet-wide.
    email_automation_dispatch_allowlist: str = ""
    # F2: max processing attempts per (message, variant) before that variant
    # freezes at ``processed_with_errors`` without further auto-retry. 3 is
    # enough to ride out transient Sheets/DB blips; a 4th failure is almost
    # always a schema mismatch that needs a code fix.
    email_automation_variant_max_attempts: int = 3
    # Optional second Gmail ``messages.list`` query in the same scan tick so client
    # replies (and other mail) can be ingested alongside the receivable workbook query.
    # Uses the same dedupe + per-tick caps as the primary query. Empty = disabled.
    email_automation_supplemental_inbox_query: str = ""
    # Collections reply intelligence — classifies threads tied to sent reminders (OpenAI).
    # Message bodies are not persisted; only structured columns + payload JSON.
    email_automation_collections_intelligence_enabled: bool = False
    email_automation_collections_openai_model: str = "gpt-4o-mini"
    # KAM reply-accuracy intelligence — scores whether the assigned KAM actively
    # tracked each payment-reminder thread (LLM 0/1 score) and measures reply
    # timing. Runs as an independently-flagged path in the scan cron. Message
    # bodies are not persisted; only structured columns + payload JSON.
    kam_reply_accuracy_enabled: bool = True
    kam_reply_accuracy_openai_model: str = "gpt-4o-mini"
    # Google Sheet (read-only, shared with the SA ``client_email``) holding KAM
    # ownership. ``mapping`` tab: KAM ↔ HANA/business keys; ``emails`` tab: KAM
    # name ↔ personal 1mg.com address (used to identify the KAM's own replies).
    kam_directory_sheet_id: str = "1VmqCetV-deiQ6uzuh1pBLsxN_RwQzhpZxLQ0PoXMmMs"
    kam_directory_mapping_tab: str = "KAM Client Mapping"
    kam_directory_emails_tab: str = "KAM EmailIDs"
    # After deterministic tab matching fails (``KeyError``), optionally ask OpenAI to
    # pick an **exact** ``wb.sheetnames`` entry. Disabled by default; requires
    # ``openai_api_key``. Output is validated — no invented tab names.
    email_automation_sheet_name_ai_fallback_enabled: bool = False
    email_automation_sheet_name_ai_model: str = "gpt-5.4-mini"
    # Flock incoming-webhook URL for receivable ingest alerts (warnings + errors).
    # Set to the group webhook URL; leave empty to disable notifications.
    # Alerts are also suppressed when ``email_automation_test_mode=true``.
    receivable_ingest_flock_webhook_url: str = ""

    # Cold Outreach — modular outreach engine (CHW pilot and future campaigns)
    # All switches default false — explicitly opt in to each capability in .env.
    outreach_enabled: bool = False                  # master switch — enables the scheduler jobs
    outreach_sync_enabled: bool = False             # sheet read → DB reconcile
    outreach_classify_enabled: bool = False         # reply scan → OpenAI classify → DB update
    outreach_dispatch_enabled: bool = False         # email sending
    outreach_sheet_writeback_enabled: bool = False  # GSheet status + reply-category writebacks
    outreach_daily_send_limit: int = 0              # 0 = unlimited (only relevant when dispatch enabled)
    outreach_test_mode: bool = True                 # redirect all sends to outreach_test_redirect_to; must be false in prod
    outreach_test_redirect_to: str = ""             # required when outreach_test_mode=true
    outreach_test_allowed_cc: str = ""              # comma-separated addresses allowed as real CC in test mode
    chw_outreach_gsheet_id: str = ""               # Google Sheet ID for CHW outreach campaign

    # Compliance call quality (GLP) — Drive ingest → STT → rubric → DB + Sheet (v1 batch).
    # Drive + Sheets auth: set ``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` (or ``GOOGLE_APPLICATION_CREDENTIALS``);
    # see ``resolve_o2c_gdrive_credentials_path`` in ``app.integrations.gdrive_o2c``.
    # Order RCA — async diagnose (Redis run state; APIs 1–5).
    order_rca_enabled: bool = False
    order_rca_use_fixtures: bool = False
    order_rca_mock_llm: bool = False
    order_rca_fixture_dir: str = ""
    order_rca_order_service_base_url: str = ""
    order_rca_sla_service_base_url: str = ""
    order_rca_groot_base_url: str = ""
    order_rca_post_order_base_url: str = ""
    order_rca_admin_service_base_url: str = ""
    order_rca_p1_msn_base_url: str = ""
    order_rca_sla_auth_token: str = ""
    order_rca_service_name: str = "sla_service"
    order_rca_service_version: str = "1.0.0"
    order_rca_http_timeout_sec: int = 30
    order_rca_http_retries: int = 2  # extra attempts after the first (3 tries total)
    order_rca_run_ttl_sec: int = 3600
    order_rca_openai_model: str = ""
    order_rca_synthesis_temperature: float = 0.2
    order_rca_persistence_enabled: bool = False
    order_rca_scheduler_enabled: bool = False

    # Responder chat rubric eval — production needs only:
    #   RESPONDER_EVAL_ENABLED=true
    #   RESPONDER_EVAL_CHAT_DB_URL_SYNC=<sync pg url for conversations/messages>
    #   OPENAI_API_KEY=<shared>
    # ORDER_RCA_ENABLED must be true so RCA dumps are created.
    responder_eval_enabled: bool = False
    responder_eval_tick_interval_minutes: int = Field(default=10, ge=1)
    responder_eval_batch_size: int = Field(default=50, ge=1, le=500)
    responder_eval_min_age_hours: float = Field(default=1.0, ge=0)
    # Avoid head-of-line blocking: re-check waiting_chat dumps at most this often.
    responder_eval_waiting_recheck_minutes: int = Field(default=60, ge=5, le=24 * 60)
    responder_eval_chat_db_url_sync: str = ""
    responder_eval_use_chat_fixtures: bool = False
    responder_eval_chat_fixture_dir: str = ""
    responder_eval_openai_model: str = "gpt-5.4-mini"
    responder_eval_mock_judge: bool = False
    responder_eval_presidio_enabled: bool = True
    responder_eval_spacy_model: str = "en_core_web_sm"
    responder_eval_spacy_auto_download: bool = True
    responder_eval_max_fail_retries: int = Field(default=5, ge=1, le=50)
    responder_eval_processing_stale_minutes: int = Field(default=60, ge=5, le=24 * 60)

    # WhatsApp JIT hold — Kafka order-status trigger + Meta Cloud API (Redis session state only).
    whatsapp_jit_hold_enabled: bool = False
    whatsapp_jit_hold_use_fixtures: bool = False
    whatsapp_jit_hold_test_mode: bool = False
    whatsapp_jit_hold_test_phone: str = ""
    whatsapp_jit_hold_split_username: str = ""
    whatsapp_jit_hold_split_stub: bool = False
    # Kafka — nexus order-status updates.
    whatsapp_jit_hold_kafka_bootstrap_servers: str = ""
    whatsapp_jit_hold_kafka_topic: str = ""
    whatsapp_jit_hold_kafka_consumer_group: str = "agentos-whatsapp-jit-hold"
    # New / expired consumer-group offsets: replay from this many hours ago (0 = latest only).
    # Committed offsets still resume exactly (full catch-up after downtime).
    whatsapp_jit_hold_kafka_lookback_hours: int = Field(default=4, ge=0, le=72)
    whatsapp_jit_hold_session_ttl_sec: int = 172800
    whatsapp_jit_hold_sent_ttl_sec: int = 604800
    # Order-level JIT qty gates (ordered = all lines; stuck = JIT held lines only).
    whatsapp_jit_hold_jit_ordered_qty_min: int = Field(default=9, ge=1)
    whatsapp_jit_hold_jit_stuck_qty_min: int = Field(default=1, ge=1)
    whatsapp_jit_hold_jit_stuck_qty_max: int = Field(default=3, ge=1)
    # Meta Cloud API (Destination WABA).
    whatsapp_meta_webhook_verify_token: str = ""
    whatsapp_meta_access_token: str = ""
    whatsapp_meta_phone_number_id: str = ""
    whatsapp_meta_graph_version: str = "v23.0"

    compliance_call_enabled: bool = False
    # Service user UUID for workflow_runs rows (single-tenant v1). Must exist in ``users``.
    compliance_call_system_user_id: str = ""
    compliance_call_gdrive_folder_id: str = ""  # recursive listing root
    compliance_deepgram_api_key: str = ""
    compliance_deepgram_model: str = "nova-3-medical"
    # Match ``transcribe-deepgram-india-once.mjs`` prerecorded options (transcript quality only; we still persist **string** only).
    compliance_deepgram_diarize: bool = True
    # Comma-separated keyterms (optional); same as env ``DEEPGRAM_KEYTERMS`` in the reference script.
    compliance_deepgram_keyterms: str = ""
    # When diarize is on, cap speakers (0 = omit param; Deepgram auto-detect). Typical consults: 2.
    compliance_deepgram_max_speakers: int = Field(default=2, ge=0, le=16)
    # Deepgram Listen ``redact``: empty / ``off`` = none; ``names`` = name-related entities only (see transcribe adapter);
    # ``pii`` = full PII bundle (includes age, dates, locations — usually avoid for rubric). Other values = single entity id (Deepgram docs).
    compliance_deepgram_redact: str = "names"
    # FFmpeg loudnorm (EBU R128) — improves level consistency for telehealth/STT; disable if CPU-bound.
    compliance_ffmpeg_loudnorm: bool = True
    # Skip rubric if canonical transcript is shorter than this (noise/silent files). 0 = disabled.
    compliance_min_transcript_chars: int = Field(default=12, ge=0, le=100000)
    # ``utterances``: prefer Deepgram utterance lines with [Sn] tags when non-empty; else ``canonical``.
    compliance_grading_transcript_source: str = "utterances"
    # Map Deepgram speaker IDs → clinician vs patient via OpenAI (utterances + talk-time); stored in DB/Sheets.
    compliance_infer_speaker_roles: bool = True
    compliance_speaker_roles_model: str = ""
    compliance_poll_interval_hours: int = Field(default=4, ge=0, le=168)
    # When poll_interval_hours is 0 (continuous mode), sleep between batch ticks.
    compliance_min_sleep_seconds: int = Field(default=600, ge=30, le=3600)
    compliance_max_files_per_tick: int = Field(default=5, ge=1, le=100)
    compliance_batch_concurrency: int = Field(default=1, ge=1, le=8)
    compliance_rubric_version: str = "glp1"
    # Override to use a custom on-disk GLP JSON; empty = bundled ``rubrics/glp1_consult_rubric.json``.
    compliance_rubric_json_path: str = ""
    # Re-claim ``running`` rows stuck without completion (scheduler / worker crash).
    compliance_stale_running_minutes: int = Field(default=45, ge=5, le=1440)
    # GLP rubric Responses model (``COMPLIANCE_OPENAI_MODEL``). Default ``gpt-5.4``; if set to empty,
    # falls back to ``openai_chat_model``.
    compliance_openai_model: str = "gpt-5.4"
    # 0 = use ``o2c_llm_max_output_tokens`` (large JSON rubric output).
    compliance_openai_max_output_tokens: int = Field(default=0, ge=0, le=200000)
    # Responses API: strict JSON Schema when supported; auto-fallback to json_object on API/model errors.
    compliance_openai_structured_outputs: bool = True
    # Optional transcript cleanup before rubric scoring (keeps raw transcript separately).
    compliance_transcript_normalize_enabled: bool = True
    # Empty = reuse ``compliance_openai_model``.
    compliance_transcript_normalize_model: str = ""
    # Keep low for stable rewrite while allowing minor ASR cleanup.
    compliance_transcript_normalize_temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    # Transient API retries (Deepgram HTTP + OpenAI SDK): 429 / 5xx / timeouts / connection errors.
    compliance_retry_attempts: int = Field(default=5, ge=1, le=15)
    compliance_retry_base_seconds: float = Field(default=0.75, ge=0.05, le=30.0)
    compliance_retry_max_sleep_seconds: float = Field(default=60.0, ge=0.5, le=300.0)
    # Google Sheet append (native Sheet id + tab name); uses SA + spreadsheets scope.
    compliance_sheet_id: str = ""
    compliance_sheet_tab: str = "ComplianceRollup"
    # Optional second tab for README §10.2 Detailed (93 columns). Empty = skip.
    compliance_sheet_tab_detailed: str = ""

    # Compliance call quality — optional MySQL 8.x source (second_opinion + calls) alongside Drive.
    compliance_call_mysql_enabled: bool = False
    # SQLAlchemy async URL, e.g. mysql+asyncmy://user:pass@host:5426/dbname
    compliance_call_mysql_url: str = ""
    # Comma-separated second_opinion_id values (matches second_opinion_conversations.second_opinion_id).
    compliance_call_second_opinion_ids: str = ""
    # Page size when scanning second_opinion_conversations (ordered by id).
    compliance_mysql_page_size: int = Field(default=20, ge=1, le=500)
    # IANA timezone for "start of previous local calendar day" → now window (MySQL DATETIME wall-clock).
    compliance_call_mysql_timezone: str = "Asia/Kolkata"
    # Merge all call legs per second_opinion_conversation_id into one transcript before rubric.
    compliance_mysql_merge_conversation_transcripts: bool = True
    # Twilio Video (Basic auth: Account SID + Auth Token) — compositions + room recordings.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    compliance_twilio_poll_interval_seconds: float = Field(default=15.0, ge=2.0, le=120.0)
    compliance_twilio_composition_max_wait_seconds: float = Field(default=600.0, ge=30.0, le=3600.0)

    # MIS draft xlsx output directory (LangGraph / API paths)
    o2c_invoice_out_dir: str = "/tmp/o2c_out"

    # O2C_OHC agent — MIS template + column hints (agenos DB URL above)
    o2c_ohc_attendance_xlsx_path: str = ""  # OHC attendance export (Summary tab); alias for older .env
    o2c_invoice_mis_template_path: str = "/Users/pankaj/Downloads/test_files/mis_template.xlsx"  # Standardized MIS Excel template
    # Summary tab: column headers (substring match, case-insensitive) for client–site key and roll type
    o2c_ohc_site_column_hints: str = "client-site,client site,site,location"
    o2c_ohc_onroll_column_hints: str = "onroll,offroll,roll"
    # Comma-separated header substrings; row must be All/blank in these columns (Excel “dropdown” filters).
    o2c_ohc_scope_filter_column_hints: str = "month,period,vertical,department"
    o2c_invoice_mis_data_start_row: int = 10  # 1-based row in template where line items begin

    # ── Optimus — Intelligence platform with sub-services ────────────────────────
    optimus_enabled: bool = False
    optimus_smartqna_enabled: bool = False
    optimus_prosight_enabled: bool = False
    optimus_quickml_enabled: bool = False
    optimus_texttoworkflow_enabled: bool = False
    optimus_documents_dir: str = ""  # directory containing documents for SmartQnA ingestion
    optimus_upload_dir: str = "./optimus_uploads"  # directory to store uploaded documents before ingestion
    optimus_openai_model: str = "gpt-4.1-mini"
    optimus_openai_api_key: str = ""  # Separate OpenAI API key for Optimus (falls back to openai_api_key if empty)
    # OpenAI embeddings (API-based)
    optimus_embedding_model: str = "text-embedding-3-small"
    optimus_embedding_dimensions: int = 1536
    # LLM-based reranker using GPT-4.1 Mini (faster than 4o-mini)
    optimus_reranker_model: str = "gpt-4.1-mini"
    # CRAG thresholds (reranker-score based; sigmoid-normalised → range [0, 1])
    optimus_crag_sufficient_threshold: float = 0.65  # top score ≥ this → SUFFICIENT
    optimus_crag_partial_threshold: float = 0.40     # top score ≥ this → PARTIAL (raised for better filtering)
    optimus_crag_filter_min_score: float = 0.30      # discard any chunk below this (raised to filter noise)
    optimus_crag_drop_gap: float = 0.25              # adaptive stop when score drops by this (relaxed for multi-part queries)
    # Retrieval parameters
    optimus_vector_search_candidates: int = 10      # candidates fetched per search type before reranking (reduced from 20 for performance)
    # Reranking skip thresholds - skip LLM reranking if top vector score exceeds threshold
    optimus_rerank_skip_threshold_simple: float = 0.85   # for simple queries, skip rerank if vector score > 0.85
    optimus_rerank_skip_threshold_complex: float = 0.90  # for complex queries, skip rerank if vector score > 0.90 (more conservative)
    # API resilience settings (staging performance optimization)
    optimus_embedding_timeout_seconds: float = 30.0   # OpenAI embedding API timeout (prevents 65s+ hangs)
    optimus_embedding_max_retries: int = 3            # OpenAI SDK built-in retry count
    optimus_request_timeout_seconds: int = 58         # Overall request timeout - graceful backoff before 60s gateway timeout
    optimus_max_concurrent_requests: int = 15         # Max simultaneous SmartQnA requests; tune up under high load or down to protect LLM budget
    # Separate from optimus_max_concurrent_requests (must not share the same Semaphore — nested acquire deadlocks).
    # Queues excess Flock handlers; never drops messages. Bounds DB/session occupancy before LLM.
    optimus_flock_max_concurrent_handlers: int = 15
    optimus_query_expansion_cache_ttl_seconds: int = 1800  # Redis query expansion cache TTL (30 minutes)
    # Session / conversation settings
    optimus_session_ttl_hours: int = 24
    optimus_session_max_turns: int = 40
    # Ingestion timeout — documents stuck longer than this are marked FAILED
    optimus_ingestion_timeout_minutes: int = 3
    # Qdrant collection for SmartQnA vectors
    qdrant_collection_optimus: str = "optimus_smartqna"
    # Flock bot integration
    optimus_flock_enabled: bool = False
    optimus_flock_app_id: str = ""
    optimus_flock_app_secret: str = ""
    optimus_flock_bot_token: str = ""
    optimus_flock_verification_token: str = ""
    # Support/issue form URL shown in Flock "raise a request" pointers.
    optimus_flock_support_form_url: str = "https://forms.gle/no4C3GCxTTyWEuUMA"
    # Full Optimus web app URL shared in responses and Flock welcome messages.
    optimus_web_app_url: str = "https://agents.1mg.com/optimus"

    # Prosight - Databricks integration
    prosight_databricks_host: str = ""  # e.g., xxx.cloud.databricks.com
    prosight_databricks_token: str = ""  # Databricks personal access token
    prosight_databricks_warehouse_id: str = ""  # SQL Warehouse ID
    # Fully-qualified source table (catalog.schema.table). Interpolated into SQL,
    # so it is validated against a strict identifier pattern in databricks_sync.
    prosight_databricks_table: str = "data_warehouse.ai_transformation.prosight_dashboard"
    # Databricks table holding Prosight actionables (segment, lens, rank, action).
    # Same catalog.schema.table validation as prosight_databricks_table.
    prosight_actionables_table: str = "shared.data_systems.prosight_actionables"
    # Actionables are fetched for a rolling window, not the whole table: the source
    # grows by one partition per BU per day forever, and an unbounded scan outgrows
    # the query timeout and the single-statement upsert. Widen only if the UI needs
    # to browse further back than this.
    prosight_actionables_lookback_days: int = 30
    # Defensive cap on the fetch so a source-side fan-out can't blow up memory.
    # Truncation is logged as an error — it means the window or the source is wrong.
    prosight_actionables_max_rows: int = 20000
    prosight_sync_hour: int = 11
    prosight_databricks_query_timeout_seconds: int = 60  # HTTP + Databricks wait_timeout for SQL queries
    # Ceilings on EXTERNAL_LINKS chunk pagination. Databricks decides how many
    # chunks a result is split into; the stitcher follows next_chunk_index until
    # the server stops offering one, holding every row in memory meanwhile. These
    # bound that walk in size, count and wall-clock. Tripping one fails the sync
    # rather than storing a partial result — a truncated read would look like a
    # clean sync and demote whatever fell off the end.
    prosight_databricks_max_result_mb: int = 256
    prosight_databricks_max_result_chunks: int = 200
    prosight_databricks_materialize_timeout_seconds: int = 600

    @model_validator(mode="after")
    def _responder_eval_fixture_defaults(self) -> Self:
        if self.responder_eval_use_chat_fixtures and not (self.responder_eval_chat_fixture_dir or "").strip():
            self.responder_eval_chat_fixture_dir = str(
                _BACKEND_ROOT / "tests" / "fixtures" / "responder_eval"
            )
        return self

    @field_validator("external_http_allowlist", mode="before")
    @classmethod
    def parse_http_allowlist(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if v is None or v == "":
            return ["http://localhost:3000", "http://127.0.0.1:3000"]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                import json

                try:
                    data = json.loads(s)
                    if isinstance(data, list):
                        return [str(x).strip() for x in data if str(x).strip()]
                except Exception:
                    pass
            return [x.strip() for x in s.split(",") if x.strip()]
        return v


# Instantiate after dotenv hydration so every field resolves: os.environ → default.
settings = Settings()
