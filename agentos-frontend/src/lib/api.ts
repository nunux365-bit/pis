/**
 * Authenticated API client — HttpOnly cookie session, refresh on 401, scoped backend routes.
 */

import { clearTokens } from "./tokens";

function normalizeApiBase(raw: string): string {
  return raw.trim().replace(/\/+$/, "");
}

/**
 * API origin for the browser.
 * - If `NEXT_PUBLIC_API_URL` is set → browser calls that host directly (CORS must allow the web app).
 * - If unset/empty → same origin as the Next app; `next.config` rewrites `/api/*` and `/health` to BACKEND_URL.
 */
const rawPublicApi = process.env.NEXT_PUBLIC_API_URL;
export const API_BASE = normalizeApiBase(
  typeof rawPublicApi === "string" && rawPublicApi.trim() !== ""
    ? rawPublicApi
    : ""
);

type Json = Record<string, unknown>;

async function tryRefresh(): Promise<boolean> {
  const res = await fetch(`${API_BASE}/api/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: "{}",
  });
  if (!res.ok) {
    clearTokens();
    return false;
  }
  try {
    await res.json();
  } catch {
    clearTokens();
    return false;
  }
  preferCookieSession();
  return true;
}

/** Ensure no stale JWT copies in sessionStorage (auth is HttpOnly cookies only). */
function preferCookieSession() {
  clearTokens();
}

export async function apiFetch(
  path: string,
  init: RequestInit = {},
  retry = true
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (
    init.body !== undefined &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    credentials: init.credentials ?? "include",
  });
  if (res.status === 401 && retry) {
    const ok = await tryRefresh();
    if (ok) return apiFetch(path, init, false);
  }
  return res;
}

/** Turn FastAPI ``detail`` (string | object | validation array) into readable text for users. */
export function formatApiErrorDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return "";
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) {
    const lines: string[] = [];
    for (const item of detail) {
      if (typeof item === "string") {
        lines.push(item);
        continue;
      }
      if (item && typeof item === "object") {
        const o = item as Record<string, unknown>;
        const msg =
          typeof o.msg === "string"
            ? o.msg
            : typeof o.message === "string"
              ? o.message
              : null;
        if (msg) {
          const loc = Array.isArray(o.loc)
            ? o.loc
              .filter((x) => typeof x === "string" || typeof x === "number")
              .join(" → ")
            : "";
          lines.push(loc ? `${loc}: ${msg}` : msg);
          continue;
        }
      }
      lines.push(JSON.stringify(item));
    }
    const out = lines.filter(Boolean).join("\n").trim();
    return out || JSON.stringify(detail);
  }
  if (typeof detail === "object") {
    const o = detail as Record<string, unknown>;
    if (typeof o.message === "string" && o.message.trim()) return o.message.trim();
    if (typeof o.msg === "string" && typeof o.type === "string") return `${o.type}: ${o.msg}`;
    if (typeof o.msg === "string") return o.msg;
    return JSON.stringify(detail);
  }
  return String(detail);
}

/** Human message from a failed ``Response`` (reads body once). */
export async function messageFromFailedResponse(
  res: Response,
  fallback?: string
): Promise<string> {
  const fb = fallback ?? `Request failed (${res.status})`;
  let text = "";
  try {
    text = await res.text();
  } catch {
    return fb;
  }
  const trimmed = text.trim();
  if (!trimmed) return fb;
  if (trimmed.startsWith("<!DOCTYPE") || trimmed.startsWith("<html")) {
    const title = trimmed.match(/<title[^>]*>([^<]*)<\/title>/i)?.[1]?.trim();
    if (title) return title;
    if (res.status === 504) return "Request timed out (504). The server took too long to respond.";
    return `Request failed (${res.status})`;
  }
  try {
    const j = JSON.parse(trimmed) as { detail?: unknown };
    const formatted = formatApiErrorDetail(j.detail);
    if (formatted) return formatted;
  } catch {
    return trimmed.slice(0, 800);
  }
  return trimmed.slice(0, 800);
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init);
  if (!res.ok) {
    const msg = await messageFromFailedResponse(res, res.statusText);
    throw new Error(msg || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function loginRequest(email: string, password: string) {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    throw new Error(await messageFromFailedResponse(res, "Invalid email or password"));
  }
  try {
    await res.json();
  } catch {
    throw new Error("Invalid response from server");
  }
  preferCookieSession();
}

export async function logoutRequest() {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: "{}",
    });
  } catch {
    /* ignore */
  }
  clearTokens();
}

export type UserMe = {
  id: string;
  email: string;
  full_name: string;
  department: string;
  roles: string[];
  /** Primary role (first in ``roles``) — backward-compatible alias from API */
  role?: string;
  is_active: boolean;
};

/** Normalize ``/me`` payload (supports legacy single ``role`` during rollout). */
export function normalizeUserMe(raw: UserMe): UserMe {
  const roles =
    raw.roles?.length ? raw.roles : raw.role ? [raw.role] : ["employee"];
  return { ...raw, roles, role: raw.role ?? roles[0] };
}

export async function fetchMe(): Promise<UserMe> {
  const raw = await apiJson<UserMe>("/api/auth/me");
  return normalizeUserMe(raw);
}

/** Load /me without throwing — for auth bootstrap (distinguish 401 vs transient errors). */
export type LoadUserResult =
  | { ok: true; user: UserMe }
  | { ok: false; clearSession: boolean; message: string };

export async function loadCurrentUser(): Promise<LoadUserResult> {
  // Session is HttpOnly cookies only (see preferCookieSession after login / refresh). /me uses credentials.
  try {
    const res = await apiFetch("/api/auth/me", {}, true);
    if (res.status === 401) {
      clearTokens();
      return { ok: false, clearSession: true, message: "Session expired" };
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      return {
        ok: false,
        clearSession: false,
        message: text?.slice(0, 200) || `HTTP ${res.status}`,
      };
    }
    const user = normalizeUserMe((await res.json()) as UserMe);
    return { ok: true, user };
  } catch (e) {
    return {
      ok: false,
      clearSession: false,
      message: e instanceof Error ? e.message : "Network error",
    };
  }
}

export async function sendChatMessage(message: string, sessionId?: string | null) {
  return apiJson<{
    user_message: ChatMessageDto;
    assistant_message: ChatMessageDto;
  }>("/api/chat/message", {
    method: "POST",
    body: JSON.stringify({
      message,
      session_id: sessionId ?? null,
    }),
  });
}

export type ChatMessageDto = {
  id: string;
  role: string;
  content: string;
  meta?: Record<string, unknown> | null;
  created_at: string;
};

export type ChatSessionDto = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export async function listChatSessions() {
  return apiJson<ChatSessionDto[]>("/api/chat/sessions");
}

export async function createChatSession(title?: string) {
  return apiJson<ChatSessionDto>("/api/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export async function listChatMessages(sessionId: string) {
  return apiJson<ChatMessageDto[]>(`/api/chat/sessions/${sessionId}/messages`);
}

export async function listPendingApprovals() {
  return apiJson<{ items: ApprovalDto[]; count: number }>("/api/approvals/pending");
}

export async function listApprovals(status?: string) {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiJson<ApprovalDto[]>(`/api/approvals${q}`);
}

export type ApprovalDto = {
  id: string;
  title: string;
  description: string | null;
  status: string;
  amount: string | null;
  currency: string;
  confidence: number | null;
  risk: string;
  agent_name: string;
  assignee_user_id: string;
  created_by_user_id: string | null;
  created_at: string;
  decided_at: string | null;
  workflow_key?: string | null;
};

export type AgentRow = {
  name: string;
  status: string;
  ring: number;
  description?: string;
  tasks_today?: number;
  uptime_pct?: number;
};

export async function listAgents() {
  return apiJson<{ agents: AgentRow[] }>("/api/agents/");
}

export type WorkflowCatalogNode = {
  id: string;
  title: string;
  role: string;
  description: string;
};

export type WorkflowCatalogEdge = {
  from: string;
  to: string;
};

export type WorkflowCatalogItem = {
  key: string;
  display_name: string;
  summary: string;
  status: string;
  source: string;
  nodes: WorkflowCatalogNode[];
  edges: WorkflowCatalogEdge[];
};

export async function listWorkflowCatalog() {
  return apiJson<{ workflows: WorkflowCatalogItem[] }>("/api/workflows/catalog");
}

export type ComplianceRunSummary = {
  window_days: number;
  completed: number;
  failed: number;
  workflow_key: string;
};

export type ComplianceSourceType = "drive" | "aws" | "ozontel";

export type ComplianceRunRow = {
  id: string;
  status: string;
  created_at: string;
  doctor_slug: string | null;
  doctor_name: string | null;
  filename: string | null;
  source_file_id: string | null;
  source_type: ComplianceSourceType;
  composite_pct: number | null;
  grade: string | null;
  grade_label: string | null;
  error_message: string | null;
  sheet_appended: boolean | null;
  mysql_second_opinion_conversation_id: number | null;
};

export async function getComplianceRunsSummary(days = 7) {
  return apiJson<ComplianceRunSummary>(`/api/compliance/runs/summary?days=${days}`);
}

export type ComplianceRunsListResponse = {
  runs: ComplianceRunRow[];
  total: number;
  limit: number;
  offset: number;
};

export type ComplianceTimeseriesPoint = {
  bucket_start: string;
  total: number;
  completed: number;
  failed: number;
};

export type ComplianceTimeseriesResponse = {
  window_days: number;
  granularity: "day" | "week";
  points: ComplianceTimeseriesPoint[];
};

export type ComplianceGradeSlice = { grade: string; count: number };

export type ComplianceGradeDistributionResponse = {
  window_days: number;
  slices: ComplianceGradeSlice[];
};

export type ComplianceListParams = {
  limit?: number;
  offset?: number;
  since?: string | null;
  until?: string | null;
  search?: string | null;
  status?: string | null;
  grades?: string[] | null;
  doctor?: string | null;
  score_min?: number | null;
  score_max?: number | null;
  sheet_appended?: boolean | null;
  sort_key?: string;
  sort_dir?: "asc" | "desc";
};

/** Shared query builder so the list and CSV export always apply the same filter set. */
function complianceQuery(params?: ComplianceListParams): URLSearchParams {
  const q = new URLSearchParams();
  if (params?.since) q.set("since", params.since);
  if (params?.until) q.set("until", params.until);
  if (params?.search?.trim()) q.set("search", params.search.trim());
  if (params?.status) q.set("status", params.status);
  for (const g of params?.grades ?? []) q.append("grade", g);
  if (params?.doctor?.trim()) q.set("doctor", params.doctor.trim());
  if (params?.score_min != null) q.set("score_min", String(params.score_min));
  if (params?.score_max != null) q.set("score_max", String(params.score_max));
  if (params?.sheet_appended != null) q.set("sheet_appended", String(params.sheet_appended));
  if (params?.sort_key) q.set("sort_key", params.sort_key);
  if (params?.sort_dir) q.set("sort_dir", params.sort_dir);
  return q;
}

export async function listComplianceRuns(params?: ComplianceListParams) {
  const q = complianceQuery(params);
  if (params?.limit != null) q.set("limit", String(params.limit));
  if (params?.offset != null) q.set("offset", String(params.offset));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiJson<ComplianceRunsListResponse>(`/api/compliance/runs${suffix}`);
}

export type ComplianceDoctorOption = {
  doctor_slug: string | null;
  doctor_name: string | null;
  label: string;
  run_count: number;
};

export async function getComplianceDoctors() {
  return apiJson<{ doctors: ComplianceDoctorOption[] }>(`/api/compliance/runs/doctors`);
}

/**
 * Bulk export of the current filter set as CSV.
 *
 * Goes through ``apiFetch`` (not a bare anchor href) so the session cookie and the
 * 401-refresh retry both apply; returns the blob plus the server-supplied filename.
 */
export async function exportComplianceRunsCsv(
  params?: ComplianceListParams
): Promise<{ blob: Blob; filename: string }> {
  const q = complianceQuery(params);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await apiFetch(`/api/compliance/runs/export${suffix}`);
  if (!res.ok) {
    const msg = await messageFromFailedResponse(res, res.statusText);
    throw new Error(msg || `HTTP ${res.status}`);
  }
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/i.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? "call-quality-export.csv" };
}

export async function getComplianceRunsTimeseries(days = 30, granularity: "day" | "week" = "day") {
  const q = new URLSearchParams({ days: String(days), granularity });
  return apiJson<ComplianceTimeseriesResponse>(`/api/compliance/runs/timeseries?${q.toString()}`);
}

export async function getComplianceRunsGradeDistribution(days = 30) {
  return apiJson<ComplianceGradeDistributionResponse>(
    `/api/compliance/runs/grade_distribution?days=${days}`
  );
}

export async function getComplianceRun(id: string) {
  return apiJson<{
    id: string;
    status: string;
    created_at: string;
    updated_at: string;
    input_data: Record<string, unknown>;
    output_data: Record<string, unknown>;
    error_message: string | null;
  }>(`/api/compliance/runs/${id}`);
}

export type SkillItem = { id: string; title: string; version: string };
export type SkillCategory = {
  name: string;
  count: number;
  skills: SkillItem[];
};

export async function listSkills() {
  return apiJson<{ categories: SkillCategory[] }>("/api/skills/");
}

export async function getAnalyticsSummary() {
  return apiJson<{
    pending_approvals: number;
    automation_rate_30d: number;
    active_agents: string;
    department_automation: { dept: string; auto_pct: number; tasks: number }[];
  }>("/api/analytics/summary");
}

export type EmailAutomationMetricsWindow = "24h" | "7d" | "14d" | "30d";

export type EmailAutomationMetricsHealth = {
  enabled: boolean;
  test_mode: boolean;
  last_message_received_at: string | null;
  last_send_sent_at: string | null;
};

export type EmailAutomationMetricsAttention = {
  sends_at_attempt_cap: number;
  messages_processed_with_errors_open: number;
  sends_stuck_sending: number;
};

export type EmailAutomationMetricsTrendPoint = {
  bucket: string;
  sent: number;
  failed: number;
  ingested: number;
};

export type DashboardMisPeriod = {
  scope: string;
  pending_human: number;
  approved: number;
  rejected: number;
  total: number;
};

export type DashboardApprovalAging = {
  lt_1h: number;
  lt_4h: number;
  lt_24h: number;
  lt_3d: number;
  lt_7d: number;
  gte_7d: number;
};

export type DashboardApprovalOriginator = {
  agent_name: string;
  pending: number;
};

export type DashboardWorkflowFailureRow = {
  workflow_key: string;
  failed: number;
};

export type DashboardO2cDailyPoint = {
  bucket: string; // YYYY-MM-DD (IST)
  created: number;
  approved: number;
  rejected: number;
};

export type DashboardO2cRevenue = {
  sites_approved: number;
  sites_pending: number;
  sites_in_scope: number;
  revenue_billed: number;
  revenue_at_risk: number;
};

export type DashboardO2cMonthProgress = {
  scope: string;
  month_label: string; // YYYY-MM
  days_in_month: number;
  day_of_month: number;
  generated_at_utc: string;
};

export type DashboardO2cPeriod = {
  scope: string;
  daily: DashboardO2cDailyPoint[];
  revenue: DashboardO2cRevenue | null;
  progress: DashboardO2cMonthProgress | null;
};

/** One row of the last-6-month MIS series (single query, oldest → newest). */
export type DashboardMisMonthRow = {
  month_key: string; // YYYY-MM (IST)
  total: number;
  approved: number;
  rejected: number;
  pending_human: number;
  sites_in_scope: number;
  sites_approved: number;
  sites_pending: number;
  revenue_billed: number;
  revenue_at_risk: number;
  /** MIS runs with ≥1 summary row where `human_correction` is set. */
  human_corrected_runs: number;
  approval_rate: number;
};

export type DashboardIntegration = {
  name: string;
  system_type: string;
  status: string;
  health_pct: number;
  last_sync_at: string | null;
  last_error: string | null;
  updated_at: string | null;
};

/** Top billing client row, per IST month (billed + at-risk INR, sites). */
export type DashboardO2cTopClient = {
  client_id: string;
  client_name: string;
  sites: number;
  approved_runs: number;
  pending_runs: number;
  revenue_billed: number;
  revenue_at_risk: number;
};

/** Top service site row, per IST month. */
export type DashboardO2cTopSite = {
  site_id: string;
  site_name: string;
  client_name: string;
  city: string;
  last_status: string;
  revenue_billed: number;
  revenue_at_risk: number;
};

/** Top clients for one IST month (``month_key`` = YYYY-MM; series is oldest → newest). */
export type DashboardO2cTopClientsByMonth = {
  month_key: string;
  rows: DashboardO2cTopClient[];
};

/** Top sites for one IST month (``month_key`` = YYYY-MM; series is oldest → newest). */
export type DashboardO2cTopSitesByMonth = {
  month_key: string;
  rows: DashboardO2cTopSite[];
};

/** One row of the contract renewal watch list. */
export type DashboardContractRenewalRow = {
  contract_id: string;
  client_name: string;
  effective_to: string; // YYYY-MM-DD
  days_remaining: number;
  kind: string;
  monthly_estimate: number;
  sites: number;
  /** expired | orange (≤90d) | yellow (91–180d) | ok */
  urgency: string;
};

export type DashboardContractRenewalSummary = {
  window_days: number;
  count_total: number;
  count_expired: number;
  count_orange: number;
  count_yellow: number;
  count_ok: number;
  monthly_estimate_total: number;
  as_of: string; // YYYY-MM-DD
};

/** Bucket counts for contract lifecycle pie. */
export type DashboardContractStageSlice = {
  stage: string;
  count: number;
};

/** Per-month procurement activity (UTC month bucket, oldest → newest in API). */
export type DashboardProcurementMonthPoint = {
  month_key: string;
  created: number;
  posted: number;
  posted_pct: number;
};

/** Procurement: rolling window KPIs + 6-month series. */
export type DashboardProcurementThroughput = {
  window_days: number;
  created: number;
  posted: number;
  pending_sap: number;
  stuck_ge_7d: number;
  posted_pct: number;
  by_kind: { kind: string; count: number; posted: number }[];
  monthly_6m: DashboardProcurementMonthPoint[];
};

/** One round-trip: approvals, email 14d+30d + ghost, workflow, O2C month, integrations, operator surface. */
export type DashboardSummary = {
  pending_approvals: number;
  automation_rate_30d: number;
  active_agents: string;
  department_automation: { dept: string; auto_pct: number; tasks: number }[];
  approvals: {
    aging: DashboardApprovalAging;
    top_originators: DashboardApprovalOriginator[];
  };
  email: {
    window: string;
    bucket_granularity: string;
    health: EmailAutomationMetricsHealth;
    messages_by_status: Record<string, number>;
    sends_by_status: Record<string, number>;
    sends_skipped_by_reason: Record<string, number>;
    sends_failed_by_reason: Record<string, number>;
    attention: EmailAutomationMetricsAttention;
    trend: EmailAutomationMetricsTrendPoint[];
    trend_prior: EmailAutomationMetricsTrendPoint[];
  };
  email_30d: {
    window: "30d";
    messages_by_status: Record<string, number>;
    sends_by_status: Record<string, number>;
  };
  operator: {
    notifications_unread: number;
    workflow: { active_runs: number; failed_30d: number };
    procurement: { my_tickets: number; pending_sap_id: number };
  };
  workflow_runs: {
    status_distribution: Record<string, number>;
    failures_by_key: DashboardWorkflowFailureRow[];
  };
  mis: {
    current_month: DashboardMisPeriod | null;
    prior_month: DashboardMisPeriod | null;
    /** Oldest → newest, last element is the current IST month. */
    monthly: DashboardMisMonthRow[];
  } | null;
  /** Always present on current API (may have null current_month if billing DB empty). */
  o2c: {
    current_month: DashboardO2cPeriod | null;
    top_clients: DashboardO2cTopClientsByMonth[];
    top_sites: DashboardO2cTopSitesByMonth[];
  } | null;
  contracts: {
    summary: DashboardContractRenewalSummary | null;
    contracts: DashboardContractRenewalRow[];
    stages: DashboardContractStageSlice[];
  } | null;
  procurement: DashboardProcurementThroughput | null;
  integrations: DashboardIntegration[];
  generated_at: string;
};

export async function getDashboardSummary() {
  return apiJson<DashboardSummary>("/api/dashboard/summary");
}

export type EmailAutomationMetricsResponse = {
  window: string;
  generated_at: string;
  bucket_granularity: string;
  health: EmailAutomationMetricsHealth;
  messages_by_status: Record<string, number>;
  sends_by_status: Record<string, number>;
  sends_by_variant: Record<string, Record<string, number>>;
  /** Counts of skipped sends in the window, keyed by primary `review_reasons[0].code` */
  sends_skipped_by_reason: Record<string, number>;
  /** Failed sends in the window, keyed by `error.type` / short `error.error` / `long_error` */
  sends_failed_by_reason: Record<string, number>;
  trend: EmailAutomationMetricsTrendPoint[];
  attention: EmailAutomationMetricsAttention;
};

export async function getEmailAutomationMetrics(
  window: EmailAutomationMetricsWindow = "24h"
) {
  const q = encodeURIComponent(window);
  return apiJson<EmailAutomationMetricsResponse>(
    `/api/email-automation/metrics?window=${q}`
  );
}

/** KAM Dashboard — per-KAM reply-tracking metrics (admin API). L7D only. */
export type KamDashboardWindow = "7d";

export type KamDashboardRow = {
  kam_name: string;
  total_overdue_lakh: number; // ₹ lakh, TDS-adjusted
  reply_rate: number; // 0–100
  accuracy_rate: number; // 0–100
  avg_reply_seconds: number | null;
  threads_scored: number;
  client_replied_threads: number;
};

export type KamDashboardResponse = {
  window: string;
  generated_at: string;
  rows: KamDashboardRow[];
};

export async function getKamDashboardMetrics(window: KamDashboardWindow = "7d") {
  const q = encodeURIComponent(window);
  return apiJson<KamDashboardResponse>(
    `/api/email-automation/intelligence/kam-metrics?window=${q}`
  );
}

/** Collections reply intelligence — aggregates + gap-filled timeline (admin API). */
export type EmailIntelligenceTimelinePoint = {
  bucket: string;
  count: number;
};

export type EmailIntelligencePriorPeriod = {
  total_threads: number;
  by_category: Record<string, number>;
  by_confidence?: Record<string, number>;
  low_confidence_count: number;
};

export type EmailIntelligenceMetricsResponse = {
  window: string;
  generated_at: string;
  kind: string;
  bucket_granularity?: "hour" | "day";
  total_threads: number;
  by_category: Record<string, number>;
  by_confidence?: Record<string, number>;
  low_confidence_count: number;
  timeline?: EmailIntelligenceTimelinePoint[];
  prior_period?: EmailIntelligencePriorPeriod;
};

export type EmailIntelligenceWindow = "24h" | "7d" | "30d";

/** @deprecated Part of the deprecated Replies tab. */
export async function getEmailIntelligenceMetrics(
  window: EmailIntelligenceWindow = "30d"
) {
  const q = encodeURIComponent(window);
  return apiJson<EmailIntelligenceMetricsResponse>(
    `/api/email-automation/intelligence/metrics?window=${q}`
  );
}

export type EmailIntelligenceThreadRow = {
  id: string;
  gmail_thread_id: string;
  kind: string;
  category: string;
  confidence: string;
  business_key: string | null;
  workflow_type: string | null;
  variant: string | null;
  classified_at: string;
  trigger_message_id: string | null;
  anchor_send_id: string | null;
};

export type EmailIntelligenceThreadsPage = {
  items: EmailIntelligenceThreadRow[];
  next_cursor: string | null;
  has_more: boolean;
};

/** @deprecated Part of the deprecated Replies tab. */
export async function getEmailIntelligenceThreads(options?: {
  limit?: number;
  cursor?: string | null;
  category?: string | null;
  /** Substring match on thread id, business key, workflow, variant (server-side). */
  search?: string | null;
  /** Rolling window on `classified_at` — must match metrics window for consistent counts. */
  window?: EmailIntelligenceWindow;
}) {
  const limit = options?.limit ?? 50;
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  const window = options?.window ?? "30d";
  params.set("window", window);
  if (options?.cursor) {
    params.set("cursor", options.cursor);
  }
  if (options?.category) {
    params.set("category", options.category);
  }
  if (options?.search?.trim()) {
    params.set("search", options.search.trim());
  }
  return apiJson<EmailIntelligenceThreadsPage>(
    `/api/email-automation/intelligence/threads?${params.toString()}`
  );
}

export type ReceivableIntelligenceRow = {
  hana_code: string;
  party_name: string;
  business_unit: string;
  total_overdue_lakh: number;
  email_sent_at: string | null;
  reply_received_at: string | null;
  reply_category: string | null;
  gmail_thread_id: string | null;
  email_subject: string | null;
};

export type ReceivableIntelligenceResponse = {
  items: ReceivableIntelligenceRow[];
  business_units: string[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export async function getReceivableIntelligence(options?: {
  businessUnit?: string;
  replyCategory?: string;
  emailSent?: "yes" | "no";
  search?: string;
  windowDays?: ReplyTrackerWindowDays;
  limit?: number;
  offset?: number;
}) {
  const bu = options?.businessUnit ?? "All";
  const q = new URLSearchParams();
  if (bu !== "All") q.set("business_unit", bu);
  if (options?.replyCategory) q.set("reply_category", options.replyCategory);
  if (options?.emailSent) q.set("email_sent", options.emailSent);
  if (options?.search?.trim()) q.set("search", options.search.trim());
  if (options?.windowDays) q.set("window_days", String(options.windowDays));
  if (options?.limit) q.set("limit", String(options.limit));
  if (options?.offset) q.set("offset", String(options.offset));
  return apiJson<ReceivableIntelligenceResponse>(
    `/api/email-automation/intelligence/receivables?${q.toString()}`
  );
}

/**
 * Download the party-level receivables XLSX.
 * - No `windowDays` → "Export All Data" (all-time, all BUs).
 * - `windowDays` set → "current view": send-anchored to that window, scoped to `businessUnit`.
 */
export async function exportReceivableIntelligence(options?: {
  windowDays?: ReplyTrackerWindowDays;
  businessUnit?: string;
}): Promise<void> {
  const q = new URLSearchParams();
  if (options?.windowDays) {
    q.set("window_days", String(options.windowDays));
    if (options.businessUnit && options.businessUnit !== "All") {
      q.set("business_unit", options.businessUnit);
    }
  }
  const qs = q.toString();
  const res = await apiFetch(
    `/api/email-automation/intelligence/receivables/export${qs ? `?${qs}` : ""}`
  );
  if (!res.ok) throw new Error(`Export failed: ${res.status}`);

  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match?.[1] ?? "receivables_export.xlsx";

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export type EmailThreadMessage = {
  message_id: string;
  from_addr: string;
  to_addr: string;
  cc_addr?: string;
  subject: string;
  date_ms: number;   // UTC epoch milliseconds
  body: string;
};

export type EmailThreadResponse = {
  thread_id: string;
  messages: EmailThreadMessage[];
};

/** Fetch all messages in a Gmail thread. Requires admin auth. */
export async function getEmailThread(threadId: string): Promise<EmailThreadResponse> {
  return apiJson<EmailThreadResponse>(
    `/api/email-automation/intelligence/thread/${encodeURIComponent(threadId)}`
  );
}

export type CategoryBreakdownRow = {
  category: string;
  reply_count: number;
  pct_of_replies: number;   // 0–100
  amount_due_lakh: number;
};

export type IntelligenceSummaryResponse = {
  business_unit: string | null;
  total_overdue_book_lakh: number;
  total_overdue_in_campaign_lakh: number;
  total_overdue_replies_lakh: number;
  emails_delivered: number;
  replies_received: number;
  reply_rate: number;       // 0–100
  category_breakdown: CategoryBreakdownRow[];
  window_days: number;      // active send-anchored view (7 / 14 / 30)
  snapshot_created_at: string | null;
  generated_at: string;
};

/** Reply Tracker view windows (send-anchored), in days. */
export type ReplyTrackerWindowDays = 7 | 14 | 30;

export async function getIntelligenceSummary(options?: {
  businessUnit?: string;
  windowDays?: ReplyTrackerWindowDays;
}): Promise<IntelligenceSummaryResponse> {
  const q = new URLSearchParams();
  if (options?.businessUnit && options.businessUnit !== "All") {
    q.set("business_unit", options.businessUnit);
  }
  if (options?.windowDays) q.set("window_days", String(options.windowDays));
  const qs = q.toString();
  return apiJson<IntelligenceSummaryResponse>(
    `/api/email-automation/intelligence/summary${qs ? `?${qs}` : ""}`
  );
}

/** Latest receivables executive snapshot (from weekly `receivables` .xlsx). */
export type ReceivableDashboardSnapshot = {
  id: string;
  created_at: string | null;
  source_message_id: string | null;
  payload: {
    meta?: {
      parser_version?: string;
      /** e.g. ``excel_5x6_v1`` = five ageing rows × six columns ``"0-1"``, ``"2"``…``"5"``, ``"6+"``. */
      reminder_grid_format?: string;
      workbook?: string;
      receivable_sheet?: string;
      unbilled_sheet?: string;
      source_message_id?: string;
      /** From live `email_automation_sends` when the snapshot was built. */
      reminder_sends?: {
        table: string;
        filters: {
          status_in?: string[];
          workflow_type: string;
          test_mode: boolean;
        };
        aggregation: string;
        row_count: number;
        distinct_business_keys: number;
        distinct_period_keys: number;
      };
      /** Parties where ageing-bucket sum significantly exceeds Net Due — stale SAP extract. */
      data_quality?: {
        stale_bucket_count: number;
        stale_bucket_parties: {
          code: string;
          name: string;
          bu: string;
          bucket_sum_lakh: number;
          net_due_lakh: number;
          gap_lakh: number;
        }[];
        /** Parties where TDS exceeded the receivable bucket — value was clamped to 0. */
        tds_clip_count?: number;
        tds_clip_parties?: {
          code: string;
          name: string;
          bu: string;
          bucket: string;
          rec_lakh: number;
          tds_lakh: number;
        }[];
      };
    };
    business_units?: string[];
    kpi_lakh?: {
      all?: Record<string, number>;
      by_business_unit?: Record<string, Record<string, number>>;
    };
    unbilled_lakh?: { all?: number; by_business_unit?: Record<string, number> };
    top_parties?: {
      all?: {
        code: string;
        name: string;
        business_unit: string;
        /** For Excel parity this is ``SUM(ageing buckets)`` in ₹ Lakh, not always ``Net Due``. */
        net_due_lakh: number;
        /** Weighted average ageing (months) on bucket amounts (executive ``J`` column). */
        wtd_age_months?: number;
      }[];
      by_business_unit?: Record<string, {
        code: string;
        name: string;
        business_unit: string;
        net_due_lakh: number;
        wtd_age_months?: number;
      }[]>;
    };
    collection_grids?: {
      all?: {
        reminder_bands: string[];
        /** Five executive-style ageing bands (0–1 … 1Y+); “Not due” is never a row. */
        ageing_buckets: string[];
        values_lakh: number[][];
        /** One line per party: `Name (₹X L, Ym)` (wtd age months), not name-only. */
        client_names: string[][][];
      };
      by_business_unit?: Record<string, {
        reminder_bands: string[];
        ageing_buckets: string[];
        values_lakh: number[][];
        client_names: string[][][];
      }>;
    };
  };
};

export async function getReceivableDashboard() {
  return apiJson<ReceivableDashboardSnapshot | null>(
    "/api/email-automation/receivable-dashboard"
  );
}

export async function commandSearch(q: string) {
  return apiJson<{ query: string; hits: SearchHit[] }>("/api/search/", {
    method: "POST",
    body: JSON.stringify({ q, limit: 20 }),
  });
}

export type SearchHit = {
  type: string;
  label: string;
  href: string;
  id?: string;
};

export async function listNotifications(unreadOnly = false) {
  return apiJson<{ notifications: NotificationDto[] }>(
    `/api/notifications/?unread_only=${unreadOnly}`
  );
}

export type NotificationDto = {
  id: string;
  category: string;
  title: string;
  body: string | null;
  read: boolean;
  link: string | null;
  created_at: string;
};

export async function markAllNotificationsRead() {
  return apiJson<{ ok: boolean }>("/api/notifications/mark-all-read", {
    method: "POST",
  });
}

export async function listAgentSessions() {
  return apiJson<{ sessions: AgentSessionDto[] }>("/api/sessions");
}

export async function createAgentSession(
  workflowName: string,
  threadId?: string | null
) {
  return apiJson<AgentSessionDto>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      workflow_name: workflowName,
      thread_id: threadId ?? null,
    }),
  });
}

export type AgentSessionDto = {
  id: string;
  thread_id: string;
  workflow_name: string;
  status: string;
  progress_pct: number;
  last_checkpoint_at: string | null;
  created_at: string;
  updated_at: string;
};

export async function listIntegrations() {
  return apiJson<{ integrations: IntegrationDto[] }>("/api/integrations");
}

export type IntegrationDto = {
  name: string;
  type: string;
  status: string;
  health: number;
  modules: string | null;
  last_sync: string | null;
  last_error: string | null;
};

export async function approveItem(id: string) {
  return apiJson<ApprovalDto>(`/api/approvals/${id}/approve`, { method: "POST" });
}

export async function rejectItem(id: string) {
  return apiJson<ApprovalDto>(`/api/approvals/${id}/reject`, { method: "POST" });
}

export async function listAuditEntries() {
  return apiJson<{ entries: AuditEntryDto[] }>("/api/audit?limit=200");
}

export type AuditEntryDto = {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  actor_user_id: string | null;
  details: Json | null;
  created_at: string;
};

export type AutomationRuleDto = {
  id: string;
  name: string;
  trigger_description: string;
  enabled: boolean;
  confidence_threshold: number;
  execution_count: number;
};

export type AutomationSuggestionDto = {
  id: string;
  title: string;
  pattern_summary: string;
  est_savings: string | null;
  status: string;
};

export async function listAutomationRules() {
  return apiJson<AutomationRuleDto[]>("/api/automations/rules");
}

export async function listAutomationSuggestions() {
  return apiJson<AutomationSuggestionDto[]>("/api/automations/suggestions");
}

export async function createAutomationRule(body: {
  name: string;
  trigger_description: string;
  confidence_threshold?: number;
}) {
  return apiJson<AutomationRuleDto>("/api/automations/rules", {
    method: "POST",
    body: JSON.stringify({
      name: body.name,
      trigger_description: body.trigger_description,
      confidence_threshold: body.confidence_threshold ?? 90,
    }),
  });
}

export async function toggleAutomationRule(ruleId: string) {
  return apiJson<AutomationRuleDto>(
    `/api/automations/rules/${ruleId}/toggle`,
    { method: "PATCH" }
  );
}

export async function enableAutomationSuggestion(suggestionId: string) {
  return apiJson<AutomationSuggestionDto>(
    `/api/automations/suggestions/${suggestionId}/enable`,
    { method: "POST" }
  );
}

export async function triggerWorkflow(
  workflowKey: string,
  payload?: Record<string, unknown> | null
) {
  return apiJson<{
    workflow_id: string;
    status: string;
    hitl?: boolean;
    message?: string;
  }>("/api/workflows/trigger", {
    method: "POST",
    body: JSON.stringify({
      workflow_key: workflowKey,
      payload: payload ?? null,
    }),
  });
}

export type WorkflowRunDto = {
  id: string;
  workflow_key: string;
  status: string;
  created_at: string;
  output: Record<string, unknown> | null;
};

export async function listWorkflowRuns(limit = 50) {
  return apiJson<{ runs: WorkflowRunDto[] }>(
    `/api/workflows?limit=${Math.min(limit, 100)}`
  );
}

export type O2CSiteRow = {
  id: string;
  billing_client_id: string;
  billing_client_name: string;
  site_key: string;
  display_name: string;
  canonical_name: string;
};

export async function listO2CSites(query = "", limit = 100) {
  const q = new URLSearchParams({
    query,
    limit: String(limit),
  });
  return apiJson<{ count: number; items: O2CSiteRow[] }>(`/api/o2c/site-alias/sites?${q.toString()}`);
}

export type O2CAliasUpsertResult = {
  status: string;
  alias: {
    source_system: string;
    alias_code: string;
    alias_display: string;
    service_site_id: string;
  };
  site: {
    service_site_id: string;
    billing_client_id: string;
    site_key: string;
    display_name: string;
  };
  mis_rerun_previous_month: {
    attempted: boolean;
    status?: string;
    reason?: string;
    mis_run_id?: string | null;
    xlsx_path?: string | null;
    period_start?: string;
    period_end?: string;
    client_site_key?: string;
    service_site_id?: string;
    error?: string;
  } | null;
};

export async function upsertO2CSiteAlias(body: {
  service_site_id: string;
  alias_code: string;
  alias_display?: string;
  source_system?: string;
  auto_run_previous_month_mis?: boolean;
  allow_expired_contract_terms?: boolean;
}) {
  return apiJson<O2CAliasUpsertResult>("/api/o2c/site-alias/upsert", {
    method: "POST",
    body: JSON.stringify({
      service_site_id: body.service_site_id,
      alias_code: body.alias_code,
      alias_display: body.alias_display ?? null,
      source_system: body.source_system ?? "manual",
      auto_run_previous_month_mis: body.auto_run_previous_month_mis ?? true,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

export type O2CAttendanceReconItem = {
  id: string;
  client_site_key: string;
  billing_period_start: string;
  billing_period_end: string;
  reason_code: string;
  detail: string;
  attendance_row_count: number;
  llm_match_attempted: boolean;
  status: string;
  first_seen_at: string;
  last_seen_at: string;
};

export async function listOpenO2CAttendanceRecon(limit = 200) {
  return apiJson<{ count: number; items: O2CAttendanceReconItem[] }>(
    "/api/o2c/attendance-recon/open",
    {
      method: "POST",
      body: JSON.stringify({ limit }),
    }
  );
}

export async function resolveO2CAttendanceReconAndRerun(body: {
  recon_id: string;
  service_site_id: string;
  alias_code?: string;
  alias_display?: string;
  source_system?: string;
  resolution_notes?: string;
  auto_run_previous_month_mis?: boolean;
  use_recon_period?: boolean;
  allow_alias_reassign?: boolean;
  allow_expired_contract_terms?: boolean;
}) {
  return apiJson<{
    status: string;
    resolved: boolean;
    recon_id: string;
    alias: {
      source_system: string;
      alias_code: string;
      alias_display: string;
      service_site_id: string;
    };
    site: {
      service_site_id: string;
      billing_client_id: string;
      site_key: string;
      display_name: string;
    };
    mis_rerun: {
      attempted: boolean;
      status?: string;
      reason?: string;
      mis_run_id?: string | null;
      xlsx_path?: string | null;
      period_start?: string;
      period_end?: string;
      client_site_key?: string;
      service_site_id?: string;
      error?: string;
    } | null;
  }>("/api/o2c/attendance-recon/resolve-and-rerun", {
    method: "POST",
    body: JSON.stringify({
      recon_id: body.recon_id,
      service_site_id: body.service_site_id,
      alias_code: body.alias_code ?? null,
      alias_display: body.alias_display ?? null,
      source_system: body.source_system ?? "manual",
      resolution_notes: body.resolution_notes ?? null,
      auto_run_previous_month_mis: body.auto_run_previous_month_mis ?? true,
      use_recon_period: body.use_recon_period ?? false,
      allow_alias_reassign: body.allow_alias_reassign ?? false,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

/* ─── MIS Review ────────────────────────────────────────────────── */

export type MisSiteLabelFields = {
  site_name: string | null;
  site_canonical_name?: string | null;
  site_city?: string | null;
  service_site_key?: string | null;
  client_site_key: string;
};

/** Set on MIS runs approved by post-draft auto-approve (`approved_by` column). */
export const MIS_AUTO_APPROVE_ACTOR = "system:auto_approve";

export type MisAutoApproveAudit = {
  approved?: boolean;
  eligible?: boolean;
  reasons?: string[];
  prior_mis_run_id?: string | null;
  current_total?: string | null;
  prior_total?: string | null;
  pct_delta?: number | null;
  tolerance_pct?: number;
  xlsx_error?: string | null;
  approve_error?: string | null;
};

export function isMisAutoApproved(approvedBy: string | null | undefined): boolean {
  return (approvedBy || "").trim() === MIS_AUTO_APPROVE_ACTOR;
}

export type MisRunListItem = MisSiteLabelFields & {
  id: string;
  status: string;
  billing_period_start: string;
  billing_period_end: string;
  xlsx_path: string | null;
  updated_at: string;
  approved_by?: string | null;
  client_name: string;
  summary_line_count?: number;
  omitted_line_count?: number;
};

export type MisSummaryRow = {
  id: string;
  contract_rate_line_id: string;
  role_code: string;
  description: string;
  employee_external_id: string | null;
  employee_name: string | null;
  /** From ``contract_rate_line.billing_model`` (drives Max Posts vs Qty columns). */
  billing_model?: string | null;
  /** Per-visit lines: billable visits this period (weekly cadence × weeks, or frozen visits_per_month). */
  visits_per_month?: number | null;
  contractual_rate: number | null;
  contracted_count: number | null;
  total_days: number | null;
  attendance_days: number | null;
  absent_days: number | null;
  final_amount: number | null;
  is_omitted: boolean;
  omit_reason: string | null;
  calc_notes: string | null;
  comments: string | null;
  tier: 1 | 2 | 3;
  tier_reason: string;
  source_rate_amount: number | null;
  source_billing_rules: Record<string, unknown> | null;
  source_billing_rule_text: string | null;
  source_service_site_id: string | null;
};

export type TierSummary = {
  tier_1_count: number;
  tier_2_count: number;
  tier_3_count: number;
  all_tier_1: boolean;
  /** All table rows */
  summary_row_count?: number;
  /** Rows still in billing (tier_* counts refer to these) */
  active_row_count?: number;
  /** Soft-removed; still listed so reviewers see LLM removals */
  omitted_row_count?: number;
};

export type MisRunStatusCounts = {
  pending_human: number;
  approved: number;
  rejected: number;
  all: number;
};

export type MisRunDetail = MisSiteLabelFields & {
  id: string;
  status: string;
  billing_period_start: string;
  billing_period_end: string;
  xlsx_path: string | null;
  approved_by?: string | null;
  client_name: string;
  summary_json: Record<string, unknown> | null;
  tier_summary: TierSummary;
  summary_rows: MisSummaryRow[];
  /** Latest ``[auto_approve]`` audit parsed from run notes (if any). */
  auto_approve?: MisAutoApproveAudit | null;
};

export async function listMisRuns(params: {
  status?: string | null;
  limit?: number;
  /** prior_month (default) = previous IST calendar month of billing_period_start; current_month | all */
  period_scope?: "prior_month" | "current_month" | "all";
}): Promise<{ items: MisRunListItem[]; status_counts: MisRunStatusCounts }> {
  const body: Record<string, unknown> = {};
  if (params.status) {
    body.status = params.status;
  } else {
    body.status = "__all__";
  }
  if (params.limit) body.limit = params.limit;
  body.period_scope = params.period_scope ?? "prior_month";
  const out = await apiJson<{
    items: MisRunListItem[];
    status_counts: MisRunStatusCounts;
  }>("/api/o2c/mis/list", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return {
    items: out.items,
    status_counts: out.status_counts ?? {
      pending_human: 0,
      approved: 0,
      rejected: 0,
      all: out.items.length,
    },
  };
}

export async function getMisRun(id: string): Promise<MisRunDetail> {
  return apiJson<MisRunDetail>("/api/o2c/mis/get", {
    method: "POST",
    body: JSON.stringify({ mis_run_id: id }),
  });
}

export async function setMisStatus(body: {
  mis_run_id: string;
  status: string;
  rejection_notes?: string;
}): Promise<Record<string, unknown>> {
  return apiJson("/api/o2c/mis/status", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteMisSummaryRow(id: string): Promise<void> {
  await apiJson("/api/o2c/mis/summary-row/delete", {
    method: "POST",
    body: JSON.stringify({ mis_summary_row_id: id }),
  });
}

/** Put an omitted line back in billing (pending runs). Does not apply to rows removed via hard delete. */
export async function restoreMisSummaryRow(id: string): Promise<{
  ok: boolean;
  restored_final_amount: number | null;
}> {
  return apiJson("/api/o2c/mis/summary-row/restore", {
    method: "POST",
    body: JSON.stringify({ mis_summary_row_id: id }),
  });
}

export async function insertMisSummaryRow(body: {
  mis_run_id: string;
  contract_rate_line_id: string;
}): Promise<{ status: string; mis_summary_row_id: string }> {
  return apiJson("/api/o2c/mis/summary-row/insert", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type AvailableRateLine = {
  id: string;
  description: string;
  role_code: string;
  billing_model: string;
  rate_amount: number | null;
  contracted_quantity: number | null;
  service_site_name: string;
  is_active: boolean;
  already_in_summary: boolean;
  overrides_contract_rate_line_id?: string | null;
};

export async function listAvailableRateLines(
  mis_run_id: string
): Promise<AvailableRateLine[]> {
  const res = await apiJson<{ items: AvailableRateLine[] }>(
    "/api/o2c/mis/available-rate-lines",
    { method: "POST", body: JSON.stringify({ mis_run_id }) }
  );
  return res.items;
}

export type AlternateContractTerm = {
  contract_terms_version_id: string;
  status: string;
  title: string | null;
  effective_from: string | null;
  effective_to: string | null;
  line_count: number;
  /** Lines that would be cloned (deduped against MIS CTV). */
  importable_line_count: number;
  created_at: string | null;
};

export async function listAlternateContractTerms(
  mis_run_id: string
): Promise<AlternateContractTerm[]> {
  const res = await apiJson<{ items: AlternateContractTerm[] }>(
    "/api/o2c/mis/alternate-contract-terms",
    { method: "POST", body: JSON.stringify({ mis_run_id }) }
  );
  return res.items;
}

export async function copyContractRateLinesToMisRun(body: {
  mis_run_id: string;
  source_contract_terms_version_id: string;
}): Promise<{ status: string; cloned_count: number }> {
  return apiJson("/api/o2c/mis/copy-contract-rate-lines", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function saveMis(body: {
  mis_run_id: string;
  row_edits: Array<Record<string, unknown>>;
  allow_expired_contract_terms?: boolean;
}): Promise<{
  ok: boolean;
  correction_count: number;
  rerun?: Record<string, unknown>;
}> {
  return apiJson("/api/o2c/mis/save", {
    method: "POST",
    body: JSON.stringify({
      mis_run_id: body.mis_run_id,
      row_edits: body.row_edits,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

export async function approveMis(body: {
  mis_run_id: string;
}): Promise<{
  ok: boolean;
  correction_count: number;
  idempotent?: boolean;
  xlsx_path?: string;
  xlsx_error?: string;
  xlsx_gdrive_error?: string;
  xlsx_gdrive_upload_attempts?: number;
}> {
  return apiJson("/api/o2c/mis/approve", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Legacy one-step: save (persist + MIS rerun) then approve + XLSX. */
export async function saveAndApproveMis(body: {
  mis_run_id: string;
  row_edits: Array<Record<string, unknown>>;
  allow_expired_contract_terms?: boolean;
}): Promise<{
  ok: boolean;
  correction_count: number;
  idempotent?: boolean;
  xlsx_path?: string;
  xlsx_error?: string;
}> {
  return apiJson("/api/o2c/mis/save-and-approve", {
    method: "POST",
    body: JSON.stringify({
      mis_run_id: body.mis_run_id,
      row_edits: body.row_edits,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

export async function rerunMisRun(body: {
  mis_run_id: string;
  allow_expired_contract_terms?: boolean;
}): Promise<Record<string, unknown>> {
  return apiJson("/api/o2c/mis/rerun", {
    method: "POST",
    body: JSON.stringify({
      mis_run_id: body.mis_run_id,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

export async function generateMisDraft(body: {
  attendance_xlsx: string;
  client_site_key: string;
  period_start: string;
  period_end: string;
  out_dir: string;
  template_path: string;
}): Promise<Record<string, unknown>> {
  return apiJson("/api/o2c/mis/generate-draft", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/* ─── Billing Clients ──────────────────────────────────────────── */

export type BillingClientItem = { id: string; name: string };

export async function listBillingClients(
  query = "",
  limit = 100
): Promise<{ count: number; items: BillingClientItem[] }> {
  const q = new URLSearchParams({ query, limit: String(limit) });
  return apiJson(`/api/o2c/billing-clients?${q.toString()}`);
}

/* ─── Re-ingest Failed Parsing ─────────────────────────────────── */

export async function reingestFailedParsing(
  failureId: string
): Promise<{ status: string; reingest_result?: Record<string, unknown>; error?: string }> {
  return apiJson("/api/o2c/contracts/review/failures/reingest", {
    method: "POST",
    body: JSON.stringify({ failure_id: failureId }),
  });
}

/* ─── Service Site Creation ────────────────────────────────────── */

export type CreateServiceSiteResult = {
  status: string;
  service_site_id: string;
  site_key: string;
  display_name: string;
  alias_code: string;
  recon_id: string;
  rate_line_clone?: Record<string, unknown> | null;
  mis_rerun: Record<string, unknown> | null;
};

export async function createServiceSite(body: {
  site_key: string;
  display_name?: string;
  billing_client_id: string;
  recon_id: string;
  /** Attendance export label from the selected open item (required). */
  alias_code: string;
  auto_run_previous_month_mis?: boolean;
  clone_rate_lines_from_site_id?: string;
  replace_existing_site_rate_lines?: boolean;
  require_same_terms_for_period?: boolean;
  clone_terms_period_start?: string;
  clone_terms_period_end?: string;
  allow_expired_contract_terms?: boolean;
}): Promise<CreateServiceSiteResult> {
  return apiJson("/api/o2c/service-site/create", {
    method: "POST",
    body: JSON.stringify({
      ...body,
      allow_expired_contract_terms: body.allow_expired_contract_terms ?? true,
    }),
  });
}

export async function cloneO2CSiteRateLines(body: {
  source_service_site_id: string;
  target_service_site_id: string;
  replace_existing?: boolean;
  require_same_terms_for_period?: boolean;
  period_start?: string;
  period_end?: string;
}): Promise<Record<string, unknown>> {
  return apiJson("/api/o2c/service-site/clone-rate-lines", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type O2CContractReviewItem = {
  id: string;
  title: string | null;
  contract_kind: string;
  status: string;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
  updated_at: string;
  client_name: string;
  rate_line_count: number;
  null_rate_count: number;
  null_site_count: number;
  empty_billing_rules_count: number;
};

export async function listO2CContractReviewItems(body: {
  statuses?: string[];
  search?: string;
  page?: number;
  page_size?: number;
}) {
  const q = new URLSearchParams({
    search: body.search ?? "",
    page: String(body.page ?? 1),
    page_size: String(body.page_size ?? 10),
  });
  q.set("statuses", body.statuses?.length ? body.statuses.join(",") : "draft,pending");
  return apiJson<{
    count: number;
    total: number;
    page: number;
    page_size: number;
    items: O2CContractReviewItem[];
  }>(
    `/api/o2c/contracts/review?${q.toString()}`
  );
}

export async function getO2CContractReviewItem(contract_terms_version_id: string) {
  return apiJson<{
    header: Record<string, unknown>;
    parties: Record<string, unknown>[];
    payment_terms: Record<string, unknown>;
    rate_lines: Record<string, unknown>[];
    documents: Record<string, unknown>[];
    latest_extraction_run: Record<string, unknown>;
  }>(`/api/o2c/contracts/review/contract_term/${encodeURIComponent(contract_terms_version_id)}`);
}

export function priorBillingMonthPeriod(): { period_start: string; period_end: string } {
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  const pad = (n: number) => String(n).padStart(2, "0");
  const startY = m === 0 ? y - 1 : y;
  const startM = m === 0 ? 12 : m;
  const endY = m === 0 ? y - 1 : y;
  const endM = m === 0 ? 12 : m;
  const lastDay = new Date(endY, endM, 0).getDate();
  return {
    period_start: `${startY}-${pad(startM)}-01`,
    period_end: `${endY}-${pad(endM)}-${pad(lastDay)}`,
  };
}

export type ContractHealthOpsSummary = {
  period_start: string;
  period_end: string;
  by_status: Record<string, number>;
  ctvs_with_null_rates: number;
  ctvs_with_unmapped_sites: number;
  ctvs_with_empty_rules: number;
  overlap_site_count: number;
  parse_failure_count: number;
  queues: {
    overlap_sites: Array<{
      service_site_id: string;
      billing_client_id: string;
      client_name: string;
      site_name: string;
      approved_ctv_count: number;
    }>;
    pending_data_issues: Array<{
      contract_terms_version_id: string;
      title: string | null;
      billing_client_id: string;
      client_name: string;
      status: string;
      null_rate_count: number;
      null_site_count: number;
    }>;
  };
};

export async function getO2CContractHealthSummary(period_start: string, period_end: string) {
  const q = new URLSearchParams({ period_start, period_end });
  return apiJson<ContractHealthOpsSummary>(`/api/o2c/contracts/review/summary?${q.toString()}`);
}

export type ContractHealthCtvSummary = {
  contract_terms_version_id: string;
  title: string | null;
  status: string;
  effective_from: string | null;
  effective_to: string | null;
  created_at: string | null;
  rate_line_count: number;
  null_rate_count: number;
  null_site_count: number;
  empty_billing_rules_count: number;
};

export type ContractHealthMisReadiness = {
  ready: boolean;
  billable_site_count: number;
  blocker_count: number;
  warning_count: number;
  blockers: Array<{
    code: string;
    service_site_id: string;
    site_name: string;
    message: string;
    contract_terms_version_id?: string;
  }>;
  warnings: Array<{
    code: string;
    service_site_id: string;
    site_name: string;
    message: string;
    contract_terms_version_id?: string;
  }>;
};

export type ContractHealthClientTree = {
  billing_client_id: string;
  client_name: string;
  period_start: string;
  period_end: string;
  global_terms: ContractHealthCtvSummary[];
  sites: Array<{
    service_site_id: string;
    site_name: string;
    site_key: string | null;
    terms: ContractHealthCtvSummary[];
    overlap_conflict: boolean;
    picker_ctv_id: string | null;
  }>;
  mis_readiness?: ContractHealthMisReadiness;
};

export async function getO2CContractHealthClientTree(
  billing_client_id: string,
  period_start: string,
  period_end: string
) {
  const q = new URLSearchParams({ period_start, period_end });
  return apiJson<ContractHealthClientTree>(
    `/api/o2c/contracts/review/clients/${encodeURIComponent(billing_client_id)}/tree?${q.toString()}`
  );
}

export async function getO2CContractReviewSiblings(
  contract_terms_version_id: string,
  period_start: string,
  period_end: string
) {
  const q = new URLSearchParams({ period_start, period_end });
  return apiJson<{
    contract_terms_version_id: string;
    items: Array<ContractHealthCtvSummary & { overlaps_period?: boolean }>;
  }>(
    `/api/o2c/contracts/review/contract_term/${encodeURIComponent(contract_terms_version_id)}/siblings?${q.toString()}`
  );
}

export async function patchO2CContractReviewHeader(
  contract_terms_version_id: string,
  body: { effective_from?: string; effective_to?: string; clear_effective_to?: boolean }
) {
  return apiJson<{
    status: string;
    contract_terms_version_id: string;
    effective_from: string | null;
    effective_to: string | null;
  }>(
    `/api/o2c/contracts/review/contract_term/${encodeURIComponent(contract_terms_version_id)}/header`,
    { method: "PATCH", body: JSON.stringify(body) }
  );
}

export async function ingestO2CContracts(body?: {
  contracts_root?: string;
  max_files?: number;
  ignore_mtime_watermark?: boolean;
}) {
  return apiJson<Record<string, unknown>>("/api/o2c/ingest-contracts", {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

export async function setO2CContractReviewStatus(body: {
  contract_terms_version_id: string;
  status: "rejected" | "expired";
  period_start?: string;
  period_end?: string;
}) {
  return apiJson<{
    status: string;
    contract_terms_version_id: string;
    contract_status: string;
    updated_by: string;
  }>("/api/o2c/contracts/review/status", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ContractRateLinePatchItem = {
  id: string;
  billing_model?: string;
  role_code?: string;
  description?: string;
  rate_amount?: number | string;
  rate_unit?: string;
  contracted_quantity?: number | string;
  attendance_required?: boolean;
  minimum_units_per_period?: number | string;
  unfilled_penalty_pct?: number | string;
  ot_multiplier?: number | string;
  service_charge_type?: string;
  service_charge_value?: number | string;
  actuals_markup_pct?: number | string;
  schedule_type?: string;
  schedule_config?: Record<string, unknown>;
  billing_rules?: Record<string, unknown>;
  billing_rule_text?: string;
  currency?: string;
  effective_from?: string;
  effective_to?: string;
  is_active?: boolean;
};

export async function patchO2CContractRateLines(
  contractTermsVersionId: string,
  items: ContractRateLinePatchItem[]
) {
  return apiJson<{ status: string; updated_count: number; updated_ids: string[] }>(
    `/api/o2c/contracts/review/contract_term/${encodeURIComponent(contractTermsVersionId)}/rate-lines`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    }
  );
}

export async function listO2CContractReviewFailures(limit = 100, openOnly = true) {
  const q = new URLSearchParams({
    limit: String(limit),
    open_only: String(openOnly),
  });
  return apiJson<{ count: number; items: Record<string, unknown>[] }>(
    `/api/o2c/contracts/review/failures?${q.toString()}`
  );
}

/* ─── Outreach ─────────────────────────────────────────────────── */

export type OutreachCampaign = {
  id: string;
  campaign_name: string;
  dashboard_display_name: string;
  visible_columns: string[];
};

export type OutreachIssue = {
  code: string;
  count: number;
};

export type OutreachDashboardStats = {
  total_leads: number;
  pending_leads: number;
  sent_leads: number;
  reply_rate: number;
  replies_received: number;
};

export type OutreachSheetHealthItem = {
  id: string;
  account_name: string;
  spoc: string;
  primary_email: string;
  gsheet_row_index: number;
  issues: string[];
  status: string;
};

export type OutreachDashboardResponse = {
  stats: OutreachDashboardStats;
  issues_summary: OutreachIssue[];
  issue_leads: OutreachSheetHealthItem[];
};

export type OutreachThreadRow = {
  id: string;
  account_name: string;
  spoc: string;
  primary_email: string;
  last_reply_category: string | null;
  last_reply_at: string | null;
  reply_count: number;
  gmail_thread_id: string;
};

export type OutreachThreadsPage = {
  items: OutreachThreadRow[];
  total: number;
  has_more: boolean;
};

export type OutreachReplyBreakdownEntry = {
  category: string;
  count: number;
};

export type OutreachSummaryResponse = {
  campaign_name: string;
  total_leads: number;
  pending: number;
  sent: number;
  failed: number;
  hold: number;
  duplicate: number;
  skipped: number;
  removed: number;
  reply_count: number;
  reply_breakdown: OutreachReplyBreakdownEntry[];
};

export type OutreachLeadRow = {
  id: number;
  account_name: string | null;
  spoc: string | null;
  primary_email: string | null;
  source: string | null;
  status: string;
  tab_name: string | null;
  issues: string[];
  sent_at: string | null;
  subject_sent: string | null;
  gmail_thread_id: string | null;
  last_reply_category: string | null;
  last_reply_at: string | null;
  reply_count: number;
  gsheet_synced_at: string | null;
};

export type OutreachLeadsPage = {
  total: number;
  page: number;
  page_size: number;
  items: OutreachLeadRow[];
};

export async function listOutreachCampaigns(): Promise<OutreachCampaign[]> {
  return apiJson<OutreachCampaign[]>("/api/outreach/campaigns");
}

export async function getOutreachSummary(
  campaignName: string,
): Promise<OutreachSummaryResponse> {
  return apiJson<OutreachSummaryResponse>(
    `/api/outreach/${encodeURIComponent(campaignName)}/summary`,
  );
}

export async function getOutreachLeads(
  campaignName: string,
  options?: {
    page?: number;
    page_size?: number;
    status?: string;
    reply_category?: string;
  },
): Promise<OutreachLeadsPage> {
  const q = new URLSearchParams();
  if (options?.page) q.set("page", String(options.page));
  if (options?.page_size) q.set("page_size", String(options.page_size));
  if (options?.status) q.set("status", options.status);
  if (options?.reply_category) q.set("reply_category", options.reply_category);
  return apiJson<OutreachLeadsPage>(
    `/api/outreach/${encodeURIComponent(campaignName)}/leads?${q.toString()}`,
  );
}

export async function getOutreachThreadMessages(
  campaignName: string,
  threadId: string,
): Promise<{ messages: EmailThreadMessage[] }> {
  const data = await apiJson<{
    lead: unknown;
    messages: {
      message_id: string;
      from: string;
      subject: string;
      date: string;
      body: string;
    }[];
  }>(
    `/api/outreach/intelligence/${encodeURIComponent(campaignName)}/threads/${encodeURIComponent(threadId)}`,
  );
  return {
    messages: data.messages.map((m) => ({
      message_id: m.message_id,
      from_addr: m.from,
      to_addr: "",
      date_ms: m.date ? new Date(m.date).getTime() : 0,
      subject: m.subject,
      body: m.body,
    })),
  };
}

export async function acknowledgeOutreachIssue(
  campaignName: string,
  leadId: number,
) {
  return apiJson<void>(
    `/api/outreach/${encodeURIComponent(campaignName)}/leads/${leadId}/acknowledge`,
    { method: "POST" },
  );
}

export async function retryOutreachSend(campaignName: string, leadId: number) {
  return apiJson<void>(
    `/api/outreach/${encodeURIComponent(campaignName)}/leads/${leadId}/retry`,
    { method: "POST" },
  );
}

// ─── SBI MIS ──────────────────────────────────────────────────────────────────

const SBI = "/api/v1/sbi-mis";

export interface SbiRun {
  month: string;
  raw_file_path: string;
  row_count: number | null;
  uploaded_at: string;
}

export interface SbiRunFile {
  month: string;
  kind: string;
  file_path: string;
  original_filename: string | null;
  row_count: number | null;
  uploaded_at: string;
}

export interface SbiRule {
  id: number;
  sheet: string;
  column_letter: string;
  rule_type: string;
  config_json: Record<string, unknown>;
  notes: string;
  status: string;
  updated_at: string;
  /** Number format override from sbi_column_formats — included in export/import JSON */
  number_format?: string | null;
}

export interface SbiJob {
  id: string;
  kind: string;
  status: "pending" | "running" | "done" | "failed";
  stage: string | null;
  progress: number;
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface SbiSheetColumn {
  id: number;
  sheet: string;
  position: number;
  col_letter: string;
  header: string;
  number_format: string | null;
  is_system: boolean;
  notes: string | null;
}

export interface SbiPreviewColumn {
  col: string;
  header: string;
  is_derived: boolean;
  rule_type: string | null;
  number_format: string | null;
}

export interface SbiPreviewGrid {
  kind: "grid";
  sheet: string;
  columns: SbiPreviewColumn[];
  rows: unknown[][];
  total: number;
  offset: number;
  limit: number;
  grand_totals: Record<string, number | null> | null;
}

export interface SbiSummaryCell {
  cell: string;
  value: unknown;
  number_format: string | null;
  rule_type: string;
}

export interface SbiPreviewSummary {
  kind: "summary";
  sheet: string;
  cells: SbiSummaryCell[];
}

export interface SbiStatusResponse {
  has_upload: boolean;
  computing: boolean;
  error: string | null;
  computed_at: number | null;
  active_month: string | null;
  active_run: SbiRun | null;
  available_months: string[];
}

export interface SbiPfHistorical {
  pf: string;
  month: string;
  pharma_total: number | null;
  ahc_total: number | null;
  pf_type: string | null;
  wallet_limit: number | null;
}

export interface SbiClient {
  id: number;
  code: string;
  display_name: string;
  active: boolean;
}

export interface SbiReconResult {
  empty?: boolean;
  error?: string;
  raw: { rows: number; gmv_mrp: number; unique_order_ids: number };
  pipeline: { rows: number; gmv_mrp: number; unique_order_ids: number };
  match: { rows: boolean; gmv_mrp: boolean; order_ids: boolean };
}

export interface SbiBillingSummary {
  empty: boolean;
  active_month: string | null;
  row_count: number;
  permissible: { rows: number; gmv_mrp: number; gmv_list: number };
  non_permissible: { rows: number; gmv_mrp: number; gmv_list: number };
  grand_total: { rows: number; gmv_mrp: number; gmv_list: number };
}

export interface SbiMonthStats {
  pf_count: number;
  pharma_total: number;
  ahc_total: number;
  wallets_filled: number;
}

// ── Fetch functions ───────────────────────────────────────────────────────────

export async function sbiGetStatus(): Promise<SbiStatusResponse> {
  return apiJson<SbiStatusResponse>(`${SBI}/status`);
}

export async function sbiGetSchema(): Promise<Record<string, unknown>> {
  return apiJson<Record<string, unknown>>(`${SBI}/schema`);
}

export async function sbiGetRuns(): Promise<SbiRun[]> {
  return apiJson<SbiRun[]>(`${SBI}/runs`);
}

export async function sbiActivateRun(month: string): Promise<SbiStatusResponse> {
  return apiJson<SbiStatusResponse>(`${SBI}/runs/${month}/activate`, { method: "POST" });
}

export async function sbiGetJob(jobId: string): Promise<SbiJob> {
  return apiJson<SbiJob>(`${SBI}/jobs/${jobId}`);
}

/** Poll a job until status is "done" or "failed". Calls onUpdate each tick. */
export async function sbiPollJob(
  jobId: string,
  onUpdate: (job: SbiJob) => void,
  intervalMs = 1500,
): Promise<SbiJob> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const job = await sbiGetJob(jobId);
        onUpdate(job);
        if (job.status === "done" || job.status === "failed") {
          resolve(job);
        } else {
          setTimeout(tick, intervalMs);
        }
      } catch (e) {
        reject(e);
      }
    };
    tick();
  });
}

export async function sbiUploadFile(
  file: File,
  month: string,
  kind: "raw" | "ahc" | "wallet_checker",
): Promise<{ job_id: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("month", month);
  form.append("kind", kind);
  const res = await apiFetch(`${SBI}/upload`, { method: "POST", body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { error?: string }).error || `Upload failed: ${res.status}`);
  }
  return res.json();
}

/** Chunked upload — returns job_id. Reports progress via onProgress(0–1). */
export async function sbiUploadFileChunked(
  file: File,
  month: string,
  kind: "raw" | "ahc" | "wallet_checker",
  onProgress: (p: number) => void,
  skipRerun = false,
): Promise<{ job_id: string }> {
  const CHUNK = 2 * 1024 * 1024;
  // Create session
  const sess = await apiJson<{ upload_id: string; chunk_size: number; chunk_count: number }>(
    `${SBI}/upload/session`,
    {
      method: "POST",
      body: JSON.stringify({ month, kind, filename: file.name, file_size: file.size }),
    },
  );
  const { upload_id, chunk_count } = sess;
  // Upload chunks
  for (let i = 0; i < chunk_count; i++) {
    const slice = file.slice(i * CHUNK, (i + 1) * CHUNK);
    const res = await apiFetch(`${SBI}/upload/chunk/${upload_id}/${i}`, {
      method: "PUT",
      body: slice,
      headers: { "Content-Type": "application/octet-stream" },
    });
    if (!res.ok) throw new Error(`Chunk ${i} failed: ${res.status}`);
    onProgress((i + 1) / chunk_count * 0.9);
  }
  // Complete
  const result = await apiJson<{ job_id: string }>(`${SBI}/upload/complete`, {
    method: "POST",
    body: JSON.stringify({ upload_id, skip_rerun: skipRerun }),
  });
  onProgress(1);
  return result;
}

export async function sbiBatchRerun(
  month: string,
  kinds: Array<"raw" | "ahc" | "wallet_checker" | "pf_summary">,
): Promise<{ job_id: string; trigger: string }> {
  return apiJson(`${SBI}/upload/batch-rerun`, {
    method: "POST",
    body: JSON.stringify({ month, kinds }),
  });
}

export async function sbiGetRules(): Promise<SbiRule[]> {
  return apiJson<SbiRule[]>(`${SBI}/rules`);
}

export async function sbiImportRules(
  rules: SbiRule[],
  rerun = true,
): Promise<{ imported: number; rerun_job_id: string }> {
  return apiJson(`${SBI}/rules/import`, {
    method: "POST",
    body: JSON.stringify({ rules, rerun }),
  });
}

export async function sbiGetRule(sheet: string, col: string): Promise<SbiRule> {
  return apiJson<SbiRule>(`${SBI}/rules/${encodeURIComponent(sheet)}/${col}`);
}

export async function sbiUpdateRule(
  sheet: string,
  col: string,
  body: { rule_type: string; config: Record<string, unknown>; status?: string },
): Promise<{ rule: SbiRule; rerun_job_id: string }> {
  return apiJson(`${SBI}/rules/${encodeURIComponent(sheet)}/${col}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function sbiGetLookupTables(): Promise<Record<string, Array<{ key: string; value: unknown }>>> {
  return apiJson(`${SBI}/lookup-tables`);
}

export async function sbiSetLookupTable(
  name: string,
  data: Array<{ key: string; value: unknown }>,
): Promise<{ rerun_job_id: string }> {
  return apiJson(`${SBI}/lookup-tables/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ data }),
  });
}

export async function sbiGetPreview(
  sheet: string,
  offset = 0,
  limit = 200,
): Promise<SbiPreviewGrid | SbiPreviewSummary> {
  return apiJson(
    `${SBI}/preview/${encodeURIComponent(sheet)}?offset=${offset}&limit=${limit}`,
  );
}

export async function sbiGetSheetColumns(sheet: string): Promise<SbiSheetColumn[]> {
  return apiJson<SbiSheetColumn[]>(`${SBI}/sheet-columns/${encodeURIComponent(sheet)}`);
}

export async function sbiAddSheetColumn(
  sheet: string,
  body: { header: string; number_format?: string; rule_type?: string },
): Promise<{ column: SbiSheetColumn; rerun_job_id: string }> {
  return apiJson(`${SBI}/sheet-columns/${encodeURIComponent(sheet)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function sbiPatchSheetColumn(
  sheet: string,
  col: string,
  body: { header?: string; number_format?: string | null },
): Promise<{ column: SbiSheetColumn; rerun_job_id: string }> {
  return apiJson(`${SBI}/sheet-columns/${encodeURIComponent(sheet)}/${col}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/**
 * Update the number format for any column (spec-defined or user-added).
 * Writes to sbi_column_formats via PUT /column-formats/{sheet}/{col}.
 * This is the correct endpoint for format-only changes — sbiPatchSheetColumn
 * only works for user-added columns in sbi_sheet_columns and 404s on spec columns.
 */
export async function sbiUpdateColumnFormat(
  sheet: string,
  col: string,
  number_format: string | null,
): Promise<void> {
  await apiFetch(`${SBI}/column-formats/${encodeURIComponent(sheet)}/${col}`, {
    method: "PUT",
    body: JSON.stringify({ number_format }),
  });
}

export async function sbiDeleteSheetColumn(
  sheet: string,
  col: string,
): Promise<void> {
  await apiFetch(`${SBI}/sheet-columns/${encodeURIComponent(sheet)}/${col}`, {
    method: "DELETE",
  });
}

export async function sbiGetHistoricalStats(): Promise<{
  months: string[];
  month_stats: Record<string, SbiMonthStats>;
  total_records: number;
}> {
  return apiJson(`${SBI}/historicals/stats`);
}

export async function sbiGetHistoricals(month?: string): Promise<{
  rows: SbiPfHistorical[];
  count: number;
}> {
  const q = month ? `?month=${month}` : "";
  return apiJson(`${SBI}/historicals${q}`);
}

export async function sbiGetBillingSummary(): Promise<SbiBillingSummary> {
  return apiJson<SbiBillingSummary>(`${SBI}/billing-summary`);
}

export async function sbiDeleteHistoricals(month: string): Promise<{ rerun_job_id: string }> {
  return apiJson(`${SBI}/historicals/${month}`, { method: "DELETE" });
}

export async function sbiGetRunFiles(): Promise<SbiRunFile[]> {
  return apiJson<SbiRunFile[]>(`${SBI}/run-files`);
}

export async function sbiGetClients(): Promise<SbiClient[]> {
  return apiJson<SbiClient[]>(`${SBI}/clients`);
}

/** @deprecated use sbiRunRecon() which is async and avoids 504 timeouts */
export async function sbiGetRecon(): Promise<SbiReconResult> {
  return apiJson<SbiReconResult>(`${SBI}/recon`);
}

/** Start an async recon job. Returns job_id; poll via sbiPollJob. */
export async function sbiRunRecon(): Promise<{ job_id: string }> {
  return apiJson<{ job_id: string }>(`${SBI}/recon/run`, { method: "POST" });
}

export function sbiDownloadUrl(): string {
  return `${SBI}/download`;
}

export async function sbiNlToFormula(body: {
  text: string;
  sheet: string;
  column: string;
  header?: string;
}): Promise<{ formula: string }> {
  return apiJson(`${SBI}/nl-to-formula`, { method: "POST", body: JSON.stringify(body) });
}
