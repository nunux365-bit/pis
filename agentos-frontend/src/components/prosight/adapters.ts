/**
 * Field adapters for Prosight spec 38 (labs cuts + static baselines).
 *
 * NOT a validation boundary. Nothing here checks an incoming payload against a
 * schema, and the `ProsightNewsData` types these functions consume are a
 * compile-time claim only — that payload enters the app through a bare
 * `fetch().then((d: ProsightNewsData) => …)` type assertion in
 * ProsightSummaryView, so TypeScript believes whatever the backend sent. Treat
 * every field read here as unverified. Where a malformed value would otherwise
 * reach arithmetic, the accessors below screen it with `finite()` and report
 * "no value" rather than emitting NaN into the UI; that is local defensive
 * coding, not a guarantee about the payload as a whole.
 *
 * These thin helpers isolate two families of backend fields whose exact names
 * are still provisional (backend spec 38 §8), so the rest of the UI codes
 * against a stable surface and a rename is a one-line change here:
 *
 *  - Part A: non-reconciling (overlapping) cuts such as `sku_name`, where raw
 *    children sum ABOVE the L0 miss and the backend supplies a redistributed
 *    `attributed_point_diff` that reconciles back to L0.
 *  - Part B: static-baseline comparisons (`forecast` / `vs_prev_day` /
 *    `vs_prev_week`) carried on the latest-day L0 row.
 *
 * Everything degrades gracefully: on older JSON (no `baselines`, no labs cuts)
 * the accessors synthesize a forecast baseline from legacy fields and fall back
 * to a name allowlist for non-reconciling detection, so today's screens render
 * unchanged.
 */
import type {
  BaselineMode,
  DirectionType,
  ProsightBaseline,
  ProsightNewsData,
  ProsightTimeseriesPoint,
  ProsightTodayRow,
  DimensionRow,
} from "./types";

type SeriesMap = Record<string, ProsightTimeseriesPoint[]>;

const L0_LEVEL = "L0_total";

/**
 * Screen a value that `./types` declares as `number` before it reaches
 * arithmetic. The declaration is unenforced (see the module note), so a null or
 * a numeric string from the still-evolving backend would otherwise flow into a
 * subtraction and surface as NaN — which renders as a plausible-looking figure
 * rather than an obvious absence. Strings are rejected rather than coerced:
 * silently accepting "1234" would hide exactly the drift worth noticing.
 */
function finite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Part B — static baselines
// ─────────────────────────────────────────────────────────────────────────────

export const BASELINE_MODES: BaselineMode[] = [
  "forecast",
  "vs_prev_day",
  "vs_prev_week",
];

export const BASELINE_LABELS: Record<BaselineMode, string> = {
  forecast: "Forecast",
  vs_prev_day: "vs yesterday",
  vs_prev_week: "vs last week",
};

/** Longer captions for stat labels / narratives. */
export const BASELINE_COMPARE_LABELS: Record<BaselineMode, string> = {
  forecast: "forecast",
  vs_prev_day: "yesterday",
  vs_prev_week: "last week",
};

/** How many days back each static mode compares against. */
const BASELINE_OFFSET_DAYS: Record<Exclude<BaselineMode, "forecast">, number> = {
  vs_prev_day: 1,
  vs_prev_week: 7,
};

/** Coerce a backend direction (which may be "normal") to the FE DirectionType. */
export function normDirection(dir: unknown): DirectionType {
  return dir === "spike" || dir === "drop" || dir === "mixed" ? dir : null;
}

/**
 * Shift an ISO date (YYYY-MM-DD) by a number of days (UTC, DST-safe). Returns
 * null on an unparseable date — `toISOString()` throws RangeError on an Invalid
 * Date, which would take down the render rather than skip one comparison.
 */
function shiftIso(iso: string, deltaDays: number): string | null {
  const [y, m, d] = String(iso).split("-").map(Number);
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (Number.isNaN(dt.getTime())) return null;
  dt.setUTCDate(dt.getUTCDate() + deltaDays);
  return dt.toISOString().slice(0, 10);
}

