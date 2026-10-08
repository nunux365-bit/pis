--
-- PostgreSQL database dump
--

\restrict yht9jsipQSVRgBJrworZJLDzxvmnJKw55JpgeC9SORnHvl6Xy5Vf8djdD2feCiu

-- Dumped from database version 14.20 (Homebrew)
-- Dumped by pg_dump version 14.20 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: btree_gin; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public;


--
-- Name: EXTENSION btree_gin; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION btree_gin IS 'support for indexing common datatypes in GIN';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: billing_model_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.billing_model_t AS ENUM (
    'fixed_monthly',
    'rate_attendance',
    'per_visit',
    'per_head',
    'as_per_actuals',
    'milestone',
    'retainer_variable'
);


ALTER TYPE public.billing_model_t OWNER TO pankaj;

--
-- Name: contract_kind_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.contract_kind_t AS ENUM (
    'msa',
    'sow',
    'service_agreement',
    'letter_of_extension',
    'amendment',
    'cwp_msa'
);


ALTER TYPE public.contract_kind_t OWNER TO pankaj;

--
-- Name: ingestion_class_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.ingestion_class_t AS ENUM (
    'text_native',
    'image_primary',
    'mixed'
);


ALTER TYPE public.ingestion_class_t OWNER TO pankaj;

--
-- Name: invoice_status_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.invoice_status_t AS ENUM (
    'draft',
    'awaiting_review',
    'pending',
    'approved',
    'rejected',
    'sent',
    'disputed',
    'paid',
    'overdue',
    'void'
);


ALTER TYPE public.invoice_status_t OWNER TO pankaj;

--
-- Name: schedule_type_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.schedule_type_t AS ENUM (
    'prescribed',
    'frequency',
    'shift_based',
    'continuous',
    'on_demand',
    'none'
);


ALTER TYPE public.schedule_type_t OWNER TO pankaj;

--
-- Name: terms_status_t; Type: TYPE; Schema: public; Owner: pankaj
--

CREATE TYPE public.terms_status_t AS ENUM (
    'draft',
    'validated',
    'active',
    'superseded',
    'expired',
    'terminated',
    'pending',
    'approved',
    'rejected'
);


ALTER TYPE public.terms_status_t OWNER TO pankaj;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_sessions; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.agent_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    thread_id character varying(80) NOT NULL,
    workflow_name character varying(200) NOT NULL,
    status character varying(32) NOT NULL,
    progress_pct integer NOT NULL,
    state jsonb,
    last_checkpoint_at timestamp with time zone,
    workflow_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.agent_sessions OWNER TO pankaj;

--
-- Name: approvals; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.approvals (
    id uuid NOT NULL,
    assignee_user_id uuid NOT NULL,
    created_by_user_id uuid,
    title character varying(500) NOT NULL,
    description text,
    status character varying(32) NOT NULL,
    amount numeric(18,2),
    currency character varying(8) NOT NULL,
    confidence integer,
    risk character varying(16) NOT NULL,
    agent_name character varying(120) NOT NULL,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone
);


ALTER TABLE public.approvals OWNER TO pankaj;

--
-- Name: apscheduler_jobs; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.apscheduler_jobs (
    id character varying(191) NOT NULL,
    next_run_time double precision,
    job_state bytea NOT NULL
);


ALTER TABLE public.apscheduler_jobs OWNER TO pankaj;

--
-- Name: attendance_import_batch; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.attendance_import_batch (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    source_system text DEFAULT 'truein_excel'::text NOT NULL,
    source_file_path text,
    imported_by text,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    row_count integer,
    unmatched_site_count integer DEFAULT 0
);


ALTER TABLE public.attendance_import_batch OWNER TO pankaj;

--
-- Name: attendance_row; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.attendance_row (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attendance_import_batch_id uuid NOT NULL,
    employee_external_id text NOT NULL,
    employee_name_snapshot text,
    service_date date NOT NULL,
    service_site_code text NOT NULL,
    role_code text,
    resolved_site_id uuid,
    resolved_role_code text,
    is_present boolean DEFAULT true NOT NULL,
    hours_worked numeric(8,4),
    shift_type text,
    is_overtime boolean DEFAULT false NOT NULL,
    overtime_hours numeric(8,4) DEFAULT 0,
    site_match_status text DEFAULT 'pending'::text NOT NULL,
    raw_row jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attendance_row_site_match_status_check CHECK ((site_match_status = ANY (ARRAY['matched'::text, 'unmatched'::text, 'manually_mapped'::text, 'flagged'::text])))
);


ALTER TABLE public.attendance_row OWNER TO pankaj;

--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    actor_user_id uuid,
    action character varying(120) NOT NULL,
    resource_type character varying(80) NOT NULL,
    resource_id character varying(80),
    details jsonb,
    ip_address character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.audit_logs OWNER TO pankaj;

