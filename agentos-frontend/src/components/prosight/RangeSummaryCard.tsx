"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  ProsightNewsData,
  ProsightTimeseriesPoint,
  AggregatedL0Range,
  ContributorRow,
  DimensionData,
  DimensionRow,
  DirectionType,
  OpenDetailPayload,
  TableColumn,
} from "./types";
import { fmtInt, fmtSigned, fmtPct, weekdayOf } from "./utils/format";
import { buDay, buSeries } from "./buSlice";
import { isNonReconciling, childShareDiff, NON_RECONCILING_CAVEAT } from "./adapters";
import { DirectionPill, SurfaceSection } from "./surfaces";
import {
  SortableHeader,
  sortRows,
  sumField,
  useSortableTable,
} from "./sortableTable";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const L0_FULL_ID = "L0_total::total::TOTAL";

const CONTRIB_COLUMNS: TableColumn<ContributorRow>[] = [
  { key: "series", label: "series", align: "left" },
  { key: "appearances", label: "appearances", align: "right" },
  { key: "anomDays", label: "anom days", align: "right" },
  {
    key: "drops",
    label: "drops / spikes",
    align: "right",
    getValue: (r) => r.drops + r.spikes,
  },
  {
    key: "sumPointDiff",
    label: "Cumulative diff vs forecast",
    align: "right",
    title: "Sum of (actual - forecast) across the range.",
  },
];

const CONTRIB_COMPACT_LIMIT = 10;

// ─────────────────────────────────────────────────────────────────────────────
// Helper Functions
// ─────────────────────────────────────────────────────────────────────────────

function bestCaseImpact(
  actual: number,
  lower: number,
  upper: number
): number {
  if (
    !Number.isFinite(actual) ||
    !Number.isFinite(lower) ||
    !Number.isFinite(upper)
  )
    return 0;
  if (actual > upper) return actual - upper;
  if (actual < lower) return actual - lower;
  return 0;
}

function aggregateL0Range(
  l0Series: ProsightTimeseriesPoint[] | undefined,
  start: string,
  end: string
): AggregatedL0Range | null {
  const inRange = (l0Series || []).filter(
    (p) => p.date >= start && p.date <= end
  );
  if (!inRange.length) return null;

  let total_actual = 0;
  let total_predicted = 0;
  let total_lower = 0;
  let total_upper = 0;
  let cumulative_point_miss = 0;
  let cumulative_band_miss = 0;
  let drop_days = 0;
  let spike_days = 0;
  let worst: AggregatedL0Range["worst"] = null;

  inRange.forEach((p) => {
    const a = Number(p.actual);
    const f = Number(p.predicted);
    const lo = Number(p.lower);
    const hi = Number(p.upper);
    if (!Number.isFinite(a)) return;

    total_actual += a;
    if (Number.isFinite(f)) total_predicted += f;
    if (Number.isFinite(lo)) total_lower += lo;
    if (Number.isFinite(hi)) total_upper += hi;

    const pointMiss = Number.isFinite(f) ? a - f : 0;
    cumulative_point_miss += pointMiss;

    const bandMiss = bestCaseImpact(a, lo, hi);
    cumulative_band_miss += bandMiss;
    if (bandMiss < 0) drop_days += 1;
    else if (bandMiss > 0) spike_days += 1;

    if (bandMiss !== 0) {
      if (!worst || Math.abs(bandMiss) > Math.abs(worst.bandMiss)) {
        worst = {
          date: p.date,
          bandMiss,
          pointMiss,
          actual: a,
          lower: lo,
          upper: hi,
          predicted: f,
        };
      }
    }
  });

  const N = inRange.length;
  return {
    N,
    total_actual,
    total_predicted,
    total_lower,
    total_upper,
    cumulative_point_miss,
    cumulative_band_miss,
    drop_days,
    spike_days,
    normal_days: N - drop_days - spike_days,
    avg_actual_per_day: total_actual / N,
    avg_predicted_per_day: total_predicted / N,
    avg_point_miss_per_day: cumulative_point_miss / N,
    avg_band_miss_per_day: cumulative_band_miss / N,
    worst,
    direction:
      cumulative_band_miss < 0
        ? "drop"
        : cumulative_band_miss > 0
          ? "spike"
          : null,
    has_anomaly: drop_days + spike_days > 0,
  };
}

