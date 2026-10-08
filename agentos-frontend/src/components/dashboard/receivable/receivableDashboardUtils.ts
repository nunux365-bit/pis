import { HEAT_EMPTY } from "@/lib/receivableDashboardConstants";

export function formatLakh(n: number) {
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

export function isCollectionNotDueLabel(label: string): boolean {
  const t = label.trim().toLowerCase();
  return t === "not due" || t.startsWith("not due ");
}

/**
 * New API: full line already includes `₹` and usually weighted months.
 * Old snapshots: plain name(s) only — add ₹ so every line has a number; for multiple
 * names we apportion the cell total equally (display-only until a fresh ingest).
 */
export function displayPartyLine(
  line: string,
  cellLakh: number,
  namesInCell: number
): string {
  if (!line.trim()) return line;
  if (line.includes("₹")) return line;
  if (cellLakh <= 0 || namesInCell < 1) return line.trim();
  const per = cellLakh / namesInCell;
  return `${line.trim()} (₹${formatLakh(per)} L)`;
}

export function formatPct(n: number) {
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(1)}%`;
}

/** First reminder column: API / legacy snapshots used `"1"`; show as **0-1** in the UI. */
export function displayReminderBandLabel(b: string): string {
  const t = (b || "").trim();
  return t === "1" ? "0-1" : t;
}

export function findKpi(
  kpi: Record<string, number> | null | undefined,
  patterns: RegExp[]
): number | null {
  if (!kpi) return null;
  for (const [k, v] of Object.entries(kpi)) {
    if (!Number.isFinite(v)) continue;
    const key = k.trim();
    for (const p of patterns) {
      if (p.test(key)) return v;
    }
  }
  return null;
}

/**
 * The workbook **Net due** (or *Net dues*) column, resilient to extra inner spaces
 * (e.g. ``"Net  Due"``) and case.
 */
export function findKpiNetDue(
  kpi: Record<string, number> | null | undefined
): number | null {
  if (!kpi) return null;
  for (const [k, v] of Object.entries(kpi)) {
    if (!Number.isFinite(v)) continue;
    const n = k.trim().replace(/\s+/g, " ").toLowerCase();
    if (n === "net due" || n === "net dues") {
      return v;
    }
  }
  return null;
}

export function isDueAgeingBucketKey(name: string): boolean {
  const t = name.trim().toLowerCase();
  if (!t) return false;
  if (t === "receivables" || t === "net due" || t === "check") return false;
  if (t === "not due" || /^not due\s/.test(t)) return false;
  if (/^due tds$|^not due tds$/i.test(t)) return false;
  if (
    t.includes("month") ||
    t.includes("yrs") ||
    t.includes("year") ||
    /^\d+\s*[-–]\s*\d+/.test(t) ||
    /^\+1|^1y\+|^>\s*1/i.test(t)
  ) {
    return true;
  }
  return false;
}

export function ageBucketSortKey(name: string): [number, string] {
  const t = name.toLowerCase();
  if (t.includes("0-1") || t.includes("0–1")) return [0, name];
  if (t.includes("1-3") || t.includes("1–3")) return [1, name];
  if (t.includes("3-6") || t.includes("3–6")) return [2, name];
  if (t.includes("6-9") || t.includes("6–9") || t.includes("6-12") || t.includes("6–12")) return [3, name];
  if (t.includes("9-12") || t.includes("9–12")) return [4, name];
  if (t.includes("yrs") || t.includes("year") || t.includes("+1") || t.includes("1y")) return [5, name];
  return [50, name];
}

export function buildDueAgeingRows(
  kpi: Record<string, number> | null | undefined
): { label: string; amount: number }[] {
  if (!kpi) return [];
  const rows = Object.entries(kpi)
    .filter(([k, v]) => isDueAgeingBucketKey(k) && Number.isFinite(v))
    .map(([k, v]) => ({ label: k.trim(), amount: v }));
  rows.sort((a, b) => {
    const [ia, sa] = ageBucketSortKey(a.label);
    const [ib, sb] = ageBucketSortKey(b.label);
    if (ia !== ib) return ia - ib;
    return sa.localeCompare(sb);
  });
  return rows;
}

/** Ageing row × reminder column (0–1 mo … 1Y+ × 0-1…6+). Matches `REMINDER_GRID_FORMAT` / `values_lakh` layout. */
const COLLECTION_HEAT_ROWS = 5;
const COLLECTION_HEAT_COLS = 6;

/**
 * Executive-style 5×6 heat: green at **(0,0)** (young book, few reminders) → red at **(4,5)**
 * (1Y+, 6+ reminders). Slightly more weight on the reminder *column* so column 1 reads
 * consistently green, matching the portfolio grid screenshot.
 * Cell-by-cell, stable HSL; **both** "Total due" and "Parties" use the same (ri, ci) from the API.
 */
export const COLLECTION_MATRIX_HEAT_5X6: readonly (readonly string[])[] = (() => {
  const m: string[][] = [];
  for (let ri = 0; ri < COLLECTION_HEAT_ROWS; ri++) {
    const tRow = COLLECTION_HEAT_ROWS > 1 ? ri / (COLLECTION_HEAT_ROWS - 1) : 0;
    const row: string[] = [];
    for (let ci = 0; ci < COLLECTION_HEAT_COLS; ci++) {
      const tCol = COLLECTION_HEAT_COLS > 1 ? ci / (COLLECTION_HEAT_COLS - 1) : 0;
      const t = Math.min(1, 0.35 * tRow + 0.65 * tCol);
      const h = 128 * (1 - t);
      const s = 40 + 28 * t;
      const l = 90 - 15 * t;
      row.push(
        `hsl(${Math.round(h)} ${Math.round(s)}% ${Math.round(l)}%)`
      );
    }
    m.push(row);
  }
  return m;
})();

/** Fixed heat for one matrix cell. `ageingRowIndex` / `reminderColIndex` = API `ri` / `ci` (0-based). */
export function collectionMatrixCellHeat(
  ageingRowIndex: number,
  reminderColIndex: number
): string {
  if (
    ageingRowIndex < 0 ||
    ageingRowIndex >= COLLECTION_HEAT_ROWS ||
    reminderColIndex < 0 ||
    reminderColIndex >= COLLECTION_HEAT_COLS
  ) {
    return HEAT_EMPTY;
  }
  return COLLECTION_MATRIX_HEAT_5X6[ageingRowIndex][reminderColIndex];
}

export function toFiniteLakh(v: unknown): number {
  if (v == null) return Number.NaN;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const t = v.trim().replace(/,/g, "");
    if (t === "") return Number.NaN;
    const n = parseFloat(t);
    return Number.isFinite(n) ? n : Number.NaN;
  }
  const n = Number(v);
  return Number.isFinite(n) ? n : Number.NaN;
}

export function toFiniteMonths(v: unknown): number | null {
  if (v == null) return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = parseFloat(v.trim().replace(/,/g, ""));
    return Number.isFinite(n) ? n : null;
  }
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function parseLakhField(s: string): number {
  const t = s.trim();
  if (t === "") return Number.NaN;
  const n = parseFloat(t.replace(/,/g, ""));
  if (Number.isFinite(n)) return n < 0 ? 0 : n;
  return Number.NaN;
}

export function maxPositiveInMatrix(m: (number | unknown)[][] | undefined): number {
  if (!m?.length) return 0;
  let x = 0;
  for (const row of m) {
    for (const v of row) {
      const t = toFiniteLakh(v);
      if (t > 0 && Number.isFinite(t) && t > x) x = t;
    }
  }
  return x;
}

export function inLakhFilter(
  valueLakh: unknown,
  minL: number,
  maxL: number | null
): boolean {
  const v = toFiniteLakh(valueLakh);
  if (!Number.isFinite(v) || v <= 0) return false;
  if (v < minL) return false;
  if (maxL != null && Number.isFinite(maxL) && v > maxL) return false;
  return true;
}

/** Min &gt; 0 or max set — apply per-party ₹ line checks, not only the cell total. */
export function isLakhRangeFilterActive(
  minL: number,
  maxL: number | null
): boolean {
  if (Number.isFinite(minL) && minL > 0) return true;
  if (maxL != null && Number.isFinite(maxL)) return true;
  return false;
}

const PARTY_LINE_LAKH_RE = /₹\s*([\d,]+(?:\.\d+)?)\s*L/i;

/** Parse Lakh from ``"Name (₹12.34 L, 1.00m)"``-style lines (same as API format). */
export function parseLakhFromPartyLine(line: string): number | null {
  const m = (line || "").match(PARTY_LINE_LAKH_RE);
  if (!m) return null;
  const n = parseFloat(m[1].replace(/,/g, ""));
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export type CollectionCellFilterView = {
  displayAmount: number;
  visibleNames: string[];
  inRange: boolean;
};

/**
 * Same background for "Total due" and "Parties" for `(ri, ci)`.
 * Background is always the positional heat map. When the ₹ min/max filter is on,
 * out-of-range cells use muted **text** only (`FILTER_MUTED` in the grid), not a flat neutral fill.
 */
export function collectionGridCellBackground(
  ageingRowIndex: number,
  reminderColIndex: number,
  _resolved: CollectionCellFilterView,
  _filterMinL: number,
  _filterMaxL: number | null
): string {
  return collectionMatrixCellHeat(ageingRowIndex, reminderColIndex);
}

/**
 * Align "Total due" and "Parties" with the min/max filter: when the filter is active, use
 * each party line's embedded ₹ L (not only the cell aggregate, which can exceed the bar while
 * every line is below it).
 */
export function resolveCollectionCellForFilter(
  cValRaw: unknown,
  names: string[] | null | undefined,
  minL: number,
  maxL: number | null
): CollectionCellFilterView {
  const cell = toFiniteLakh(cValRaw);
  const cVal = Number.isFinite(cell) && cell >= 0 ? cell : 0;
  const list = names?.length ? [...names] : [];

  if (!isLakhRangeFilterActive(minL, maxL)) {
    return {
      displayAmount: cVal,
      visibleNames: list,
      inRange: inLakhFilter(cVal, minL, maxL),
    };
  }

  if (list.length === 0) {
    return {
      displayAmount: cVal,
      visibleNames: [],
      inRange: inLakhFilter(cVal, minL, maxL),
    };
  }

  const withVal: { line: string; a: number }[] = [];
  for (const line of list) {
    const a = parseLakhFromPartyLine(line);
    if (a != null && Number.isFinite(a)) {
      withVal.push({ line, a });
    }
  }

  if (withVal.length > 0) {
    const passed = withVal.filter((p) => inLakhFilter(p.a, minL, maxL));
    const sum = passed.reduce((s, p) => s + p.a, 0);
    return {
      displayAmount: sum,
      visibleNames: passed.map((p) => p.line),
      inRange: sum > 0,
    };
  }

  return {
    displayAmount: cVal,
    visibleNames: list,
    inRange: inLakhFilter(cVal, minL, maxL),
  };
}