--
-- Name: automation_rules; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.automation_rules (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(300) NOT NULL,
    trigger_description text NOT NULL,
    enabled boolean NOT NULL,
    confidence_threshold integer NOT NULL,
    pattern jsonb,
    execution_count integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.automation_rules OWNER TO pankaj;

--
-- Name: automation_suggestions; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.automation_suggestions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    title character varying(400) NOT NULL,
    pattern_summary text NOT NULL,
    est_savings character varying(120),
    status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.automation_suggestions OWNER TO pankaj;

--
-- Name: billing_client; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.billing_client (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    short_name text,
    slug text,
    gstin text,
    pan text,
    cin text,
    registered_address text,
    city text,
    state text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.billing_client OWNER TO pankaj;

--
-- Name: catalog_agents; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.catalog_agents (
    id uuid NOT NULL,
    slug character varying(120) NOT NULL,
    name character varying(200) NOT NULL,
    status character varying(32) NOT NULL,
    ring integer NOT NULL,
    description text,
    tasks_today integer NOT NULL,
    uptime_pct numeric(6,3),
    sort_order integer NOT NULL
);


ALTER TABLE public.catalog_agents OWNER TO pankaj;

--
-- Name: catalog_skill_categories; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.catalog_skill_categories (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    sort_order integer NOT NULL
);


ALTER TABLE public.catalog_skill_categories OWNER TO pankaj;

--
-- Name: catalog_skills; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.catalog_skills (
    id uuid NOT NULL,
    category_id uuid NOT NULL,
    slug character varying(160) NOT NULL,
    title character varying(300) NOT NULL,
    version character varying(32) NOT NULL,
    sort_order integer NOT NULL
);


ALTER TABLE public.catalog_skills OWNER TO pankaj;

--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.chat_messages (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    content text NOT NULL,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.chat_messages OWNER TO pankaj;

--
-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.chat_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    title character varying(500) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.chat_sessions OWNER TO pankaj;

--
-- Name: contract_document; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_document (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    original_filename text NOT NULL,
    folder_path text,
    storage_uri text,
    sha256 text NOT NULL,
    page_count integer,
    ingestion_class public.ingestion_class_t NOT NULL,
    source_file_modified_at timestamp with time zone,
    ingestion_root text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.contract_document OWNER TO pankaj;

--
-- Name: COLUMN contract_document.source_file_modified_at; Type: COMMENT; Schema: public; Owner: pankaj
--

COMMENT ON COLUMN public.contract_document.source_file_modified_at IS 'Filesystem mtime when ingested; used to skip unchanged files on folder scan.';


--
-- Name: COLUMN contract_document.ingestion_root; Type: COMMENT; Schema: public; Owner: pankaj
--

COMMENT ON COLUMN public.contract_document.ingestion_root IS 'Absolute path of scanned root (normalized) for watermark queries.';


--
-- Name: contract_extraction_run; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_extraction_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contract_document_id uuid NOT NULL,
    pipeline_version text NOT NULL,
    ocr_engine text,
    llm_model text,
    status text NOT NULL,
    confidence numeric(4,3),
    needs_human_review boolean DEFAULT false NOT NULL,
    raw_text text,
    llm_raw_output jsonb,
    error_message text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT contract_extraction_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))
);


ALTER TABLE public.contract_extraction_run OWNER TO pankaj;

--
-- Name: contract_party; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_party (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contract_terms_version_id uuid NOT NULL,
    billing_client_id uuid,
    party_role text NOT NULL,
    legal_name text NOT NULL,
    short_name text,
    gstin text,
    pan text,
    signatory_name text,
    signatory_title text,
    CONSTRAINT contract_party_party_role_check CHECK ((party_role = ANY (ARRAY['service_provider'::text, 'primary_client'::text, 'co_client'::text, 'payer'::text, 'guarantor'::text])))
);


ALTER TABLE public.contract_party OWNER TO pankaj;

--
-- Name: contract_rate_line; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_rate_line (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contract_terms_version_id uuid NOT NULL,
    service_site_id uuid,
    billing_model public.billing_model_t NOT NULL,
    role_code text NOT NULL,
    description text NOT NULL,
    rate_amount numeric(18,4),
    rate_unit text,
    contracted_quantity numeric(10,2),
    attendance_required boolean DEFAULT true NOT NULL,
    minimum_units_per_period numeric(18,4),
    unfilled_penalty_pct numeric(5,2) DEFAULT 0,
    ot_multiplier numeric(6,3),
    service_charge_type text DEFAULT 'none'::text,
    service_charge_value numeric(18,4),
    actuals_markup_pct numeric(5,2),
    schedule_type public.schedule_type_t DEFAULT 'none'::public.schedule_type_t NOT NULL,
    schedule_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    billing_rules jsonb DEFAULT '{}'::jsonb NOT NULL,
    billing_rule_text text,
    model_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_ref jsonb DEFAULT '{}'::jsonb,
    currency text DEFAULT 'INR'::text NOT NULL,
    effective_from date,
    effective_to date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT contract_rate_line_rate_unit_check CHECK ((rate_unit = ANY (ARRAY['month'::text, 'day'::text, 'hour'::text, 'visit'::text, 'head'::text, 'shift'::text, NULL::text]))),
    CONSTRAINT contract_rate_line_service_charge_type_check CHECK ((service_charge_type = ANY (ARRAY['fixed'::text, 'percentage'::text, 'none'::text])))
);


ALTER TABLE public.contract_rate_line OWNER TO pankaj;

--
-- Name: contract_terms_document; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_terms_document (
    contract_terms_version_id uuid NOT NULL,
    contract_document_id uuid NOT NULL,
    doc_role text NOT NULL,
    CONSTRAINT contract_terms_document_doc_role_check CHECK ((doc_role = ANY (ARRAY['msa'::text, 'primary_sow'::text, 'amendment'::text, 'rate_exhibit'::text, 'extension'::text, 'other'::text])))
);


ALTER TABLE public.contract_terms_document OWNER TO pankaj;

--
-- Name: contract_terms_version; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.contract_terms_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    title text,
    contract_kind public.contract_kind_t NOT NULL,
    ref_number text,
    docusign_envelope_id text,
    status public.terms_status_t DEFAULT 'draft'::public.terms_status_t NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    execution_date date,
    non_solicitation_months integer DEFAULT 0,
    termination_notice_days integer DEFAULT 30,
    special_obligations jsonb DEFAULT '{}'::jsonb,
    superseded_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    billing_profile text DEFAULT 'generic'::text NOT NULL
);


ALTER TABLE public.contract_terms_version OWNER TO pankaj;

--
-- Name: failed_contract_parsing; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.failed_contract_parsing (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    folder_root text NOT NULL,
    relative_path text NOT NULL,
    original_filename text NOT NULL,
    file_sha256 text,
    source_file_modified_at timestamp with time zone,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error text,
    last_validation_errors jsonb,
    last_llm_json jsonb,
    markdown_snapshot text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    resolved_note text
);


