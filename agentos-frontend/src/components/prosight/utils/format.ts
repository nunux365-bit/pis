
/*** Formatting utilities for Prosight dashboard
 */

/**
 * Format integer with Indian locale (commas)
 */
export function fmtInt(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return Math.round(v).toLocaleString("en-IN");
}

/**
 * Format signed integer with +/- prefix
 */
export function fmtSigned(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const r = Math.round(v);
  return `${r > 0 ? "+" : ""}${r.toLocaleString("en-IN")}`;
}

/**
 * Format percentage with +/- prefix
 */
export function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

/**
 * Clean a value for display (handle nan, None, empty string)
 */
export function clean(v: unknown): string | null {
  if (v == null || v === "" || v === "nan" || v === "None") return null;
  return String(v);
}

/**
 * Get weekday abbreviation from date string
 */
const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function weekdayOf(dateStr: string): string {
  const d = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  return WEEKDAY[d.getDay()];
}

/**
 * Get level short name
 */
export function levelShort(lvl: string): string {
  const map: Record<string, string> = {
    L0_total: "L0",
    L1_single: "L1",
    L2_pair: "L2",
    L3_full: "L3",
  };
  return map[lvl] || lvl || "—";
}
