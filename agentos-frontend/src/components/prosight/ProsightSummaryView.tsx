"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  ProsightNewsData,
  OpenDetailPayload,
  DirectionType,
  BaselineMode,
} from "./types";
import FlockDayCard from "./FlockDayCard";
import { directionToneSet, l0Verdict } from "./verdict";
import RangeSummaryCard from "./RangeSummaryCard";
import ActionablesCard from "./ActionablesCard";
import { resolveBu, buDay, buSeries, buFeatureImportance } from "./buSlice";
import { hasBaselines, BASELINE_MODES, BASELINE_LABELS } from "./adapters";
import { parseProsightNews } from "./newsSchema";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const L0_FULL_ID = "L0_total::total::TOTAL";

// ─────────────────────────────────────────────────────────────────────────────
// Helper Functions
// ─────────────────────────────────────────────────────────────────────────────

function pickDefaultDate(dates: string[] | undefined): string {
  if (!dates?.length) return "";
  return dates[dates.length - 1];
}

function stepDate(dates: string[], current: string, delta: number): string {
  if (!dates?.length || !current) return current;
  const idx = dates.indexOf(current);
  if (idx < 0) return current;
  const next = Math.min(dates.length - 1, Math.max(0, idx + delta));
  return dates[next];
}

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

interface ProsightSummaryViewProps {
  onOpenDetail?: (payload: OpenDetailPayload) => void;
  /** Part B: static-baseline comparison, lifted to the parent so it survives tab switches. */
  baselineMode?: BaselineMode;
  onBaselineModeChange?: (mode: BaselineMode) => void;
}