ALTER TABLE public.failed_contract_parsing OWNER TO pankaj;

--
-- Name: TABLE failed_contract_parsing; Type: COMMENT; Schema: public; Owner: pankaj
--

COMMENT ON TABLE public.failed_contract_parsing IS 'O2C_OHC: rows after max LLM repair attempts still fail validation or DB insert.';


--
-- Name: integration_health; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.integration_health (
    id uuid NOT NULL,
    name character varying(120) NOT NULL,
    system_type character varying(80) NOT NULL,
    status character varying(32) NOT NULL,
    health_pct numeric(5,2) NOT NULL,
    modules text,
    last_sync_at timestamp with time zone,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.integration_health OWNER TO pankaj;

--
-- Name: invoice_line; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.invoice_line (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    invoice_run_id uuid NOT NULL,
    line_no integer NOT NULL,
    line_kind text NOT NULL,
    service_site_id uuid,
    contract_rate_line_id uuid,
    description text NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit text NOT NULL,
    unit_rate numeric(18,4) NOT NULL,
    service_charge numeric(18,4) DEFAULT 0 NOT NULL,
    line_total numeric(18,4) NOT NULL,
    calculation_notes text,
    is_variable boolean DEFAULT false NOT NULL,
    CONSTRAINT invoice_line_line_kind_check CHECK ((line_kind = ANY (ARRAY['manpower_base'::text, 'manpower_minimum'::text, 'service_charge'::text, 'package'::text, 'per_visit'::text, 'actuals'::text, 'adjustment'::text, 'gst'::text, 'tds'::text])))
);


ALTER TABLE public.invoice_line OWNER TO pankaj;

--
-- Name: invoice_run; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.invoice_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    service_site_id uuid,
    contract_terms_version_id uuid NOT NULL,
    attendance_batch_id uuid,
    invoice_number text,
    billing_period_start date NOT NULL,
    billing_period_end date NOT NULL,
    status public.invoice_status_t DEFAULT 'pending'::public.invoice_status_t NOT NULL,
    subtotal numeric(18,4),
    service_charge_total numeric(18,4),
    taxable_amount numeric(18,4),
    gst_rate numeric(5,2) DEFAULT 18.00 NOT NULL,
    gst_amount numeric(18,4),
    total_with_gst numeric(18,4),
    tds_rate numeric(5,2),
    tds_amount numeric(18,4),
    net_payable numeric(18,4),
    due_date date,
    paid_date date,
    payment_reference text,
    dispute_raised_at date,
    dispute_resolved_at date,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.invoice_run OWNER TO pankaj;

--
-- Name: notifications; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.notifications (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    category character varying(32) NOT NULL,
    title character varying(500) NOT NULL,
    body text,
    read boolean NOT NULL,
    link character varying(1024),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.notifications OWNER TO pankaj;

--
-- Name: o2c_attendance_site_recon; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.o2c_attendance_site_recon (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_site_key text NOT NULL,
    billing_period_start date NOT NULL,
    billing_period_end date NOT NULL,
    reason_code text NOT NULL,
    detail text,
    attendance_row_count integer DEFAULT 0 NOT NULL,
    llm_match_attempted boolean DEFAULT false NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    resolved_service_site_id uuid,
    resolved_at timestamp with time zone,
    resolved_by text,
    resolution_notes text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.o2c_attendance_site_recon OWNER TO pankaj;

--
-- Name: o2c_mis_run; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.o2c_mis_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    service_site_id uuid NOT NULL,
    contract_terms_version_id uuid NOT NULL,
    client_site_key text NOT NULL,
    billing_period_start date NOT NULL,
    billing_period_end date NOT NULL,
    status text DEFAULT 'pending_human'::text NOT NULL,
    template_path text,
    xlsx_path text,
    detailed_json jsonb,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_at timestamp with time zone,
    approved_by text,
    rejected_at timestamp with time zone,
    rejected_by text,
    rejection_notes text,
    summary_json jsonb
);


ALTER TABLE public.o2c_mis_run OWNER TO pankaj;

--
-- Name: o2c_mis_summary_row; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.o2c_mis_summary_row (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    mis_run_id uuid NOT NULL,
    contract_rate_line_id uuid NOT NULL,
    role_code text,
    description text NOT NULL,
    contractual_rate numeric(18,4),
    total_days numeric(18,4),
    attendance_days numeric(18,4),
    final_amount numeric(18,4),
    is_omitted boolean DEFAULT false NOT NULL,
    omit_reason text,
    calc_notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    employee_external_id text,
    employee_name text,
    contracted_count numeric(18,4),
    absent_days numeric(18,4),
    comments text,
    human_correction jsonb
);


ALTER TABLE public.o2c_mis_summary_row OWNER TO pankaj;

--
-- Name: package_usage_period; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.package_usage_period (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    contract_terms_version_id uuid NOT NULL,
    contract_rate_line_id uuid NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    billable_quantity numeric(18,4) NOT NULL,
    quantity_unit text DEFAULT 'head'::text NOT NULL,
    confirmed_by text,
    confirmed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.package_usage_period OWNER TO pankaj;

--
-- Name: payment_terms; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.payment_terms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    contract_terms_version_id uuid NOT NULL,
    payment_due_days integer DEFAULT 30 NOT NULL,
    payment_due_trigger text DEFAULT 'invoice_receipt'::text NOT NULL,
    invoice_raise_by_day integer DEFAULT 5,
    invoice_dispute_window_days integer DEFAULT 7,
    late_payment_interest_rate numeric(5,2),
    gst_rate numeric(5,2) DEFAULT 18.00 NOT NULL,
    tds_applicable boolean DEFAULT true NOT NULL,
    tds_section text,
    tds_rate numeric(5,2),
    annual_increment_clause boolean DEFAULT true NOT NULL,
    increment_confirmation_via text DEFAULT 'email'::text,
    CONSTRAINT payment_terms_payment_due_trigger_check CHECK ((payment_due_trigger = ANY (ARRAY['invoice_date'::text, 'invoice_receipt'::text, 'month_end'::text])))
);


ALTER TABLE public.payment_terms OWNER TO pankaj;

--
-- Name: post_hitl_outbox; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.post_hitl_outbox (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    workflow_run_id uuid,
    approval_id uuid,
    workflow_key character varying(120) NOT NULL,
    canonical_workflow_key character varying(120) NOT NULL,
    status character varying(32) NOT NULL,
    snapshot jsonb,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.post_hitl_outbox OWNER TO pankaj;

--
-- Name: refresh_tokens; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.refresh_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.refresh_tokens OWNER TO pankaj;

--
-- Name: service_site; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.service_site (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    billing_client_id uuid NOT NULL,
    canonical_name text NOT NULL,
    display_name text,
    address text,
    city text,
    state text,
    pincode text,
    service_category text DEFAULT 'ohc'::text NOT NULL,
    site_key text,
    holiday_calendar_id text,
    is_active boolean DEFAULT true NOT NULL,
    active_from date,
    active_to date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT service_site_service_category_check CHECK ((service_category = ANY (ARRAY['ohc'::text, 'medical_room'::text, 'visiting_room'::text, 'cwp'::text, 'other'::text])))
);


ALTER TABLE public.service_site OWNER TO pankaj;

--
-- Name: site_alias; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.site_alias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_site_id uuid NOT NULL,
    source_system text NOT NULL,
    alias_code text NOT NULL,
    alias_display text,
    verified_by text,
    verified_at timestamp with time zone
);


ALTER TABLE public.site_alias OWNER TO pankaj;

--
-- Name: user_oauth_tokens; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.user_oauth_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    provider character varying(64) NOT NULL,
    credential_json jsonb NOT NULL,
    scopes text NOT NULL,
    account_email character varying(320),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.user_oauth_tokens OWNER TO pankaj;

--
-- Name: users; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email character varying(320) NOT NULL,
    hashed_password character varying(255) NOT NULL,
    full_name character varying(200) NOT NULL,
    department character varying(120) NOT NULL,
    role character varying(32) NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.users OWNER TO pankaj;

--
-- Name: v_billing_rules_summary; Type: VIEW; Schema: public; Owner: pankaj
--

CREATE VIEW public.v_billing_rules_summary AS
 SELECT bc.short_name AS client,
    ss.display_name AS site,
    crl.role_code,
    crl.billing_model,
    crl.rate_amount,
    crl.service_charge_type,
    crl.service_charge_value,
    ((crl.billing_rules ->> 'deduction'::text) IS NOT NULL) AS has_deduction_rule,
    crl.billing_rule_text
   FROM (((public.contract_rate_line crl
     JOIN public.contract_terms_version ctv ON ((ctv.id = crl.contract_terms_version_id)))
     JOIN public.billing_client bc ON ((bc.id = ctv.billing_client_id)))
     LEFT JOIN public.service_site ss ON ((ss.id = crl.service_site_id)))
  WHERE (ctv.status = 'active'::public.terms_status_t)
  ORDER BY bc.short_name, ss.display_name;


ALTER TABLE public.v_billing_rules_summary OWNER TO pankaj;

--
-- Name: v_expiring_contracts; Type: VIEW; Schema: public; Owner: pankaj
--

CREATE VIEW public.v_expiring_contracts AS
 SELECT ctv.id,
    ctv.title,
    ctv.effective_to,
    (ctv.effective_to - CURRENT_DATE) AS days_remaining,
    bc.name AS client_name,
    ctv.contract_kind,
    ctv.status
   FROM (public.contract_terms_version ctv
     JOIN public.billing_client bc ON ((bc.id = ctv.billing_client_id)))
  WHERE ((ctv.status = 'active'::public.terms_status_t) AND (ctv.effective_to IS NOT NULL) AND (ctv.effective_to <= (CURRENT_DATE + '90 days'::interval)))
  ORDER BY ctv.effective_to;


ALTER TABLE public.v_expiring_contracts OWNER TO pankaj;

--
-- Name: v_invoice_summary; Type: VIEW; Schema: public; Owner: pankaj
--

CREATE VIEW public.v_invoice_summary AS
 SELECT ir.id,
    ir.invoice_number,
    bc.name AS client_name,
    ss.display_name AS site_name,
    ir.billing_period_start,
    ir.billing_period_end,
    ir.subtotal,
    ir.service_charge_total,
    ir.taxable_amount,
    ir.gst_rate,
    ir.gst_amount,
    ir.total_with_gst,
    ir.tds_rate,
    ir.tds_amount,
    ir.net_payable,
    ir.status,
    ir.due_date,
    ir.paid_date,
        CASE
            WHEN (ir.status = 'overdue'::public.invoice_status_t) THEN round(((((ir.subtotal * pt.late_payment_interest_rate) / (100)::numeric) / (365)::numeric) * ((CURRENT_DATE - ir.due_date))::numeric), 2)
            ELSE (0)::numeric
        END AS interest_accrued
   FROM (((public.invoice_run ir
     JOIN public.billing_client bc ON ((bc.id = ir.billing_client_id)))
     LEFT JOIN public.service_site ss ON ((ss.id = ir.service_site_id)))
     LEFT JOIN public.payment_terms pt ON ((pt.contract_terms_version_id = ir.contract_terms_version_id)));


ALTER TABLE public.v_invoice_summary OWNER TO pankaj;

--
-- Name: v_unmatched_sites; Type: VIEW; Schema: public; Owner: pankaj
--

CREATE VIEW public.v_unmatched_sites AS
 SELECT DISTINCT aib.period_start,
    bc.name AS client_name,
    ar.service_site_code AS raw_site_code,
    count(*) AS row_count
   FROM ((public.attendance_row ar
     JOIN public.attendance_import_batch aib ON ((aib.id = ar.attendance_import_batch_id)))
     JOIN public.billing_client bc ON ((bc.id = aib.billing_client_id)))
  WHERE (ar.site_match_status = 'unmatched'::text)
  GROUP BY aib.period_start, bc.name, ar.service_site_code
  ORDER BY aib.period_start DESC, (count(*)) DESC;


ALTER TABLE public.v_unmatched_sites OWNER TO pankaj;

--
-- Name: workflow_definitions; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.workflow_definitions (
    id uuid NOT NULL,
    workflow_key character varying(120) NOT NULL,
    display_name character varying(200) NOT NULL,
    description text,
    steps jsonb NOT NULL,
    required_input_keys jsonb,
    enabled boolean NOT NULL,
    version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.workflow_definitions OWNER TO pankaj;

--
-- Name: workflow_runs; Type: TABLE; Schema: public; Owner: pankaj
--

CREATE TABLE public.workflow_runs (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    workflow_key character varying(120) NOT NULL,
    status character varying(32) NOT NULL,
    input_data jsonb,
    output_data jsonb,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.workflow_runs OWNER TO pankaj;

--
-- Name: agent_sessions agent_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: apscheduler_jobs apscheduler_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.apscheduler_jobs
    ADD CONSTRAINT apscheduler_jobs_pkey PRIMARY KEY (id);


--
-- Name: attendance_import_batch attendance_import_batch_billing_client_id_period_start_peri_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_import_batch
    ADD CONSTRAINT attendance_import_batch_billing_client_id_period_start_peri_key UNIQUE (billing_client_id, period_start, period_end, source_system);


--
-- Name: attendance_import_batch attendance_import_batch_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_import_batch
    ADD CONSTRAINT attendance_import_batch_pkey PRIMARY KEY (id);


--
-- Name: attendance_row attendance_row_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_row
    ADD CONSTRAINT attendance_row_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: automation_rules automation_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.automation_rules
    ADD CONSTRAINT automation_rules_pkey PRIMARY KEY (id);


--
-- Name: automation_suggestions automation_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.automation_suggestions
    ADD CONSTRAINT automation_suggestions_pkey PRIMARY KEY (id);


--
-- Name: billing_client billing_client_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.billing_client
    ADD CONSTRAINT billing_client_pkey PRIMARY KEY (id);


--
-- Name: billing_client billing_client_slug_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.billing_client
    ADD CONSTRAINT billing_client_slug_key UNIQUE (slug);


--
-- Name: catalog_agents catalog_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_agents
    ADD CONSTRAINT catalog_agents_pkey PRIMARY KEY (id);


--
-- Name: catalog_skill_categories catalog_skill_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_skill_categories
    ADD CONSTRAINT catalog_skill_categories_pkey PRIMARY KEY (id);


--
-- Name: catalog_skills catalog_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_skills
    ADD CONSTRAINT catalog_skills_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: chat_sessions chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id);


--
-- Name: contract_document contract_document_billing_client_id_sha256_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_document
    ADD CONSTRAINT contract_document_billing_client_id_sha256_key UNIQUE (billing_client_id, sha256);


--
-- Name: contract_document contract_document_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_document
    ADD CONSTRAINT contract_document_pkey PRIMARY KEY (id);


--
-- Name: contract_extraction_run contract_extraction_run_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_extraction_run
    ADD CONSTRAINT contract_extraction_run_pkey PRIMARY KEY (id);


--
-- Name: contract_party contract_party_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_party
    ADD CONSTRAINT contract_party_pkey PRIMARY KEY (id);


--
-- Name: contract_rate_line contract_rate_line_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_rate_line
    ADD CONSTRAINT contract_rate_line_pkey PRIMARY KEY (id);


--
-- Name: contract_terms_document contract_terms_document_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_document
    ADD CONSTRAINT contract_terms_document_pkey PRIMARY KEY (contract_terms_version_id, contract_document_id);


--
-- Name: contract_terms_version contract_terms_version_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_version
    ADD CONSTRAINT contract_terms_version_pkey PRIMARY KEY (id);


--
-- Name: failed_contract_parsing failed_contract_parsing_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.failed_contract_parsing
    ADD CONSTRAINT failed_contract_parsing_pkey PRIMARY KEY (id);


--
-- Name: integration_health integration_health_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.integration_health
    ADD CONSTRAINT integration_health_pkey PRIMARY KEY (id);


--
-- Name: invoice_line invoice_line_invoice_run_id_line_no_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_line
    ADD CONSTRAINT invoice_line_invoice_run_id_line_no_key UNIQUE (invoice_run_id, line_no);


--
-- Name: invoice_line invoice_line_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_line
    ADD CONSTRAINT invoice_line_pkey PRIMARY KEY (id);


--
-- Name: invoice_run invoice_run_invoice_number_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_invoice_number_key UNIQUE (invoice_number);


--
-- Name: invoice_run invoice_run_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: o2c_attendance_site_recon o2c_attendance_site_recon_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_attendance_site_recon
    ADD CONSTRAINT o2c_attendance_site_recon_pkey PRIMARY KEY (id);


--
-- Name: o2c_mis_run o2c_mis_run_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_run
    ADD CONSTRAINT o2c_mis_run_pkey PRIMARY KEY (id);


--
-- Name: o2c_mis_summary_row o2c_mis_summary_row_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_summary_row
    ADD CONSTRAINT o2c_mis_summary_row_pkey PRIMARY KEY (id);


--
-- Name: package_usage_period package_usage_period_contract_rate_line_id_period_start_per_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.package_usage_period
    ADD CONSTRAINT package_usage_period_contract_rate_line_id_period_start_per_key UNIQUE (contract_rate_line_id, period_start, period_end);


--
-- Name: package_usage_period package_usage_period_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.package_usage_period
    ADD CONSTRAINT package_usage_period_pkey PRIMARY KEY (id);


--
-- Name: payment_terms payment_terms_contract_terms_version_id_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.payment_terms
    ADD CONSTRAINT payment_terms_contract_terms_version_id_key UNIQUE (contract_terms_version_id);


--
-- Name: payment_terms payment_terms_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.payment_terms
    ADD CONSTRAINT payment_terms_pkey PRIMARY KEY (id);


--
-- Name: post_hitl_outbox post_hitl_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.post_hitl_outbox
    ADD CONSTRAINT post_hitl_outbox_pkey PRIMARY KEY (id);


--
-- Name: refresh_tokens refresh_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT refresh_tokens_pkey PRIMARY KEY (id);


--
-- Name: refresh_tokens refresh_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT refresh_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: service_site service_site_billing_client_id_canonical_name_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.service_site
    ADD CONSTRAINT service_site_billing_client_id_canonical_name_key UNIQUE (billing_client_id, canonical_name);


--
-- Name: service_site service_site_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.service_site
    ADD CONSTRAINT service_site_pkey PRIMARY KEY (id);


--
-- Name: site_alias site_alias_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.site_alias
    ADD CONSTRAINT site_alias_pkey PRIMARY KEY (id);


--
-- Name: site_alias site_alias_source_system_alias_code_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.site_alias
    ADD CONSTRAINT site_alias_source_system_alias_code_key UNIQUE (source_system, alias_code);


--
-- Name: agent_sessions uq_agent_sessions_thread_id; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT uq_agent_sessions_thread_id UNIQUE (thread_id);


--
-- Name: catalog_agents uq_catalog_agents_slug; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_agents
    ADD CONSTRAINT uq_catalog_agents_slug UNIQUE (slug);


--
-- Name: catalog_skill_categories uq_catalog_skill_categories_name; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_skill_categories
    ADD CONSTRAINT uq_catalog_skill_categories_name UNIQUE (name);


--
-- Name: catalog_skills uq_catalog_skills_slug; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_skills
    ADD CONSTRAINT uq_catalog_skills_slug UNIQUE (slug);


--
-- Name: integration_health uq_integration_health_name; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.integration_health
    ADD CONSTRAINT uq_integration_health_name UNIQUE (name);


--
-- Name: post_hitl_outbox uq_post_hitl_outbox_approval_id; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.post_hitl_outbox
    ADD CONSTRAINT uq_post_hitl_outbox_approval_id UNIQUE (approval_id);


--
-- Name: user_oauth_tokens uq_user_oauth_tokens_user_provider; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT uq_user_oauth_tokens_user_provider UNIQUE (user_id, provider);


--
-- Name: workflow_definitions uq_workflow_definitions_key; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.workflow_definitions
    ADD CONSTRAINT uq_workflow_definitions_key UNIQUE (workflow_key);


--
-- Name: user_oauth_tokens user_oauth_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT user_oauth_tokens_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: workflow_definitions workflow_definitions_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.workflow_definitions
    ADD CONSTRAINT workflow_definitions_pkey PRIMARY KEY (id);


--
-- Name: workflow_runs workflow_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id);


--
-- Name: ix_agent_sessions_user; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_agent_sessions_user ON public.agent_sessions USING btree (user_id, updated_at);


--
-- Name: ix_approvals_assignee_status; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_approvals_assignee_status ON public.approvals USING btree (assignee_user_id, status);


--
-- Name: ix_approvals_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_approvals_created ON public.approvals USING btree (created_at);


--
-- Name: ix_apscheduler_jobs_next_run_time; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_apscheduler_jobs_next_run_time ON public.apscheduler_jobs USING btree (next_run_time);


--
-- Name: ix_att_row_batch; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_att_row_batch ON public.attendance_row USING btree (attendance_import_batch_id);


--
-- Name: ix_att_row_resolved; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_att_row_resolved ON public.attendance_row USING btree (resolved_site_id, service_date) WHERE (resolved_site_id IS NOT NULL);


--
-- Name: ix_att_row_site_date; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_att_row_site_date ON public.attendance_row USING btree (service_site_code, service_date);


--
-- Name: ix_audit_user_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_audit_user_created ON public.audit_logs USING btree (actor_user_id, created_at);


--
-- Name: ix_automation_rules_user; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_automation_rules_user ON public.automation_rules USING btree (user_id);


--
-- Name: ix_automation_suggestions_user; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_automation_suggestions_user ON public.automation_suggestions USING btree (user_id);


--
-- Name: ix_billing_client_name; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_billing_client_name ON public.billing_client USING gin (name public.gin_trgm_ops);


--
-- Name: ix_chat_messages_session_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_chat_messages_session_created ON public.chat_messages USING btree (session_id, created_at);


--
-- Name: ix_chat_sessions_user_updated; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_chat_sessions_user_updated ON public.chat_sessions USING btree (user_id, updated_at);


--
-- Name: ix_contract_document_ingestion_root; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_contract_document_ingestion_root ON public.contract_document USING btree (ingestion_root);


--
-- Name: ix_failed_contract_folder; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_failed_contract_folder ON public.failed_contract_parsing USING btree (folder_root, created_at DESC);


--
-- Name: ix_invoice_line_run; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_invoice_line_run ON public.invoice_line USING btree (invoice_run_id);


--
-- Name: ix_invoice_run_client; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_invoice_run_client ON public.invoice_run USING btree (billing_client_id, billing_period_start);


--
-- Name: ix_invoice_run_status; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_invoice_run_status ON public.invoice_run USING btree (status);


--
-- Name: ix_notifications_user_read; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_notifications_user_read ON public.notifications USING btree (user_id, read);


--
-- Name: ix_o2c_attendance_site_recon_open; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_o2c_attendance_site_recon_open ON public.o2c_attendance_site_recon USING btree (status, last_seen_at DESC);


--
-- Name: ix_o2c_mis_run_status; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_o2c_mis_run_status ON public.o2c_mis_run USING btree (status, updated_at DESC);


--
-- Name: ix_o2c_mis_summary_run; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_o2c_mis_summary_run ON public.o2c_mis_summary_row USING btree (mis_run_id);


--
-- Name: ix_party_version; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_party_version ON public.contract_party USING btree (contract_terms_version_id);


--
-- Name: ix_post_hitl_outbox_status_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_post_hitl_outbox_status_created ON public.post_hitl_outbox USING btree (status, created_at);


--
-- Name: ix_post_hitl_outbox_user_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_post_hitl_outbox_user_created ON public.post_hitl_outbox USING btree (user_id, created_at);


--
-- Name: ix_rate_attendance; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_rate_attendance ON public.contract_rate_line USING btree (contract_terms_version_id) WHERE (billing_model = 'rate_attendance'::public.billing_model_t);


--
-- Name: ix_rate_line_site; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_rate_line_site ON public.contract_rate_line USING btree (service_site_id);


--
-- Name: ix_rate_line_version; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_rate_line_version ON public.contract_rate_line USING btree (contract_terms_version_id);


--
-- Name: ix_rate_per_visit; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_rate_per_visit ON public.contract_rate_line USING btree (contract_terms_version_id) WHERE (billing_model = 'per_visit'::public.billing_model_t);


--
-- Name: ix_refresh_tokens_user_id; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_refresh_tokens_user_id ON public.refresh_tokens USING btree (user_id);


--
-- Name: ix_site_alias_lookup; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_site_alias_lookup ON public.site_alias USING btree (source_system, alias_code);


--
-- Name: ix_terms_client; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_terms_client ON public.contract_terms_version USING btree (billing_client_id, effective_from);


--
-- Name: ix_terms_status; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_terms_status ON public.contract_terms_version USING btree (status);


--
-- Name: ix_user_oauth_tokens_user_provider; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_user_oauth_tokens_user_provider ON public.user_oauth_tokens USING btree (user_id, provider);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_workflow_definitions_enabled; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_workflow_definitions_enabled ON public.workflow_definitions USING btree (enabled);


--
-- Name: ix_workflow_runs_user_created; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE INDEX ix_workflow_runs_user_created ON public.workflow_runs USING btree (user_id, created_at);


--
-- Name: uq_o2c_attendance_site_recon_key; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE UNIQUE INDEX uq_o2c_attendance_site_recon_key ON public.o2c_attendance_site_recon USING btree (client_site_key, billing_period_start, billing_period_end, reason_code);


--
-- Name: uq_o2c_mis_run_site_period; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE UNIQUE INDEX uq_o2c_mis_run_site_period ON public.o2c_mis_run USING btree (service_site_id, billing_period_start, billing_period_end);


--
-- Name: uq_o2c_mis_summary_employee; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE UNIQUE INDEX uq_o2c_mis_summary_employee ON public.o2c_mis_summary_row USING btree (mis_run_id, contract_rate_line_id, employee_external_id);


--
-- Name: uq_service_site_client_sitekey; Type: INDEX; Schema: public; Owner: pankaj
--

CREATE UNIQUE INDEX uq_service_site_client_sitekey ON public.service_site USING btree (billing_client_id, site_key) WHERE (site_key IS NOT NULL);


--
-- Name: agent_sessions agent_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: agent_sessions agent_sessions_workflow_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_workflow_run_id_fkey FOREIGN KEY (workflow_run_id) REFERENCES public.workflow_runs(id) ON DELETE SET NULL;


--
-- Name: approvals approvals_assignee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_assignee_user_id_fkey FOREIGN KEY (assignee_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: approvals approvals_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: attendance_import_batch attendance_import_batch_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_import_batch
    ADD CONSTRAINT attendance_import_batch_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE CASCADE;


--
-- Name: attendance_row attendance_row_attendance_import_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_row
    ADD CONSTRAINT attendance_row_attendance_import_batch_id_fkey FOREIGN KEY (attendance_import_batch_id) REFERENCES public.attendance_import_batch(id) ON DELETE CASCADE;


--
-- Name: attendance_row attendance_row_resolved_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.attendance_row
    ADD CONSTRAINT attendance_row_resolved_site_id_fkey FOREIGN KEY (resolved_site_id) REFERENCES public.service_site(id);


--
-- Name: audit_logs audit_logs_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: automation_rules automation_rules_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.automation_rules
    ADD CONSTRAINT automation_rules_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: automation_suggestions automation_suggestions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.automation_suggestions
    ADD CONSTRAINT automation_suggestions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: catalog_skills catalog_skills_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.catalog_skills
    ADD CONSTRAINT catalog_skills_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.catalog_skill_categories(id) ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: chat_sessions chat_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: contract_document contract_document_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_document
    ADD CONSTRAINT contract_document_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE CASCADE;


--
-- Name: contract_extraction_run contract_extraction_run_contract_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_extraction_run
    ADD CONSTRAINT contract_extraction_run_contract_document_id_fkey FOREIGN KEY (contract_document_id) REFERENCES public.contract_document(id) ON DELETE CASCADE;


--
-- Name: contract_party contract_party_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_party
    ADD CONSTRAINT contract_party_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id);


--
-- Name: contract_party contract_party_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_party
    ADD CONSTRAINT contract_party_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id) ON DELETE CASCADE;


--
-- Name: contract_rate_line contract_rate_line_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_rate_line
    ADD CONSTRAINT contract_rate_line_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id) ON DELETE CASCADE;


