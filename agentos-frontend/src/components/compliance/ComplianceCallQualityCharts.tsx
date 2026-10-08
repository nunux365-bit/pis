"use client";

import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  ComposedChart,
} from "recharts";
import type {
  ComplianceGradeDistributionResponse,
  ComplianceRunSummary,
  ComplianceTimeseriesResponse,
} from "@/lib/api";
import { complianceGradeChartFill, complianceGradeSortKey } from "@/lib/complianceGradeVisual";

const CHART_COLORS = {
  completed: "var(--chart-completed, #34d399)",
  failed: "var(--chart-failed, #f87171)",
  other: "var(--chart-other, #94a3b8)",
};

function formatBucketLabel(iso: string, granularity: "day" | "week"): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  if (granularity === "week") {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Sum ``completed`` in the last ``n`` buckets vs the preceding ``n`` buckets (daily / weekly series). */
function completedDeltaWindow(points: { completed: number }[], n: number) {
  if (points.length < n * 2) return null;
  const recent = points.slice(-n);
  const prior = points.slice(-2 * n, -n);
  const sum = (arr: { completed: number }[]) => arr.reduce((a, p) => a + p.completed, 0);
  const a = sum(recent);
  const b = sum(prior);
  if (b <= 0 && a <= 0) return null;
  const pct = b > 0 ? ((a - b) / b) * 100 : a > 0 ? 100 : 0;
  return { recent: a, prior: b, pct };
}

type Props = {
  timeseries: ComplianceTimeseriesResponse | null;
  grades: ComplianceGradeDistributionResponse | null;
  loading: boolean;
  chartDays: number;
  onChartDaysChange: (days: number) => void;
  granularity: "day" | "week";
  onGranularityChange: (g: "day" | "week") => void;
  /** Rolling calendar summary from API (e.g. last 30 days). */
  summary30: ComplianceRunSummary | null;
  /** Runs matching current table filters (paginated total). */
  tableTotal: number;
  tableLoading: boolean;
};

export function ComplianceCallQualityCharts({
  timeseries,
  grades,
  loading,
  chartDays,
  onChartDaysChange,
  granularity,
  onGranularityChange,
  summary30,
  tableTotal,
  tableLoading,
}: Props) {
  const chartWindowTotals = useMemo(() => {
    const pts = timeseries?.points ?? [];
    return pts.reduce(
      (acc, p) => ({
        total: acc.total + p.total,
        completed: acc.completed + p.completed,
        failed: acc.failed + p.failed,
      }),
      { total: 0, completed: 0, failed: 0 }
    );
  }, [timeseries]);

  const gradedInWindow = useMemo(
    () => (grades?.slices ?? []).reduce((n, s) => n + s.count, 0),
    [grades]
  );

  const chartData = useMemo(() => {
    const pts = timeseries?.points ?? [];
    return pts.map((p) => ({
      ...p,
      label: formatBucketLabel(p.bucket_start, timeseries?.granularity ?? granularity),
      other: Math.max(0, p.total - p.completed - p.failed),
    }));
  }, [timeseries, granularity]);

  const wow = useMemo(() => {
    const pts = timeseries?.points ?? [];
    const n = granularity === "day" ? 7 : 4;
    return completedDeltaWindow(pts, n);
  }, [timeseries, granularity]);

  const pieData = useMemo(() => {
    const slices = [...(grades?.slices ?? [])];
    slices.sort((a, b) => complianceGradeSortKey(a.grade) - complianceGradeSortKey(b.grade));
    return slices.map((s) => ({
      name: s.grade || "—",
      value: s.count,
      fill: complianceGradeChartFill(s.grade || "—"),
    }));
  }, [grades]);

  const s30c = summary30?.completed ?? 0;
  const s30f = summary30?.failed ?? 0;
  const s30all = s30c + s30f;
  const failRate30 = s30all > 0 ? Math.round((s30f / s30all) * 1000) / 10 : null;

  return (
    <section
      className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm sm:p-3.5"
      aria-labelledby="compliance-charts-heading"
      aria-busy={loading}
    >
      <div className="flex flex-col gap-2.5 border-b border-[var(--border)]/80 pb-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <h2
            id="compliance-charts-heading"
            className="text-[15px] font-semibold tracking-tight text-[var(--text-primary)]"
          >
            Volume & grades
          </h2>
          <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)]">
            Bars = runs per {granularity} (UTC). Pie = letter grades for <span className="text-[var(--text-secondary)]">completed</span>{" "}
            runs in the chart window. KPIs: 30d roll-up vs selected window vs your table filters.
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Key metrics">
            <span className="inline-flex items-center gap-1.5 rounded-md border border-emerald-600/25 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-950/40 dark:text-emerald-100">
              <span className="opacity-75">30d</span>
              <span className="tabular-nums">{summary30 == null ? "—" : s30all}</span>
              <span className="text-emerald-700/80 dark:text-emerald-200/90">ok {summary30 == null ? "—" : s30c}</span>
              {s30f > 0 ? (
                <span className="tabular-nums text-rose-700 dark:text-rose-300">fail {s30f}</span>
              ) : (
                <span className="text-emerald-700/70 dark:text-emerald-300/80">fail 0</span>
              )}
            </span>
            {failRate30 != null && s30f > 0 ? (
              <span className="inline-flex items-center rounded-md border border-amber-500/30 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-950 dark:border-amber-500/25 dark:bg-amber-950/35 dark:text-amber-100">
                30d fail rate {failRate30}%
              </span>
            ) : null}
            <span className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
              <span className="text-[var(--text-muted)]">{chartDays}d view</span>
              <span className="tabular-nums font-medium text-[var(--text-primary)]">{chartWindowTotals.total}</span>
              <span className="text-[var(--text-muted)]">runs</span>
            </span>
            <span className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)]">
              <span className="text-[var(--text-muted)]">graded</span>
              <span className="tabular-nums font-medium text-[var(--text-primary)]">{gradedInWindow}</span>
            </span>
            <span className="inline-flex items-center gap-1 rounded-md border border-sky-500/25 bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-950 dark:border-sky-500/30 dark:bg-sky-950/40 dark:text-sky-100">
              <span className="text-sky-800/80 dark:text-sky-200/90">Table</span>
              <span className="tabular-nums">{tableLoading ? "…" : tableTotal}</span>
              <span className="font-normal opacity-80">match</span>
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
            <span className="whitespace-nowrap">Window</span>
            <select
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-xs outline-none focus:border-[var(--border-active)]"
              value={String(chartDays)}
              onChange={(e) => onChartDaysChange(Number(e.target.value))}
              aria-label="Chart time window in days"
            >
              <option value="14">14 days</option>
              <option value="30">30 days</option>
              <option value="90">90 days</option>
            </select>
          </label>
          <fieldset className="flex items-center gap-2 border-0 p-0">
            <legend className="sr-only">Bucket size</legend>
            {(["day", "week"] as const).map((g) => (
              <label
                key={g}
                className={`cursor-pointer rounded-md border px-2 py-1 text-xs font-medium ${
                  granularity === g
                    ? "border-sky-700 bg-sky-100 text-sky-950 dark:border-sky-400/70 dark:bg-sky-950/40 dark:text-sky-50"
                    : "border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                }`}
              >
                <input
                  type="radio"
                  className="sr-only"
                  name="granularity"
                  checked={granularity === g}
                  onChange={() => onGranularityChange(g)}
                />
                {g === "day" ? "Daily" : "Weekly"}
              </label>
            ))}
          </fieldset>
        </div>
      </div>

      {wow && (
        <p className="mt-2 text-[11px] text-[var(--text-secondary)]" role="status">
          <span className="font-medium text-[var(--text-primary)]">Completed trend:</span>{" "}
          {wow.recent} latest {granularity === "day" ? "7d" : "4 wk"} vs {wow.prior} prior —{" "}
          <span className={wow.pct >= 0 ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-300"}>
            {wow.pct >= 0 ? "+" : ""}
            {Math.round(wow.pct * 10) / 10}%
          </span>
        </p>
      )}

      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {loading
          ? "Loading charts."
          : `Charts: ${chartWindowTotals.total} runs in selected window, ${chartWindowTotals.completed} completed, ${chartWindowTotals.failed} failed. ${gradedInWindow} graded letter grades in window.`}
      </p>

      {loading ? (
        <p className="mt-4 text-xs text-[var(--text-muted)]">Loading charts…</p>
      ) : (
        <div className="mt-3 grid gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <h3 id="compliance-chart-volume-title" className="sr-only">
              Runs per time bucket
            </h3>
            <div className="h-[200px] w-full min-h-[160px]" role="img" aria-labelledby="compliance-chart-volume-title">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="label" tick={{ fontSize: 10 }} stroke="var(--text-muted)" />
                  <YAxis allowDecimals={false} tick={{ fontSize: 10 }} stroke="var(--text-muted)" width={32} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: "var(--bg-elev)",
                      border: "1px solid var(--border)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    labelFormatter={(_, payload) => {
                      const p = payload?.[0]?.payload as { bucket_start?: string } | undefined;
                      return p?.bucket_start ? new Date(p.bucket_start).toLocaleString() : "";
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="completed" name="Completed" stackId="a" fill={CHART_COLORS.completed} />
                  <Bar dataKey="failed" name="Failed" stackId="a" fill={CHART_COLORS.failed} />
                  <Bar dataKey="other" name="Other" stackId="a" fill={CHART_COLORS.other} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="flex flex-col items-center lg:col-span-2">
            <h3 id="compliance-chart-grades-title" className="sr-only">
              Letter grade distribution for completed runs
            </h3>
            <div className="h-[180px] w-full max-w-[240px]" role="img" aria-labelledby="compliance-chart-grades-title">
              {pieData.length === 0 ? (
                <p className="text-[11px] text-[var(--text-muted)]">No graded completed runs in this window.</p>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={40}
                      outerRadius={68}
                      paddingAngle={1}
                    >
                      {pieData.map((entry, index) => (
                        <Cell key={`cell-${entry.name}-${index}`} fill={entry.fill} stroke="var(--bg-card)" />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "var(--bg-elev)",
                        border: "1px solid var(--border)",
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Legend layout="horizontal" verticalAlign="bottom" wrapperStyle={{ fontSize: 10 }} />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