function aggregateDimensions(
  data: ProsightNewsData | null,
  start: string,
  end: string,
  parentPointMiss: number | undefined,
  parentBandMiss: number | undefined,
  bu: string
): DimensionData[] {
  if (!data?.dates?.length) return [];
  const dimMap = new Map<string, Map<string, DimensionRow>>();

  data.dates
    .filter((d) => d >= start && d <= end)
    .forEach((date) => {
      const todayRows = buDay(data.data_by_date?.[date], bu)?.today_rows || [];
      const l0Row = todayRows.find((r) => r.level === "L0_total");
      const byDim = l0Row?.rca?.by_dimension || {};

      Object.entries(byDim).forEach(([dimName, block]) => {
        if (!dimMap.has(dimName)) dimMap.set(dimName, new Map());
        const segMap = dimMap.get(dimName)!;
        (block.children || []).forEach((c) => {
          const key = c.label;
          if (!segMap.has(key)) {
            segMap.set(key, {
              label: key,
              actual: 0,
              forecast: 0,
              lower: 0,
              upper: 0,
              point_diff: 0,
              band_diff: 0,
              point_share_pct: null,
              band_share_pct: null,
              drop_days: 0,
              spike_days: 0,
              anom_days: 0,
            });
          }
          const item = segMap.get(key)!;
          const point_diff = (c.actual ?? 0) - (c.predicted ?? 0);
          const band_diff = c.band_violation ?? 0;
          if (Number.isFinite(c.actual)) item.actual += c.actual;
          if (Number.isFinite(c.predicted)) item.forecast += c.predicted;
          if (Number.isFinite(c.lower)) item.lower += c.lower;
          if (Number.isFinite(c.upper)) item.upper += c.upper;
          item.point_diff += point_diff;
          item.band_diff += band_diff;
          if (c.flagged) {
            if (point_diff < 0) item.drop_days = (item.drop_days || 0) + 1;
            else if (point_diff > 0)
              item.spike_days = (item.spike_days || 0) + 1;
          }
        });
      });
    });

  const out: DimensionData[] = [];
  dimMap.forEach((segMap, name) => {
    const rows = [...segMap.values()].map((item) => {
      const totAnomDays = (item.drop_days || 0) + (item.spike_days || 0);
      let anom: DirectionType = null;
      if (totAnomDays > 0) {
        if ((item.drop_days || 0) > (item.spike_days || 0)) anom = "drop";
        else if ((item.spike_days || 0) > (item.drop_days || 0)) anom = "spike";
        else anom = item.band_diff < 0 ? "drop" : item.band_diff > 0 ? "spike" : null;
      }
      return {
        ...item,
        point_share_pct: parentPointMiss
          ? (item.point_diff / parentPointMiss) * 100
          : null,
        band_share_pct: parentBandMiss
          ? (item.band_diff / parentBandMiss) * 100
          : null,
        anom_days: totAnomDays,
        anom,
      };
    });
    rows.sort(
      (a, b) => Math.abs(b.point_diff || 0) - Math.abs(a.point_diff || 0)
    );
    out.push({ name, rows });
  });
  return out;
}

function aggregateContributors(
  data: ProsightNewsData | null,
  start: string,
  end: string,
  limit = 10,
  bu = ""
): ContributorRow[] {
  if (!data?.dates?.length) return [];
  const bySeries = new Map<string, ContributorRow>();

  data.dates
    .filter((d) => d >= start && d <= end)
    .forEach((date) => {
      const todayRows = buDay(data.data_by_date?.[date], bu)?.today_rows || [];
      const l0Row = todayRows.find((r) => r.level === "L0_total");
      const byDim = l0Row?.rca?.by_dimension || {};

      Object.values(byDim).forEach((block) => {
        (block.children || []).forEach((c) => {
          const seriesKey = c.group_id || c.label;
          if (!seriesKey) return;
          const point_diff = (c.actual ?? 0) - (c.predicted ?? 0);
          const band_diff = c.band_violation ?? 0;
          const flagged = Boolean(c.flagged);

          if (!bySeries.has(seriesKey)) {
            bySeries.set(seriesKey, {
              series: seriesKey,
              appearances: 0,
              anomDays: 0,
              drops: 0,
              spikes: 0,
              sumPointDiff: 0,
              sumBandDiff: 0,
              sumActual: 0,
              sumForecast: 0,
              bestRank: Number.POSITIVE_INFINITY,
              dominantDirection: null,
            });
          }
          const item = bySeries.get(seriesKey)!;
          item.appearances += 1;
          if (flagged) {
            item.anomDays += 1;
            if (point_diff < 0) item.drops += 1;
            else if (point_diff > 0) item.spikes += 1;
          }
          if (Number.isFinite(point_diff)) item.sumPointDiff += point_diff;
          if (Number.isFinite(band_diff)) item.sumBandDiff += band_diff;
          if (Number.isFinite(c.actual)) item.sumActual += c.actual;
          if (Number.isFinite(c.predicted)) item.sumForecast += c.predicted;
        });
      });
    });

  return [...bySeries.values()]
    .map((item) => ({
      ...item,
      bestRank: Number.isFinite(item.bestRank) ? item.bestRank : 0,
      dominantDirection:
        item.drops > item.spikes
          ? ("drop" as DirectionType)
          : item.spikes > item.drops
            ? ("spike" as DirectionType)
            : ("mixed" as DirectionType),
    }))
    .sort(
      (a, b) =>
        Math.abs(b.sumPointDiff) - Math.abs(a.sumPointDiff) ||
        Math.abs(b.sumBandDiff) - Math.abs(a.sumBandDiff)
    )
    .slice(0, limit);
}

interface RangeDayPoint {
  date: string;
  weekday: string;
  actual: number;
  predicted: number;
  lower: number;
  upper: number;
  pointMiss: number;
  bandMiss: number;
  direction: DirectionType;
}