--
-- Name: contract_rate_line contract_rate_line_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_rate_line
    ADD CONSTRAINT contract_rate_line_service_site_id_fkey FOREIGN KEY (service_site_id) REFERENCES public.service_site(id);


--
-- Name: contract_terms_document contract_terms_document_contract_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_document
    ADD CONSTRAINT contract_terms_document_contract_document_id_fkey FOREIGN KEY (contract_document_id) REFERENCES public.contract_document(id) ON DELETE RESTRICT;


--
-- Name: contract_terms_document contract_terms_document_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_document
    ADD CONSTRAINT contract_terms_document_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id) ON DELETE CASCADE;


--
-- Name: contract_terms_version contract_terms_version_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_version
    ADD CONSTRAINT contract_terms_version_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE RESTRICT;


--
-- Name: contract_terms_version contract_terms_version_superseded_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.contract_terms_version
    ADD CONSTRAINT contract_terms_version_superseded_by_id_fkey FOREIGN KEY (superseded_by_id) REFERENCES public.contract_terms_version(id);


--
-- Name: invoice_line invoice_line_contract_rate_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_line
    ADD CONSTRAINT invoice_line_contract_rate_line_id_fkey FOREIGN KEY (contract_rate_line_id) REFERENCES public.contract_rate_line(id);


