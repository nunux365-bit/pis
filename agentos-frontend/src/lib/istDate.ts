/**
 * IST calendar-day helpers.
 *
 * Rows are stored in UTC, but users pick dates as IST calendar days. A date
 * picked as "2026-07-23" means the IST day, which spans
 * 2026-07-22T18:30:00Z → 2026-07-23T18:29:59.999Z in UTC.
 *
 * IST is a fixed UTC+5:30 offset with no daylight saving, so a constant offset
 * is exact — no timezone database needed.
 */

export const IST_OFFSET_MINUTES = 330;
const IST_OFFSET_MS = IST_OFFSET_MINUTES * 60_000;

function parseIsoDate(isoDate: string): [number, number, number] | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** First instant of the given IST calendar day, as a UTC ISO string. */
export function istDayStartUtc(isoDate: string): string | null {
  const parts = parseIsoDate(isoDate);
  if (!parts) return null;
  const [y, m, d] = parts;
  return new Date(Date.UTC(y, m - 1, d, 0, 0, 0, 0) - IST_OFFSET_MS).toISOString();
}

/** Last instant of the given IST calendar day, as a UTC ISO string. */
export function istDayEndUtc(isoDate: string): string | null {
  const parts = parseIsoDate(isoDate);
  if (!parts) return null;
  const [y, m, d] = parts;
  return new Date(Date.UTC(y, m - 1, d, 23, 59, 59, 999) - IST_OFFSET_MS).toISOString();
}

/** The IST calendar date (yyyy-mm-dd) that the given instant falls on. */
export function istDateOf(instant: Date): string {
  return new Date(instant.getTime() + IST_OFFSET_MS).toISOString().slice(0, 10);
}

/** Today's IST calendar date (yyyy-mm-dd). */
export function istToday(now: Date = new Date()): string {
  return istDateOf(now);
}

/** Inclusive IST date range covering the last ``days`` IST days, ending today. */
export function istLastNDays(days: number, now: Date = new Date()): { from: string; to: string } {
  const to = istDateOf(now);
  const from = istDateOf(new Date(now.getTime() - (days - 1) * 86_400_000));
  return { from, to };
}

/** Render a UTC/ISO timestamp in IST, so the table agrees with the IST date filter. */
export function formatIst(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: true });
}
