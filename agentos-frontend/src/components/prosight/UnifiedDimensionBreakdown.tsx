"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import type {
  ProsightRca,
  ProsightRcaChild,
  ProsightNewsData,
  ProsightTodayRow,
  DimensionRow,
  TableColumn,
  BaselineMode,
  ProsightTimeseriesPoint,
} from "./types";
import {
  isNonReconciling,
  childShareDiff,
  seriesBaseline,
  knownSeriesDims,
  pairCutsFor,
  pairOtherDim,
  BASELINE_COMPARE_LABELS,
  NON_RECONCILING_CAVEAT,
} from "./adapters";
import { DIM_LABEL, DIM_ORDER } from "./dimensions";
import PairDrilldown from "./PairDrilldown";
import { fmtInt, fmtSigned, fmtPct } from "./utils/format";
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

const COLUMNS: TableColumn<DimensionRow>[] = [
  { key: "label", label: "label", align: "left" },
  { key: "actual", label: "actual", align: "right" },
  { key: "forecast", label: "forecast", align: "right" },
  { key: "point_diff", label: "Diff vs forecast", align: "right" },
  {
    key: "point_share_pct",
    label: "% of total miss",
    align: "right",
    title: "Share of the L0 miss attributable to this segment.",
  },
  {
    key: "anom",
    label: "anom",
    align: "center",
    getValue: (r) => (r.flagged ? 1 : 0),
  },
];

const COMPACT_LIMIT = 5;

// ─────────────────────────────────────────────────────────────────────────────
// Helper Functions
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Map an RCA child onto the table's row shape. Typed against
 * `ProsightRcaChild` rather than an inline copy of it: a field added upstream
 * is structurally compatible with a narrower inline shape, so a duplicate
 * would keep compiling while quietly ignoring the new field.
 */
function flattenChild(child: ProsightRcaChild): DimensionRow {
  const point_diff = (child.actual ?? 0) - (child.predicted ?? 0);
  return {
    label: child.label,
    group_id: child.group_id,
    actual: child.actual,
    forecast: child.predicted,
    lower: child.lower,
    upper: child.upper,
    point_diff,
    point_share_pct: child.point_pct,
    band_diff: child.band_violation,
    band_share_pct: child.band_pct,
    verdict: child.verdict,
    flagged: child.flagged,
    self_z: child.self_z,
    attributed_point_diff: child.attributed_point_diff,
    synthetic: child.synthetic,
  };
}

/** The raw segment value a row stands for (`city::delhi` → `delhi`). */
function rowValue(r: DimensionRow): string {
  if (!r.group_id) return r.label;
  const i = r.group_id.indexOf("::");
  return i === -1 ? r.group_id : r.group_id.slice(i + 2);
}

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

interface UnifiedDimensionBreakdownProps {
  rca: ProsightRca | null;
  headlineDim?: string;
  anomalyOnly?: boolean;
  /** Part B (spec 38 §4.5): which comparison the table reads. Default forecast. */
  baselineMode?: BaselineMode;
  /** Per-BU series map; used to compute each segment's static baseline. */
  seriesTimeseries?: Record<string, ProsightTimeseriesPoint[]>;
  /** The as-of day these segments describe. */
  date?: string;
  /** Spec 40: full payload + BU, for `display_contract` reconciliation flags. */
  data?: ProsightNewsData | null;
  bu?: string;
  /** Spec 40: the day's rows — enriches drill-down pairs that flagged. */
  todayRows?: ProsightTodayRow[];
}