export default function ProsightSummaryView({
  onOpenDetail,
  baselineMode = "forecast",
  onBaselineModeChange,
}: ProsightSummaryViewProps) {
  const [data, setData] = useState<ProsightNewsData | null>(null);
  const [error, setError] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  useEffect(() => {
    fetch("/api/prosight/news")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((raw) => {
        // Validated, not asserted: `as ProsightNewsData` here was the gap
        // finding 4 named — a malformed payload used to reach the adapters and
        // surface as NaN. Throwing lands in the .catch below as an error panel.
        const d = parseProsightNews(raw);
        setData(d);
        const dflt = pickDefaultDate(d.dates || []);
        setStartDate(dflt);
        setEndDate(dflt);
        setSelectedBu(resolveBu(d));
      })
      .catch((err) =>
        setError(err.message || "Unable to load prosight news data")
      );
  }, []);

  const [selectedBu, setSelectedBu] = useState<string>("");
  const [selectedMetric, setSelectedMetric] = useState<string>("orders");

  const dates = data?.dates || [];
  const minDate = dates[0] || "";
  const maxDate = dates[dates.length - 1] || "";

  // Effective BU (explicit selection, else the JSON default) and its series slice.
  const bu = resolveBu(data, selectedBu);
  const l0Series = buSeries(data, bu)[L0_FULL_ID] || [];

  // BUs come straight from the JSON now (spec 27). Falls back to the resolved
  // default so the dropdown is never empty on per-BU data.
  const buOptions = useMemo(() => {
    const list = data?.bus ?? [];
    return list.length ? list : bu ? [bu] : [];
  }, [data, bu]);

  // Part B: only offer the comparison toggle when the data actually carries
  // static baselines (spec 38 §4.6) — hidden on older JSON, so no visible change.
  const baselinesAvailable = useMemo(() => hasBaselines(data), [data]);

  // Normalize: ensure start <= end before deriving the mode.
  const normalized = useMemo(() => {
    if (!startDate || !endDate) return { start: "", end: "" };
    return startDate <= endDate
      ? { start: startDate, end: endDate }
      : { start: endDate, end: startDate };
  }, [startDate, endDate]);

  const isSingleDay =
    Boolean(normalized.start) && normalized.start === normalized.end;
  const isRange =
    Boolean(normalized.start) &&
    Boolean(normalized.end) &&
    normalized.start !== normalized.end;

  // Derive the L0 verdict for the selected single day so the toolbar can
  // surface "what kind of day is this?" at a glance. Prefers
  // summary.l0_anchor (richer); falls back to l0Series timeseries.
  const dayVerdictPill = useMemo(() => {
    if (!isSingleDay || !data) return null;
    const dayData = buDay(data.data_by_date?.[normalized.start], bu);
    const summary = dayData?.summary;
    let direction: DirectionType = null;
    let known = false;
    if (summary?.l0_anchor) {
      const d = summary.l0_anchor.direction;
      direction = d === "in_band" ? null : (d as DirectionType);
      known = true;
    } else {
      const tsPoint = l0Series.find((p) => p.date === normalized.start);
      if (tsPoint) {
        const v = l0Verdict(tsPoint);
        if (v) {
          direction = v.direction;
          known = true;
        }
      }
    }
    if (!known) return null;
    const tone = directionToneSet(direction);
    const label =
      direction === "spike"
        ? "SPIKE DAY"
        : direction === "drop"
          ? "DROP DAY"
          : direction === "mixed"
            ? "MIXED"
            : "IN BAND";
    return {
      arrow: tone.arrow,
      bg: tone.pillBg,
      text: tone.pillText,
      border: tone.pillBorder,
      label,
    };
  }, [isSingleDay, data, normalized.start, l0Series, bu]);

  const quickRange = (days: number | "all") => {
    if (!dates.length) return;
    if (days === "all") {
      setStartDate(dates[0]);
      setEndDate(dates[dates.length - 1]);
      return;
    }
    setEndDate(dates[dates.length - 1]);
    setStartDate(dates[Math.max(0, dates.length - days)]);
  };

  const activeQuickRange = useMemo(() => {
    if (!dates.length || !normalized.start || !normalized.end) return null;
    const last = dates[dates.length - 1];
    if (normalized.end !== last) return null;
    if (normalized.start === dates[0]) return "all";
    for (const days of [7, 14, 30]) {
      if (normalized.start === dates[Math.max(0, dates.length - days)])
        return days;
    }
    return null;
  }, [dates, normalized.start, normalized.end]);

  const setLatestDay = () => {
    const last = dates[dates.length - 1];
    if (!last) return;
    setStartDate(last);
    setEndDate(last);
  };

  const drillToDay = (date: string) => {
    if (!date) return;
    setStartDate(date);
    setEndDate(date);
  };

  const stepSingleDay = (delta: number) => {
    if (!isSingleDay) return;
    const next = stepDate(dates, normalized.start, delta);
    setStartDate(next);
    setEndDate(next);
  };

  if (error) {
    return (
      <div className="h-full flex items-center justify-center bg-ink-50 text-sm text-red-600">
        Could not load data: {error}
      </div>
    );
  }
  if (!data) {
    return (
      <div className="h-full flex items-center justify-center bg-ink-50 text-sm text-ink-400">
        Loading summary...
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-white px-4 py-3">
      <div className="max-w-screen-2xl mx-auto space-y-3">
        {/* ACTIONABLES — for the selected BU and day (end of range) */}
        <ActionablesCard bu={bu} date={normalized.end} />

        {/* TOOLBAR */}
        <div className="rounded-md border border-ink-200 bg-white shadow-sm p-3 flex flex-wrap items-end gap-3">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-[2px] text-ink-400 font-semibold flex items-center gap-1.5">
              <span
                className="inline-block w-1.5 h-1.5 rounded-sm bg-blue-600"
                aria-hidden="true"
              />
              PROSIGHT - daily anomaly report
            </div>
            <div className="text-base font-semibold text-ink-800 mt-1 flex items-center gap-3 flex-wrap">
              {isSingleDay ? (
                <span>
                  Single day .{" "}
                  <span className="font-mono">{normalized.start}</span>
                </span>
              ) : isRange ? (
                <span>
                  Range .{" "}
                  <span className="font-mono">
                    {normalized.start} - {normalized.end}
                  </span>
                </span>
              ) : (
                <span>Pick a date</span>
              )}
              {dayVerdictPill && (
                <span
                  className={`inline-flex items-center gap-1 rounded border font-semibold uppercase tracking-wide text-[11px] px-2 py-0.5 ${dayVerdictPill.bg} ${dayVerdictPill.text} ${dayVerdictPill.border}`}
                >
                  <span aria-hidden="true">{dayVerdictPill.arrow}</span>
                  {dayVerdictPill.label}
                </span>
              )}
            </div>
          </div>

          <label className="text-[10px] text-ink-500 uppercase tracking-wide font-medium flex flex-col gap-1">
            BU
            <select
              value={selectedBu}
              onChange={(e) => setSelectedBu(e.target.value)}
              disabled={!buOptions.length}
              className="text-[11px] rounded border border-ink-200 bg-white px-2 py-1.5 text-ink-700 normal-case tracking-normal min-w-[8rem] disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                buOptions.length
                  ? "Filter by Business Unit"
                  : "No BU breakdown available in current data"
              }
            >
              {buOptions.map((b) => (
                <option key={b} value={b}>
                  {b.charAt(0).toUpperCase() + b.slice(1)}
                </option>
              ))}
            </select>
          </label>

          <label className="text-[10px] text-ink-500 uppercase tracking-wide font-medium flex flex-col gap-1">
            Metric
            <select
              value={selectedMetric}
              onChange={(e) => setSelectedMetric(e.target.value)}
              className="text-[11px] rounded border border-ink-200 bg-white px-2 py-1.5 text-ink-700 normal-case tracking-normal min-w-[8rem]"
              title="Filter by metric"
            >
              <option value="orders">Orders</option>
            </select>
          </label>

          {isSingleDay && baselinesAvailable && (
            <div className="text-[10px] text-ink-500 uppercase tracking-wide font-medium flex flex-col gap-1">
              Compare
              <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
                {BASELINE_MODES.map((m) => {
                  const active = baselineMode === m;
                  return (
                    <button
                      key={m}
                      type="button"
                      onClick={() => onBaselineModeChange?.(m)}
                      className={`px-2 py-1 rounded text-[11px] normal-case tracking-normal transition-colors ${
                        active
                          ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                          : "text-ink-600 hover:bg-white hover:shadow-sm"
                      }`}
                      title={`Compare against ${BASELINE_LABELS[m]}`}
                    >
                      {BASELINE_LABELS[m]}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <div className="ml-auto flex flex-wrap items-end gap-3">
            <label className="text-[10px] text-ink-500 uppercase tracking-wide font-medium flex flex-col gap-1">
              From
              <input
                type="date"
                min={minDate}
                max={maxDate}
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="text-[11px] rounded border border-ink-200 bg-white px-2 py-1.5 text-ink-700 font-mono normal-case tracking-normal"
              />
            </label>

            <label className="text-[10px] text-ink-500 uppercase tracking-wide font-medium flex flex-col gap-1">
              To
              <input
                type="date"
                min={minDate}
                max={maxDate}
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="text-[11px] rounded border border-ink-200 bg-white px-2 py-1.5 text-ink-700 font-mono normal-case tracking-normal"
              />
            </label>

            <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
              <button
                type="button"
                disabled={!isSingleDay}
                onClick={() => stepSingleDay(-1)}
                title="Previous day"
                className={`px-2 py-1 rounded text-[12px] ${isSingleDay ? "text-ink-700 hover:bg-white hover:shadow-sm" : "text-ink-300 cursor-not-allowed"}`}
              >
                &larr;
              </button>
              <button
                type="button"
                disabled={!isSingleDay}
                onClick={() => stepSingleDay(1)}
                title="Next day"
                className={`px-2 py-1 rounded text-[12px] ${isSingleDay ? "text-ink-700 hover:bg-white hover:shadow-sm" : "text-ink-300 cursor-not-allowed"}`}
              >
                &rarr;
              </button>
            </div>

            <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
              {(
                [
                  ["7d", 7],
                  ["14d", 14],
                  ["30d", 30],
                  ["All", "all"],
                ] as const
              ).map(([label, days]) => {
                const isActive = activeQuickRange === days;
                return (
                  <button
                    key={label}
                    type="button"
                    onClick={() => quickRange(days)}
                    className={`px-2 py-1 rounded text-[11px] transition-colors ${
                      isActive
                        ? "bg-blue-50 text-blue-800 ring-1 ring-blue-600/30 font-semibold"
                        : "text-ink-600 hover:bg-white hover:shadow-sm"
                    }`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>

            <button
              type="button"
              onClick={setLatestDay}
              className="px-2.5 py-1.5 rounded border border-ink-200 bg-white text-ink-700 text-[11px] hover:bg-ink-50"
            >
              Latest day
            </button>
          </div>
        </div>

        {/* CARD */}
        {isSingleDay ? (
          <FlockDayCard
            date={normalized.start}
            dayData={buDay(data.data_by_date?.[normalized.start], bu)}
            l0Series={l0Series}
            seriesTimeseries={buSeries(data, bu)}
            featureImportance={buFeatureImportance(data, bu)}
            hideNoAnomaly={false}
            baselineMode={baselineMode}
            data={data}
            bu={bu}
            onOpenDetail={onOpenDetail}
          />
        ) : isRange ? (
          <RangeSummaryCard
            startDate={normalized.start}
            endDate={normalized.end}
            data={data}
            bu={bu}
            onOpenDetail={onOpenDetail}
            onDrillDay={drillToDay}
          />
        ) : (
          <div className="rounded-md border border-ink-200 bg-white p-6 text-center text-[12px] text-ink-400">
            Pick a start and end date.
          </div>
        )}
      </div>
    </div>
  );
}
