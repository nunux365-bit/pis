/**
 * Prosight API client — Anomaly detection dashboard
 */

import { apiJson } from "./api";

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightSummary {
  as_of: string;
  model_version: string;
  total_series: number;
  qualified_flagged: number;
  drops: number;
  spikes: number;
  sustained_3plus: number;
  new_today: number;
  resolved_today: number;
  reviewed: number;
}

export interface ProsightDistribution {
  by_severity: {
    high: number;
    med: number;
    low: number;
  };
  by_level: {
    L0_total: number;
    L1_single: number;
    L2_pair: number;
    L3_full: number;
  };
}

export interface ProsightTop5Item {
  group_id: string;
  level: string;
  anomaly_score: number;
  direction: "drop" | "spike";
  history_scores: number[];
}

export interface ProsightDriver {
  feature: string;
  wow_pct: number;
}

export interface ProsightHistoryPoint {
  date: string;
  actual: number;
  predicted: number;
  upper: number;
  lower: number;
  deviation: number;
  is_anomaly: boolean;
}

export interface ProsightTreeNode {
  group_id: string;
  level: string;
  label: string;
  direction: "drop" | "spike" | "stable";
  delta: number;
  contribution_pct: number | null;
  actual: number;
  predicted: number;
  deviation: number;
  rmse: number;
  anomaly_score: number;
  is_anomaly: boolean;
  smape_qualified: boolean;
  current_streak: number;
  date: string;
  history: ProsightHistoryPoint[];
  drivers?: ProsightDriver[];
  explanation?: string;
  children?: ProsightTreeNode[];
}

export interface ProsightDashboard {
  summary: ProsightSummary;
  distribution: ProsightDistribution;
  top5: ProsightTop5Item[];
  tree: ProsightTreeNode;
}

export interface ProsightSnapshot {
  id: string;
  snapshot_date: string;
  model_version?: string;
  total_series?: number;
  qualified_flagged?: number;
  created_at?: string;
}

export interface ProsightStatus {
  service: string;
  status: "active" | "no_data";
  databricks_configured: boolean;
  latest_snapshot: string | null;
  sync_hour: number;  // Hour (IST) when daily sync runs
  description: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Dashboard API
// ─────────────────────────────────────────────────────────────────────────────

export async function prosightGetLatestRun(): Promise<ProsightDashboard> {
  return apiJson<ProsightDashboard>("/api/prosight/runs/latest");
}

export async function prosightGetRunByDate(date: string): Promise<ProsightDashboard> {
  return apiJson<ProsightDashboard>(`/api/prosight/runs/${date}`);
}

export async function prosightListSnapshots(
  limit = 30
): Promise<{ snapshots: ProsightSnapshot[]; total: number }> {
  return apiJson(`/api/prosight/snapshots?limit=${limit}`);
}

export async function prosightGetStatus(): Promise<ProsightStatus> {
  return apiJson<ProsightStatus>("/api/prosight/status");
}

// ─────────────────────────────────────────────────────────────────────────────
// Admin API (sync & upload)
// ─────────────────────────────────────────────────────────────────────────────

export async function prosightTriggerSync(): Promise<{
  status: string;
  message: string;
}> {
  return apiJson("/api/prosight/sync/trigger", {
    method: "POST",
  });
}

export async function prosightSyncNow(): Promise<{
  status: string;
  snapshot_id?: string;
  snapshot_date?: string;
  synced_at?: string;
  error?: string;
}> {
  return apiJson("/api/prosight/sync/now", {
    method: "POST",
  });
}

export async function prosightUploadSnapshot(data: {
  snapshot_date: string;
  data: ProsightDashboard;
  model_version?: string;
  total_series?: number;
  qualified_flagged?: number;
}): Promise<{ status: string; snapshot_id: string; snapshot_date: string }> {
  return apiJson("/api/prosight/snapshots/upload", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Actionables API
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightActionable {
  action_hash: string;
  as_of_date: string | null;
  bu: string;
  segment: string;
  lens: string;
  rank: number | null;
  /** Legacy one-line fallback — never render; compose the component fields. */
  action: string;
  // Componentized display fields (docs/ui_actionables_render_prompt.md)
  segment_label: string | null;
  dimension: string | null;
  impact_display: string | null;
  fact: string | null;
  l2_pocket: string | null;
  why: string | null;
  lever: string | null;
  news_summary: string | null;
  news_source: string | null;
  news_url: string | null;
  news_relation: string | null;
  run_days: number | null;
  wow_pct: number | null;
  dod_pct: number | null;
  daily_order_gap: number | null;
  l2_gap_orders: number | null;
  l2_share_pct: number | null;
  impact_inr_1d: number | null;
  impact_inr_3d: number | null;
  // Feedback (shared team-wide, last write wins)
  is_actionable: boolean | null;
  days_saved: number | null;
  feedback_updated_by: string | null;
  feedback_updated_at: string | null;
  synced_at: string | null;
}

/**
 * ok             → actionable rows returned
 * no_actionables → day processed, upstream marker says nothing to act on
 * not_processed  → no rows at all for this BU/day
 */
export type ProsightActionablesStatus = "ok" | "no_actionables" | "not_processed";

export interface ProsightActionablesResponse {
  actionables: ProsightActionable[];
  /** Rows in this response — not the count of matching rows in the table. */
  total: number;
  status: ProsightActionablesStatus;
  /** The model's plain-English read of the whole day (per date+bu slice). */
  day_summary: string | null;
  synced_at: string | null;
  /** Effective row cap the server applied — it clamps the requested limit. */
  limit: number;
  /**
   * True when `limit` cut the read short, so this is a partial set: rows are
   * ordered newest day first then by rank, so what's missing is the oldest
   * days and the lowest-ranked actions. Any roll-up over `actionables` is a
   * roll-up over a partial set and must say so.
   */
  truncated: boolean;
}

export async function prosightListActionables(
  bu?: string,
  date?: string
): Promise<ProsightActionablesResponse> {
  const params = new URLSearchParams();
  if (bu) params.set("bu", bu);
  if (date) params.set("date", date);
  const qs = params.toString();
  return apiJson<ProsightActionablesResponse>(
    `/api/prosight/actionables${qs ? `?${qs}` : ""}`
  );
}

export async function prosightSaveActionableFeedback(
  actionHash: string,
  body: { is_actionable: boolean | null; days_saved: number | null }
): Promise<ProsightActionable> {
  return apiJson<ProsightActionable>(
    `/api/prosight/actionables/${actionHash}/feedback`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    }
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Utility functions
// ─────────────────────────────────────────────────────────────────────────────

export const formatInt = (n: number | null | undefined): string => {
  if (n == null) return "—";
  if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
  return Math.round(n).toLocaleString();
};

export const formatSigned = (n: number | null | undefined, digits = 0): string => {
  if (n == null) return "—";
  const s = n >= 0 ? "+" : "";
  return s + n.toFixed(digits);
};

export const formatPct = (n: number | null | undefined, digits = 1): string => {
  if (n == null) return "—";
  const s = n >= 0 ? "+" : "";
  return s + n.toFixed(digits) + "%";
};

export const scoreClass = (s: number): "red" | "amber" | "gray" => {
  if (s >= 3) return "red";
  if (s >= 1.5) return "amber";
  return "gray";
};

export const directionLabel = (d: string): string =>
  d === "drop" ? "↓" : d === "spike" ? "↑" : "—";

export const trimLabel = (label: string | undefined, max = 60): string => {
  if (!label) return "";
  return label.length > max ? label.slice(0, max - 1) + "…" : label;
};
