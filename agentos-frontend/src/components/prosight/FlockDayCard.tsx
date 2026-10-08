"use client";

import { useMemo, useState } from "react";
import type {
  ProsightDayData,
  ProsightNewsData,
  ProsightTimeseriesPoint,
  ProsightFeatureLevelData,
  L0Verdict,
  Softness,
  TopImpactedRow,
  DirectionType,
  OpenDetailPayload,
  TableColumn,
  SortDirection,
  BaselineMode,
} from "./types";
import { fmtInt, fmtSigned, fmtPct, weekdayOf } from "./utils/format";
import { wowPct } from "./utils/wow";
import {
  rowBaseline,
  seriesBaseline,
  BASELINE_LABELS,
  BASELINE_COMPARE_LABELS,
} from "./adapters";
import { computeSoftness, l0Verdict } from "./verdict";
import { DirectionPill, MiniStat, SurfaceSection } from "./surfaces";
import {
  SortableHeader,
  sortRows,
  useSortableTable,
} from "./sortableTable";
import UnifiedDimensionBreakdown from "./UnifiedDimensionBreakdown";
import CounterfactualImpact from "./CounterfactualImpact";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const L0_FULL_ID = "L0_total::total::TOTAL";


function seriesToFullId(series: string): string | null {
  if (!series) return null;
  const parts = series.split(" . ").filter(Boolean);
  if (parts.length <= 1) return `L1_single::${series}`;
  if (parts.length === 2) return `L2_pair::${parts.join("::")}`;
  return `L${parts.length}_full::${parts.join("::")}`;
}




// ─────────────────────────────────────────────────────────────────────────────
// Top Impacted Columns
// ─────────────────────────────────────────────────────────────────────────────

const TOP_IMPACTED_COLUMNS: TableColumn<TopImpactedRow>[] = [
  { key: "rank", label: "#", align: "left", sortByAbs: false },
  { key: "series", label: "series", align: "left" },
  { key: "actual", label: "actual", align: "right" },
  { key: "forecast", label: "forecast", align: "right" },
  { key: "point_diff", label: "Diff vs forecast", align: "right" },
  { key: "wow_pct", label: "wow%", align: "right" },
  { key: "streak", label: "streak", align: "right", sortByAbs: false },
  {
    key: "anom",
    label: "anom",
    align: "center",
    getValue: (r) => (r.anom ? 1 : 0),
  },
];

const TOP_IMPACTED_COMPACT_LIMIT = 5;

// ─────────────────────────────────────────────────────────────────────────────
// L0 Sparkline (14-day actual vs forecast band)
// ─────────────────────────────────────────────────────────────────────────────