--
-- Name: invoice_line invoice_line_invoice_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_line
    ADD CONSTRAINT invoice_line_invoice_run_id_fkey FOREIGN KEY (invoice_run_id) REFERENCES public.invoice_run(id) ON DELETE CASCADE;


--
-- Name: invoice_line invoice_line_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_line
    ADD CONSTRAINT invoice_line_service_site_id_fkey FOREIGN KEY (service_site_id) REFERENCES public.service_site(id);


--
-- Name: invoice_run invoice_run_attendance_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_attendance_batch_id_fkey FOREIGN KEY (attendance_batch_id) REFERENCES public.attendance_import_batch(id);


--
-- Name: invoice_run invoice_run_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE RESTRICT;


--
-- Name: invoice_run invoice_run_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id);


--
-- Name: invoice_run invoice_run_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.invoice_run
    ADD CONSTRAINT invoice_run_service_site_id_fkey FOREIGN KEY (service_site_id) REFERENCES public.service_site(id);


--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: o2c_attendance_site_recon o2c_attendance_site_recon_resolved_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_attendance_site_recon
    ADD CONSTRAINT o2c_attendance_site_recon_resolved_service_site_id_fkey FOREIGN KEY (resolved_service_site_id) REFERENCES public.service_site(id) ON DELETE SET NULL;