/**
 * Resolve the baseline block for a row in a given mode.
 *
 * Returns the backend-provided block when present (schema: `anchor_value` is the
 * comparison basis, `actual` is today's value, `delta = actual − anchor_value`).
 * For `forecast` on legacy rows (no `baselines`), synthesizes an equivalent block
 * from the flat fields so the existing forecast view keeps working. Returns null
 * for a static mode the backend did not compute (older JSON) — callers hide those.
 */
export function rowBaseline(
  row: ProsightTodayRow | null | undefined,
  mode: BaselineMode,
): ProsightBaseline | null {
  if (!row) return null;
  const provided = row.baselines?.[mode];
  if (provided) {
    const pActual = finite(provided.actual);
    const pAnchor = finite(provided.anchor_value);
    // A block whose two core numbers are malformed is treated as absent rather
    // than passed through to become NaN in a caller's subtraction. For
    // `forecast` that falls through to the legacy synthesis below; for a static
    // mode it yields null, which callers already handle as "not computed".
    if (pActual !== null && pAnchor !== null) {
      const pDelta = finite(provided.delta) ?? pActual - pAnchor;
      return {
        ...provided,
        actual: pActual,
        anchor_value: pAnchor,
        delta: pDelta,
        pct: finite(provided.pct) ?? (pAnchor ? (pDelta / pAnchor) * 100 : 0),
        lower: finite(provided.lower),
        upper: finite(provided.upper),
        direction: normDirection(provided.direction),
      };
    }
  }

  if (mode === "forecast") {
    const actual = finite(row.actual);
    const anchor = finite(row.predicted);
    if (actual === null || anchor === null) return null;
    const isAnomaly = row.direction === "drop" || row.direction === "spike";
    const delta = actual - anchor;
    return {
      actual,
      anchor_value: anchor,
      delta,
      pct: anchor ? (delta / anchor) * 100 : 0,
      lower: null,
      upper: null,
      is_anomaly: isAnomaly,
      direction: row.direction ?? null,
    };
  }
  return null;
}

/**
 * Compute a static baseline for ANY series from its own timeseries, comparing the
 * as-of day's actual to the actual N days earlier (1 = yesterday, 7 = last week).
 *
 * The per-segment tables (Top Impacted / Per-Dimension) are built from
 * `rca.by_dimension` children, which carry no `baselines` block — but
 * `series_timeseries` covers those series 100%, and prior-day/week actuals
 * reproduce the backend's L0 baselines exactly (spec 38 §4.5). Returns null for
 * forecast mode or when the compared day is missing (callers fall back).
 */
export function seriesBaseline(
  series: ProsightTimeseriesPoint[] | undefined,
  asOfDate: string,
  mode: BaselineMode,
): ProsightBaseline | null {
  if (mode === "forecast" || !series?.length || !asOfDate) return null;
  // `finite` supersedes the old `== null || Number.isNaN(…)` pair, which let a
  // numeric string through untouched (Number.isNaN("abc") is false).
  const actual = finite(series.find((p) => p.date === asOfDate)?.actual);
  if (actual === null) return null;
  const priorDate = shiftIso(asOfDate, -BASELINE_OFFSET_DAYS[mode]);
  if (priorDate === null) return null;
  const anchor = finite(series.find((p) => p.date === priorDate)?.actual);
  if (anchor === null) return null;

  const delta = actual - anchor;
  return {
    actual,
    anchor_value: anchor,
    delta,
    pct: anchor ? (delta / anchor) * 100 : 0,
    lower: null,
    upper: null,
    is_outside_band: null,
    direction: delta < 0 ? "drop" : delta > 0 ? "spike" : null,
  };
}

