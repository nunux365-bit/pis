"use client";

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  EmailAutomationMetricsResponse,
  EmailAutomationMetricsWindow,
} from "@/lib/api";

const WINDOW_OPTIONS: { id: EmailAutomationMetricsWindow; label: string }[] = [
  { id: "24h", label: "24h" },
  { id: "7d", label: "7d" },
  { id: "30d", label: "30d" },
];

function humanizeStatus(key: string) {
  return key.replaceAll("_", " ");
}

/** Primary skip reason code for display (first `review_reasons[]` item on the server). */
function formatSkipReasonCode(key: string) {
  return key.replace(/^skip:/, "").replaceAll("_", " ");
}

/** Failure bucket from `EmailAutomationSend.error` (type, short message, or `long_error`). */
function formatFailedReasonKey(key: string) {
  if (key === "long_error") return "Long error message";
  if (key === "unknown") return "Unknown";
  const spaced = key.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  return spaced.replaceAll("_", " ");
}

function formatBucketLabel(iso: string, granularity: string | undefined) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return granularity === "hour"
    ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Non-negative integer for charts / tiles; bad API values become 0 (never NaN). */
function safeMetric(n: unknown): number {
  const x = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(x) || x < 0) return 0;
  return Math.floor(x);
}

type ThroughputChartRow = {
  /** Shown on axis (may collide across DST / data quirks); prefer bucketIso for tooltip title. */
  name: string;
  bucketIso: string;
  received: number;
  sent: number;
  failed: number;
};

const SERIES_ORDER = ["received", "sent", "failed"] as const;
const SERIES_LABEL: Record<(typeof SERIES_ORDER)[number], string> = {
  received: "Received",
  sent: "Sent",
  failed: "Failed",
};

const SERIES_COLOR: Record<(typeof SERIES_ORDER)[number], string> = {
  received: "var(--accent-blue)",
  sent: "var(--accent-green)",
  failed: "var(--accent-red)",
};