--
-- Name: o2c_mis_run o2c_mis_run_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_run
    ADD CONSTRAINT o2c_mis_run_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE RESTRICT;


--
-- Name: o2c_mis_run o2c_mis_run_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_run
    ADD CONSTRAINT o2c_mis_run_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id) ON DELETE RESTRICT;


--
-- Name: o2c_mis_run o2c_mis_run_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_run
    ADD CONSTRAINT o2c_mis_run_service_site_id_fkey FOREIGN KEY (service_site_id) REFERENCES public.service_site(id) ON DELETE RESTRICT;


--
-- Name: o2c_mis_summary_row o2c_mis_summary_row_contract_rate_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_summary_row
    ADD CONSTRAINT o2c_mis_summary_row_contract_rate_line_id_fkey FOREIGN KEY (contract_rate_line_id) REFERENCES public.contract_rate_line(id) ON DELETE RESTRICT;


--
-- Name: o2c_mis_summary_row o2c_mis_summary_row_mis_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.o2c_mis_summary_row
    ADD CONSTRAINT o2c_mis_summary_row_mis_run_id_fkey FOREIGN KEY (mis_run_id) REFERENCES public.o2c_mis_run(id) ON DELETE CASCADE;


--
-- Name: package_usage_period package_usage_period_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.package_usage_period
    ADD CONSTRAINT package_usage_period_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE CASCADE;


