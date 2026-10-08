/**
 * Utility functions for NewsPage - Prosight N Dashboard
 */

import type { TodayRow, SeriesTimeseries } from "../types";
import { LEVELS, FEAT_EXP } from "./constants";
import type { LevelType, FilterState, SeriesInfo } from "./types";

// ══════════════════════════════════════════════════════════════════════════════
// Formatters
// ══════════════════════════════════════════════════════════════════════════════

/** Format number with Indian locale or dash for null */
export const fmt = (v: number | null | undefined): string =>
  v == null ? "—" : Math.round(v).toLocaleString("en-IN");

/** Abbreviate large numbers (M for millions, k for thousands) */
export const abbr = (v: number): string =>
  Math.abs(v) >= 1e6
    ? `${(v / 1e6).toFixed(1)}M`
    : Math.abs(v) >= 1000
      ? `${(v / 1000).toFixed(0)}k`
      : String(Math.round(v));

/** Shorten level string (L0_total -> L0) */
export const lvlS = (l: string): string =>
  ({ L0_total: "L0", L1_single: "L1", L2_pair: "L2", L3_full: "L3" })[l] ?? l;

/** Extract MM-DD from date string */
export const mmdd = (d: string | undefined): string => d?.slice(5) ?? "";

// ══════════════════════════════════════════════════════════════════════════════
// Label & Series Functions
// ══════════════════════════════════════════════════════════════════════════════

/** Generate human-readable label from dimensions */
export function seriesLabel(
  dims: Record<string, string | null> | undefined,
  level: string
): string {
  if (level === "L0_total") return "L0 — Total (all series)";
  const parts = Object.entries(dims || {}).filter(
    ([, v]) => v && !["nan", "None", ""].includes(String(v))
  );
  if (!parts.length) return "Total";
  return (
    parts
      .slice(0, 4)
      .map(([k, v]) => `${k}: ${v}`)
      .join(" · ") + (parts.length > 4 ? "…" : "")
  );
}

/** Get direct children in hierarchy */
export function directChildren(
  dims: Record<string, string | null> | undefined,
  level: string,
  allSeries: SeriesInfo[]
): SeriesInfo[] {
  const i = LEVELS.indexOf(level as LevelType);
  if (i < 0 || i >= LEVELS.length - 1) return [];
  const nextLevel = LEVELS[i + 1];
  const parentEntries = Object.entries(dims || {});
  return allSeries.filter((s) => {
    if (s.level !== nextLevel) return false;
    if (level === "L0_total") return true;
    return parentEntries.every(([k, v]) => s.dims?.[k] === v);
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Statistics & Matching
// ══════════════════════════════════════════════════════════════════════════════

/** Compute model accuracy statistics for a series */
export function computeSeriesStats(pts: SeriesTimeseries[] | undefined) {
  if (!pts || !pts.length) return null;
  const n = pts.length;
  const smape =
    pts.reduce((s, p) => {
      const denom = Math.abs(p.actual) + Math.abs(p.predicted);
      return (
        s +
        (denom > 0
          ? ((2 * Math.abs(p.actual - p.predicted)) / denom) * 100
          : 0)
      );
    }, 0) / n;
  const rmse = Math.sqrt(
    pts.reduce((s, p) => s + (p.actual - p.predicted) ** 2, 0) / n
  );
  const mae = pts.reduce((s, p) => s + Math.abs(p.actual - p.predicted), 0) / n;
  const mean = pts.reduce((s, p) => s + p.actual, 0) / n;
  const anomPts = pts.filter((p) => p.is_anomaly && p.qualified);
  return {
    smape: smape.toFixed(1) + "%",
    rmse: Math.round(rmse).toLocaleString("en-IN"),
    mae: Math.round(mae).toLocaleString("en-IN"),
    meanActual: Math.round(mean).toLocaleString("en-IN"),
    qualified: pts.some((p) => p.qualified),
    anomDays: anomPts.length,
  };
}

/** Check if row matches filter criteria */
export function matchRow(row: TodayRow, F: FilterState): boolean {
  if (F.qualifiedOnly && !row.qualified) return false;
  if (F.dir !== "all" && row.direction !== F.dir) return false;
  if (row.score < F.minScore) return false;
  if (!F.levels.has(row.level)) return false;
  // BU is selected upstream by slicing data_by_date to the chosen BU (spec 27);
  // per-BU rows no longer carry a `bu` dim, so don't filter on it here.
  return true;
}

// ══════════════════════════════════════════════════════════════════════════════
// Driver Explanation
// ══════════════════════════════════════════════════════════════════════════════

/** Generate human-readable explanation for driver text */
export function explainDriver(driverText: string | undefined): string | null {
  if (!driverText || driverText === "—") return null;
  const explanations = driverText
    .split(" · ")
    .slice(0, 2)
    .map((part) => {
      for (const [feat, desc] of Object.entries(FEAT_EXP)) {
        if (part.includes(feat)) {
          return part.includes(" up ") ? desc.up : desc.down;
        }
      }
      return null;
    })
    .filter(Boolean);
  return explanations.length ? explanations.join(" ") : null;
}

// ══════════════════════════════════════════════════════════════════════════════
// Direction Helpers
// ══════════════════════════════════════════════════════════════════════════════

/** Get color for direction (drop = red, spike = amber) */
export const arrowColor = (dir: string): string =>
  dir === "drop" ? "#dc2626" : "#f59e0b";

/** Get arrow symbol for direction */
export const arrowSymbol = (dir: string): string => (dir === "drop" ? "↓" : "↑");
