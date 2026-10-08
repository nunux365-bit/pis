"use client";

/** @deprecated Part of the deprecated Replies tab. */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  EmailIntelligenceMetricsResponse,
  EmailIntelligenceThreadRow,
  EmailIntelligenceWindow,
} from "@/lib/api";
import {
  getEmailIntelligenceMetrics,
  getEmailIntelligenceThreads,
} from "@/lib/api";

const PAGE_SIZE = 25;

const WINDOW_OPTIONS: { id: EmailIntelligenceWindow; label: string; priorHint: string }[] = [
  { id: "24h", label: "24h", priorHint: "vs prior 24h" },
  { id: "7d", label: "7d", priorHint: "vs prior 7d (week-over-week style)" },
  { id: "30d", label: "30d", priorHint: "vs prior 30d (month-over-month style)" },
];

const CATEGORY_CHART_COLORS = [
  "var(--accent-green)",
  "var(--accent-blue)",
  "var(--accent-purple)",
  "var(--accent-cyan)",
  "var(--accent-orange)",
  "var(--accent-red)",
];

function formatCategoryLabel(slug: string) {
  return slug.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function gmailThreadUrl(threadId: string) {
  const id = encodeURIComponent(threadId);
  return `https://mail.google.com/mail/u/0/#all/${id}`;
}

function formatClassified(iso: string): { abs: string; rel: string } {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { abs: iso, rel: "—" };
  const abs = d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const sec = Math.round((d.getTime() - Date.now()) / 1000);
  const divisions: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 60 * 60 * 24 * 365],
    ["month", 60 * 60 * 24 * 30],
    ["week", 60 * 60 * 24 * 7],
    ["day", 60 * 60 * 24],
    ["hour", 60 * 60],
    ["minute", 60],
  ];
  let rel = "";
  for (const [unit, div] of divisions) {
    if (Math.abs(sec) >= div || unit === "minute") {
      rel = rtf.format(Math.round(sec / div), unit);
      break;
    }
  }
  return { abs, rel };
}

function truncateMiddle(s: string, max = 22) {
  if (s.length <= max) return s;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

function confidenceTone(c: string) {
  const x = (c || "").toLowerCase();
  if (x === "high")
    return "border-emerald-200/90 bg-emerald-50/90 text-emerald-900 ring-1 ring-emerald-900/10";
  if (x === "medium")
    return "border-amber-200/90 bg-amber-50/90 text-amber-950 ring-1 ring-amber-900/10";
  if (x === "low")
    return "border-red-200/90 bg-red-50/90 text-red-950 ring-1 ring-red-900/10";
  return "border-[var(--border)] bg-[var(--bg-elev)] text-[var(--text-secondary)] ring-1 ring-black/[0.04]";
}

function formatBucketLabel(iso: string, granularity: "hour" | "day" | undefined) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return granularity === "hour"
    ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function safeMetric(n: unknown): number {
  const x = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(x) || x < 0) return 0;
  return Math.floor(x);
}

/** Human-readable delta for KPI sublines. */
function formatDelta(current: number, prior: number): string {
  if (prior <= 0 && current <= 0) return "Flat vs prior window";
  if (prior <= 0) return "Up from zero in prior window";
  const pct = Math.round(((current - prior) / prior) * 1000) / 10;
  const dir = pct > 0 ? "Up" : pct < 0 ? "Down" : "Flat";
  return `${dir} ${pct >= 0 ? "+" : ""}${pct}% vs prior`;
}

type TimelineRow = { name?: string; bucketIso: string; count: number };

function TimelineTooltip({
  active,
  payload,
  granularity,
}: {
  active?: boolean;
  payload?: readonly { payload?: TimelineRow }[];
  granularity?: "hour" | "day";
}) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  const title =
    row.name && row.name.trim() !== ""
      ? row.name
      : formatBucketLabel(row.bucketIso, granularity);
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]/98 px-3 py-2 text-left text-[12px] shadow-lg ring-1 ring-black/[0.06] backdrop-blur-sm">
      <p className="mb-1 text-[11px] font-semibold text-[var(--text-primary)]">{title}</p>
      <p className="tabular-nums text-[var(--text-secondary)]">
        <span className="font-semibold text-[var(--text-primary)]">{row.count}</span> classified
      </p>
    </div>
  );
}