--
-- Name: package_usage_period package_usage_period_contract_rate_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.package_usage_period
    ADD CONSTRAINT package_usage_period_contract_rate_line_id_fkey FOREIGN KEY (contract_rate_line_id) REFERENCES public.contract_rate_line(id);


--
-- Name: package_usage_period package_usage_period_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.package_usage_period
    ADD CONSTRAINT package_usage_period_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id);


--
-- Name: payment_terms payment_terms_contract_terms_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.payment_terms
    ADD CONSTRAINT payment_terms_contract_terms_version_id_fkey FOREIGN KEY (contract_terms_version_id) REFERENCES public.contract_terms_version(id) ON DELETE CASCADE;


--
-- Name: post_hitl_outbox post_hitl_outbox_approval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.post_hitl_outbox
    ADD CONSTRAINT post_hitl_outbox_approval_id_fkey FOREIGN KEY (approval_id) REFERENCES public.approvals(id) ON DELETE SET NULL;


--
-- Name: post_hitl_outbox post_hitl_outbox_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.post_hitl_outbox
    ADD CONSTRAINT post_hitl_outbox_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: post_hitl_outbox post_hitl_outbox_workflow_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.post_hitl_outbox
    ADD CONSTRAINT post_hitl_outbox_workflow_run_id_fkey FOREIGN KEY (workflow_run_id) REFERENCES public.workflow_runs(id) ON DELETE SET NULL;


--
-- Name: refresh_tokens refresh_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT refresh_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: service_site service_site_billing_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.service_site
    ADD CONSTRAINT service_site_billing_client_id_fkey FOREIGN KEY (billing_client_id) REFERENCES public.billing_client(id) ON DELETE CASCADE;


--
-- Name: site_alias site_alias_service_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.site_alias
    ADD CONSTRAINT site_alias_service_site_id_fkey FOREIGN KEY (service_site_id) REFERENCES public.service_site(id) ON DELETE CASCADE;


--
-- Name: user_oauth_tokens user_oauth_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT user_oauth_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workflow_runs workflow_runs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: pankaj
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict yht9jsipQSVRgBJrworZJLDzxvmnJKw55JpgeC9SORnHvl6Xy5Vf8djdD2feCiu