/** Observability-style tooltip: full bucket row (all series), same colours as the bars. */
function ThroughputTooltip(props: {
  active?: boolean;
  payload?: readonly { payload?: ThroughputChartRow; dataKey?: unknown; value?: unknown; color?: string }[];
  label?: string;
  granularity?: string;
}) {
  const { active, payload, label, granularity } = props;
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  const iso = typeof row.bucketIso === "string" ? row.bucketIso : "";
  const title =
    iso && !Number.isNaN(new Date(iso).getTime())
      ? formatBucketLabel(iso, granularity)
      : (label ?? row.name ?? "—");
  return (
    <div
      className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]/98 px-3 py-2.5 text-left shadow-lg backdrop-blur-sm ring-1 ring-black/[0.06]"
      style={{ minWidth: 172 }}
    >
      <p className="mb-2 border-b border-[var(--border)]/80 pb-1.5 text-xs font-semibold text-[var(--text-primary)]">
        {title}
      </p>
      <div className="space-y-1.5">
        {SERIES_ORDER.map((key) => {
          const v = safeMetric(row[key]);
          const fill = SERIES_COLOR[key];
          return (
            <div key={key} className="flex items-center justify-between gap-6 text-[11px]">
              <span className="flex items-center gap-2 text-[var(--text-secondary)]">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-sm ring-1 ring-black/[0.08]"
                  style={{ backgroundColor: fill }}
                  aria-hidden
                />
                {SERIES_LABEL[key]}
              </span>
              <span className="tabular-nums font-semibold text-[var(--text-primary)]">{v}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TrendChart({
  trend,
  granularity,
}: {
  trend: EmailAutomationMetricsResponse["trend"];
  granularity: string | undefined;
}) {
  const chartData = useMemo<ThroughputChartRow[]>(() => {
    const seen = new Map<string, number>();
    return trend.map((p) => {
      const base = formatBucketLabel(p.bucket, granularity);
      const n = (seen.get(base) ?? 0) + 1;
      seen.set(base, n);
      const name = n > 1 ? `${base} · ${n}` : base;
      return {
        name,
        bucketIso: p.bucket,
        received: safeMetric(p.ingested),
        sent: safeMetric(p.sent),
        failed: safeMetric(p.failed),
      };
    });
  }, [trend, granularity]);

  const manyTicks = chartData.length > 16;

  if (trend.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--bg-elev)]/30 px-3 py-2 text-xs text-[var(--text-muted)]">
        No timeline buckets returned.
      </div>
    );
  }

  const totalVol = trend.reduce(
    (s, p) => s + safeMetric(p.ingested) + safeMetric(p.sent) + safeMetric(p.failed),
    0
  );
  const allBucketsZero = totalVol === 0;

  return (
    <div className="relative overflow-visible rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)]/90 p-2 sm:p-2.5 shadow-[0_1px_0_rgba(15,23,42,0.04)] ring-1 ring-black/[0.02]">
      {allBucketsZero && (
        <p className="mb-2 rounded-lg border border-[var(--border)]/80 bg-[var(--bg-elev)]/50 px-2.5 py-1.5 text-[10px] leading-snug text-[var(--text-secondary)]">
          <span className="font-medium text-[var(--text-primary)]">Quiet period.</span> No ingested,
          sent, or failed events in the chart buckets for this range — the headline totals use the
          same series, so they should also read zero.
        </p>
      )}
      <div
        className="h-[240px] w-full min-h-[200px] sm:h-[260px] [&_.recharts-tooltip-wrapper]:pointer-events-none [&_.recharts-tooltip-wrapper]:z-20 [&_.recharts-surface]:outline-none"
        role="img"
        aria-label="Chart of received, sent, and failed mail per time period"
      >
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            margin={{ top: 8, right: 8, left: 0, bottom: manyTicks ? 8 : 4 }}
            barCategoryGap="14%"
            barGap={3}
          >
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="currentColor"
              className="text-[var(--border)]"
              vertical={false}
              opacity={0.9}
            />
            <XAxis
              dataKey="name"
              tick={{ fill: "var(--text-muted)", fontSize: 10 }}
              tickLine={false}
              axisLine={{ stroke: "var(--border)" }}
              interval={manyTicks ? "preserveStartEnd" : 0}
              angle={manyTicks ? -32 : 0}
              textAnchor={manyTicks ? "end" : "middle"}
              height={manyTicks ? 52 : 28}
            />
            <YAxis
              width={40}
              allowDecimals={false}
              tick={{ fill: "var(--text-muted)", fontSize: 10 }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              shared
              content={<ThroughputTooltip granularity={granularity} />}
              cursor={{ fill: "var(--accent-blue)", opacity: 0.07 }}
              wrapperStyle={{ outline: "none" }}
            />
            <Bar
              dataKey="received"
              name="Received"
              fill="var(--accent-blue)"
              radius={[2, 2, 0, 0]}
              maxBarSize={42}
            />
            <Bar dataKey="sent" name="Sent" fill="var(--accent-green)" radius={[2, 2, 0, 0]} maxBarSize={42} />
            <Bar
              dataKey="failed"
              name="Failed"
              fill="var(--accent-red)"
              radius={[2, 2, 0, 0]}
              maxBarSize={42}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 border-t border-[var(--border)]/60 pt-2.5 text-[11px] text-[var(--text-secondary)]">
        <span className="inline-flex items-center gap-2">
          <span className="h-3 w-2.5 shrink-0 rounded-[2px] bg-[var(--accent-blue)] ring-1 ring-[var(--accent-blue)]/25" />
          Received
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="h-3 w-2.5 shrink-0 rounded-[2px] bg-[var(--accent-green)] ring-1 ring-emerald-600/20" />
          Sent
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="h-3 w-2.5 shrink-0 rounded-[2px] bg-[var(--accent-red)] ring-1 ring-[var(--accent-red)]/25" />
          Failed
        </span>
      </div>
    </div>
  );
}

function StatusDistribution({
  title,
  rows,
  accentClass,
  formatKey = humanizeStatus,
}: {
  title: string;
  rows: [string, number][];
  accentClass: string;
  formatKey?: (k: string) => string;
}) {
  const max = Math.max(1, ...rows.map(([, v]) => v));
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02]">
      <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--text-muted)]">
        {title}
      </h4>
      {rows.length === 0 ? (
        <p className="text-xs text-[var(--text-muted)]">No rows in this window.</p>
      ) : (
        <ul className="space-y-2.5">
          {rows.map(([k, n]) => {
            const pct = (n / max) * 100;
            return (
              <li key={k}>
                <div className="flex items-baseline justify-between gap-3 mb-1">
                  <span
                    className="text-[13px] font-medium text-[var(--text-primary)] capitalize truncate"
                    title={k}
                  >
                    {formatKey(k)}
                  </span>
                  <span className="tabular-nums text-sm font-semibold text-[var(--text-primary)] shrink-0">
                    {n}
                  </span>
                </div>
                <div className="h-2 rounded-full bg-[var(--bg-elev)] overflow-hidden ring-1 ring-inset ring-black/[0.03]">
                  <div
                    className={`h-full rounded-full bg-gradient-to-r ${accentClass} motion-safe:transition-all motion-safe:duration-700 ease-out`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function PanelSkeleton() {
  return (
    <div className="space-y-5 animate-pulse" aria-hidden>
      <div className="flex flex-wrap gap-3">
        <div className="h-9 w-40 rounded-full bg-[var(--bg-elev)]" />
        <div className="h-9 flex-1 min-w-[120px] rounded-full bg-[var(--bg-elev)]" />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-20 rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
          />
        ))}
      </div>
      <div className="h-36 rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]" />
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="h-36 rounded-xl bg-[var(--bg-elev)]" />
        <div className="h-36 rounded-xl bg-[var(--bg-elev)]" />
      </div>
    </div>
  );
}

export function EmailAutomationMetricsPanel({
  window: activeWindow,
  onWindowChange,
  metrics,
  error,
}: {
  window: EmailAutomationMetricsWindow;
  onWindowChange: (w: EmailAutomationMetricsWindow) => void;
  metrics: EmailAutomationMetricsResponse | null;
  error: string | null;
}) {
  const derived = useMemo(() => {
    const trend = metrics?.trend ?? [];
    const trendSums = trend.reduce(
      (acc, p) => ({
        ingested: acc.ingested + safeMetric(p.ingested),
        sent: acc.sent + safeMetric(p.sent),
        failed: acc.failed + safeMetric(p.failed),
      }),
      { ingested: 0, sent: 0, failed: 0 }
    );
    // KPI row uses the same gap-filled `trend` series as the bar chart (sums match tiles).
    const att = metrics?.attention;
    const attentionTotal =
      (att?.sends_at_attempt_cap ?? 0) +
      (att?.messages_processed_with_errors_open ?? 0) +
      (att?.sends_stuck_sending ?? 0);
    const msgTop = metrics
      ? Object.entries(metrics.messages_by_status)
          .map(([k, v]) => [k, safeMetric(v)] as [string, number])
          .sort((a, b) => b[1] - a[1])
          .slice(0, 6)
      : [];
    const sendTop = metrics
      ? Object.entries(metrics.sends_by_status)
          .map(([k, v]) => [k, safeMetric(v)] as [string, number])
          .sort((a, b) => b[1] - a[1])
          .slice(0, 6)
      : [];
    const topVariants = metrics
      ? Object.entries(metrics.sends_by_variant)
          .map(([variant, byStatus]) => ({
            variant,
            total: Object.values(byStatus).reduce((s, n) => s + safeMetric(n), 0),
            byStatus: Object.fromEntries(
              Object.entries(byStatus).map(([k, v]) => [k, safeMetric(v)])
            ),
          }))
          .sort((a, b) => b.total - a.total)
          .slice(0, 4)
      : [];
    const skippedByReasonTop = metrics
      ? Object.entries(metrics.sends_skipped_by_reason ?? {})
          .map(([k, v]) => [k, safeMetric(v)] as [string, number])
          .filter(([, v]) => v > 0)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 10)
      : [];
    const failedByReasonTop3 = metrics
      ? Object.entries(metrics.sends_failed_by_reason ?? {})
          .map(([k, v]) => [k, safeMetric(v)] as [string, number])
          .filter(([, v]) => v > 0)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 3)
      : [];
    return {
      trend,
      trendSums,
      att,
      attentionTotal,
      msgTop,
      sendTop,
      topVariants,
      skippedByReasonTop,
      failedByReasonTop3,
    };
  }, [metrics]);

  const loading = !metrics && !error;

  return (
    <section className="ea-metrics-panel relative z-0 isolate overflow-hidden rounded-3xl border border-[var(--border)] shadow-[0_18px_50px_-24px_rgba(15,23,42,0.18)] ring-1 ring-black/[0.03]">
      <div
        className="ea-metrics-hero pointer-events-none absolute inset-0 opacity-100"
        aria-hidden
      />
      <div className="relative px-4 pb-4 pt-5 sm:px-5 sm:pb-5 sm:pt-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between md:gap-5">
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-card)]/90 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-[var(--text-muted)] shadow-sm backdrop-blur-sm">
                Operations
              </span>
              {metrics?.health.enabled && !metrics.health.test_mode && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/25 bg-emerald-500/[0.08] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-800">
                  <span className="relative flex h-2 w-2">
                    <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-40" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
                  </span>
                  Live
                </span>
              )}
              {metrics?.health.enabled && metrics.health.test_mode && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-400/30 bg-amber-400/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-amber-900">
                  Test mode
                </span>
              )}
              {metrics && !metrics.health.enabled && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--accent-red)]/30 bg-[rgba(217,79,69,0.08)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--accent-red)]">
                  Paused
                </span>
              )}
            </div>
            <div>
              <h2 className="text-lg font-semibold tracking-tight text-[var(--text-primary)] sm:text-xl">
                Email automation
              </h2>
              <p className="mt-0.5 max-w-xl text-[13px] leading-snug text-[var(--text-secondary)]">
                Aggregate pipeline health — no recipient-level data on this surface.
              </p>
            </div>
            {metrics && (
              <p className="text-xs text-[var(--text-muted)]">
                Snapshot{" "}
                <time dateTime={metrics.generated_at}>
                  {Number.isNaN(Date.parse(metrics.generated_at))
                    ? "—"
                    : new Date(metrics.generated_at).toLocaleString(undefined, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      })}
                </time>
              </p>
            )}
          </div>

          <div
            className="flex shrink-0 rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-0.5 shadow-inner backdrop-blur-md"
            role="group"
            aria-label="Metrics time window"
          >
            {WINDOW_OPTIONS.map(({ id, label }) => {
              const active = activeWindow === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => onWindowChange(id)}
                  className={`relative z-10 cursor-pointer rounded-lg px-3 py-1.5 text-[11px] font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-blue)]/35 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-card)] ${
                    active
                      ? "bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-sm ring-1 ring-black/[0.06]"
                      : "text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-elev)]/80"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-5">
          {error && (
            <div
              className="mb-6 flex gap-3 rounded-2xl border border-[var(--accent-red)]/25 bg-[rgba(217,79,69,0.06)] px-4 py-3.5 text-sm text-[var(--text-primary)]"
              role="alert"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border border-[var(--accent-red)]/20 bg-[var(--bg-card)] text-[var(--accent-red)]">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path
                    d="M12 9v4M12 17h.01M10.3 4.6h3.4l7.1 12.8H3.2l7.1-12.8z"
                    stroke="currentColor"
                    strokeWidth="1.75"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <div>
                <p className="font-semibold text-[var(--text-primary)]">Could not load metrics</p>
                <p className="mt-0.5 text-[var(--text-secondary)]">{error}</p>
              </div>
            </div>
          )}

          {loading && <PanelSkeleton />}

          {metrics && (
            <div className="ea-panel-content-in space-y-4">
              {metrics.health.enabled && metrics.health.test_mode && (
                <div className="rounded-lg border border-amber-400/25 bg-amber-400/[0.06] px-3 py-2 text-[11px] leading-snug text-amber-950">
                  <span className="font-semibold">Test mode.</span> Outbound recipients may be
                  redirected. <span className="font-medium">Metrics are identical to live</span>{" "}
                  (same tables and aggregation); test mode does not change these counts — only where
                  mail is delivered.
                </div>
              )}

              {derived.attentionTotal > 0 && derived.att && (
                <div
                  className="relative overflow-hidden rounded-xl border border-amber-500/25 bg-gradient-to-br from-amber-50/90 via-[var(--bg-card)] to-[var(--bg-card)] p-3 shadow-sm ring-1 ring-amber-500/10"
                  role="status"
                >
                  <div className="absolute right-0 top-0 h-24 w-24 translate-x-6 -translate-y-6 rounded-full bg-amber-400/10 blur-2xl" aria-hidden />
                  <div className="relative flex flex-wrap items-start gap-2.5">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-amber-500/15 text-amber-800">
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
                        <path
                          d="M12 9v4M12 17h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                          stroke="currentColor"
                          strokeWidth="1.75"
                          strokeLinecap="round"
                        />
                      </svg>
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-[13px] font-semibold text-[var(--text-primary)]">
                        Attention needed
                      </p>
                      <ul className="mt-1.5 space-y-1 text-[12px] text-[var(--text-secondary)]">
                        {derived.att.sends_at_attempt_cap > 0 && (
                          <li className="flex flex-wrap gap-x-2">
                            <span className="tabular-nums font-semibold text-amber-950">
                              {derived.att.sends_at_attempt_cap}
                            </span>
                            <span>at send retry cap</span>
                          </li>
                        )}
                        {derived.att.messages_processed_with_errors_open > 0 && (
                          <li className="flex flex-wrap gap-x-2">
                            <span className="tabular-nums font-semibold text-amber-950">
                              {derived.att.messages_processed_with_errors_open}
                            </span>
                            <span>messages with errors (open)</span>
                          </li>
                        )}
                        {derived.att.sends_stuck_sending > 0 && (
                          <li className="flex flex-wrap gap-x-2">
                            <span className="tabular-nums font-semibold text-amber-950">
                              {derived.att.sends_stuck_sending}
                            </span>
                            <span>sends stuck in sending</span>
                          </li>
                        )}
                      </ul>
                    </div>
                  </div>
                </div>
              )}

              <div className="xl:grid xl:grid-cols-12 xl:items-start xl:gap-5">
                <div className="space-y-3 xl:col-span-7">
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    <div className="group relative overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] transition motion-safe:duration-200 hover:shadow-md hover:ring-[var(--accent-blue)]/12">
                      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-[var(--accent-blue)]/50 to-[var(--accent-purple)]/40" />
                      <div className="flex items-start justify-between gap-1">
                        <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--text-muted)]">
                          Inbox mail
                        </span>
                        <span className="rounded-md border border-[var(--border)] bg-[var(--bg-elev)]/80 p-1 text-[var(--accent-blue)]">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                            <path
                              d="M4 6h16v12H4V6zm0 0l8 6 8-6"
                              stroke="currentColor"
                              strokeWidth="1.5"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </span>
                      </div>
                      <p className="mt-2 tabular-nums text-2xl font-semibold tracking-tight text-[var(--text-primary)]">
                        {derived.trendSums.ingested}
                      </p>
                      <p className="mt-0.5 text-[10px] leading-tight text-[var(--text-muted)]">
                        Sum of received (blue) across all buckets in this range
                      </p>
                    </div>
                    <div className="group relative overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] transition motion-safe:duration-200 hover:shadow-md hover:ring-emerald-500/15">
                      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-[var(--accent-green)] to-cyan-500/70" />
                      <div className="flex items-start justify-between gap-1">
                        <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--text-muted)]">
                          Sent
                        </span>
                        <span className="rounded-md border border-[var(--border)] bg-[var(--bg-elev)]/80 p-1 text-[var(--accent-green)]">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                            <path
                              d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"
                              stroke="currentColor"
                              strokeWidth="1.5"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </span>
                      </div>
                      <p className="mt-2 tabular-nums text-2xl font-semibold tracking-tight text-emerald-700">
                        {derived.trendSums.sent}
                      </p>
                      <p className="mt-0.5 text-[10px] leading-tight text-[var(--text-muted)]">
                        Successfully sent
                      </p>
                    </div>
                    <div className="group relative overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] transition motion-safe:duration-200 hover:shadow-md hover:ring-[var(--accent-red)]/15">
                      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-orange-400 to-[var(--accent-red)]" />
                      <div className="flex items-start justify-between gap-1">
                        <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--text-muted)]">
                          Failed
                        </span>
                        <span className="rounded-md border border-[var(--border)] bg-[var(--bg-elev)]/80 p-1 text-[var(--accent-red)]">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                            <path
                              d="M18 6L6 18M6 6l12 12"
                              stroke="currentColor"
                              strokeWidth="1.75"
                              strokeLinecap="round"
                            />
                          </svg>
                        </span>
                      </div>
                      <p className="mt-2 tabular-nums text-2xl font-semibold tracking-tight text-[var(--accent-red)]">
                        {derived.trendSums.failed}
                      </p>
                      <p className="mt-0.5 text-[10px] leading-tight text-[var(--text-muted)]">
                        Sends that failed
                      </p>
                    </div>
                  </div>

                  <p className="text-[10px] leading-snug text-[var(--text-muted)]">
                    The chart is one group of bars per{" "}
                    {metrics.bucket_granularity === "hour" ? "hour" : "calendar day"}. The three KPIs
                    above are{" "}
                    <span className="font-medium text-[var(--text-secondary)]">
                      totals for the whole selected range
                    </span>{" "}
                    (sum of every chart bucket—for example, 30d adds all 30 daily buckets), not one day
                    in isolation.
                  </p>

                  <div>
                    <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
                      <div>
                        <h3 className="text-[13px] font-semibold text-[var(--text-primary)]">
                          Throughput trend
                        </h3>
                        <p className="text-[10px] text-[var(--text-muted)]">
                          Hover a bucket to see received, sent, and failed for that hour or day.
                        </p>
                      </div>
                    </div>
                    <TrendChart
                      trend={derived.trend}
                      granularity={metrics.bucket_granularity}
                    />
                  </div>

                  {(metrics.health.last_message_received_at || metrics.health.last_send_sent_at) && (
                    <div className="flex flex-col gap-2 rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)]/60 p-2.5 sm:flex-row sm:items-stretch sm:gap-2">
                  <div className="flex flex-1 items-center gap-3 rounded-xl border border-[var(--border)]/80 bg-[var(--bg-card)] px-3 py-2.5 shadow-sm">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--accent-blue)]/10 text-[var(--accent-blue)]">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
                        <path
                          d="M4 6h16v12H4V6zm0 0l8 6 8-6"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>
                    <div className="min-w-0">
                      <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                        Last inbound
                      </p>
                      <p className="truncate text-sm font-medium text-[var(--text-primary)]">
                        {metrics.health.last_message_received_at
                          ? new Date(metrics.health.last_message_received_at).toLocaleString(
                              undefined,
                              { dateStyle: "medium", timeStyle: "short" }
                            )
                          : "—"}
                      </p>
                    </div>
                  </div>
                  <div className="flex flex-1 items-center gap-3 rounded-xl border border-[var(--border)]/80 bg-[var(--bg-card)] px-3 py-2.5 shadow-sm">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--accent-green)]/10 text-[var(--accent-green)]">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
                        <path
                          d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </span>
                    <div className="min-w-0">
                      <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                        Last outbound send
                      </p>
                      <p className="truncate text-sm font-medium text-[var(--text-primary)]">
                        {metrics.health.last_send_sent_at
                          ? new Date(metrics.health.last_send_sent_at).toLocaleString(undefined, {
                              dateStyle: "medium",
                              timeStyle: "short",
                            })
                          : "—"}
                      </p>
                    </div>
                  </div>
                </div>
              )}
                </div>

                <aside className="mt-4 space-y-3 xl:mt-0 xl:col-span-5">
                  <StatusDistribution
                    title="Messages by status"
                    rows={derived.msgTop}
                    accentClass="from-[var(--accent-blue)]/90 to-[var(--accent-purple)]/80"
                  />
                  <StatusDistribution
                    title="Sends by status"
                    rows={derived.sendTop}
                    accentClass="from-[var(--accent-green)] to-[var(--accent-cyan)]/90"
                  />
                  {derived.trendSums.failed > 0 && (
                    <StatusDistribution
                      title="Top send failure reasons"
                      rows={derived.failedByReasonTop3}
                      formatKey={formatFailedReasonKey}
                      accentClass="from-orange-500/80 to-[var(--accent-red)]/85"
                    />
                  )}
                  <StatusDistribution
                    title="Skipped by reason"
                    rows={derived.skippedByReasonTop}
                    formatKey={formatSkipReasonCode}
                    accentClass="from-amber-500/80 to-[var(--accent-red)]/70"
                  />
                  {derived.topVariants.length > 0 && (
                    <div>
                      <h3 className="mb-1 text-[13px] font-semibold text-[var(--text-primary)]">
                        Workflow variants
                      </h3>
                      <p className="mb-2 text-[10px] text-[var(--text-muted)]">
                        Highest outbound volume in this window
                      </p>
                      <div className="space-y-2">
                        {derived.topVariants.map(({ variant, total, byStatus }) => (
                          <div
                            key={variant}
                            className="relative overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] motion-safe:transition-shadow motion-safe:duration-200 hover:shadow-md"
                          >
                            <div className="absolute left-0 top-0 h-full w-0.5 bg-gradient-to-b from-[var(--accent-purple)]/70 to-[var(--accent-blue)]/50" />
                            <div className="pl-2">
                              <div className="flex items-start justify-between gap-2">
                                <p className="break-all font-mono text-[12px] font-medium text-[var(--text-primary)]">
                                  {variant}
                                </p>
                                <span className="shrink-0 rounded-full bg-[var(--bg-elev)] px-2 py-0.5 text-[10px] font-semibold tabular-nums text-[var(--text-secondary)] ring-1 ring-[var(--border)]">
                                  {total}
                                </span>
                              </div>
                              <div className="mt-2 flex flex-wrap gap-1">
                                {Object.entries(byStatus)
                                  .sort((a, b) => b[1] - a[1])
                                  .map(([st, c]) => (
                                    <span
                                      key={st}
                                      className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)]/90 px-1.5 py-0.5 text-[10px] text-[var(--text-secondary)]"
                                    >
                                      <span className="capitalize">{humanizeStatus(st)}</span>
                                      <span className="tabular-nums font-semibold text-[var(--text-primary)]">
                                        {c}
                                      </span>
                                    </span>
                                  ))}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </aside>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