function L0Sparkline({
  series,
  asOfDate,
  direction,
  days = 14,
  baselineMode = "forecast",
}: {
  series: ProsightTimeseriesPoint[] | undefined;
  asOfDate: string;
  direction: DirectionType;
  days?: number;
  baselineMode?: BaselineMode;
}) {
  if (!series?.length || !asOfDate) return null;
  const idx = series.findIndex((p) => p.date === asOfDate);
  if (idx < 0) return null;
  const start = Math.max(0, idx - (days - 1));
  const slice = series.slice(start, idx + 1);
  if (slice.length < 2) return null;

  // In a static mode, replace the forecast band with a dashed reference line =
  // each day's comparison basis (its own actual N days earlier). §4.5.
  const staticMode = baselineMode !== "forecast";
  const offset = baselineMode === "vs_prev_week" ? 7 : 1;
  const actualByDate = new Map(series.map((p) => [p.date, p.actual]));
  const shiftDays = (iso: string, d: number) => {
    const [y, m, dd] = iso.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, dd));
    dt.setUTCDate(dt.getUTCDate() + d);
    return dt.toISOString().slice(0, 10);
  };
  const refVals = staticMode
    ? slice.map((p) => actualByDate.get(shiftDays(p.date, -offset)))
    : [];

  // Scale to what is actually drawn: in static modes the band is hidden, and
  // labs-style wide bands (lower ≪ actual) would otherwise flatten the actual
  // and reference lines into a single flat stroke.
  const allVals = slice
    .flatMap((p) =>
      staticMode ? [p.actual] : [p.actual, p.lower, p.upper, p.predicted],
    )
    .concat(refVals.filter((v): v is number => Number.isFinite(v)))
    .filter((v): v is number => Number.isFinite(v));
  if (!allVals.length) return null;
  const yMin = Math.min(...allVals);
  const yMax = Math.max(...allVals);
  const range = yMax - yMin || 1;
  const pad = range * 0.1;
  const lo = yMin - pad;
  const hi = yMax + pad;
  const yRange = hi - lo;

  const W = 600;
  const H = 70;
  const stepX = W / Math.max(1, slice.length - 1);
  const toX = (i: number) => i * stepX;
  const toY = (v: number) => H - ((v - lo) / yRange) * H;

  const upperPts = slice
    .map((p, i) => `${toX(i).toFixed(1)},${toY(p.upper).toFixed(1)}`)
    .join(" ");
  const lowerPtsRev = slice
    .map((p, i) => ({ i, p }))
    .reverse()
    .map(({ i, p }) => `${toX(i).toFixed(1)},${toY(p.lower).toFixed(1)}`)
    .join(" ");
  const bandPath = `M${upperPts} L${lowerPtsRev} Z`;

  const actualPts = slice
    .map((p, i) => `${toX(i).toFixed(1)},${toY(p.actual).toFixed(1)}`)
    .join(" ");

  // Dashed reference line (static modes) — split into segments so days with no
  // comparison basis (e.g. the first week under vs-last-week) leave a gap.
  const refSegments: string[] = [];
  if (staticMode) {
    let cur: string[] = [];
    slice.forEach((p, i) => {
      const v = refVals[i];
      if (v != null && Number.isFinite(v)) {
        cur.push(`${toX(i).toFixed(1)},${toY(v).toFixed(1)}`);
      } else if (cur.length) {
        refSegments.push(cur.join(" "));
        cur = [];
      }
    });
    if (cur.length) refSegments.push(cur.join(" "));
  }

  const lastIdx = slice.length - 1;
  const lastP = slice[lastIdx];
  const dotColor =
    direction === "drop"
      ? "#dc2626"
      : direction === "spike"
        ? "#d97706"
        : "#16a34a";

  return (
    <div className="pt-2 border-t border-ink-100">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[9px] uppercase tracking-wide text-ink-500 font-medium">
          {slice.length}-day trend ·{" "}
          {staticMode
            ? baselineMode === "vs_prev_week"
              ? "actual vs last week"
              : "actual vs yesterday"
            : "actual vs forecast band"}
        </span>
        <span className="text-[9px] text-ink-400 font-mono">today ●</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full h-16"
        role="img"
        aria-label={`${slice.length}-day trend sparkline`}
      >
        {!staticMode && <path d={bandPath} fill="#dbeafe" opacity="0.7" />}
        {staticMode &&
          refSegments.map((pts, i) => (
            <polyline
              key={i}
              points={pts}
              fill="none"
              stroke="#94a3b8"
              strokeWidth="1.25"
              strokeDasharray="4 3"
            />
          ))}
        <polyline
          points={actualPts}
          fill="none"
          stroke="#334155"
          strokeWidth="1.5"
        />
        <circle
          cx={toX(lastIdx)}
          cy={toY(lastP.actual)}
          r="4"
          fill={dotColor}
          stroke="white"
          strokeWidth="1.5"
        />
      </svg>
      <div className="flex justify-between text-[9px] text-ink-400 font-mono mt-0.5">
        <span>{slice[0].date.slice(5)}</span>
        <span>{slice[Math.floor(slice.length / 2)].date.slice(5)}</span>
        <span className="text-ink-700 font-semibold">{lastP.date.slice(5)}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// FlockDayCard Component
// ─────────────────────────────────────────────────────────────────────────────

interface FlockDayCardProps {
  date: string;
  dayData: ProsightDayData | undefined;
  l0Series: ProsightTimeseriesPoint[];
  seriesTimeseries: Record<string, ProsightTimeseriesPoint[]>;
  featureImportance: Record<string, ProsightFeatureLevelData>;
  hideNoAnomaly?: boolean;
  collapseNoAnomaly?: boolean;
  /** Part B (spec 38 §4): which comparison the Summary stats read. Default forecast. */
  baselineMode?: BaselineMode;
  /** Spec 40: full payload + BU, for the pair drill-down's display_contract flags. */
  data?: ProsightNewsData | null;
  bu?: string;
  onOpenDetail?: (payload: OpenDetailPayload) => void;
}

export default function FlockDayCard({
  date,
  dayData,
  l0Series,
  seriesTimeseries,
  featureImportance,
  hideNoAnomaly = false,
  collapseNoAnomaly = false,
  baselineMode = "forecast",
  data,
  bu,
  onOpenDetail,
}: FlockDayCardProps) {
  const anomalyOnly = false;
  const [topImpactedExpanded, setTopImpactedExpanded] = useState(false);
  const [topImpactedType, setTopImpactedType] = useState<
    "all" | "spike" | "drop" | "inband"
  >("all");
  const topImpactedSort = useSortableTable("point_diff", "desc");

  const tsPoint = useMemo(
    () => l0Series?.find((p) => p.date === date) || null,
    [l0Series, date]
  );

  const summary = dayData?.summary || null;
  const todayRows = dayData?.today_rows || [];
  const l0Row = useMemo(
    () => todayRows.find((r) => r.level === "L0_total") || null,
    [todayRows]
  );
  const rca = l0Row?.rca || null;

  const derivedL0 = useMemo((): L0Verdict | null => {
    if (summary?.l0_anchor) {
      const a = summary.l0_anchor;
      const lower = a.expected_range?.[0];
      const upper = a.expected_range?.[1];
      const dir: DirectionType = a.direction === "in_band" ? null : a.direction;
      return {
        is_anomaly: Boolean(dir),
        direction: dir,
        actual: a.actual,
        forecast: a.predicted,
        forecast_lower: lower,
        forecast_upper: upper,
        point_miss: a.point_residual,
        band_miss: a.band_violation,
        trigger:
          dir === "drop"
            ? "actual fell below the lower forecast band"
            : dir === "spike"
              ? "actual rose above the upper forecast band"
              : "actual stayed inside the forecast band",
      };
    }
    return l0Verdict(tsPoint);
  }, [summary, tsPoint]);

  const softness = computeSoftness(l0Series, date);

  // Part B: the Summary stats can compare against a static baseline instead of
  // the forecast. Falls back to forecast when the row carries no baselines
  // (older JSON) — the toggle is hidden in that case, so this stays inert. §4.4.
  const baseline = rowBaseline(l0Row, baselineMode);
  const useStaticBaseline = baselineMode !== "forecast" && baseline != null;

  // Build top impacted series. In a static mode the "forecast" column becomes the
  // comparison basis (prev-day / prev-week actual) and the diff becomes the delta
  // vs that basis: L0 reads its explicit baselines block; each segment computes
  // its baseline from its own series_timeseries (rca children carry none). §4.5.
  const topImpacted = useMemo((): TopImpactedRow[] => {
    const staticMode = baselineMode !== "forecast";
    const rows: TopImpactedRow[] = [];
    if (derivedL0?.actual != null) {
      const wow = wowPct(l0Series, date);
      const b = staticMode ? rowBaseline(l0Row, baselineMode) : null;
      rows.push({
        rank: 1,
        series: "total::TOTAL",
        full_id: L0_FULL_ID,
        actual: derivedL0.actual,
        forecast: b ? b.anchor_value : derivedL0.forecast,
        lower: derivedL0.forecast_lower,
        upper: derivedL0.forecast_upper,
        point_diff: b ? b.delta : derivedL0.point_miss,
        band_diff: derivedL0.band_miss,
        wow_pct: wow,
        streak: l0Row?.streak ?? 0,
        anom: b ? b.direction : derivedL0.direction,
      });
    }
    const byDim = rca?.by_dimension || {};
    const streakLookup = new Map(todayRows.map((r) => [r.group_id, r]));
    for (const [, block] of Object.entries(byDim)) {
      for (const c of block.children || []) {
        const seriesKey = c.group_id;
        const fullId = `L1_single::${seriesKey}`;
        const tr = streakLookup.get(seriesKey);
        const wow = wowPct(seriesTimeseries?.[fullId], date);
        const fcPointDiff = (c.actual ?? 0) - (c.predicted ?? 0);
        const b = staticMode
          ? seriesBaseline(seriesTimeseries?.[fullId], date, baselineMode)
          : null;
        rows.push({
          rank: 0,
          series: seriesKey,
          full_id: fullId,
          actual: c.actual,
          forecast: b ? b.anchor_value : c.predicted,
          lower: c.lower,
          upper: c.upper,
          point_diff: b ? b.delta : fcPointDiff,
          band_diff: c.band_violation ?? 0,
          wow_pct: wow,
          streak: tr?.streak ?? 0,
          anom: b
            ? b.direction
            : c.flagged
              ? fcPointDiff < 0
                ? "drop"
                : "spike"
              : null,
        });
      }
    }
    rows.sort((a, b) => Math.abs(b.point_diff ?? 0) - Math.abs(a.point_diff ?? 0));
    rows.forEach((r, i) => {
      r.rank = i + 1;
    });
    return rows;
  }, [derivedL0, rca, todayRows, l0Series, seriesTimeseries, date, l0Row, baselineMode]);

  // Column labels reflect the active comparison (forecast → yesterday / last week).
  const topImpactedColumns = useMemo(() => {
    if (baselineMode === "forecast") return TOP_IMPACTED_COLUMNS;
    const compareLabel = BASELINE_COMPARE_LABELS[baselineMode];
    return TOP_IMPACTED_COLUMNS.map((c) =>
      c.key === "forecast"
        ? { ...c, label: compareLabel }
        : c.key === "point_diff"
          ? { ...c, label: `Diff vs ${compareLabel}` }
          : c,
    );
  }, [baselineMode]);

  const wd = weekdayOf(date);
  const isAnomaly = Boolean(derivedL0?.is_anomaly);

  if (!isAnomaly && hideNoAnomaly) return null;

  if (!isAnomaly && collapseNoAnomaly) {
    return (
      <article className="rounded-md border border-ink-200 bg-white px-3 py-2 flex items-center gap-3">
        <span className="text-[11px] font-mono text-ink-700">{date}</span>
        {wd && <span className="text-[10px] text-ink-400">({wd})</span>}
        <span className="px-1.5 py-0.5 rounded border text-[10px] font-semibold text-emerald-700 bg-emerald-50 border-emerald-200">
          no anomaly
        </span>
        {derivedL0 ? (
          <span className="font-mono text-[10px] text-ink-500">
            actual {fmtInt(derivedL0.actual)} . fcst {fmtInt(derivedL0.forecast)} .
            pt {fmtSigned(derivedL0.point_miss)}
          </span>
        ) : (
          <span className="text-[10px] text-ink-400 italic">
            no L0 timeseries
          </span>
        )}
      </article>
    );
  }

  return (
    <article className="rounded-md border border-ink-200 bg-white shadow-sm">
      {/* BODY */}
      <div className="px-4 py-4 space-y-3">
        {/* PAIR: Summary stats (+ 14d sparkline) | Top impacted series */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          {derivedL0 && (() => {
            // Anchor / compared-value / delta are baseline-mode aware; forecast
            // mode preserves the original wording and values exactly. §4.4/§4.5.
            // "Orders today" is always today's actual; the mode only changes the
            // comparison basis, delta, %, and label. The backend baseline's
            // anchor_value IS that basis (not today's value). §4.4/§4.5.
            const ordersValue = derivedL0.actual;
            const comparedValue = useStaticBaseline
              ? baseline!.anchor_value
              : derivedL0.forecast;
            const compareLabel = useStaticBaseline
              ? BASELINE_COMPARE_LABELS[baselineMode]
              : "forecast";
            const vsDelta = useStaticBaseline
              ? baseline!.delta
              : derivedL0.point_miss;
            const vsPct = useStaticBaseline
              ? baseline!.pct
              : comparedValue
                ? (derivedL0.point_miss / comparedValue) * 100
                : null;
            const vsLabel = useStaticBaseline
              ? BASELINE_LABELS[baselineMode]
              : "Vs forecast";
            const vsTone: "" | "drop" | "spike" =
              vsDelta < 0 ? "drop" : vsDelta > 0 ? "spike" : "";
            const hasBand =
              baseline?.lower != null && baseline?.upper != null;
            return (
            <SurfaceSection
              title={
                useStaticBaseline
                  ? `Summary — vs ${compareLabel}`
                  : "Summary - anomaly & impact"
              }
            >
              <div className="space-y-3">
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-3">
                  <MiniStat
                    label="Orders today"
                    value={fmtInt(ordersValue)}
                    hint={`${compareLabel} ${fmtInt(comparedValue)}`}
                  />
                  <MiniStat
                    label={vsLabel}
                    value={`${fmtSigned(vsDelta)}${vsPct != null ? ` (${fmtPct(vsPct)})` : ""}`}
                    tone={vsTone}
                    hint={
                      useStaticBaseline && !hasBand
                        ? "no band for this mode"
                        : undefined
                    }
                  />
                  {softness && (
                    <MiniStat
                      label={`Recent drift (${softness.window_days}d)`}
                      value={`${fmtSigned(softness.avg_per_day)} / day`}
                      hint={`${fmtSigned(softness.cumulative)} cumulative`}
                      tone={
                        softness.cumulative < 0
                          ? "drop"
                          : softness.cumulative > 0
                            ? "spike"
                            : ""
                      }
                    />
                  )}
                </div>
                <L0Sparkline
                  series={l0Series}
                  asOfDate={date}
                  direction={
                    useStaticBaseline ? baseline!.direction : derivedL0.direction
                  }
                  baselineMode={baselineMode}
                />
              </div>
            </SurfaceSection>
            );
          })()}

          {/* TOP IMPACTED SERIES (paired with Summary) */}
          {(() => {
          const typeFiltered =
            topImpactedType === "all"
              ? topImpacted
              : topImpactedType === "inband"
                ? topImpacted.filter((r) => !r.anom)
                : topImpacted.filter((r) => r.anom === topImpactedType);
          const filtered = anomalyOnly
            ? typeFiltered.filter((r) => r.anom)
            : typeFiltered;
          const sorted = sortRows(
            filtered,
            topImpactedColumns,
            topImpactedSort.sortKey,
            topImpactedSort.sortDir
          );
          const showAll =
            topImpactedExpanded || sorted.length <= TOP_IMPACTED_COMPACT_LIMIT;
          const visible = showAll
            ? sorted
            : sorted.slice(0, TOP_IMPACTED_COMPACT_LIMIT);
          const hidden = sorted.length - visible.length;
          const canExpand = sorted.length > TOP_IMPACTED_COMPACT_LIMIT;

          const titleNode = (
            <span className="inline-flex items-baseline gap-2">
              <span>Top impacted series</span>
              <span className="font-normal normal-case tracking-normal text-ink-400">
                ({sorted.length} series
                {anomalyOnly && topImpacted.length !== sorted.length
                  ? ` of ${topImpacted.length}`
                  : ""}
                {" "}· sorted by diff size)
              </span>
            </span>
          );
          const action = (
            <>
              <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
                {(
                  [
                    ["All", "all"],
                    ["Spike", "spike"],
                    ["Drop", "drop"],
                    ["In band", "inband"],
                  ] as const
                ).map(([label, val]) => {
                  const active = topImpactedType === val;
                  return (
                    <button
                      key={val}
                      type="button"
                      onClick={() => setTopImpactedType(val)}
                      className={`px-2 py-0.5 rounded text-[10px] transition-colors ${
                        active
                          ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                          : "text-ink-600 hover:bg-white"
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
              {canExpand && (
                <button
                  type="button"
                  onClick={() => setTopImpactedExpanded((v) => !v)}
                  className={`text-[10px] px-2 py-0.5 rounded border ${topImpactedExpanded ? "border-blue-600 bg-blue-50 text-blue-800" : "border-ink-200 bg-white text-ink-700 hover:bg-ink-50"}`}
                  title={
                    topImpactedExpanded
                      ? "Collapse to top contributors"
                      : `Show all ${sorted.length} rows`
                  }
                >
                  {topImpactedExpanded ? "Compact" : `Expand all (+${hidden})`}
                </button>
              )}
            </>
          );

          return (
            <SurfaceSection title={titleNode} action={action} collapsible>
              {topImpacted.length ? (
                <table className="w-full text-[11px]">
                  <thead>
                    <SortableHeader
                      columns={topImpactedColumns}
                      sortKey={topImpactedSort.sortKey}
                      sortDir={topImpactedSort.sortDir}
                      onSort={topImpactedSort.onSort}
                    />
                  </thead>
                  <tbody>
                    {visible.map((r) => {
                      const clickable = !!onOpenDetail;
                      const handleOpen = () => {
                        if (!clickable) return;
                        onOpenDetail({
                          full_id: r.full_id || seriesToFullId(r.series) || "",
                          label: r.series,
                          clickedDate: date,
                        });
                      };
                      return (
                        <tr
                          key={`${r.rank}-${r.series}`}
                          className={`border-b border-ink-50 ${clickable ? "cursor-pointer hover:bg-blue-50/60" : "hover:bg-blue-50/40"}`}
                          onClick={handleOpen}
                          title={
                            clickable ? `Open ${r.series} in graph tab` : undefined
                          }
                        >
                          <td className="py-1 px-1 font-mono text-ink-500">
                            {r.rank}
                          </td>
                          <td className="py-1 px-1">
                            {clickable ? (
                              // A real button, not a styled <span>: the row's
                              // onClick is mouse-only, so without this the
                              // link-looking label cannot be reached or
                              // activated by keyboard at all. stopPropagation
                              // keeps the row handler from firing twice.
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleOpen();
                                }}
                                className="rounded-sm text-left text-blue-700 hover:underline focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue-600"
                              >
                                {r.series}
                              </button>
                            ) : (
                              r.series
                            )}
                          </td>
                          <td className="py-1 px-1 text-right font-mono">
                            {fmtInt(r.actual)}
                          </td>
                          <td className="py-1 px-1 text-right font-mono">
                            {fmtInt(r.forecast)}
                          </td>
                          <td
                            className={`py-1 px-1 text-right font-mono ${r.point_diff < 0 ? "text-red-700" : r.point_diff > 0 ? "text-amber-700" : ""}`}
                          >
                            {fmtSigned(r.point_diff)}
                            {r.forecast ? (
                              <span className="ml-1 text-[10px] text-ink-400">
                                ({fmtPct((r.point_diff / r.forecast) * 100)})
                              </span>
                            ) : null}
                          </td>
                          <td
                            className={`py-1 px-1 text-right font-mono ${r.wow_pct == null ? "text-ink-400" : r.wow_pct < 0 ? "text-red-700" : r.wow_pct > 0 ? "text-emerald-700" : ""}`}
                          >
                            {r.wow_pct == null ? "—" : fmtPct(r.wow_pct)}
                          </td>
                          <td className="py-1 px-1 text-right font-mono text-ink-600">
                            {r.streak ?? 0}
                          </td>
                          <td className="py-1 px-1 text-center text-[10px]">
                            {r.anom ? (
                              <DirectionPill
                                direction={
                                  typeof r.anom === "string"
                                    ? r.anom
                                    : r.point_diff < 0
                                      ? "drop"
                                      : "spike"
                                }
                              />
                            ) : (
                              <DirectionPill direction={null} />
                            )}
                          </td>
                        </tr>
                      );
                    })}
                    {sorted.length === 0 && (
                      <tr>
                        <td
                          colSpan={8}
                          className="py-2 px-1 text-[11px] text-ink-400 italic"
                        >
                          No rows match the anomaly filter.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              ) : (
                <div className="text-[11px] text-ink-400 italic">
                  No L0 anomaly today - top-impacted series are sourced from{" "}
                  <span className="font-mono">summary.l0_anchor</span> +{" "}
                  <span className="font-mono">today_rows[].rca.by_dimension</span>.
                </div>
              )}
            </SurfaceSection>
          );
        })()}
        </div>

        {/* PER-DIMENSION BREAKDOWN */}
        <UnifiedDimensionBreakdown
          rca={rca}
          headlineDim={summary?.headline_dim}
          anomalyOnly={anomalyOnly}
          baselineMode={baselineMode}
          seriesTimeseries={seriesTimeseries}
          date={date}
          data={data}
          bu={bu}
          todayRows={todayRows}
        />

        {/* FEATURE IMPACT */}
        <CounterfactualImpact
          driver={l0Row?.driver}
          ranked={(() => {
            if (!featureImportance || !l0Row) return null;
            const lvlKey = Object.keys(featureImportance).find((k) =>
              k.startsWith("L0_total")
            );
            if (!lvlKey) return null;
            const key = `${l0Row.group_id}::${date}`;
            return featureImportance[lvlKey]?.daily?.[key]?.ranked || null;
          })()}
        />
      </div>
    </article>
  );
}
