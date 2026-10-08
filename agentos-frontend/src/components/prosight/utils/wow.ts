/**
 * Week-on-week percent change calculation
 */

import type { ProsightTimeseriesPoint } from "../types";

/**
 * Calculate week-on-week percent change for a series timeseries at a given date.
 * @param seriesTs - Array of timeseries points (chronological order)
 * @param dateStr - The date to calculate WoW for
 * @returns Percent change or null if not available
 */
export function wowPct(
  seriesTs: ProsightTimeseriesPoint[] | undefined,
  dateStr: string
): number | null {
  if (!Array.isArray(seriesTs) || !seriesTs.length || !dateStr) return null;

  const idx = seriesTs.findIndex((p) => p.date === dateStr);
  if (idx < 7) return null;

  const today = seriesTs[idx]?.actual;
  const lw = seriesTs[idx - 7]?.actual;

  if (today == null || lw == null || lw === 0) return null;

  return ((today - lw) / lw) * 100;
}