export default function UnifiedDimensionBreakdown({
  rca,
  headlineDim,
  anomalyOnly = false,
  baselineMode = "forecast",
  seriesTimeseries,
  date,
  data,
  bu,
  todayRows,
}: UnifiedDimensionBreakdownProps) {
  const byDim = rca?.by_dimension || {};
  const availableDims = useMemo(() => {
    const present = Object.keys(byDim);
    const ordered = DIM_ORDER.filter((d) => present.includes(d));
    const extras = present.filter((d) => !DIM_ORDER.includes(d));
    return [...ordered, ...extras];
  }, [byDim]);

  const [selectedDim, setSelectedDim] = useState(
    headlineDim && byDim[headlineDim] ? headlineDim : availableDims[0]
  );
  const [expanded, setExpanded] = useState(false);
  const [dimType, setDimType] = useState<
    "all" | "spike" | "drop" | "inband"
  >("all");
  const { sortKey, sortDir, onSort } = useSortableTable("point_diff", "desc");

  // Spec 40: L2 pair cuts drillable from the selected dim, ordered by the
  // other side's DIM_ORDER rank. Empty on payloads without pair series.
  const knownDims = useMemo(
    () => knownSeriesDims(seriesTimeseries),
    [seriesTimeseries]
  );
  const drillCuts = useMemo(() => {
    const rank = (cut: string) => {
      const other = pairOtherDim(cut, selectedDim, knownDims) ?? "";
      const i = DIM_ORDER.indexOf(other);
      return i === -1 ? DIM_ORDER.length : i;
    };
    return pairCutsFor(seriesTimeseries, selectedDim).sort(
      (a, b) => rank(a) - rank(b)
    );
  }, [seriesTimeseries, selectedDim, knownDims]);
  // The clicked parent segment's raw value ("delhi"); one open per tile.
  const [drillValue, setDrillValue] = useState<string | null>(null);

  useEffect(() => {
    if (!byDim[selectedDim] && availableDims[0]) setSelectedDim(availableDims[0]);
    if (headlineDim && byDim[headlineDim]) setSelectedDim(headlineDim);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [headlineDim, availableDims.join(",")]);

  useEffect(() => {
    setDrillValue(null);
  }, [selectedDim, date, bu]);

  if (!availableDims.length) {
    return (
      <SurfaceSection title="Per-dimension breakdown" collapsible>
        <div className="text-[11px] text-ink-400 italic">
          No RCA available for this day - per-dimension breakdown requires{" "}
          <span className="font-mono">today_rows[].rca.by_dimension</span>.
        </div>
      </SurfaceSection>
    );
  }

  const block = byDim[selectedDim] || { children: [] };
  const forecastRows = (block.children || []).map(flattenChild);

  // Overlapping cut (e.g. labs `sku_name`): rows sum ABOVE L0, so the "% of total
  // miss" partition is undefined. Show the attributed contribution (Σ reconciles
  // to L0 once the backend ships it) and suppress the misleading share. spec §3.3.
  const nonReconciling = isNonReconciling(selectedDim, data);
  const staticMode = baselineMode !== "forecast";
  const compareLabel = BASELINE_COMPARE_LABELS[baselineMode];

  // In a static mode, re-express each segment against its own actual N days ago
  // (from series_timeseries — rca children carry no baselines block). §4.5. The
  // "forecast" column becomes the basis and "diff" becomes the day-over-day move;
  // the share is recomputed as a fraction of the total move (suppressed for
  // overlapping cuts, whose segments sum above the total either way).
  const allRaw: DimensionRow[] = ((): DimensionRow[] => {
    if (!staticMode) return forecastRows;
    const mapped = forecastRows.map((r) => {
      const b = r.group_id
        ? seriesBaseline(
            seriesTimeseries?.[`L1_single::${r.group_id}`],
            date ?? "",
            baselineMode,
          )
        : null;
      if (!b) return r; // fallback: keep forecast-space values for this segment
      return {
        ...r,
        forecast: b.anchor_value,
        point_diff: b.delta,
        flagged: b.direction != null,
        verdict: null,
        attributed_point_diff: undefined,
      };
    });
    const totalMove = mapped.reduce((s, r) => s + (r.point_diff ?? 0), 0);
    return mapped.map((r) => ({
      ...r,
      point_share_pct:
        nonReconciling || !totalMove
          ? null
          : ((r.point_diff ?? 0) / totalMove) * 100,
    }));
  })();

  // Column labels reflect the active comparison.
  const columns: TableColumn<DimensionRow>[] = staticMode
    ? COLUMNS.map((c) =>
        c.key === "forecast"
          ? { ...c, label: compareLabel }
          : c.key === "point_diff"
            ? { ...c, label: `Diff vs ${compareLabel}` }
            : c.key === "point_share_pct"
              ? {
                  ...c,
                  label: "% of total move",
                  title:
                    "Share of the total day-over-day move attributable to this segment.",
                }
              : c,
      )
    : COLUMNS;

  const diffOf = (r: DimensionRow) =>
    nonReconciling ? childShareDiff(r) ?? r.point_diff : r.point_diff;
  const typeFiltered =
    dimType === "all"
      ? allRaw
      : dimType === "inband"
        ? allRaw.filter((r) => !r.flagged)
        : dimType === "spike"
          ? allRaw.filter((r) => r.flagged && r.point_diff > 0)
          : allRaw.filter((r) => r.flagged && r.point_diff < 0);
  const filtered = anomalyOnly
    ? typeFiltered.filter((r) => r.flagged)
    : typeFiltered;
  const sortedAll = sortRows(filtered, columns, sortKey, sortDir);
  const total = sortedAll.length;
  const rows = expanded ? sortedAll : sortedAll.slice(0, COMPACT_LIMIT);
  const hidden = total - rows.length;

  const tot = {
    actual: sumField(allRaw, "actual"),
    forecast: sumField(allRaw, "forecast"),
    point_diff: nonReconciling
      ? allRaw.reduce((s, r) => s + (diffOf(r) ?? 0), 0)
      : sumField(allRaw, "point_diff"),
    // Suppressed for overlapping cuts: raw shares sum >100% and are meaningless.
    point_share_pct: nonReconciling ? null : sumField(allRaw, "point_share_pct"),
  };

  const titleNode = (
    <span className="inline-flex items-baseline gap-2">
      <span>Per-dimension breakdown</span>
      <span className="font-normal normal-case tracking-normal text-ink-400">
        ({total} segment{total === 1 ? "" : "s"} · sorted by diff size
        {hidden > 0 ? ` · showing top ${rows.length}` : ""})
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
          const active = dimType === val;
          return (
            <button
              key={val}
              type="button"
              onClick={() => setDimType(val)}
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
      {total > COMPACT_LIMIT && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${expanded ? "border-blue-600 bg-blue-50 text-blue-800" : "border-ink-200 bg-white text-ink-700 hover:bg-ink-50"}`}
          title={
            expanded
              ? "Show only the top contributors"
              : `Show all ${total} segments`
          }
        >
          {expanded ? "Compact" : `Expand all (+${total - COMPACT_LIMIT})`}
        </button>
      )}
    </>
  );

  return (
    <SurfaceSection title={titleNode} action={action} collapsible>
      {/* Granularity selector */}
      <div className="mb-2 flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
          Granularity
        </span>
        <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
          {availableDims.map((dim) => {
            const active = dim === selectedDim;
            return (
              <button
                key={dim}
                type="button"
                onClick={() => setSelectedDim(dim)}
                className={`px-2 py-1 rounded text-[11px] transition-colors ${
                  active
                    ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                    : "text-ink-600 hover:bg-white hover:shadow-sm"
                }`}
                title={`Switch to ${dim}`}
              >
                {DIM_LABEL[dim] || dim}
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
            columns={columns}
            sortKey={sortKey}
            sortDir={sortDir}
            onSort={onSort}
          />
        </thead>
        <tbody>
          {rows.map((row) => {
            const value = rowValue(row);
            const canDrill =
              drillCuts.length > 0 && !row.synthetic && !!row.group_id;
            const isOpen = canDrill && drillValue === value;
            return (
              <Fragment key={row.label}>
                <tr
                  className={`border-b border-ink-50 hover:bg-blue-50/40 ${canDrill ? "cursor-pointer" : ""} ${isOpen ? "bg-blue-50/40" : ""}`}
                  onClick={
                    canDrill
                      ? () => setDrillValue(isOpen ? null : value)
                      : undefined
                  }
                  // Deliberately no role="button"/aria-expanded on the <tr>: that
                  // overrides the row's implicit `row` role, so assistive tech
                  // loses the row/cell relationship for every cell in it — and a
                  // <tr> cannot hold focus, so it announced a control that no
                  // keyboard could reach. The real control is the <button> in the
                  // first cell; this handler is a mouse-only convenience.
                  title={
                    canDrill
                      ? isOpen
                        ? "Hide breakdown"
                        : `Break ${row.label} down by a second dimension`
                      : undefined
                  }
                >
                  <td className="py-1 px-1 font-mono">
                    {canDrill ? (
                      <button
                        type="button"
                        // Without stopPropagation the row's onClick also fires and
                        // toggles a second time, cancelling the drill-down.
                        onClick={(e) => {
                          e.stopPropagation();
                          setDrillValue(isOpen ? null : value);
                        }}
                        aria-expanded={isOpen}
                        title={
                          isOpen
                            ? "Hide breakdown"
                            : `Break ${row.label} down by a second dimension`
                        }
                        className="inline-flex items-center rounded-sm text-left font-mono hover:underline focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue-600"
                      >
                        <span
                          aria-hidden="true"
                          className="mr-1 inline-block w-2 text-[9px] text-blue-700"
                        >
                          {isOpen ? "▾" : "▸"}
                        </span>
                        {row.label}
                      </button>
                    ) : (
                      row.label
                    )}
                  </td>
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
                    {row.flagged ? (
                      <DirectionPill
                        direction={row.point_diff < 0 ? "drop" : "spike"}
                      />
                    ) : row.verdict === "structural" ? (
                      <span
                        className="text-ink-400"
                        title="Structural contributor (within own band)"
                      >
                        .
                      </span>
                    ) : (
                      <DirectionPill direction={null} />
                    )}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="border-b border-ink-50">
                    <td colSpan={6} className="p-0">
                      <PairDrilldown
                        cuts={drillCuts}
                        dim={selectedDim}
                        knownDims={knownDims}
                        value={value}
                        parentLabel={row.label}
                        parentRow={row}
                        seriesTimeseries={seriesTimeseries}
                        date={date}
                        baselineMode={baselineMode}
                        todayRows={todayRows}
                        data={data}
                        bu={bu}
                        onClose={() => setDrillValue(null)}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {hidden > 0 && (
            <tr className="border-b border-ink-50 bg-ink-50/40">
              <td colSpan={6} className="py-1 px-1 text-[10px] text-ink-400 italic">
                +{hidden} more segment{hidden === 1 ? "" : "s"} hidden - included in
                total below.
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
              className={`py-1 px-1 text-right font-mono ${tot.point_diff && tot.point_diff < 0 ? "text-red-700" : tot.point_diff && tot.point_diff > 0 ? "text-amber-700" : "text-ink-700"}`}
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
            <td className="py-1 px-1 text-center text-[10px] text-ink-400">-</td>
          </tr>
        </tbody>
      </table>
    </SurfaceSection>
  );
}
