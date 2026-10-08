"use client";

/**
 * L2 pair drill-down (spec 40 §4.3) — the sub-table opened by clicking a row in
 * UnifiedDimensionBreakdown.
 *
 * Extracted from that component: it owns its own cut selection, sort and
 * expand/compact state, and its own reconciliation maths (share-of-parent, the
 * "other / unqualified" remainder row), none of which the parent table reads.
 * The parent's only coupling to it is the props below.
 */

import { useEffect, useMemo, useState } from "react";
import type {
  ProsightNewsData,
  ProsightTodayRow,
  DimensionRow,
  TableColumn,
  BaselineMode,
  ProsightTimeseriesPoint,
} from "./types";
import {
  seriesBaseline,
  pairOtherDim,
  pairChildren,
  cutReconciles,
  BASELINE_COMPARE_LABELS,
} from "./adapters";
import { DIM_LABEL } from "./dimensions";
import { fmtInt, fmtSigned, fmtPct } from "./utils/format";
import { DirectionPill } from "./surfaces";
import {
  SortableHeader,
  sortRows,
  sumField,
  useSortableTable,
} from "./sortableTable";


const DRILL_COMPACT_LIMIT = 5;

interface PairDrilldownProps {
  /** Pair cuts drillable from `dim`, in display order. Never empty. */
  cuts: string[];
  dim: string;
  knownDims: string[];
  /** The clicked parent segment's raw value ("delhi"). */
  value: string;
  parentLabel: string;
  /** The parent row as displayed (already remapped for the active baseline mode). */
  parentRow: DimensionRow;
  seriesTimeseries?: Record<string, ProsightTimeseriesPoint[]>;
  date?: string;
  baselineMode: BaselineMode;
  todayRows?: ProsightTodayRow[];
  data?: ProsightNewsData | null;
  bu?: string;
  onClose: () => void;
}