function buildRangeDays(
  l0Series: ProsightTimeseriesPoint[] | undefined,
  start: string,
  end: string
): RangeDayPoint[] {
  return (l0Series || [])
    .filter((p) => p.date >= start && p.date <= end)
    .map((p) => {
      const a = Number(p.actual);
      const f = Number(p.predicted);
      const lo = Number(p.lower);
      const hi = Number(p.upper);
      const pointMiss = Number.isFinite(f) && Number.isFinite(a) ? a - f : 0;
      const bandMiss = bestCaseImpact(a, lo, hi);
      const direction: DirectionType =
        bandMiss < 0 ? "drop" : bandMiss > 0 ? "spike" : null;
      return {
        date: p.date,
        weekday: weekdayOf(p.date),
        actual: a,
        predicted: f,
        lower: lo,
        upper: hi,
        pointMiss,
        bandMiss,
        direction,
      };
    });
}

function pickBestDay(days: RangeDayPoint[]): RangeDayPoint | null {
  const inBand = days.filter((d) => d.direction === null);
  const pool = inBand.length ? inBand : days;
  if (!pool.length) return null;
  return pool.reduce((best, cur) =>
    Math.abs(cur.pointMiss) < Math.abs(best.pointMiss) ? cur : best
  );
}

function DayStripCell({
  day,
  maxAbsPointMiss,
  onDrill,
}: {
  day: RangeDayPoint;
  maxAbsPointMiss: number;
  onDrill?: (date: string) => void;
}) {
  const widthPct =
    maxAbsPointMiss > 0
      ? Math.min(50, (Math.abs(day.pointMiss) / maxAbsPointMiss) * 50)
      : 0;
  const isSpike = day.direction === "spike";
  const isDrop = day.direction === "drop";
  const positive = day.pointMiss >= 0;

  const containerClass = isSpike
    ? "border-amber-300 bg-amber-50/40 hover:bg-amber-100/40 hover:border-amber-600"
    : isDrop
      ? "border-red-300 bg-red-50/40 hover:bg-red-100/40 hover:border-red-600"
      : "border-ink-200 bg-white hover:bg-blue-50/40 hover:border-blue-600/40";

  const weekdayClass = isSpike
    ? "text-amber-700"
    : isDrop
      ? "text-red-700"
      : "text-ink-500";

  const barColor = isSpike
    ? "bg-amber-500"
    : isDrop
      ? "bg-red-500"
      : "bg-ink-300";

  const pillClass = isSpike
    ? "bg-amber-100 text-amber-800 border-amber-200"
    : isDrop
      ? "bg-red-100 text-red-800 border-red-200"
      : "";

  return (
    <button
      type="button"
      onClick={() => onDrill?.(day.date)}
      className={`text-left rounded-md border transition-colors p-2 flex flex-col gap-1.5 ${containerClass}`}
      title={`Drill into ${day.date}`}
    >
      <div className="flex items-baseline justify-between gap-1">
        <span
          className={`text-[10px] uppercase tracking-wide font-semibold ${weekdayClass}`}
        >
          {day.weekday}
        </span>
        <span className="text-[10px] text-ink-400 font-mono">
          {day.date.slice(5)}
        </span>
      </div>
      <div className="text-[12px] font-mono font-semibold text-ink-800">
        {fmtInt(day.actual)}
      </div>
      <div className="h-8 flex items-center">
        <div className="relative w-full h-1.5 bg-ink-100 rounded">
          <div className="absolute top-0 bottom-0 left-1/2 w-px bg-ink-300" />
          <div
            className={`absolute top-0 bottom-0 ${barColor} ${
              positive ? "left-1/2 rounded-r" : "right-1/2 rounded-l"
            }`}
            style={{ width: `${widthPct}%` }}
          />
        </div>
      </div>
      <div className="text-[10px] font-mono flex items-baseline gap-1">
        <span
          className={
            isSpike
              ? "text-amber-700 font-semibold"
              : isDrop
                ? "text-red-700 font-semibold"
                : "text-ink-500"
          }
        >
          {fmtSigned(day.pointMiss)}
        </span>
        {day.direction ? (
          <span
            className={`inline-flex items-center gap-0.5 rounded border font-semibold uppercase tracking-wide text-[8px] px-1 py-[1px] ${pillClass}`}
          >
            {isSpike ? "▲ SPIKE" : "▼ DROP"}
          </span>
        ) : (
          <span className="text-ink-400">in band</span>
        )}
      </div>
    </button>
  );
}

interface ExemplarCardProps {
  kind: "worst" | "best";
  date: string;
  weekday: string;
  actual: number;
  predicted: number;
  pointMiss: number;
  direction: DirectionType;
  onDrill?: (date: string) => void;
}

