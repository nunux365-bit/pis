"use client";

/**
 * SeriesCard and related components for NewsPage - Prosight N Dashboard
 * Includes: SeriesCard, ImpactBreakdown, AttentionView
 */

import { useEffect, useMemo, useState } from "react";
import { Mets } from "./atoms";
import {
  fmt,
  lvlS,
  seriesLabel,
  directChildren,
  computeSeriesStats,
  explainDriver,
  arrowColor,
  arrowSymbol,
} from "./utils";
import type {
  SeriesCardProps,
  ChildDelta,
  AttentionData,
  TFTImportance,
} from "./types";
import { isNonReconciling, NON_RECONCILING_CAVEAT } from "../adapters";

// ══════════════════════════════════════════════════════════════════════════════
// Impact Breakdown Component
// ══════════════════════════════════════════════════════════════════════════════

interface ImpactBreakdownProps {
  parentLabel: string;
  parentDelta: number | null;
  parentPct: number | null;
  childDeltas: ChildDelta[];
  date: string;
  /** True when the child cut overlaps (e.g. SKU): children sum above parent, so
   *  the "unaccounted" partition is undefined and is suppressed. spec §3.3. */
  nonReconciling?: boolean;
}

function ImpactBreakdown({
  parentLabel,
  parentDelta,
  parentPct,
  childDeltas,
  date,
  nonReconciling = false,
}: ImpactBreakdownProps) {
  if (!parentDelta && !childDeltas.length) return null;
  const isDrop = (parentDelta ?? 0) < 0;
  const pColor = isDrop ? "#dc2626" : "#f59e0b";
  const accounted = childDeltas.reduce((s, c) => s + c.delta, 0);
  const gap = (parentDelta ?? 0) - accounted;

  return (
    <div className="border-t border-[#f3f4f6] p-2 px-3 bg-[#f8f9ff]">
      <div className="text-[9px] uppercase tracking-[1px] text-[#6b7280] mb-2 font-bold">
        Impact breakdown · {date}
      </div>

      {/* parent row */}
      <div className="flex items-center gap-2 mb-1 p-1 px-1.5 bg-white rounded border border-[#e5e7eb]">
        <span
          className="font-bold text-[13px] flex-shrink-0"
          style={{ color: pColor }}
        >
          {isDrop ? "↓" : "↑"}
        </span>
        <span className="text-[10px] font-mono text-[#374151] flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
          {parentLabel}
        </span>
        <span
          className="text-[11px] font-mono font-bold flex-shrink-0"
          style={{ color: pColor }}
        >
          {fmt(parentDelta)}
        </span>
        <span className="text-[10px] flex-shrink-0" style={{ color: pColor }}>
          ({parentPct != null && parentPct >= 0 ? "+" : ""}
          {parentPct?.toFixed(1)}%)
        </span>
      </div>

      {/* child rows */}
      {childDeltas.slice(0, 6).map((c, i) => {
        const isLast =
          i === Math.min(childDeltas.length, 6) - 1 &&
          (nonReconciling || Math.abs(gap) < Math.abs(parentDelta ?? 1) * 0.05);
        const cColor =
          c.direction === "drop"
            ? "#dc2626"
            : c.direction === "spike"
              ? "#f59e0b"
              : "#9ca3af";
        const barW = Math.min(100, Math.abs(c.pct));
        return (
          <div key={c.label} className="flex items-center gap-1.5 mb-0.5 pl-3">
            <span className="text-[#9ca3af] text-[10px] flex-shrink-0">
              {isLast ? "└─" : "├─"}
            </span>
            <span className="text-[9px] bg-[#e5e7eb] text-[#4b5563] px-1 rounded font-mono flex-shrink-0">
              {lvlS(c.level)}
            </span>
            <span className="text-[10px] font-mono text-[#374151] flex-1 min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
              {c.label}
            </span>
            {c.isAnom && (
              <span
                className="text-[9px] text-white px-1 rounded flex-shrink-0"
                style={{ background: cColor }}
              >
                ●
              </span>
            )}
            <span
              className="text-[10px] font-mono font-semibold flex-shrink-0"
              style={{ color: cColor }}
            >
              {fmt(c.delta)}
            </span>
            <div className="w-12 flex-shrink-0">
              <div className="h-1 bg-[#f3f4f6] rounded overflow-hidden">
                <div
                  className="h-full rounded"
                  style={{
                    width: `${barW}%`,
                    background: cColor,
                    opacity: 0.7,
                  }}
                />
              </div>
              <span
                className="text-[8px] block text-right"
                style={{ color: cColor }}
              >
                {c.pct >= 0 ? "+" : ""}
                {c.pct.toFixed(0)}%
              </span>
            </div>
          </div>
        );
      })}

      {/* gap / unaccounted — undefined for overlapping cuts, so suppressed */}
      {!nonReconciling &&
        childDeltas.length > 0 &&
        Math.abs(gap) > Math.abs(parentDelta ?? 1) * 0.05 && (
          <div className="pl-3 mt-0.5 text-[9px] text-[#9ca3af] flex gap-1.5 items-center">
            <span>└─</span>
            <span>
              Other / unaccounted:{" "}
              <span className="font-mono text-[#6b7280]">{fmt(gap)}</span> orders
              ({((gap / (parentDelta ?? 1)) * 100).toFixed(0)}%)
            </span>
          </div>
        )}
      {nonReconciling && childDeltas.length > 0 && (
        <div className="pl-3 mt-1 text-[9px] text-[#92400e] leading-relaxed">
          {NON_RECONCILING_CAVEAT}
        </div>
      )}
      {childDeltas.length === 0 && (
        <div className="pl-3 text-[10px] text-[#9ca3af]">
          No child data available for this date
        </div>
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Attention View Component
// ══════════════════════════════════════════════════════════════════════════════

interface AttentionViewProps {
  data: Record<string, unknown>;
  attentionData: AttentionData[];
}

export function AttentionView({ data: impData, attentionData }: AttentionViewProps) {
  // Check if real TFT importance data exists
  const hasTFT =
    impData &&
    Object.keys(impData).length > 0 &&
    Object.values(impData).some((raw) => {
      const imp = raw as TFTImportance;
      return (
        Object.keys(imp.encoder || {}).length > 0 ||
        Object.keys(imp.decoder || {}).length > 0 ||
        (imp.attention_lags || []).length > 0
      );
    });

  // Fallback: WoW-based driver analysis
  if (!hasTFT) {
    const d = attentionData || [];
    if (!d.length)
      return (
        <div className="text-center text-[#9ca3af] text-[13px] pt-12">
          No driver data. Run evaluate.py to generate feature importance.
        </div>
      );

    const topFeat = d[0];
    const topDrop = [...d].sort((a, b) => b.drops - a.drops)[0];
    const topSpike = [...d].sort((a, b) => b.spikes - a.spikes)[0];

    return (
      <div className="max-w-[640px]">
        <div className="bg-[#fef3c7] border border-[#f59e0b] rounded-md p-2 px-3 mb-3.5 text-[10px] text-[#92400e]">
          ⓘ Showing WoW-based driver analysis. Run the updated{" "}
          <code>evaluate.py</code> to unlock TFT encoder/decoder/attention
          importance.
        </div>

        {/* summary */}
        <div className="bg-[#f0f9ff] border border-[#bae6fd] rounded-md p-2.5 px-3.5 mb-4">
          <div className="text-[10px] font-bold uppercase tracking-[1px] text-[#0369a1] mb-2">
            Key insights
          </div>
          <ul className="m-0 pl-3.5 leading-[1.8]">
            <li className="text-[11px] text-[#0c4a6e]">
              <strong>{topFeat?.cleanName}</strong> is the most frequent driver
              — {topFeat?.mentions} anomalies, avg WoW change{" "}
              {topFeat?.avgPct >= 0 ? "+" : ""}
              {topFeat?.avgPct?.toFixed(0)}%.
            </li>
            {topDrop?.drops > 0 && (
              <li className="text-[11px] text-[#0c4a6e]">
                Drops mainly driven by <strong>{topDrop.cleanName}</strong> (
                {topDrop.drops}↓ of {topDrop.drops + topDrop.spikes} total).
              </li>
            )}
            {topSpike?.spikes > 0 && topSpike.feature !== topDrop?.feature && (
              <li className="text-[11px] text-[#0c4a6e]">
                Spikes mainly driven by <strong>{topSpike.cleanName}</strong> (
                {topSpike.spikes}↑).
              </li>
            )}
          </ul>
        </div>

        {/* table */}
        <div className="grid grid-cols-[1fr_80px_100px_120px] gap-2 py-1 border-b-2 border-[#e5e7eb] mb-2">
          {["Feature", "Avg Δ", "Occurrences", "Correlates with"].map((h) => (
            <span
              key={h}
              className="text-[9px] uppercase tracking-[1px] text-[#9ca3af] font-bold"
            >
              {h}
            </span>
          ))}
        </div>
        {d.map((f) => {
          const total = f.drops + f.spikes;
          const dominant =
            f.drops > f.spikes * 1.5
              ? "drop"
              : f.spikes > f.drops * 1.5
                ? "spike"
                : "mixed";
          const domColor = { drop: "#dc2626", spike: "#f59e0b", mixed: "#d97706" }[
            dominant
          ];
          return (
            <div
              key={f.feature}
              className="grid grid-cols-[1fr_80px_100px_120px] gap-2 items-center py-2 border-b border-[#f3f4f6]"
            >
              <div>
                <div className="font-semibold text-[11px] text-[#111827] mb-1">
                  {f.cleanName}
                </div>
                <div className="h-1.5 bg-[#e5e7eb] rounded overflow-hidden">
                  <div
                    className="h-full bg-[#2563eb] rounded"
                    style={{ width: `${f.barPct}%` }}
                  />
                </div>
              </div>
              <div>
                <span
                  className="text-xs font-mono font-bold"
                  style={{ color: f.avgPct >= 0 ? "#16a34a" : "#dc2626" }}
                >
                  {f.avgPct >= 0 ? "+" : ""}
                  {f.avgPct.toFixed(0)}%
                </span>
                <div className="text-[9px] text-[#9ca3af] mt-0.5">avg WoW Δ</div>
              </div>
              <div>
                <span className="text-xs font-mono font-bold text-[#374151]">
                  {f.mentions}×
                </span>
                {total > 0 && (
                  <div className="text-[9px] text-[#9ca3af] mt-0.5">
                    {f.drops}↓ {f.spikes}↑
                  </div>
                )}
              </div>
              <div>
                <div
                  className="text-[10px] font-semibold mb-0.5"
                  style={{ color: domColor }}
                >
                  {dominant === "drop"
                    ? "↓ Drops"
                    : dominant === "spike"
                      ? "↑ Spikes"
                      : "Mixed"}
                </div>
                {total > 0 && (
                  <div className="flex h-1.5 rounded overflow-hidden">
                    <div
                      className="bg-[#fca5a5] rounded-l"
                      style={{ width: `${total ? (f.drops / total) * 100 : 50}%` }}
                    />
                    <div
                      className="bg-[#86efac] rounded-r"
                      style={{ width: `${total ? (f.spikes / total) * 100 : 50}%` }}
                    />
                  </div>
                )}
              </div>
            </div>
          );
        })}
        <div className="mt-3.5 text-[9px] text-[#9ca3af] leading-[1.6]">
          Avg Δ = avg WoW change in this feature when it drove an anomaly.
          Positive can still drive a drop (e.g. high returns).
        </div>
      </div>
    );
  }

  // TFT 4-tab view placeholder (for when evaluate.py generates feature_importance)
  return (
    <div className="text-center text-[#9ca3af] text-[13px] pt-12">
      TFT feature importance view available when data is generated.
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// SeriesCard Component
// ══════════════════════════════════════════════════════════════════════════════

export function SeriesCard({
  card,
  allSeries,
  allDatesInRange,
  ts,
  selIds,
  onAddToChart,
  isFav,
  onToggleFav,
  focused,
  pinned,
  rEnd,
}: SeriesCardProps) {
  const recent = card.recentRow ?? card;

  const anomalyDates = useMemo(
    () => [...new Set(card.dates || [])].filter(Boolean).sort(),
    [card.dates]
  );

  const [impactDate, setImpactDate] = useState(
    recent._date || anomalyDates[anomalyDates.length - 1] || rEnd
  );

  // Sync impactDate when anomalyDates change
  useEffect(() => {
    if (!impactDate || (anomalyDates.length && !anomalyDates.includes(impactDate))) {
      setImpactDate(recent._date || anomalyDates[anomalyDates.length - 1] || rEnd);
    }
  }, [impactDate, anomalyDates, recent._date, rEnd]);

  const impactPt = useMemo(
    () => (ts[card.full_id] ?? []).find((p) => p.date === impactDate) ?? null,
    [ts, card.full_id, impactDate]
  );

  const impactDelta = impactPt ? impactPt.actual - impactPt.predicted : null;
  const impactPct =
    impactDelta != null && impactPt?.predicted
      ? (impactDelta / impactPt.predicted) * 100
      : null;
  const impactDirection =
    impactPt?.direction || (impactDelta != null && impactDelta < 0 ? "drop" : "spike");

  const isDrop = card.drops > card.spikes;
  const sev =
    card.maxScore >= 3 ? "#dc2626" : card.maxScore >= 1.5 ? "#d97706" : "#16a34a";
  const dirColor = arrowColor(isDrop ? "drop" : "spike");
  const label = seriesLabel(card.dims, card.level);
  const children = useMemo(
    () => directChildren(card.dims, card.level, allSeries),
    [card.dims, card.level, allSeries]
  );
  // The dimension the children add over the parent; if it's an overlapping cut
  // (e.g. SKU) the child deltas sum above the parent, so suppress the gap. §3.3.
  const childrenNonReconciling = useMemo(() => {
    if (!children.length) return false;
    const parentKeys = new Set(Object.keys(card.dims || {}));
    const childCut = Object.keys(children[0].dims || {}).find(
      (k) => !parentKeys.has(k)
    );
    return isNonReconciling(childCut);
  }, [children, card.dims]);
  const stats = useMemo(() => computeSeriesStats(ts[card.full_id]), [card.full_id, ts]);

  const childDeltas = useMemo(() => {
    if (!children.length || !ts || !impactDate || !impactPt) return [];
    const parentDelta = impactPt.actual - impactPt.predicted;
    if (Math.abs(parentDelta) < 1) return [];
    return children
      .map((c) => {
        const childPt = (ts[c.fid] ?? []).find((p) => p.date === impactDate);
        if (!childPt) return null;
        const delta = childPt.actual - childPt.predicted;
        return {
          label: c.label,
          level: c.level,
          delta,
          pct: parentDelta !== 0 ? (delta / parentDelta) * 100 : 0,
          isAnom: childPt.is_anomaly && childPt.qualified,
          direction: childPt.direction,
        };
      })
      .filter((x): x is ChildDelta => x !== null)
      .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
  }, [children, ts, impactDate, impactPt]);

  const todayPt = useMemo(
    () => (ts[card.full_id] ?? []).find((p) => p.date === rEnd) ?? null,
    [ts, card.full_id, rEnd]
  );

  const todayDelta = todayPt ? todayPt.actual - todayPt.predicted : null;
  const todayDeltaPct =
    todayDelta != null && todayPt?.predicted
      ? (todayDelta / todayPt.predicted) * 100
      : null;
  const todayStatus = todayPt
    ? todayPt.actual < todayPt.lower
      ? { label: "below band", color: "#dc2626" }
      : todayPt.actual > todayPt.upper
        ? { label: "above band", color: "#f59e0b" }
        : { label: "in band", color: "#16a34a" }
    : null;

  const isTodayAlert = rEnd === recent._date;
  const recentPt = useMemo(
    () => (ts[card.full_id] ?? []).find((p) => p.date === recent._date) ?? null,
    [ts, card.full_id, recent._date]
  );

  const _dLow = recentPt ? recentPt.actual - recentPt.lower : null;
  const _dHigh = recentPt ? recentPt.actual - recentPt.upper : null;
  const bandDiff =
    _dLow != null && _dHigh != null
      ? Math.abs(_dLow) < Math.abs(_dHigh)
        ? _dLow
        : _dHigh
      : null;
  const bandPct =
    bandDiff != null && recentPt?.predicted
      ? (bandDiff / recentPt.predicted) * 100
      : null;

  const inChart = selIds.includes(card.full_id);
  const explain = explainDriver(recent.driver);
  const safeId = "card-" + card.full_id.replace(/[^a-zA-Z0-9_-]/g, "_");

  return (
    <article
      id={safeId}
      className={`rounded-md overflow-hidden transition-all duration-300 ${
        focused
          ? "bg-[#eff6ff] border-2 border-[#2563eb]"
          : pinned
            ? "bg-[#f8faff] border border-[#bfdbfe]"
            : "bg-white border border-[#e5e7eb]"
      }`}
      style={{
        borderLeft: pinned ? "4px solid #2563eb" : `4px solid ${dirColor}`,
      }}
    >
      {/* identity row */}
      <div className="p-2 px-3 border-b border-[#f3f4f6] flex items-start gap-2">
        <span
          className="font-extrabold text-base leading-none flex-shrink-0 mt-0.5"
          style={{ color: dirColor }}
        >
          {arrowSymbol(isDrop ? "drop" : "spike")}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex gap-1 items-center mb-0.5 flex-wrap">
            <span className="text-[9px] bg-[#e5e7eb] text-[#4b5563] px-1 rounded font-mono">
              {lvlS(card.level)}
            </span>
            <span className="text-[9px] text-[#6b7280]">
              {card.flaggedDays}/{allDatesInRange.length} days
            </span>
            <span className="text-[9px]" style={{ color: arrowColor("drop") }}>
              {card.drops}↓
            </span>
            <span className="text-[9px]" style={{ color: arrowColor("spike") }}>
              {card.spikes}↑
            </span>
            {!card.qualified && (
              <span className="text-[9px] bg-[#fff7ed] text-[#c2410c] border border-[#fed7aa] px-1 rounded">
                unqualified
              </span>
            )}
            {pinned && (
              <span className="text-[9px] bg-[#eff6ff] text-[#1d4ed8] border border-[#bfdbfe] px-1 rounded font-semibold">
                in chart
              </span>
            )}
          </div>
          <p className="text-[11px] font-mono text-[#111827] leading-tight break-words">
            {label}
          </p>
        </div>
        <div className="flex flex-col gap-1 items-end flex-shrink-0">
          <span
            className="text-[11px] font-mono font-bold px-2 py-0.5 rounded"
            style={{
              background:
                card.maxScore >= 3
                  ? "#fef2f2"
                  : card.maxScore >= 1.5
                    ? "#fffbeb"
                    : "#f0fdf4",
              color: sev,
            }}
          >
            {card.maxScore}
          </span>
          <div className="flex gap-1">
            <button
              onClick={onToggleFav}
              title={isFav ? "Remove favourite" : "Add to favourites"}
              className="text-[13px] px-1 border border-[#e5e7eb] rounded cursor-pointer"
              style={{
                background: isFav ? "#fffbeb" : "#fff",
                color: isFav ? "#d97706" : "#9ca3af",
              }}
            >
              {isFav ? "★" : "☆"}
            </button>
            <button
              onClick={onAddToChart}
              title={inChart ? "Remove from chart" : "Add to chart"}
              className="text-[10px] px-1.5 py-0.5 rounded cursor-pointer"
              style={{
                border: `1px solid ${inChart ? "#2563eb" : "#d1d5db"}`,
                background: inChart ? "#eff6ff" : "#fff",
                color: inChart ? "#1d4ed8" : "#6b7280",
                fontWeight: inChart ? 600 : 400,
              }}
            >
              {inChart ? "Chart" : "+ Chart"}
            </button>
          </div>
        </div>
      </div>

      {/* mini timeline */}
      <div className="px-3 py-1 border-b border-[#f3f4f6] flex gap-0.5 flex-wrap items-center bg-[#fafafa]">
        <span className="text-[9px] text-[#9ca3af] mr-1 flex-shrink-0">Period</span>
        {allDatesInRange.map((d) => {
          const dir = card.dateDir[d];
          return (
            <span
              key={d}
              title={`${d}: ${dir || "normal"}`}
              className="w-2 h-2 rounded-sm flex-shrink-0 cursor-default"
              style={{
                background:
                  dir === "drop"
                    ? "#dc2626"
                    : dir === "spike"
                      ? "#f59e0b"
                      : "#e5e7eb",
              }}
            />
          );
        })}
        <span className="text-[9px] text-[#9ca3af] ml-1.5 flex-shrink-0">
          <span style={{ color: "#dc2626" }}>■</span>drop{" "}
          <span style={{ color: "#f59e0b" }}>■</span>spike
        </span>
      </div>

      {/* THREE columns: Today | Latest alert | Period */}
      <div className="grid grid-cols-[1fr_1px_1fr_1px_1fr]">
        <Mets
          title={`Today (${rEnd})`}
          rows={[
            ["Actual", todayPt ? fmt(todayPt.actual) : "—", "#111827"],
            ["Predicted", todayPt ? fmt(todayPt.predicted) : "—", "#9ca3af"],
            [
              "Δ",
              todayDelta != null
                ? `${todayDelta >= 0 ? "+" : ""}${Math.round(todayDelta).toLocaleString("en-IN")} (${todayDeltaPct != null && todayDeltaPct >= 0 ? "+" : ""}${todayDeltaPct?.toFixed(1)}%)`
                : "—",
              todayDelta != null ? (todayDelta < 0 ? "#dc2626" : "#16a34a") : "#9ca3af",
            ],
            ["Status", todayStatus?.label ?? "—", todayStatus?.color ?? "#9ca3af"],
          ]}
        />
        <div className="bg-[#f3f4f6]" />
        <Mets
          title={isTodayAlert ? "Latest alert = today" : `Latest alert (${recent._date || "—"})`}
          rows={[
            ["Actual", fmt(recent.actual), "#111827"],
            ["Predicted", fmt(recent.predicted), "#9ca3af"],
            [
              "Δ",
              `${fmt(recent.magnitude)} (${recent.pct_change >= 0 ? "+" : ""}${recent.pct_change}%)`,
              arrowColor(recent.direction === "drop" ? "drop" : "spike"),
            ],
            [
              "Band edge Δ",
              bandDiff != null
                ? `${bandDiff >= 0 ? "+" : ""}${Math.round(bandDiff).toLocaleString("en-IN")} (${bandPct != null && bandPct >= 0 ? "+" : ""}${bandPct?.toFixed(1)}%)`
                : "—",
              bandDiff != null ? arrowColor(isDrop ? "drop" : "spike") : "#9ca3af",
            ],
          ]}
        />
        <div className="bg-[#f3f4f6]" />
        <Mets
          title={`Period (${allDatesInRange.length}d)`}
          rows={[
            ["Flagged", `${card.flaggedDays} / ${allDatesInRange.length} days`, "#374151"],
            ["Drops / Spikes", `${card.drops}↓  ${card.spikes}↑`, "#374151"],
            [
              "Avg Δ /day",
              `${card.avgMagnitude >= 0 ? "+" : ""}${Math.round(card.avgMagnitude)}`,
              arrowColor(isDrop ? "drop" : "spike"),
            ],
            [
              "Cumulative",
              `${fmt(-Math.abs(card.cumulative_magnitude ?? 0))} orders`,
              arrowColor(isDrop ? "drop" : "spike"),
            ],
          ]}
        />
      </div>

      {/* model accuracy */}
      {stats && (
        <div className="border-t border-[#f3f4f6] px-3 py-1 bg-[#f8f9ff] flex gap-3.5 flex-wrap items-center">
          <span className="text-[9px] uppercase tracking-[1px] text-[#6b7280] font-bold">
            Model accuracy
          </span>
          {(
            [
              ["SMAPE", stats.smape, parseFloat(stats.smape) > 20 ? "#dc2626" : "#374151"],
              ["RMSE", stats.rmse, "#374151"],
              ["Avg", stats.meanActual, "#374151"],
            ] as [string, string, string][]
          ).map(([k, v, c]) => (
            <span key={k} className="text-[10px]">
              <span className="text-[#9ca3af] mr-1">{k}</span>
              <span className="font-mono font-semibold" style={{ color: c }}>
                {v}
              </span>
            </span>
          ))}
          {!stats.qualified && (
            <span className="text-[9px] bg-[#fff7ed] text-[#92400e] border border-[#fed7aa] px-1.5 rounded ml-auto">
              High error — less reliable
            </span>
          )}
        </div>
      )}

      {/* driver + WoW explanation */}
      {recent.driver && recent.driver !== "—" && (
        <div className="border-t border-[#f3f4f6] px-3 py-1.5 bg-[#fafafa]">
          <div className="flex gap-2 text-[10px] items-start">
            <span className="text-[#9ca3af] font-semibold flex-shrink-0 w-7">Why</span>
            <span className="text-[#374151] flex-1">{recent.driver}</span>
          </div>
          {explain && (
            <div className="flex gap-2 text-[10px] mt-0.5">
              <span className="text-[#9ca3af] flex-shrink-0 w-7">↳</span>
              <span className="text-[#6b7280] italic leading-relaxed">{explain}</span>
            </div>
          )}
        </div>
      )}

      {/* impact breakdown date selector */}
      {(children.length > 0 || childDeltas.length > 0) && anomalyDates.length > 0 && (
        <div className="border-t border-[#f3f4f6] px-3 py-1.5 bg-white flex items-center gap-2 flex-wrap">
          <span className="text-[9px] uppercase tracking-[1px] text-[#6b7280] font-bold">
            Impact breakdown date
          </span>
          <select
            value={impactDate || ""}
            onChange={(e) => setImpactDate(e.target.value)}
            className="text-[10px] px-2 py-0.5 border border-[#d1d5db] rounded bg-white text-[#374151]"
          >
            {anomalyDates.map((d) => (
              <option key={d} value={d}>
                {d} · {card.dateDir?.[d] || "alert"}
              </option>
            ))}
          </select>
          {impactDelta != null && (
            <span
              className="text-[10px] font-mono font-semibold"
              style={{ color: arrowColor(impactDirection) }}
            >
              Δ {impactDelta >= 0 ? "+" : ""}
              {Math.round(impactDelta).toLocaleString("en-IN")}
              {impactPct != null
                ? ` (${impactPct >= 0 ? "+" : ""}${impactPct.toFixed(1)}%)`
                : ""}
            </span>
          )}
        </div>
      )}

      {/* impact breakdown */}
      {(children.length > 0 || childDeltas.length > 0) && (
        <ImpactBreakdown
          parentLabel={label}
          parentDelta={impactDelta ?? recent.magnitude}
          parentPct={impactPct ?? recent.pct_change}
          childDeltas={childDeltas}
          date={impactDate || recent._date}
          nonReconciling={childrenNonReconciling}
        />
      )}

      {/* no-child warning for drops */}
      {children.length === 0 && isDrop && (
        <div className="border-t border-[#fed7aa] px-3 py-1.5 bg-[#fffbeb] flex gap-2 items-start text-[10px]">
          <span className="text-[#d97706] flex-shrink-0">⚠</span>
          <span className="text-[#92400e] leading-relaxed">
            {card.level === "L3_full"
              ? "Most granular level (L3) — this is the root signal, no further breakdown available."
              : "No child series in current filters. Adjust Level or BU filters to see which sub-series drive this drop."}
          </span>
        </div>
      )}
    </article>
  );
}