export function CollectionsReplyIntelligencePanel({
  timeWindow,
  onTimeWindowChange,
}: {
  timeWindow: EmailIntelligenceWindow;
  onTimeWindowChange: (w: EmailIntelligenceWindow) => void;
}) {
  const [metrics, setMetrics] = useState<EmailIntelligenceMetricsResponse | null>(null);
  const [threads, setThreads] = useState<EmailIntelligenceThreadRow[]>([]);
  const [threadsCursor, setThreadsCursor] = useState<string | null>(null);
  const [threadsHasMore, setThreadsHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [threadsLoadingMore, setThreadsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [searchApplied, setSearchApplied] = useState("");

  const windowMeta = WINDOW_OPTIONS.find((w) => w.id === timeWindow);

  useEffect(() => {
    const t = globalThis.setTimeout(() => setSearchApplied(searchInput.trim()), 320);
    return () => globalThis.clearTimeout(t);
  }, [searchInput]);

  const loadInitial = useCallback(async () => {
    const [m, t] = await Promise.all([
      getEmailIntelligenceMetrics(timeWindow),
      getEmailIntelligenceThreads({
        limit: PAGE_SIZE,
        window: timeWindow,
        category: categoryFilter ?? undefined,
        search: searchApplied || undefined,
      }),
    ]);
    setMetrics(m);
    setThreads(t.items);
    setThreadsCursor(t.next_cursor);
    setThreadsHasMore(t.has_more);
  }, [timeWindow, categoryFilter, searchApplied]);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sync UI with in-flight fetch
    setLoading(true);
    setError(null);
    loadInitial()
      .then(() => {
        if (!cancelled) setError(null);
      })
      .catch((e) => {
        if (!cancelled) {
          setMetrics(null);
          setThreads([]);
          setThreadsCursor(null);
          setThreadsHasMore(false);
          setError(e instanceof Error ? e.message : "Could not load intelligence.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadInitial]);

  const sortedCategories = useMemo(() => {
    if (!metrics) return [];
    return Object.entries(metrics.by_category).sort((a, b) => b[1] - a[1]);
  }, [metrics]);
  const granularity = metrics?.bucket_granularity ?? "day";
  const prior = metrics?.prior_period;

  const timelineChartData = useMemo(() => {
    const timeline = metrics?.timeline ?? [];
    const seen = new Map<string, number>();
    return timeline.map((p) => {
      const base = formatBucketLabel(p.bucket, granularity);
      const n = (seen.get(base) ?? 0) + 1;
      seen.set(base, n);
      const name = n > 1 ? `${base} · ${n}` : base;
      return {
        name,
        bucketIso: p.bucket,
        count: safeMetric(p.count),
      } satisfies TimelineRow;
    });
  }, [metrics, granularity]);

  const categoryChartData = useMemo(() => {
    return sortedCategories.map(([slug, count]) => ({
      slug,
      label: formatCategoryLabel(slug),
      count: safeMetric(count),
    }));
  }, [sortedCategories]);

  const categoryCompareData = useMemo(() => {
    if (!metrics) return [];
    const cur = metrics.by_category;
    const pcat = prior?.by_category ?? {};
    const keys = new Set([...Object.keys(cur), ...Object.keys(pcat)]);
    const rows = [...keys].map((slug) => ({
      slug,
      label: formatCategoryLabel(slug),
      current: safeMetric(cur[slug]),
      prior: safeMetric(pcat[slug]),
    }));
    rows.sort((a, b) => b.current + b.prior - (a.current + a.prior));
    return rows;
  }, [metrics, prior?.by_category]);

  const summaryLine = useMemo(() => {
    if (!metrics) return "";
    const gen = new Date(metrics.generated_at);
    const genStr = Number.isNaN(gen.getTime())
      ? metrics.generated_at
      : gen.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    return `Updated ${genStr} · ${metrics.window} · ${granularity === "hour" ? "hourly" : "daily"} buckets`;
  }, [metrics, granularity]);

  const listSummary = useMemo(() => {
    if (!metrics) return "";
    const loaded = threads.length;
    const parts: string[] = [];
    if (searchApplied) parts.push(`Search “${searchApplied}”`);
    if (categoryFilter) parts.push(`category ${formatCategoryLabel(categoryFilter)}`);
    const filterNote = parts.length ? ` (${parts.join(" · ")})` : "";

    if (categoryFilter || searchApplied) {
      const tail = threadsHasMore ? " — more below" : "";
      return `${loaded} thread${loaded === 1 ? "" : "s"} loaded${filterNote}${tail}`;
    }
    const total = metrics.total_threads;
    if (loaded >= total && !threadsHasMore) {
      return `All ${total} thread${total === 1 ? "" : "s"} in this window${filterNote}.`;
    }
    return `Showing ${loaded} of ${total} thread${total === 1 ? "" : "s"}${filterNote}${threadsHasMore ? " — keyset pages, oldest last" : ""
      }.`;
  }, [metrics, threads.length, threadsHasMore, categoryFilter, searchApplied]);

  const byConf = metrics?.by_confidence ?? {};
  const lowPrior = safeMetric(prior?.low_confidence_count);
  const highCount = safeMetric(byConf.high);
  const medCount = safeMetric(byConf.medium);
  const lowCount = safeMetric(byConf.low);

  const currentBarLabel = timeWindow === "24h" ? "Last 24h" : timeWindow === "7d" ? "Last 7d" : "Last 30d";
  const priorBarLabel = timeWindow === "24h" ? "Prior 24h" : timeWindow === "7d" ? "Prior 7d" : "Prior 30d";

  return (
    <div className="intel-replies-panel-in flex flex-col gap-5">
      <div className="intel-replies-hero overflow-hidden rounded-2xl border border-[var(--border)] shadow-[0_1px_0_rgba(15,23,42,0.04)] ring-1 ring-black/[0.03]">
        <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="min-w-0 flex-1">
            <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-[var(--text-muted)]">
              Collections reply intelligence
            </p>
            <p className="mt-1.5 max-w-[40rem] text-[13px] leading-relaxed text-[var(--text-secondary)]">
              Classifications for threads tied to payment-reminder sends.
            </p>
          </div>
          <div className="flex shrink-0 flex-col gap-2 sm:items-end" role="group" aria-label="Time window">
            <span className="text-[11px] font-medium text-[var(--text-muted)]">Time window</span>
            <div className="inline-flex rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/90 p-0.5 shadow-sm">
              {WINDOW_OPTIONS.map(({ id, label }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => onTimeWindowChange(id)}
                  className={
                    timeWindow === id
                      ? "relative z-10 cursor-pointer rounded-lg bg-[var(--text-primary)] px-3 py-2 text-[12px] font-semibold text-white shadow-sm"
                      : "relative z-10 cursor-pointer rounded-lg px-3 py-2 text-[12px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]/80"
                  }
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div
          className="rounded-xl border border-red-200/90 bg-red-50/80 px-3 py-2.5 text-[13px] text-red-950 shadow-sm ring-1 ring-red-900/10"
          role="alert"
        >
          {error}
        </div>
      )}

      {loading && !metrics && (
        <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading intelligence">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
              />
            ))}
          </div>
          <div className="h-56 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]" />
          <div className="h-64 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]" />
        </div>
      )}

      {metrics && (
        <>
          <p className="text-[11px] text-[var(--text-muted)]">{summaryLine}</p>
          {windowMeta && (
            <p className="-mt-3 text-[10px] text-[var(--text-muted)]">{windowMeta.priorHint}</p>
          )}

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-3.5">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                Threads
              </p>
              <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight text-[var(--text-primary)]">
                {metrics.total_threads}
              </p>
              <p className="mt-1 text-[11px] leading-snug text-[var(--text-muted)]">
                {prior ? formatDelta(metrics.total_threads, prior.total_threads) : "—"}
              </p>
            </div>
            <div
              className={`rounded-xl border p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-3.5 ${metrics.low_confidence_count > 0
                  ? "border-amber-200/90 bg-amber-50/45 ring-amber-900/8"
                  : "border-[var(--border)] bg-[var(--bg-card)]"
                }`}
            >
              <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                Low confidence
              </p>
              <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight text-[var(--text-primary)]">
                {metrics.low_confidence_count}
              </p>
              <p className="mt-1 text-[11px] leading-snug text-[var(--text-muted)]">
                {prior ? formatDelta(metrics.low_confidence_count, lowPrior) : "—"}
              </p>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-3.5">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                Confidence
              </p>
              <p className="mt-2 text-[13px] font-medium tabular-nums leading-snug text-[var(--text-primary)]">
                <span className="text-emerald-800">High {highCount}</span>
                <span className="mx-1.5 text-[var(--text-muted)]">·</span>
                <span className="text-amber-900">Med {medCount}</span>
                <span className="mx-1.5 text-[var(--text-muted)]">·</span>
                <span className="text-red-900">Low {lowCount}</span>
              </p>
              <p className="mt-1.5 truncate font-mono text-[10px] text-[var(--text-muted)]" title={metrics.kind}>
                {metrics.kind}
              </p>
            </div>
          </div>

          {(metrics.timeline?.length ?? 0) > 0 && (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)]/95 p-2 shadow-sm ring-1 ring-black/[0.02] sm:p-3">
              <div className="mb-2 px-1">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                  Classifications over time
                </h3>
                <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
                  Volume in the selected window ({granularity === "hour" ? "UTC hours" : "UTC days"}).
                </p>
              </div>
              <div
                className="h-[220px] w-full min-h-[200px] sm:h-[240px] [&_.recharts-tooltip-wrapper]:pointer-events-none [&_.recharts-tooltip-wrapper]:z-[400] [&_.recharts-surface]:outline-none"
                role="img"
                aria-label="Classified thread counts over time"
              >
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={timelineChartData} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
                    <defs>
                      <linearGradient id="intelAreaFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--accent-blue)" stopOpacity={0.28} />
                        <stop offset="100%" stopColor="var(--accent-blue)" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="currentColor"
                      className="text-[var(--border)]"
                      vertical={false}
                      opacity={0.85}
                    />
                    <XAxis
                      dataKey="name"
                      tick={{ fill: "var(--text-muted)", fontSize: 10 }}
                      tickLine={false}
                      axisLine={{ stroke: "var(--border)" }}
                      interval={timelineChartData.length > 18 ? "preserveStartEnd" : 0}
                      angle={timelineChartData.length > 12 ? -28 : 0}
                      textAnchor={timelineChartData.length > 12 ? "end" : "middle"}
                      height={timelineChartData.length > 12 ? 48 : 28}
                    />
                    <YAxis
                      width={36}
                      allowDecimals={false}
                      tick={{ fill: "var(--text-muted)", fontSize: 10 }}
                      tickLine={false}
                      axisLine={false}
                    />
                    <Tooltip
                      content={<TimelineTooltip granularity={granularity} />}
                      cursor={{ stroke: "var(--accent-blue)", strokeWidth: 1, strokeDasharray: "4 4" }}
                      wrapperStyle={{ outline: "none" }}
                    />
                    <Area
                      type="monotone"
                      dataKey="count"
                      name="Classified"
                      stroke="var(--accent-blue)"
                      strokeWidth={2}
                      fill="url(#intelAreaFill)"
                      activeDot={{ r: 4, strokeWidth: 0, fill: "var(--accent-blue)" }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {sortedCategories.length > 0 && (
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                  Categories (this window)
                </h3>
                <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
                  Click a slice to filter threads.
                </p>
                <div className="mt-2 h-[260px] w-full min-h-[220px] sm:h-[280px] [&_.recharts-tooltip-wrapper]:pointer-events-none [&_.recharts-surface]:outline-none">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                      <Pie
                        data={categoryChartData}
                        dataKey="count"
                        nameKey="label"
                        cx="50%"
                        cy="46%"
                        innerRadius="44%"
                        outerRadius="74%"
                        paddingAngle={2}
                        cursor="pointer"
                        onClick={(_entry: unknown, index?: number) => {
                          const row =
                            typeof index === "number" ? categoryChartData[index] : undefined;
                          if (!row?.slug) return;
                          setCategoryFilter((prev) => (prev === row.slug ? null : row.slug));
                        }}
                      >
                        {categoryChartData.map((_, i) => (
                          <Cell
                            key={categoryChartData[i].slug}
                            fill={CATEGORY_CHART_COLORS[i % CATEGORY_CHART_COLORS.length]}
                            stroke={
                              categoryFilter === categoryChartData[i].slug
                                ? "var(--border-active)"
                                : "var(--bg-card)"
                            }
                            strokeWidth={categoryFilter === categoryChartData[i].slug ? 2 : 1}
                          />
                        ))}
                      </Pie>
                      <Tooltip
                        content={({ active, payload }) => {
                          if (!active || !payload?.length) return null;
                          const row = payload[0].payload as {
                            label: string;
                            count: number;
                            slug: string;
                          };
                          const total = Math.max(1, metrics.total_threads);
                          const pct = Math.round((row.count / total) * 1000) / 10;
                          return (
                            <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[11px] shadow-md">
                              <span className="font-semibold text-[var(--text-primary)]">{row.label}</span>
                              <div className="mt-0.5 tabular-nums text-[var(--text-secondary)]">
                                {row.count} ({pct}%)
                              </div>
                            </div>
                          );
                        }}
                        wrapperStyle={{ outline: "none" }}
                      />
                      <Legend
                        verticalAlign="bottom"
                        height={40}
                        formatter={(value) => (
                          <span className="text-[11px] text-[var(--text-secondary)]">{value}</span>
                        )}
                        wrapperStyle={{ paddingTop: 4 }}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              </div>

              <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                  Categories vs prior window
                </h3>
                <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
                  Same UTC length immediately before this window. Use 7d for week-style, 30d for
                  month-style comparisons.
                </p>
                {categoryCompareData.length === 0 ? (
                  <p className="mt-6 text-[12px] text-[var(--text-muted)]">No category data.</p>
                ) : (
                  <div className="mt-2 h-[260px] w-full min-h-[220px] sm:h-[280px] [&_.recharts-tooltip-wrapper]:pointer-events-none [&_.recharts-surface]:outline-none">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        layout="vertical"
                        data={categoryCompareData}
                        margin={{ top: 4, right: 12, left: 4, bottom: 4 }}
                      >
                        <CartesianGrid
                          strokeDasharray="3 3"
                          stroke="currentColor"
                          className="text-[var(--border)]"
                          horizontal={false}
                          opacity={0.85}
                        />
                        <XAxis
                          type="number"
                          allowDecimals={false}
                          tick={{ fontSize: 10, fill: "var(--text-muted)" }}
                        />
                        <YAxis
                          type="category"
                          dataKey="label"
                          width={100}
                          tick={{ fontSize: 10, fill: "var(--text-secondary)" }}
                          tickLine={false}
                          axisLine={false}
                        />
                        <Tooltip
                          cursor={{ fill: "var(--accent-green)", opacity: 0.06 }}
                          content={({ active, payload }) => {
                            if (!active || !payload?.length) return null;
                            const row = payload[0]?.payload as {
                              label: string;
                              current: number;
                              prior: number;
                              slug: string;
                            };
                            if (!row) return null;
                            return (
                              <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[11px] shadow-md">
                                <p className="font-semibold text-[var(--text-primary)]">{row.label}</p>
                                <p className="mt-1 tabular-nums text-[var(--text-secondary)]">
                                  {currentBarLabel}: {row.current}
                                </p>
                                <p className="tabular-nums text-[var(--text-secondary)]">
                                  {priorBarLabel}: {row.prior}
                                </p>
                              </div>
                            );
                          }}
                          wrapperStyle={{ outline: "none" }}
                        />
                        <Legend
                          verticalAlign="top"
                          align="right"
                          wrapperStyle={{ fontSize: 11, paddingBottom: 4 }}
                        />
                        <Bar
                          dataKey="current"
                          name={currentBarLabel}
                          fill="var(--accent-blue)"
                          radius={[0, 4, 4, 0]}
                          maxBarSize={14}
                          cursor="pointer"
                          onClick={(_e: unknown, index?: number) => {
                            const row =
                              typeof index === "number" ? categoryCompareData[index] : undefined;
                            if (!row?.slug) return;
                            setCategoryFilter((prev) => (prev === row.slug ? null : row.slug));
                          }}
                        />
                        <Bar
                          dataKey="prior"
                          name={priorBarLabel}
                          fill="var(--text-muted)"
                          radius={[0, 4, 4, 0]}
                          maxBarSize={14}
                          opacity={0.85}
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="flex flex-col gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                  Threads
                </p>
                <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
                  Search and filters apply to the list below.
                </p>
              </div>
              <label className="flex w-full min-w-0 flex-col gap-1 sm:max-w-sm" htmlFor="intel-thread-search">
                <span className="sr-only">Search threads</span>
                <input
                  id="intel-thread-search"
                  type="search"
                  autoComplete="off"
                  placeholder="Thread id, business key, workflow…"
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2 text-[13px] text-[var(--text-primary)] shadow-inner outline-none transition placeholder:text-[var(--text-muted)] focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/35"
                />
              </label>
            </div>

            <div>
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                Category
              </p>
              <div className="flex flex-wrap gap-1.5">
                <button
                  type="button"
                  onClick={() => setCategoryFilter(null)}
                  className={`rounded-full border px-3 py-1.5 text-[12px] font-medium transition ${categoryFilter === null
                      ? "border-[var(--border-active)] bg-[var(--accent-green)]/12 text-[var(--text-primary)] ring-1 ring-[var(--accent-green)]/25"
                      : "border-[var(--border)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                    }`}
                >
                  All
                </button>
                {sortedCategories.map(([name]) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setCategoryFilter(name)}
                    className={`max-w-[220px] truncate rounded-full border px-3 py-1.5 text-[12px] font-medium transition ${categoryFilter === name
                        ? "border-[var(--border-active)] bg-[var(--accent-green)]/12 text-[var(--text-primary)] ring-1 ring-[var(--accent-green)]/25"
                        : "border-[var(--border)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                      }`}
                    title={formatCategoryLabel(name)}
                  >
                    {formatCategoryLabel(name)}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </>
      )}

      {metrics && threads.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-[12px] leading-snug text-[var(--text-secondary)]">{listSummary}</p>
          <div className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-[13px]">
                <caption className="sr-only">
                  Classified Gmail threads for collections reply intelligence
                </caption>
                <thead className="sticky top-0 z-[1] border-b border-[var(--border)] bg-[var(--bg-elev)]/95 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)] shadow-[0_1px_0_0_var(--border)] backdrop-blur-sm">
                  <tr>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Category</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Confidence</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Gmail thread</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Workflow</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Business key</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Variant</th>
                    <th className="whitespace-nowrap px-3 py-2.5 sm:px-4">Classified</th>
                  </tr>
                </thead>
                <tbody>
                  {threads.map((r, idx) => {
                    const { abs, rel } = formatClassified(r.classified_at);
                    return (
                      <tr
                        key={r.id}
                        className={
                          idx % 2 === 0
                            ? "border-b border-[var(--border)]/50 bg-[var(--bg-card)] hover:bg-[var(--bg-elev)]/50"
                            : "border-b border-[var(--border)]/50 bg-[var(--bg-primary)]/40 hover:bg-[var(--bg-elev)]/50"
                        }
                      >
                        <td className="px-3 py-2.5 font-medium text-[var(--text-primary)] sm:px-4">
                          {formatCategoryLabel(r.category)}
                        </td>
                        <td className="px-3 py-2.5 sm:px-4">
                          <span
                            className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold capitalize ${confidenceTone(r.confidence)}`}
                          >
                            {r.confidence}
                          </span>
                        </td>
                        <td className="max-w-[200px] px-3 py-2.5 sm:max-w-[240px] sm:px-4">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <code
                              className="truncate font-mono text-[11px] text-[var(--text-secondary)]"
                              title={r.gmail_thread_id}
                            >
                              {truncateMiddle(r.gmail_thread_id)}
                            </code>
                            <button
                              type="button"
                              className="shrink-0 rounded border border-[var(--border)] bg-[var(--bg-card)] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)] hover:border-[var(--accent-green)]/40 hover:text-[var(--text-primary)]"
                              onClick={() => {
                                void navigator.clipboard.writeText(r.gmail_thread_id).then(() => {
                                  setCopiedId(r.id);
                                  globalThis.setTimeout(
                                    () => setCopiedId((x) => (x === r.id ? null : x)),
                                    1600
                                  );
                                });
                              }}
                            >
                              {copiedId === r.id ? "Copied" : "Copy"}
                            </button>
                            <a
                              href={gmailThreadUrl(r.gmail_thread_id)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="shrink-0 text-[11px] font-semibold text-[var(--accent-blue)] hover:underline"
                            >
                              Gmail
                            </a>
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[var(--text-secondary)] sm:px-4">
                          {r.workflow_type ?? "—"}
                        </td>
                        <td className="px-3 py-2.5 font-mono text-[12px] text-[var(--text-secondary)] sm:px-4">
                          {r.business_key ?? "—"}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[var(--text-secondary)] sm:px-4">
                          {r.variant ?? "—"}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-[var(--text-secondary)] sm:px-4">
                          <span title={abs}>{rel}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-[11px] tabular-nums text-[var(--text-muted)]">
              Loaded {threads.length} row{threads.length === 1 ? "" : "s"}
              {metrics ? ` · ${PAGE_SIZE} per fetch` : ""}
              {threadsHasMore ? " · more available" : " · end of list"}
            </p>
            {threadsHasMore && threadsCursor && (
              <button
                type="button"
                disabled={threadsLoadingMore}
                onClick={() => {
                  setThreadsLoadingMore(true);
                  getEmailIntelligenceThreads({
                    limit: PAGE_SIZE,
                    window: timeWindow,
                    category: categoryFilter ?? undefined,
                    search: searchApplied || undefined,
                    cursor: threadsCursor,
                  })
                    .then((page) => {
                      setThreads((prev) => [...prev, ...page.items]);
                      setThreadsCursor(page.next_cursor);
                      setThreadsHasMore(page.has_more);
                    })
                    .catch((e) => {
                      setError(
                        e instanceof Error ? e.message : "Could not load more threads."
                      );
                    })
                    .finally(() => setThreadsLoadingMore(false));
                }}
                className="inline-flex min-h-[44px] items-center justify-center rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-5 py-2.5 text-[13px] font-semibold text-[var(--text-primary)] shadow-sm transition hover:border-[var(--border-active)]/45 hover:bg-[var(--bg-elev)] disabled:opacity-50"
              >
                {threadsLoadingMore ? "Loading…" : "Load more"}
              </button>
            )}
          </div>
        </div>
      )}

      {metrics && metrics.total_threads === 0 && !error && !loading && (
        <p className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--bg-elev)]/40 px-3 py-3 text-[13px] text-[var(--text-secondary)]">
          No classified threads in this window yet. Enable collections intelligence and ensure
          sends have{" "}
          <code className="font-mono text-[12px] text-[var(--text-primary)]">gmail_thread_id</code>{" "}
          populated after dispatch, or run the anchor backfill if inbound messages are not
          ingested locally.
        </p>
      )}

      {metrics && metrics.total_threads > 0 && threads.length === 0 && !loading && !error && (
        <p className="rounded-lg border border-[var(--border)]/80 bg-[var(--bg-elev)]/30 px-3 py-2.5 text-[13px] text-[var(--text-secondary)]">
          No threads match the current filter or search. Clear{" "}
          {searchApplied ? (
            <button
              type="button"
              className="font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline"
              onClick={() => {
                setSearchInput("");
                setSearchApplied("");
              }}
            >
              search
            </button>
          ) : (
            <span className="font-medium">search</span>
          )}{" "}
          or choose <span className="font-medium">All</span> categories.
        </p>
      )}

    </div>
  );
}
