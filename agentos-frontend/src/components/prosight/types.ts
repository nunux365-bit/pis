/**
 * Types for Prosight News Dashboard (N dashboard)
 * Matches the structure of prosight_news.json
 */

// ─────────────────────────────────────────────────────────────────────────────
// Data Row Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightDims {
  [key: string]: string | null;
}

export interface ProsightRcaChild {
  label: string;
  group_id: string;
  actual: number;
  predicted: number;
  lower: number;
  upper: number;
  point_pct: number;
  band_pct: number;
  band_violation: number;
  verdict: string | null;
  flagged: boolean;
  self_z: number;
  /** Backend rollup rows (`unqualified`, `low order`) — not real segments. */
  synthetic?: boolean;
  /**
   * Part A / spec 38 §3.5: for a non-reconciling (overlapping) cut such as
   * `sku_name`, raw children sum to MORE than the L0 miss. When present, this is
   * the backend's redistributed contribution whose Σ over surviving segments
   * equals the L0 point miss. Field name is provisional (backend §8 #1) — read
   * it via `childShareDiff` in adapters.ts so a rename is a one-line change.
   */
  attributed_point_diff?: number;
}

export interface ProsightRcaDimension {
  js_surprise: number;
  band_incoherence: number;
  children: ProsightRcaChild[];
}

export interface ProsightRca {
  by_dimension: Record<string, ProsightRcaDimension>;
}

export interface ProsightTodayRow {
  label: string;
  level: string;
  group_id: string;
  full_id: string;
  dims: ProsightDims;
  actual: number;
  predicted: number;
  magnitude: number;
  pct_change: number;
  direction: "spike" | "drop";
  score: number;
  streak: number;
  qualified: boolean;
  driver: string;
  rca: ProsightRca | null;
  longest_streak: number;
  anomaly_ratio: number;
  total_flagged_days: number;
  cumulative_magnitude: number;
  /**
   * Part B / spec 38 §4.6: static-baseline comparisons for this row. Present on
   * the latest-day L0 row once backend PR B1–B2 lands; absent on older JSON (the
   * FE synthesizes a `forecast` baseline from the legacy fields — see
   * `rowBaseline` in adapters.ts).
   */
  baselines?: Record<BaselineMode, ProsightBaseline>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Static-baseline Types (Part B, spec 38 §4.6/§4.7)
// ─────────────────────────────────────────────────────────────────────────────

export type BaselineMode = "forecast" | "vs_prev_day" | "vs_prev_week";

export interface ProsightBaseline {
  /**
   * Today's actual — the headline value. Identical across all three modes (the
   * mode only changes what we compare it against). Backend field: `actual`.
   */
  actual: number;
  /**
   * The value today is compared against: forecast → predicted; vs_prev_day →
   * yesterday's actual; vs_prev_week → last-week's actual. Backend field:
   * `anchor_value`. (NOTE: this is the *basis*, NOT today's value — a common
   * point of confusion; today's value lives in `actual`.)
   */
  anchor_value: number;
  /** actual − anchor_value. >0 = above the basis (spike), <0 = below (drop). */
  delta: number;
  /** delta as a percentage of anchor_value. */
  pct: number;
  /** Forecast/prev-week band edges; null when the mode has no band (prev-day). */
  lower: number | null;
  upper: number | null;
  /** Forecast mode only: whether the point is an anomaly (drives the DROP/SPIKE badge). */
  is_anomaly?: boolean;
  /** Static modes with a band (prev-week): whether the value fell outside the band; null when no band. */
  is_outside_band?: boolean | null;
  /** Backend may send "normal"; read via `normDirection` so it maps to null. */
  direction: DirectionType;
}

// ─────────────────────────────────────────────────────────────────────────────
// Summary Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightL0Anchor {
  actual: number;
  predicted: number;
  expected_range: [number, number];
  point_residual: number;
  band_violation: number;
  direction: "spike" | "drop" | "in_band";
}

export interface ProsightTopAlert {
  label: string;
  score: number;
  direction: "spike" | "drop";
  driver: string;
}