export default function PairDrilldown({
  cuts,
  dim,
  knownDims,
  value,
  parentLabel,
  parentRow,
  seriesTimeseries,
  date,
  baselineMode,
  todayRows,
  data,
  bu,
  onClose,
}: PairDrilldownProps) {
  const [cutKey, setCutKey] = useState(cuts[0]);
  const [expanded, setExpanded] = useState(false);
  const { sortKey, sortDir, onSort } = useSortableTable("point_diff", "desc");

  useEffect(() => {
    if (!cuts.includes(cutKey) && cuts[0]) setCutKey(cuts[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cuts.join(",")]);

  const staticMode = baselineMode !== "forecast";
  const compareLabel = BASELINE_COMPARE_LABELS[baselineMode];
  const reconciling = cutReconciles(data, bu, cutKey, knownDims);
  const otherDim = pairOtherDim(cutKey, dim, knownDims) ?? "";
  const otherLabel = DIM_LABEL[otherDim] || otherDim;

  const raw = useMemo(
    () =>
      pairChildren(seriesTimeseries, cutKey, dim, value, date ?? "", todayRows),
    [seriesTimeseries, cutKey, dim, value, date, todayRows]
  );

  // Static modes: same remap as the parent table — each pair compared to its
  // own actual N days ago from its L2 series; forecast-space fallback per row.
  const mapped: DimensionRow[] = useMemo(() => {
    if (!staticMode) return raw;
    return raw.map((r) => {
      const b = seriesBaseline(
        seriesTimeseries?.[`L2_pair::${r.group_id}`],
        date ?? "",
        baselineMode
      );
      if (!b) return r;
      return {
        ...r,
        forecast: b.anchor_value,
        point_diff: b.delta,
        flagged: b.direction != null,
        anom: b.direction,
      };
    });
  }, [raw, staticMode, seriesTimeseries, date, baselineMode]);

  // Share of the CLICKED segment's miss/move (not of L0) — parentRow carries
  // the mode-consistent diff. Suppressed for overlapping cuts.
  const parentDiff = parentRow.point_diff ?? 0;
  const withShare = useMemo(
    () =>
      mapped.map((r) => ({
        ...r,
        point_share_pct:
          reconciling && parentDiff
            ? ((r.point_diff ?? 0) / parentDiff) * 100
            : null,
      })),
    [mapped, reconciling, parentDiff]
  );

  const columns: TableColumn<DimensionRow>[] = [
    { key: "label", label: otherLabel, align: "left" },
    { key: "actual", label: "actual", align: "right" },
    { key: "forecast", label: staticMode ? compareLabel : "forecast", align: "right" },
    { key: "point_diff", label: `Diff vs ${compareLabel}`, align: "right" },
    {
      key: "point_share_pct",
      label: staticMode ? `% of ${parentLabel} move` : `% of ${parentLabel} miss`,
      align: "right",
      title: `Share of ${parentLabel}'s ${staticMode ? "day-over-day move" : "miss"} attributable to this segment.`,
    },
    { key: "anom", label: "anom", align: "center", getValue: (r) => (r.flagged ? 1 : 0) },
  ];

  const sortedAll = sortRows(withShare, columns, sortKey, sortDir);
  const total = sortedAll.length;
  const rows = expanded ? sortedAll : sortedAll.slice(0, DRILL_COMPACT_LIMIT);
  const hidden = total - rows.length;

  // Remainder: qualified pairs rarely cover the parent 1:1 — surface the gap so
  // the sub-table foots to the parent row. Only meaningful for partition cuts.
  const sum = {
    actual: sumField(withShare, "actual") ?? 0,
    forecast: sumField(withShare, "forecast") ?? 0,
    point_diff: sumField(withShare, "point_diff") ?? 0,
  };
  const rem = {
    actual: (parentRow.actual ?? 0) - sum.actual,
    forecast: (parentRow.forecast ?? 0) - sum.forecast,
    point_diff: (parentRow.point_diff ?? 0) - sum.point_diff,
  };
  const showRem =
    reconciling && (Math.abs(rem.actual) > 0.5 || Math.abs(rem.point_diff) > 0.5);
  const totals = reconciling
    ? { actual: parentRow.actual, forecast: parentRow.forecast, point_diff: parentRow.point_diff }
    : sum;

  return (
    <div
      className="m-1 ml-3 rounded border-l-2 border-blue-300 bg-blue-50/30 p-2"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="mb-1.5 flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
          <span className="font-mono normal-case text-ink-700">{parentLabel}</span>
          {" — break down by"}
        </span>
        <div className="inline-flex rounded-md border border-ink-200 bg-white p-0.5">
          {cuts.map((cut) => {
            const label =
              DIM_LABEL[pairOtherDim(cut, dim, knownDims) ?? ""] ||
              pairOtherDim(cut, dim, knownDims) ||
              cut;
            const active = cut === cutKey;
            return (
              <button
                key={cut}
                type="button"
                onClick={() => setCutKey(cut)}
                className={`px-2 py-0.5 rounded text-[10px] transition-colors ${
                  active
                    ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                    : "text-ink-600 hover:bg-ink-50"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-[10px] px-2 py-0.5 rounded border border-ink-200 bg-white text-ink-600 hover:bg-ink-50"
        >
          ✕ close
        </button>
      </div>

      {!reconciling && total > 0 && (
        <div className="mb-1.5 flex items-start gap-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] text-amber-800">
          <span aria-hidden="true">ⓘ</span>
          <span>
            Overlapping cut — one order maps to many {otherLabel} segments, so
            rows sum above the {parentLabel} total; the share partition is
            suppressed.
          </span>
        </div>
      )}

      {total === 0 ? (
        <div className="text-[11px] text-ink-400 italic">
          No qualified {otherLabel} segments for{" "}
          <span className="font-mono">{parentLabel}</span> on this day.
        </div>
      ) : (
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
            {rows.map((row) => (
              <tr
                key={row.label}
                className="border-b border-ink-50 hover:bg-white/60"
              >
                <td className="py-1 px-1 font-mono">{row.label}</td>
                <td className="py-1 px-1 text-right font-mono">
                  {fmtInt(row.actual)}
                </td>
                <td className="py-1 px-1 text-right font-mono">
                  {fmtInt(row.forecast)}
                </td>
                <td
                  className={`py-1 px-1 text-right font-mono ${(row.point_diff ?? 0) < 0 ? "text-red-700" : (row.point_diff ?? 0) > 0 ? "text-amber-700" : ""}`}
                >
                  {fmtSigned(row.point_diff)}
                  {row.forecast ? (
                    <span className="ml-1 text-[10px] text-ink-400">
                      ({fmtPct(((row.point_diff ?? 0) / row.forecast) * 100)})
                    </span>
                  ) : null}
                </td>
                <td
                  className="py-1 px-1 text-right font-mono"
                  title={!reconciling ? "Not defined for overlapping cuts" : undefined}
                >
                  {fmtPct(row.point_share_pct)}
                </td>
                <td
                  className="py-1 px-1 text-center text-[10px] text-ink-500"
                  title={row.self_z != null ? `score ${row.self_z}` : undefined}
                >
                  {row.flagged ? (
                    <DirectionPill
                      direction={
                        row.anom ?? (row.point_diff < 0 ? "drop" : "spike")
                      }
                    />
                  ) : (
                    <DirectionPill direction={null} />
                  )}
                </td>
              </tr>
            ))}
            {hidden > 0 && (
              <tr className="border-b border-ink-50">
                <td colSpan={6} className="py-1 px-1 text-[10px] text-ink-400 italic">
                  +{hidden} more segment{hidden === 1 ? "" : "s"} hidden -{" "}
                  <button
                    type="button"
                    onClick={() => setExpanded(true)}
                    className="underline hover:text-ink-600"
                  >
                    expand all
                  </button>
                  {" "}- included in the total below.
                </td>
              </tr>
            )}
            {expanded && total > DRILL_COMPACT_LIMIT && (
              <tr className="border-b border-ink-50">
                <td colSpan={6} className="py-1 px-1 text-[10px] text-ink-400 italic">
                  <button
                    type="button"
                    onClick={() => setExpanded(false)}
                    className="underline hover:text-ink-600"
                  >
                    compact
                  </button>
                </td>
              </tr>
            )}
            {showRem && (
              <tr className="border-b border-ink-50 text-ink-500">
                <td className="py-1 px-1 font-mono italic">other / unqualified</td>
                <td className="py-1 px-1 text-right font-mono">
                  {fmtInt(rem.actual)}
                </td>
                <td className="py-1 px-1 text-right font-mono">
                  {fmtInt(rem.forecast)}
                </td>
                <td className="py-1 px-1 text-right font-mono">
                  {fmtSigned(rem.point_diff)}
                </td>
                <td className="py-1 px-1 text-right font-mono">
                  {reconciling && parentDiff
                    ? fmtPct((rem.point_diff / parentDiff) * 100)
                    : "—"}
                </td>
                <td className="py-1 px-1 text-center text-[10px] text-ink-400">-</td>
              </tr>
            )}
            <tr className="border-t border-ink-300 bg-ink-100/50 font-semibold">
              <td className="py-1 px-1 font-mono text-ink-700">
                Total ({parentLabel})
              </td>
              <td className="py-1 px-1 text-right font-mono text-ink-700">
                {fmtInt(totals.actual)}
              </td>
              <td className="py-1 px-1 text-right font-mono text-ink-700">
                {fmtInt(totals.forecast)}
              </td>
              <td
                className={`py-1 px-1 text-right font-mono ${totals.point_diff && totals.point_diff < 0 ? "text-red-700" : totals.point_diff && totals.point_diff > 0 ? "text-amber-700" : "text-ink-700"}`}
              >
                {fmtSigned(totals.point_diff)}
              </td>
              <td className="py-1 px-1 text-right font-mono text-ink-700">
                {reconciling && parentDiff ? "100%" : "—"}
              </td>
              <td className="py-1 px-1 text-center text-[10px] text-ink-400">-</td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  );
}