function ExemplarCard({
  kind,
  date,
  weekday,
  actual,
  predicted,
  pointMiss,
  direction,
  onDrill,
}: ExemplarCardProps) {
  const isWorst = kind === "worst";

  const headerBg = isWorst
    ? direction === "drop"
      ? "bg-red-50"
      : "bg-amber-50"
    : "bg-emerald-50/60";
  const headerText = isWorst
    ? direction === "drop"
      ? "text-red-800"
      : "text-amber-800"
    : "text-emerald-800";
  const borderColor = isWorst
    ? direction === "drop"
      ? "border-red-200"
      : "border-amber-200"
    : "border-emerald-200";
  const hoverBg = isWorst
    ? direction === "drop"
      ? "hover:bg-red-50/40"
      : "hover:bg-amber-50/40"
    : "hover:bg-emerald-50/40";

  const eyebrow = isWorst ? "Worst day in range" : "Best day in range";
  const eyebrowMethod = "by |diff vs forecast|";

  const pctVsForecast =
    predicted && Number.isFinite(predicted) && predicted !== 0
      ? (pointMiss / predicted) * 100
      : null;

  const pill = isWorst ? (
    <DirectionPill direction={direction} size="sm" />
  ) : (
    <span className="inline-flex items-center gap-1 rounded border font-semibold uppercase tracking-wide text-[9px] px-1.5 py-[1px] bg-ink-100 text-ink-700 border-ink-200">
      ● ON FORECAST
    </span>
  );

  const diffCell = (
    <span
      className={`font-mono ${
        pointMiss > 0
          ? "text-amber-700 font-semibold"
          : pointMiss < 0
            ? "text-red-700 font-semibold"
            : "text-ink-700"
      }`}
    >
      {fmtSigned(pointMiss)}
      {pctVsForecast != null ? (
        <span className="ml-1 text-[10px] text-ink-400">
          ({fmtPct(pctVsForecast)})
        </span>
      ) : null}
    </span>
  );

  return (
    <section
      className={`rounded-md border ${borderColor} bg-white overflow-hidden`}
    >
      <div
        className={`flex items-center gap-2 px-3 py-1.5 border-b border-ink-200 ${headerBg}`}
      >
        <h3
          className={`text-[11px] font-semibold uppercase tracking-[1.5px] flex-1 ${headerText}`}
        >
          {eyebrow}
        </h3>
        <span className="text-[9px] uppercase tracking-wide text-ink-400 font-medium">
          {eyebrowMethod}
        </span>
      </div>
      <button
        type="button"
        onClick={() => onDrill?.(date)}
        className={`w-full text-left px-4 py-3 transition-colors ${hoverBg}`}
        title={`Drill into ${date}`}
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="font-mono text-[14px] font-semibold text-ink-800">
            {date}
          </span>
          {weekday && (
            <span className="text-[11px] text-ink-500">({weekday})</span>
          )}
          {pill}
          <span className="ml-auto text-[11px] text-blue-600 font-medium">
            Drill in -
          </span>
        </div>
        <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
          <div>
            <div className="text-[9px] uppercase tracking-wide text-ink-500">
              Diff vs forecast
            </div>
            <div>{diffCell}</div>
          </div>
          <div>
            <div className="text-[9px] uppercase tracking-wide text-ink-500">
              Actual / forecast
            </div>
            <div className="font-mono text-ink-700">
              {fmtInt(actual)} / {fmtInt(predicted)}
            </div>
          </div>
        </div>
      </button>
    </section>
  );
}