export interface ProsightDaySummary {
  l0_anchor?: ProsightL0Anchor;
  headline_dim?: string;
  /**
   * Part B / spec 38 §4.7: the summary may become mode-keyed once static
   * baselines land. The legacy flat fields above are retained during rollout;
   * these optional per-mode blocks carry the static-mode narratives when present.
   */
  forecast?: unknown;
  vs_prev_day?: unknown;
  vs_prev_week?: unknown;
}

// ─────────────────────────────────────────────────────────────────────────────
// Day Data Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightDayData {
  today_count: number;
  today_drops: number;
  today_spikes: number;
  l0_today: boolean;
  prev_count: number;
  top_alert: ProsightTopAlert | null;
  today_rows: ProsightTodayRow[];
  summary?: ProsightDaySummary;
  /** Per-BU day payloads (spec 27). Each value has the same shape as this object. */
  per_bu?: Record<string, ProsightDayData>;
  flock_view?: {
    dimensions?: Array<{
      name: string;
      rows: Array<{
        label: string;
        actual: number;
        forecast: number;
        lower: number;
        upper: number;
        point_diff: number;
        band_diff: number;
        anom: "spike" | "drop" | null;
      }>;
    }>;
    top_impacted?: Array<{
      rank: number;
      series: string;
      actual: number;
      forecast: number;
      point_diff: number;
      band_diff: number;
      anom: boolean;
    }>;
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Timeseries Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightTimeseriesPoint {
  date: string;
  actual: number;
  predicted: number;
  lower: number;
  upper: number;
  /**
   * Spec 40: L2 pair series (and newer L1 payloads) carry per-day flags so the
   * pair drill-down can render anomaly pills without an RCA block. Older
   * payloads omit them.
   */
  score?: number;
  is_anomaly?: boolean;
  qualified?: boolean;
  direction?: "spike" | "drop" | string;
  streak?: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// Feature Importance Types
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightRankedFeature {
  feature: string;
  pct: number;
  kind: string;
  suffix: string | null;
}

export interface ProsightFeatureDaily {
  ranked: ProsightRankedFeature[];
}

export interface ProsightFeatureLevelData {
  daily: Record<string, ProsightFeatureDaily>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Display Contract (spec 40 §2.3)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Per-cut metadata the backend emits under `display_contract.by_bu.<bu>.<cut>`.
 * Pair cuts (`level: "L2_pair"`) declare here whether their segments partition
 * the parent (`reconciled`) or overlap (`non_partitioning`) — this supersedes
 * the top-level `non_reconciling_cuts` list, which newer payloads omit.
 */
export interface ProsightDisplayContractCut {
  level?: string;
  reconciled?: boolean;
  non_partitioning?: boolean;
  unqualified_members?: string[];
}

export interface ProsightDisplayContract {
  by_bu?: Record<string, Record<string, ProsightDisplayContractCut>>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Data Structure
// ─────────────────────────────────────────────────────────────────────────────

export interface ProsightNewsData {
  dates: string[];
  /** Multi-BU (spec 27): the available BUs and the default selection. */
  bus?: string[];
  default_bu?: string;
  /**
   * Part A / spec 38 §3.5: dimensions whose segments overlap (one order → many
   * segments) so raw children sum above L0 — currently `sku_name` for labs.
   * When absent, `isNonReconciling` in adapters.ts falls back to a name allowlist.
   */
  non_reconciling_cuts?: string[];
  /** Spec 40 §2.3: per-BU cut metadata (reconciliation flags for L1 + L2 cuts). */
  display_contract?: ProsightDisplayContract;
  data_by_date: Record<string, ProsightDayData>;
  // NOTE (spec 27): these two maps carry BOTH the legacy flat keys
  // (full_id / level_tag, from the default BU) AND per-BU sub-objects keyed by
  // BU name. Use the buSeries / buFeatureImportance helpers to read a BU slice
  // rather than indexing directly.
  series_timeseries: Record<string, ProsightTimeseriesPoint[]>;
  feature_importance: Record<string, ProsightFeatureLevelData>;
}

// ─────────────────────────────────────────────────────────────────────────────
// Component Prop Types
// ─────────────────────────────────────────────────────────────────────────────

export interface OpenDetailPayload {
  full_id: string;
  label: string;
  clickedDate?: string;
  startDate?: string;
  endDate?: string;
  detailStartDate?: string;
  detailEndDate?: string;
  summaryStartDate?: string;
  summaryEndDate?: string;
  nonce?: string;
}

export type DirectionType = "spike" | "drop" | "mixed" | null;

// ─────────────────────────────────────────────────────────────────────────────
// Derived Types for Components
// ─────────────────────────────────────────────────────────────────────────────

export interface L0Verdict {
  is_anomaly: boolean;
  direction: DirectionType;
  actual: number;
  forecast: number;
  forecast_lower: number;
  forecast_upper: number;
  point_miss: number;
  band_miss: number;
  trigger: string;
}

export interface Softness {
  window_days: number;
  avg_per_day: number;
  cumulative: number;
}

export interface TopImpactedRow {
  rank: number;
  series: string;
  full_id: string;
  actual: number;
  forecast: number;
  lower: number;
  upper: number;
  point_diff: number;
  band_diff: number;
  wow_pct: number | null;
  streak: number;
  anom: DirectionType;
}

export interface AggregatedL0Range {
  N: number;
  total_actual: number;
  total_predicted: number;
  total_lower: number;
  total_upper: number;
  cumulative_point_miss: number;
  cumulative_band_miss: number;
  drop_days: number;
  spike_days: number;
  normal_days: number;
  avg_actual_per_day: number;
  avg_predicted_per_day: number;
  avg_point_miss_per_day: number;
  avg_band_miss_per_day: number;
  worst: {
    date: string;
    bandMiss: number;
    pointMiss: number;
    actual: number;
    lower: number;
    upper: number;
    predicted: number;
  } | null;
  direction: DirectionType;
  has_anomaly: boolean;
}

export interface DimensionRow {
  label: string;
  group_id?: string;
  actual: number;
  forecast: number;
  lower: number;
  upper: number;
  point_diff: number;
  point_share_pct: number | null;
  band_diff: number;
  band_share_pct: number | null;
  verdict?: string | null;
  flagged?: boolean;
  self_z?: number;
  anom?: DirectionType;
  drop_days?: number;
  spike_days?: number;
  anom_days?: number;
  /** Part A / spec 38 §3.5: attributed contribution for non-reconciling cuts (Σ = L0 miss). */
  attributed_point_diff?: number;
  /** Spec 40: backend rollup rows (`unqualified`, `low order`) — no drill-down. */
  synthetic?: boolean;
}

export interface DimensionData {
  name: string;
  rows: DimensionRow[];
}

export interface ContributorRow {
  series: string;
  appearances: number;
  anomDays: number;
  drops: number;
  spikes: number;
  sumPointDiff: number;
  sumBandDiff: number;
  sumActual: number;
  sumForecast: number;
  bestRank: number;
  dominantDirection: DirectionType;
}

// ─────────────────────────────────────────────────────────────────────────────
// Table Types
// ─────────────────────────────────────────────────────────────────────────────

export interface TableColumn<T = unknown> {
  key: string;
  label: string;
  align?: "left" | "right" | "center";
  title?: string;
  getValue?: (row: T) => unknown;
  sortByAbs?: boolean;
}

export type SortDirection = "asc" | "desc";

// ─────────────────────────────────────────────────────────────────────────────
// Type Aliases for NewsPage Component
// ─────────────────────────────────────────────────────────────────────────────

/** Alias for ProsightTodayRow - used in NewsPage */
export type TodayRow = ProsightTodayRow;

/** Extended timeseries point with anomaly info - used in NewsPage */
export interface SeriesTimeseries extends ProsightTimeseriesPoint {
  is_anomaly?: boolean;
  qualified?: boolean;
  direction?: "spike" | "drop" | string;
}