/** Probe the default-BU latest-day L0 row: does this dataset carry real static baselines? */
export function hasBaselines(data: ProsightNewsData | null | undefined): boolean {
  if (!data?.dates?.length) return false;
  const latest = data.dates[data.dates.length - 1];
  const day = data.data_by_date?.[latest];
  const bu = data.default_bu;
  const rows =
    (bu ? day?.per_bu?.[bu]?.today_rows : undefined) ?? day?.today_rows ?? [];
  return rows.some(
    (r) =>
      r.level === L0_LEVEL &&
      r.baselines != null &&
      (r.baselines.vs_prev_day != null || r.baselines.vs_prev_week != null),
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Part A — non-reconciling (overlapping) cuts
// ─────────────────────────────────────────────────────────────────────────────

const NON_RECONCILING_FALLBACK = new Set(["sku_name", "sku", "name"]);

/**
 * Is this dimension an overlapping cut whose raw children sum above L0?
 * Prefers the backend's declared list; falls back to a name allowlist so labs
 * `sku_name` is handled even before the backend emits `non_reconciling_cuts`.
 */
export function isNonReconciling(
  dimName: string | null | undefined,
  data?: ProsightNewsData | null,
): boolean {
  if (!dimName) return false;
  const declared = data?.non_reconciling_cuts;
  if (declared && declared.length) return declared.includes(dimName);
  return NON_RECONCILING_FALLBACK.has(dimName);
}

/**
 * The contribution to show for a segment: the backend's attributed figure when
 * available (Σ reconciles to L0), else the raw point_diff. Used in place of raw
 * point_diff for non-reconciling cuts so the Total reconciles once the backend
 * ships attribution.
 */
export function childShareDiff(row: DimensionRow): number | null {
  return finite(row.attributed_point_diff) ?? finite(row.point_diff);
}

/** Caveat shown on a non-reconciling breakdown so the >L0 sum isn't read as a bug. */
export const NON_RECONCILING_CAVEAT =
  "Overlapping cut — one order maps to many segments, so rows sum above the L0 total. " +
  "Per-segment diffs are shown as delivered; the L0 share partition is suppressed.";

// ─────────────────────────────────────────────────────────────────────────────
// Part C — L2 pair cuts (spec 40)
//
// L2 arrives in `series_timeseries` as `L2_pair::<cutKey>::<valA>|<valB>` where
// `<cutKey>` is two L1 series dims joined with `_` in pipeline order
// (`city_channel`, `city_name`, `name_channel`) and the value order matches the
// cut key's dim order. Flagged pairs additionally appear in `today_rows` with
// `level: "L2_pair"`. Cut-level rollup keys (`…::low order`, `…::unqualified`)
// have no `|` in the value part and are NOT (a, b) pairs — every enumerator
// below uses "exactly one `|`" as the membership test.
// ─────────────────────────────────────────────────────────────────────────────

const L2_SERIES_PREFIX = "L2_pair::";

/** RCA dim names → the series-fid dim vocabulary (labs RCA says `sku_name`,
 *  series fids and pair cut keys say `name`). */
const DIM_TO_SERIES_DIM: Record<string, string> = { sku_name: "name", sku: "name" };

export function seriesDimOf(dim: string): string {
  return DIM_TO_SERIES_DIM[dim] ?? dim;
}

/** L1 series dims present in a BU-sliced series map — the vocabulary pair cut
 *  keys are composed from. */
export function knownSeriesDims(series: SeriesMap | null | undefined): string[] {
  const out = new Set<string>();
  for (const k of Object.keys(series ?? {})) {
    if (!k.startsWith("L1_single::")) continue;
    const dim = k.split("::")[1];
    if (dim) out.add(dim);
  }
  return [...out];
}

/**
 * Split a pair cut key ("city_channel") into its two dims against the known dim
 * vocabulary. Dims themselves may contain underscores (`coupon_flag`), so every
 * underscore is tried as the boundary; null when no split matches.
 */
export function pairDims(
  cutKey: string,
  knownDims: string[],
): [string, string] | null {
  let i = cutKey.indexOf("_");
  while (i !== -1) {
    const a = cutKey.slice(0, i);
    const b = cutKey.slice(i + 1);
    if (knownDims.includes(a) && knownDims.includes(b)) return [a, b];
    i = cutKey.indexOf("_", i + 1);
  }
  return null;
}

/**
 * Pair cuts available for drill-down from dimension D, enumerated reflectively
 * from the series map (the series ARE the drill-down's data source, so a cut
 * only counts when at least one real pair series exists for it). Unordered —
 * callers sort for display.
 */
export function pairCutsFor(
  series: SeriesMap | null | undefined,
  dim: string,
): string[] {
  const sDim = seriesDimOf(dim);
  const knownDims = knownSeriesDims(series);
  const out = new Set<string>();
  for (const k of Object.keys(series ?? {})) {
    if (!k.startsWith(L2_SERIES_PREFIX)) continue;
    const parts = k.split("::");
    if (parts.length !== 3 || parts[2].split("|").length !== 2) continue; // rollup / malformed
    const cutKey = parts[1];
    if (out.has(cutKey)) continue;
    const dims = pairDims(cutKey, knownDims);
    if (dims && (dims[0] === sDim || dims[1] === sDim)) out.add(cutKey);
  }
  return [...out];
}

/** The other side of pair cut `cutKey` relative to dim D (null if D isn't a side). */
export function pairOtherDim(
  cutKey: string,
  dim: string,
  knownDims: string[],
): string | null {
  const sDim = seriesDimOf(dim);
  const dims = pairDims(cutKey, knownDims);
  if (!dims) return null;
  if (dims[0] === sDim) return dims[1];
  if (dims[1] === sDim) return dims[0];
  return null;
}

/**
 * Sub-table rows for parent segment (dim=D, value=v) under pair cut `cutKey`,
 * built from the pair series' as-of-day points. Row labels are the OTHER dim's
 * value; `group_id` is `<cutKey>::<valA>|<valB>` so `L2_pair::<group_id>`
 * addresses the series again (baselines, detail links). Pairs with no point on
 * `date` are skipped. Flagged = the point's own band verdict, gated on
 * `qualified` (unqualified anomalies stay pill-less, matching today_rows).
 * `todayRows` (optional) enriches matching flagged pairs with their score.
 */
export function pairChildren(
  series: SeriesMap | null | undefined,
  cutKey: string,
  dim: string,
  value: string,
  date: string,
  todayRows?: ProsightTodayRow[] | null,
): DimensionRow[] {
  const sDim = seriesDimOf(dim);
  const dims = pairDims(cutKey, knownSeriesDims(series));
  if (!dims || !date) return [];
  const side = dims[0] === sDim ? 0 : dims[1] === sDim ? 1 : -1;
  if (side === -1) return [];

  const prefix = `${L2_SERIES_PREFIX}${cutKey}::`;
  const out: DimensionRow[] = [];
  for (const [key, pts] of Object.entries(series ?? {})) {
    if (!key.startsWith(prefix)) continue;
    const vals = key.slice(prefix.length).split("|");
    if (vals.length !== 2 || vals[side] !== value) continue;
    const p = pts?.find((pt) => pt.date === date);
    if (!p) continue;
    const actual = finite(p.actual);
    if (actual === null) continue; // no usable value for this day — skip the row
    const predicted = finite(p.predicted);
    const lower = finite(p.lower);
    const upper = finite(p.upper);

    const groupId = `${cutKey}::${vals[0]}|${vals[1]}`;
    const flagged = p.is_anomaly === true && p.qualified !== false;
    const enrich = todayRows?.find(
      (r) => r.level === "L2_pair" && r.group_id === groupId,
    );
    out.push({
      label: vals[1 - side],
      group_id: groupId,
      actual,
      forecast: predicted ?? 0,
      lower: lower ?? 0,
      upper: upper ?? 0,
      point_diff: actual - (predicted ?? 0),
      point_share_pct: null, // computed by the caller against the parent
      // A missing band bound means "no band", not a zero bound: the old
      // comparison coerced null to 0 and reported the whole actual as an
      // excursion.
      band_diff:
        upper !== null && actual > upper
          ? actual - upper
          : lower !== null && actual < lower
            ? actual - lower
            : 0,
      band_share_pct: null,
      verdict: null,
      flagged,
      self_z: finite(enrich?.score) ?? finite(p.score) ?? undefined,
      anom: flagged ? normDirection(p.direction) : null,
    });
  }
  return out;
}

/**
 * Does this cut's displayed segments partition its parent? Prefers the
 * backend's `display_contract` flags; falls back to per-side
 * `isNonReconciling` for pair cuts, then to the cut name itself.
 */
export function cutReconciles(
  data: ProsightNewsData | null | undefined,
  bu: string | null | undefined,
  cutKey: string,
  knownDims: string[],
): boolean {
  const dc = bu ? data?.display_contract?.by_bu?.[bu]?.[cutKey] : undefined;
  if (dc && (dc.reconciled != null || dc.non_partitioning != null)) {
    return dc.reconciled !== false && dc.non_partitioning !== true;
  }
  const dims = pairDims(cutKey, knownDims);
  if (dims) return !dims.some((d) => isNonReconciling(d, data));
  return !isNonReconciling(cutKey, data);
}