function seriesToFullId(series: string): string | null {
  if (!series) return null;
  const parts = series.split(" . ").filter(Boolean);
  if (parts.length <= 1) return `L1_single::${series}`;
  if (parts.length === 2) return `L2_pair::${parts.join("::")}`;
  return `L${parts.length}_full::${parts.join("::")}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Dimension Tables Component
// ─────────────────────────────────────────────────────────────────────────────

const DIM_COLUMNS: TableColumn<DimensionRow>[] = [
  { key: "label", label: "label" },
  { key: "actual", label: "actual", align: "right" },
  { key: "forecast", label: "forecast", align: "right" },
  {
    key: "point_diff",
    label: "Diff vs forecast",
    align: "right",
    title: "Actual − forecast summed across the range for this segment.",
  },
  {
    key: "point_share_pct",
    label: "% of total miss",
    align: "right",
    title: "Share of the range's total miss attributable to this segment.",
  },
  { key: "anom", label: "anom", align: "center" },
];

const COMPACT_ROW_LIMIT = 5;

const DIM_LABEL: Record<string, string> = {
  state: "state",
  bu: "bu",
  channel: "channel",
  hour: "hour",
  sku_name: "SKU",
  name: "SKU",
  order_state: "order state",
  payment_method: "payment",
  coupon_flag: "coupon",
};

// `bu` is a partition/selector now, never an RCA child dim, so it is excluded;
// unknown dims are appended by `availableDims` so labs `hour`/`sku_name` show.
const DIM_ORDER = [
  "state",
  "channel",
  "hour",
  "sku_name",
  "name",
  "order_state",
  "payment_method",
  "coupon_flag",
];

function DimensionTables({
  dimensions,
  anomalyOnly,
  renderAnomCell,
}: {
  dimensions: DimensionData[];
  anomalyOnly: boolean;
  renderAnomCell?: (row: DimensionRow) => React.ReactNode;
}) {
  const availableDims = useMemo(() => {
    const present = new Set(dimensions.map((d) => d.name));
    const ordered = DIM_ORDER.filter((d) => present.has(d));
    const extras = dimensions
      .map((d) => d.name)
      .filter((n) => !DIM_ORDER.includes(n));
    return [...ordered, ...extras];
  }, [dimensions]);

  const [selectedDim, setSelectedDim] = useState<string>(
    availableDims[0] || ""
  );
  const [expanded, setExpanded] = useState(false);
  const { sortKey, sortDir, onSort } = useSortableTable("point_diff", "desc");

  useEffect(() => {
    if (!availableDims.includes(selectedDim) && availableDims[0]) {
      setSelectedDim(availableDims[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableDims.join(",")]);

  if (!availableDims.length) {
    return (
      <SurfaceSection title="Per-dimension breakdown" collapsible>
        <div className="text-[11px] text-ink-400 italic">
          No RCA available for this range - per-dimension breakdown requires{" "}
          <span className="font-mono">today_rows[].rca.by_dimension</span>.
        </div>
      </SurfaceSection>
    );
  }

  const dim =
    dimensions.find((d) => d.name === selectedDim) || dimensions[0];
  const allRaw = dim?.rows || [];
  const filtered = anomalyOnly ? allRaw.filter((r) => r.anom) : allRaw;
  const sortedAll = sortRows(filtered, DIM_COLUMNS, sortKey, sortDir);
  const total = sortedAll.length;
  const totalUnfiltered = allRaw.length;
  const rows = expanded ? sortedAll : sortedAll.slice(0, COMPACT_ROW_LIMIT);
  const hidden = total - rows.length;

  // Overlapping cut (e.g. labs `sku_name`): rows sum above the range total, so
  // suppress the "% of total miss" partition and flag it. spec §3.3.
  const nonReconciling = isNonReconciling(dim?.name);
  const diffOf = (r: DimensionRow) =>
    nonReconciling ? childShareDiff(r) ?? r.point_diff : r.point_diff;

  const tot = {
    actual: sumField(allRaw, "actual"),
    forecast: sumField(allRaw, "forecast"),
    point_diff: nonReconciling
      ? allRaw.reduce((s, r) => s + (diffOf(r) ?? 0), 0)
      : sumField(allRaw, "point_diff"),
    point_share_pct: nonReconciling ? null : sumField(allRaw, "point_share_pct"),
  };

  const dimTone: "drop" | "spike" | "neutral" =
    (tot.point_diff ?? 0) < 0
      ? "drop"
      : (tot.point_diff ?? 0) > 0
        ? "spike"
        : "neutral";

  const titleNode = (
    <span className="inline-flex items-baseline gap-2">
      <span>Per-dimension breakdown</span>
      <span className="font-normal normal-case tracking-normal text-ink-400">
        ({total} segment{total === 1 ? "" : "s"}
        {anomalyOnly && totalUnfiltered !== total
          ? ` of ${totalUnfiltered}`
          : ""}
        {" "}· sorted by diff size
        {hidden > 0 ? ` · showing top ${rows.length}` : ""})
      </span>
    </span>
  );

  const action =
    total > COMPACT_ROW_LIMIT ? (
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={`text-[10px] px-2 py-0.5 rounded border ${expanded ? "border-blue-600 bg-blue-50 text-blue-800" : "border-ink-200 bg-white text-ink-700 hover:bg-ink-50"}`}
        title={
          expanded
            ? "Show only the top segments"
            : `Show all ${total} segments`
        }
      >
        {expanded ? "Compact" : `Expand all (+${total - COMPACT_ROW_LIMIT})`}
      </button>
    ) : null;

  return (
    <SurfaceSection title={titleNode} action={action} tone={dimTone} collapsible>
      {/* Granularity selector */}
      <div className="mb-2 flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
          Granularity
        </span>
        <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
          {availableDims.map((d) => {
            const active = d === selectedDim;
            return (
              <button
                key={d}
                type="button"
                onClick={() => setSelectedDim(d)}
                className={`px-2 py-1 rounded text-[11px] transition-colors ${
                  active
                    ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                    : "text-ink-600 hover:bg-white hover:shadow-sm"
                }`}
                title={`Switch to ${d}`}
              >
                {DIM_LABEL[d] || d}
              </button>
            );
          })}
        </div>
      </div>

      {nonReconciling && (
        <div className="mb-2 flex items-start gap-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] text-amber-800">
          <span aria-hidden="true">ⓘ</span>
          <span>{NON_RECONCILING_CAVEAT}</span>
        </div>
      )}

      <table className="w-full text-[11px]">
        <thead>
          <SortableHeader
            columns={DIM_COLUMNS}
            sortKey={sortKey}
            sortDir={sortDir}
            onSort={onSort}
          />
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.label}
              className="border-b border-ink-50 hover:bg-blue-50/40"
            >
              <td className="py-1 px-1 font-mono">{row.label}</td>
              <td className="py-1 px-1 text-right font-mono">
                {fmtInt(row.actual)}
              </td>
              <td className="py-1 px-1 text-right font-mono">
                {fmtInt(row.forecast)}
              </td>
              <td
                className={`py-1 px-1 text-right font-mono ${(diffOf(row) ?? 0) < 0 ? "text-red-700" : (diffOf(row) ?? 0) > 0 ? "text-amber-700" : ""}`}
              >
                {fmtSigned(diffOf(row))}
                {row.forecast ? (
                  <span className="ml-1 text-[10px] text-ink-400">
                    ({fmtPct(((diffOf(row) ?? 0) / row.forecast) * 100)})
                  </span>
                ) : null}
              </td>
              <td
                className="py-1 px-1 text-right font-mono"
                title={nonReconciling ? "Not defined for overlapping cuts" : undefined}
              >
                {nonReconciling ? "—" : fmtPct(row.point_share_pct)}
              </td>
              <td className="py-1 px-1 text-center text-[10px] text-ink-500">
                {renderAnomCell ? (
                  renderAnomCell(row)
                ) : (
                  <DirectionPill direction={row.anom || null} />
                )}
              </td>
            </tr>
          ))}
          {hidden > 0 && (
            <tr className="border-b border-ink-50 bg-ink-50/40">
              <td
                colSpan={6}
                className="py-1 px-1 text-[10px] text-ink-400 italic"
              >
                +{hidden} more segment{hidden === 1 ? "" : "s"} hidden -
                included in total below.
              </td>
            </tr>
          )}
          <tr className="border-t-2 border-ink-300 bg-ink-100/70 font-semibold">
            <td className="py-1 px-1 font-mono text-ink-700">
              Total ({total} segment{total === 1 ? "" : "s"})
            </td>
            <td className="py-1 px-1 text-right font-mono text-ink-700">
              {fmtInt(tot.actual)}
            </td>
            <td className="py-1 px-1 text-right font-mono text-ink-700">
              {fmtInt(tot.forecast)}
            </td>
            <td
              className={`py-1 px-1 text-right font-mono ${(tot.point_diff ?? 0) < 0 ? "text-red-700" : (tot.point_diff ?? 0) > 0 ? "text-amber-700" : "text-ink-700"}`}
            >
              {fmtSigned(tot.point_diff)}
              {tot.forecast ? (
                <span className="ml-1 text-[10px] text-ink-400">
                  ({fmtPct(((tot.point_diff ?? 0) / tot.forecast) * 100)})
                </span>
              ) : null}
            </td>
            <td className="py-1 px-1 text-right font-mono text-ink-700">
              {fmtPct(tot.point_share_pct)}
            </td>
            <td className="py-1 px-1 text-center text-[10px] text-ink-400">
              —
            </td>
          </tr>
        </tbody>
      </table>
    </SurfaceSection>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// RangeSummaryCard Component
// ─────────────────────────────────────────────────────────────────────────────

interface RangeSummaryCardProps {
  startDate: string;
  endDate: string;
  data: ProsightNewsData;
  bu?: string;
  onOpenDetail?: (payload: OpenDetailPayload) => void;
  onDrillDay?: (date: string) => void;
}

export default function RangeSummaryCard({
  startDate,
  endDate,
  data,
  bu = "",
  onOpenDetail,
  onDrillDay,
}: RangeSummaryCardProps) {
  const [anomalyOnly, setAnomalyOnly] = useState(false);
  const [contribExpanded, setContribExpanded] = useState(false);
  const contribSort = useSortableTable("sumBandDiff", "desc");

  const l0Series = buSeries(data, bu)[L0_FULL_ID] || [];

  const verdict = useMemo(
    () => aggregateL0Range(l0Series, startDate, endDate),
    [l0Series, startDate, endDate]
  );

  const rangeDays = useMemo(
    () => buildRangeDays(l0Series, startDate, endDate),
    [l0Series, startDate, endDate]
  );

  const bestDay = useMemo(() => pickBestDay(rangeDays), [rangeDays]);
  const maxAbsPointMiss = useMemo(
    () =>
      rangeDays.reduce(
        (m, d) => Math.max(m, Math.abs(d.pointMiss)),
        0
      ),
    [rangeDays]
  );

  const contributors = useMemo(
    () => aggregateContributors(data, startDate, endDate, 50, bu),
    [data, startDate, endDate, bu]
  );

  const dimensions = useMemo(
    () =>
      aggregateDimensions(
        data,
        startDate,
        endDate,
        verdict?.cumulative_point_miss,
        verdict?.cumulative_band_miss,
        bu
      ),
    [data, startDate, endDate, verdict?.cumulative_point_miss, verdict?.cumulative_band_miss, bu]
  );

  if (!verdict || verdict.N === 0) {
    return (
      <article className="rounded-md border border-ink-200 bg-white shadow-sm">
        <div className="p-4 text-[12px] text-ink-400">
          No L0 data available for the selected range.
        </div>
      </article>
    );
  }

  const verdictDirection: DirectionType = verdict.has_anomaly
    ? verdict.direction || "mixed"
    : null;

  const bannerToneClass =
    verdictDirection === "spike"
      ? "border-amber-200 bg-amber-50/40"
      : verdictDirection === "drop"
        ? "border-red-200 bg-red-50/40"
        : "border-ink-200 bg-white";

  const verdictPillClass =
    verdictDirection === "spike"
      ? "bg-amber-100 text-amber-800 border-amber-300"
      : verdictDirection === "drop"
        ? "bg-red-100 text-red-800 border-red-300"
        : "bg-ink-100 text-ink-700 border-ink-300";

  const verdictPillText =
    verdictDirection === "spike"
      ? "▲ NET SPIKE"
      : verdictDirection === "drop"
        ? "▼ NET DROP"
        : "● NO ANOMALIES";

  const pctVsForecast =
    verdict.total_predicted && verdict.total_predicted !== 0
      ? (verdict.cumulative_point_miss / verdict.total_predicted) * 100
      : null;

  return (
    <article className="rounded-md border border-ink-200 bg-white shadow-sm">
      {/* BODY */}
      <div className="px-4 py-4 space-y-3">
        {/* BANNER: verdict pill + 3 stats + anomaly checkbox */}
        <section
          className={`rounded-md border ${bannerToneClass} overflow-hidden`}
        >
          <div className="px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-2">
            <div className="flex items-baseline gap-2 mr-2 min-w-0">
              <span
                className={`inline-flex items-center gap-1 rounded border font-semibold uppercase tracking-wide text-[10px] px-2 py-[3px] ${verdictPillClass}`}
              >
                {verdictPillText}
              </span>
              <span className="text-[11px] text-ink-500">range verdict</span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
                Total orders
              </span>
              <span className="text-[14px] font-mono font-semibold text-ink-800">
                {fmtInt(verdict.total_actual)}
              </span>
              <span className="text-[10px] text-ink-400 font-mono">
                forecast {fmtInt(verdict.total_predicted)}
              </span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
                Vs forecast
              </span>
              <span
                className={`text-[14px] font-mono font-semibold flex items-baseline gap-1 ${
                  verdict.cumulative_point_miss > 0
                    ? "text-amber-700"
                    : verdict.cumulative_point_miss < 0
                      ? "text-red-700"
                      : "text-ink-800"
                }`}
              >
                <span
                  className={`text-[10px] ${
                    verdict.cumulative_point_miss > 0
                      ? "text-amber-600"
                      : verdict.cumulative_point_miss < 0
                        ? "text-red-600"
                        : "text-ink-400"
                  }`}
                  aria-hidden="true"
                >
                  {verdict.cumulative_point_miss > 0
                    ? "▲"
                    : verdict.cumulative_point_miss < 0
                      ? "▼"
                      : ""}
                </span>
                {fmtSigned(verdict.cumulative_point_miss)}
                {pctVsForecast != null ? (
                  <span className="ml-1 text-[11px] opacity-80">
                    ({fmtPct(pctVsForecast)})
                  </span>
                ) : null}
              </span>
              <span className="text-[10px] text-ink-400 font-mono">
                {fmtSigned(verdict.avg_point_miss_per_day)} / day avg
              </span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
                Days breakdown
              </span>
              <span className="text-[14px] font-mono font-semibold text-ink-800 flex items-baseline gap-1.5">
                <span className="text-red-700">{verdict.drop_days}d</span>
                <span className="text-ink-300">/</span>
                <span className="text-amber-700">{verdict.spike_days}d</span>
                <span className="text-ink-300">/</span>
                <span className="text-ink-600">{verdict.normal_days}d</span>
              </span>
              <span className="text-[10px] text-ink-400 font-mono">
                drop / spike / normal · {verdict.N} total
              </span>
            </div>
            <label
              className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-ink-600 cursor-pointer select-none"
              title="Hide rows whose anomaly column is not set"
            >
              <input
                type="checkbox"
                checked={anomalyOnly}
                onChange={(e) => setAnomalyOnly(e.target.checked)}
                className="h-3 w-3 accent-blue-600"
              />
              Anomaly only
            </label>
          </div>
        </section>

        {/* DAY-BY-DAY SPINE */}
        <SurfaceSection
          title={
            <span className="inline-flex items-baseline gap-2">
              <span>Day-by-day spine</span>
              <span className="font-normal normal-case tracking-normal text-ink-400">
                ({verdict.N} day{verdict.N === 1 ? "" : "s"} · click any to drill)
              </span>
            </span>
          }
          action={
            <span className="text-[9px] text-ink-400 font-mono uppercase tracking-wide">
              bar = signed diff vs forecast, scaled within range
            </span>
          }
        >
          <div
            className="grid gap-2"
            style={{
              gridTemplateColumns: `repeat(${Math.min(
                rangeDays.length,
                7
              )}, minmax(0, 1fr))`,
            }}
          >
            {rangeDays.map((day) => (
              <DayStripCell
                key={day.date}
                day={day}
                maxAbsPointMiss={maxAbsPointMiss}
                onDrill={onDrillDay}
              />
            ))}
          </div>
          <div className="mt-2 flex items-center gap-4 text-[10px] text-ink-500 font-mono flex-wrap">
            <span className="inline-flex items-center gap-1">
              <span className="inline-block w-2 h-1.5 bg-amber-500 rounded-sm" />
              spike (above band)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block w-2 h-1.5 bg-red-500 rounded-sm" />
              drop (below band)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block w-2 h-1.5 bg-ink-300 rounded-sm" />
              in band
            </span>
          </div>
        </SurfaceSection>

        {/* WORST + BEST EXEMPLAR PAIR */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          {verdict.worst ? (
            <ExemplarCard
              kind="worst"
              date={verdict.worst.date}
              weekday={weekdayOf(verdict.worst.date)}
              actual={verdict.worst.actual}
              predicted={verdict.worst.predicted}
              pointMiss={verdict.worst.pointMiss}
              direction={
                verdict.worst.pointMiss < 0
                  ? "drop"
                  : verdict.worst.pointMiss > 0
                    ? "spike"
                    : null
              }
              onDrill={onDrillDay}
            />
          ) : (
            <SurfaceSection title="Worst day in range" tone="neutral">
              <div className="text-[11px] text-ink-400 italic">
                No day in range crossed its forecast band.
              </div>
            </SurfaceSection>
          )}
          {bestDay ? (
            <ExemplarCard
              kind="best"
              date={bestDay.date}
              weekday={bestDay.weekday}
              actual={bestDay.actual}
              predicted={bestDay.predicted}
              pointMiss={bestDay.pointMiss}
              direction={bestDay.direction}
              onDrill={onDrillDay}
            />
          ) : (
            <SurfaceSection title="Best day in range" tone="neutral">
              <div className="text-[11px] text-ink-400 italic">
                No comparable day in this range.
              </div>
            </SurfaceSection>
          )}
        </div>

        {/* TOP CONTRIBUTORS */}
        {(() => {
          const filtered = anomalyOnly
            ? contributors.filter((c) => c.anomDays > 0)
            : contributors;
          const sorted = sortRows(
            filtered,
            CONTRIB_COLUMNS,
            contribSort.sortKey,
            contribSort.sortDir
          );
          const showAll = contribExpanded || sorted.length <= CONTRIB_COMPACT_LIMIT;
          const visible = showAll
            ? sorted
            : sorted.slice(0, CONTRIB_COMPACT_LIMIT);
          const hidden = sorted.length - visible.length;
          const canExpand = sorted.length > CONTRIB_COMPACT_LIMIT;

          const titleNode = (
            <span className="inline-flex items-baseline gap-2">
              <span>Top contributing series</span>
              <span className="font-normal normal-case tracking-normal text-ink-400">
                ({sorted.length} series
                {anomalyOnly && contributors.length !== sorted.length
                  ? ` of ${contributors.length}`
                  : ""}
                {" "}· sorted by diff size)
              </span>
            </span>
          );
          const action = canExpand ? (
            <button
              type="button"
              onClick={() => setContribExpanded((v) => !v)}
              className={`text-[10px] px-2 py-0.5 rounded border ${contribExpanded ? "border-blue-600 bg-blue-50 text-blue-800" : "border-ink-200 bg-white text-ink-700 hover:bg-ink-50"}`}
              title={
                contribExpanded
                  ? "Collapse to top contributors"
                  : `Show all ${sorted.length} rows`
              }
            >
              {contribExpanded ? "Compact" : `Expand all (+${hidden})`}
            </button>
          ) : null;

          return (
            <SurfaceSection title={titleNode} action={action} collapsible>
              {contributors.length ? (
                <table className="w-full text-[11px]">
                  <thead>
                    <SortableHeader
                      columns={[
                        { key: "#", label: "#", align: "left" },
                        ...CONTRIB_COLUMNS,
                      ]}
                      sortKey={contribSort.sortKey}
                      sortDir={contribSort.sortDir}
                      onSort={(key) =>
                        key === "#" ? null : contribSort.onSort(key)
                      }
                    />
                  </thead>
                  <tbody>
                    {visible.map((c, i) => {
                      const clickable = !!onOpenDetail;
                      const handleOpen = () => {
                        if (!clickable) return;
                        onOpenDetail({
                          full_id: seriesToFullId(c.series) || "",
                          label: c.series,
                          summaryStartDate: startDate,
                          summaryEndDate: endDate,
                        });
                      };
                      return (
                        <tr
                          key={c.series}
                          className={`border-b border-ink-50 ${clickable ? "cursor-pointer hover:bg-blue-50/60" : "hover:bg-blue-50/40"}`}
                          title={clickable ? `Open ${c.series} in graph tab` : c.series}
                          onClick={handleOpen}
                        >
                          <td className="py-1 px-1 font-mono text-ink-500">
                            {i + 1}
                          </td>
                          <td className="py-1 px-1">
                            <div
                              className={`text-[11px] truncate ${clickable ? "text-blue-700 hover:underline" : "text-ink-800"}`}
                              style={{ maxWidth: 360 }}
                            >
                              {c.series}
                            </div>
                          </td>
                          <td className="py-1 px-1 text-right font-mono">
                            {c.appearances}
                          </td>
                          <td className="py-1 px-1 text-right font-mono">
                            {c.anomDays || "—"}
                          </td>
                          <td className="py-1 px-1 text-right font-mono">
                            <span className="text-red-700">{c.drops}</span>{" "}
                            <span className="text-amber-700">{c.spikes}</span>
                          </td>
                          <td
                            className={`py-1 px-1 text-right font-mono ${c.sumPointDiff < 0 ? "text-red-700" : c.sumPointDiff > 0 ? "text-amber-700" : ""}`}
                          >
                            {fmtSigned(c.sumPointDiff)}
                            {c.sumForecast ? (
                              <span className="ml-1 text-[10px] text-ink-400">
                                ({fmtPct((c.sumPointDiff / c.sumForecast) * 100)})
                              </span>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                    {sorted.length === 0 && (
                      <tr>
                        <td
                          colSpan={6}
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
                  No contributing series in this range.
                </div>
              )}
            </SurfaceSection>
          );
        })()}

        {/* PER-DIMENSION BREAKDOWN - rolled up across range */}
        <DimensionTables
          dimensions={dimensions}
          anomalyOnly={anomalyOnly}
          renderAnomCell={(row) => {
            const drops = row.drop_days || 0;
            const spikes = row.spike_days || 0;
            if (!drops && !spikes) return <DirectionPill direction={null} />;
            return (
              <span className="inline-flex items-center gap-1 text-[10px] font-mono">
                {drops > 0 ? (
                  <span className="text-red-700">{drops}d</span>
                ) : null}
                {drops > 0 && spikes > 0 ? (
                  <span className="text-ink-300">.</span>
                ) : null}
                {spikes > 0 ? (
                  <span className="text-amber-700">{spikes}d</span>
                ) : null}
              </span>
            );
          }}
        />

      </div>
    </article>
  );
}
