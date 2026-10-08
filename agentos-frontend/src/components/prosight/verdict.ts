/**
 * Day-verdict logic and the direction→tone vocabulary.
 *
 * Extracted from `FlockDayCard.tsx`, which had become the de-facto design
 * system for the whole Prosight surface: 1151 lines, of which the first ~470
 * were a shared kit imported by five other components. A file named after one
 * card should not be where every other card gets its buttons.
 *
 * Pure logic, no JSX — `surfaces.tsx` renders these tones, and
 * `ProsightSummaryView` reads `l0Verdict` / `directionToneSet` directly.
 */
import type {
  DirectionType,
  L0Verdict,
  ProsightTimeseriesPoint,
  Softness,
} from "./types";

export function computeSoftness(
  l0Series: ProsightTimeseriesPoint[] | undefined,
  asOfDate: string,
  windowDays = 16
): Softness | null {
  if (!l0Series?.length) return null;
  const idx = l0Series.findIndex((p) => p.date === asOfDate);
  if (idx < 0) return null;
  const start = Math.max(0, idx - windowDays + 1);
  const slice = l0Series.slice(start, idx + 1);
  if (!slice.length) return null;
  const misses = slice.map((p) => (p.actual ?? 0) - (p.predicted ?? 0));
  const cumulative = misses.reduce((s, v) => s + v, 0);
  return {
    window_days: slice.length,
    avg_per_day: cumulative / slice.length,
    cumulative,
  };
}

export function l0Verdict(point: ProsightTimeseriesPoint | null): L0Verdict | null {
  if (!point) return null;
  const { actual, predicted, lower, upper } = point;
  const isDrop = actual < lower;
  const isSpike = actual > upper;
  const direction: DirectionType = isDrop ? "drop" : isSpike ? "spike" : null;
  const point_miss = actual - predicted;
  const band_miss = isDrop ? actual - lower : isSpike ? actual - upper : 0;
  return {
    is_anomaly: Boolean(direction),
    direction,
    actual,
    forecast: predicted,
    forecast_lower: lower,
    forecast_upper: upper,
    point_miss,
    band_miss,
    trigger: isDrop
      ? "actual fell below the lower forecast band"
      : isSpike
        ? "actual rose above the upper forecast band"
        : "actual stayed inside the forecast band",
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Direction Tone Helpers
// ─────────────────────────────────────────────────────────────────────────────

export function directionToneSet(dir: DirectionType): {
  surface: string;
  border: string;
  softBorder: string;
  text: string;
  accent: string;
  fill: string;
  pillBg: string;
  pillText: string;
  pillBorder: string;
  arrow: string;
} {
  if (dir === "drop")
    return {
      surface: "bg-red-50",
      border: "border-red-600",
      softBorder: "border-red-200",
      text: "text-red-800",
      accent: "text-red-700",
      fill: "bg-red-600",
      pillBg: "bg-red-100",
      pillText: "text-red-800",
      pillBorder: "border-red-200",
      arrow: "\u25BC",
    };
  if (dir === "spike")
    return {
      surface: "bg-amber-50",
      border: "border-amber-600",
      softBorder: "border-amber-200",
      text: "text-amber-800",
      accent: "text-amber-700",
      fill: "bg-amber-600",
      pillBg: "bg-amber-100",
      pillText: "text-amber-800",
      pillBorder: "border-amber-200",
      arrow: "\u25B2",
    };
  if (dir === "mixed")
    return {
      surface: "bg-blue-50",
      border: "border-blue-600",
      softBorder: "border-blue-200",
      text: "text-blue-800",
      accent: "text-blue-700",
      fill: "bg-blue-600",
      pillBg: "bg-blue-100",
      pillText: "text-blue-800",
      pillBorder: "border-blue-200",
      arrow: "\u25C6",
    };
  return {
    surface: "bg-green-50",
    border: "border-green-600",
    softBorder: "border-green-200",
    text: "text-green-800",
    accent: "text-green-700",
    fill: "bg-green-600",
    pillBg: "bg-green-100",
    pillText: "text-green-800",
    pillBorder: "border-green-200",
    arrow: "\u25CF",
  };
}
