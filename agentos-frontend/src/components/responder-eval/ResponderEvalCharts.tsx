"use client";

import { useMemo, type ReactNode } from "react";
import {
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
  Line,
  ComposedChart,
} from "recharts";
import type {
  ResponderEvalGradeDistribution,
  ResponderEvalIssueGrade,
  ResponderEvalResolutionDistribution,
  ResponderEvalScoreBuckets,
  ResponderEvalSegment,
  ResponderEvalSummaryStats,
  ResponderEvalTimeseries,
  ResponderEvalTopIssues,
} from "@/lib/responderEval";
import { RESPONDER_EVAL_ISSUE_GRADES, RESPONDER_EVAL_SEGMENTS } from "@/lib/responderEval";
import {
  evalIssueAttributeLabel,
  evalIssueLabel,
  resolutionMetric,
} from "@/lib/responderEvalLabels";
import { responderGradeChartFill, responderGradeLabel, responderGradeSortKey } from "@/lib/responderGradeVisual";

function hallucinationParentLabel(
  issue: string,
  attributes?: { id: string; count: number }[]
): string {
  if (issue !== "hallucination") return evalIssueLabel(issue);
  const sevs = new Set(
    (attributes ?? []).filter((a) => a.id.startsWith("severity_")).map((a) => a.id)
  );
  const hasSoft = sevs.has("severity_soft");
  const hasHard = sevs.has("severity_P0") || sevs.has("severity_P1");
  if (hasSoft && hasHard) return "Hallucination (soft + hard gate)";
  if (hasSoft) return "Hallucination (soft)";
  if (hasHard) return "Hallucination (hard gate)";
  return "Hallucination";
}

const CHART = {
  volume: "var(--chart-other, #94a3b8)",
  scoreLine: "var(--chart-completed, #059669)",
  botLine: "#6366f1",
  botClosedLine: "#4f46e5",
  botPreHandoffLine: "#7c3aed",
  humanLine: "#d97706",
  bucket: "#6366f1",
  resolution: "#0d9488",
};

function formatBucketLabel(iso: string, granularity: "day" | "week"): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const tooltipStyle = {
  backgroundColor: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
};

type SegmentLineSpec = {
  dataKey:
    | "avg_score"
    | "avg_bot_score"
    | "avg_bot_closed_score"
    | "avg_bot_pre_handoff_score"
    | "avg_human_score";
  volumeKey: "total" | "total_bot" | "total_bot_closed" | "total_bot_pre_handoff" | "total_human";
  name: string;
  stroke: string;
};

const SEGMENT_LINE: Record<ResponderEvalSegment, SegmentLineSpec> = {
  composite: { dataKey: "avg_score", volumeKey: "total", name: "Composite", stroke: CHART.scoreLine },
  bot: { dataKey: "avg_bot_score", volumeKey: "total_bot", name: "AI Bot", stroke: CHART.botLine },
  bot_closed: {
    dataKey: "avg_bot_closed_score",
    volumeKey: "total_bot_closed",
    name: "AI Bot · closed",
    stroke: CHART.botClosedLine,
  },
  bot_pre_handoff: {
    dataKey: "avg_bot_pre_handoff_score",
    volumeKey: "total_bot_pre_handoff",
    name: "AI Bot · with hand-off",
    stroke: CHART.botPreHandoffLine,
  },
  human: { dataKey: "avg_human_score", volumeKey: "total_human", name: "Human", stroke: CHART.humanLine },
};

type Props = {
  summary: ResponderEvalSummaryStats | null;
  grades: ResponderEvalGradeDistribution | null;
  timeseries: ResponderEvalTimeseries | null;
  buckets: ResponderEvalScoreBuckets | null;
  resolutions: ResponderEvalResolutionDistribution | null;
  topIssues: ResponderEvalTopIssues | null;
  loading: boolean;
  days: number;
  granularity: "day" | "week";
  segment: ResponderEvalSegment;
  onDaysChange: (d: number) => void;
  onGranularityChange: (g: "day" | "week") => void;
  onSegmentChange: (s: ResponderEvalSegment) => void;
};

function StatCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: string | number;
  hint?: string;
  accent: "emerald" | "amber" | "sky" | "violet";
}) {
  const tones = {
    emerald:
      "border-emerald-600/20 bg-gradient-to-br from-emerald-50/90 to-[var(--bg-card)] ",
    amber:
      "border-amber-500/25 bg-gradient-to-br from-amber-50/90 to-[var(--bg-card)] ",
    sky: "border-sky-500/25 bg-gradient-to-br from-sky-50/90 to-[var(--bg-card)] ",
    violet:
      "border-violet-500/25 bg-gradient-to-br from-violet-50/90 to-[var(--bg-card)] ",
  };
  const valueTone = {
    emerald: "text-emerald-800 ",
    amber: "text-amber-900 ",
    sky: "text-sky-900 ",
    violet: "text-violet-900 ",
  };

  return (
    <div className={`rounded-xl border p-4 shadow-sm ${tones[accent]}`}>
      <p className="text-[11px] font-medium uppercase tracking-wide text-[var(--text-muted)]">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums tracking-tight ${valueTone[accent]}`}>{value}</p>
      {hint ? <p className="mt-1 text-[11px] text-[var(--text-secondary)]">{hint}</p> : null}
    </div>
  );
}

function TopIssuesBarList({
  data,
}: {
  data: {
    id: string;
    label: string;
    count: number;
    attributes?: { id: string; label: string; count: number }[];
  }[];
}) {
  const max = Math.max(1, ...data.map((d) => d.count));
  const hasNestedAttrs = data.some((d) => (d.attributes?.length ?? 0) > 0);

  return (
    <div className="flex w-full flex-col gap-2">
      {hasNestedAttrs ? (
        <p className="rounded-md border border-amber-500/25 bg-amber-50/60 px-2 py-1.5 text-[10px] leading-snug text-amber-950 ">
          Nested counts are multi-label: one chat can carry several attributes under the same
          parent, so attribute totals can sum past the parent count (and past 100%). Bar width for
          each attribute is capped at 100% of the parent.
        </p>
      ) : null}
      <ul className="flex w-full flex-col gap-3 px-0.5 py-0.5">
        {data.map((item) => {
          const pct = (item.count / max) * 100;
          const attrs = item.attributes ?? [];
          return (
            <li key={item.id} className="min-w-0">
              <div className="mb-1 flex items-start justify-between gap-2">
                <span className="text-[11px] font-medium leading-snug text-[var(--text-primary)]">
                  {item.label}
                </span>
                <span className="shrink-0 tabular-nums text-[11px] font-medium text-[var(--text-secondary)]">
                  {item.count}
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-sm bg-[var(--bg-elev)]">
                <div
                  className="h-full rounded-r-sm bg-[#dc2626]"
                  style={{ width: `${pct}%` }}
                  title={`${item.label}: ${item.count} chats`}
                />
              </div>
              {attrs.length > 0 ? (
                <ul className="mt-1.5 flex flex-col gap-1 border-l border-[var(--border)] pl-2.5">
                  {attrs.map((attr) => {
                    const sharePct = Math.min(
                      100,
                      (attr.count / Math.max(1, item.count)) * 100
                    );
                    return (
                      <li key={`${item.id}:${attr.id}`} className="min-w-0">
                        <div className="mb-0.5 flex items-start justify-between gap-2">
                          <span className="text-[10px] leading-snug text-[var(--text-muted)]">
                            {attr.label}
                          </span>
                          <span
                            className="shrink-0 tabular-nums text-[10px] text-[var(--text-muted)]"
                            title={`${attr.count} of ${item.count} parent chats (${Math.round(sharePct)}%)`}
                          >
                            {attr.count}
                            <span className="text-[var(--text-muted)]/70">/{item.count}</span>
                          </span>
                        </div>
                        <div className="h-1 w-full overflow-hidden rounded-sm bg-[var(--bg-elev)]">
                          <div
                            className="h-full rounded-r-sm bg-[#f87171]"
                            style={{ width: `${sharePct}%` }}
                            title={`${attr.count} of ${item.count} parent chats`}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const DEFAULT_CHART_EMPTY =
  "No data in this window yet. Scores appear after the scheduler evaluates pending chats.";
const ISSUES_EMPTY_NO_GRADE = "No chats with this letter grade in the selected window.";
const ISSUES_EMPTY_NO_TAGS =
  "No tagged failure modes for these graded chats. A grade can appear without tags when the low grade is score-band only.";

function ChartPanel({
  title,
  subtitle,
  children,
  empty,
  emptyMessage,
  scrollCap,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  empty?: boolean;
  emptyMessage?: string;
  /** Cap inner height so nested issue lists scroll instead of stretching the grid. */
  scrollCap?: boolean;
}) {
  return (
    <div
      className={`flex min-h-[260px] flex-col rounded-xl border border-[var(--border)] bg-[var(--bg-primary)]/50 p-3 shadow-sm ${
        scrollCap ? "max-h-[420px] w-full self-start overflow-hidden" : "h-full"
      }`}
    >
      <div className="mb-2 shrink-0">
        <h3 className="text-[13px] font-semibold text-[var(--text-primary)]">{title}</h3>
        {subtitle ? <p className="text-[11px] text-[var(--text-muted)]">{subtitle}</p> : null}
      </div>
      <div
        className={`flex min-h-0 flex-1 justify-center ${
          empty
            ? "items-center"
            : scrollCap
              ? "items-start overflow-y-auto"
              : "items-center"
        }`}
      >
        {empty ? (
          <p className="px-4 text-center text-[11px] leading-relaxed text-[var(--text-muted)]">
            {emptyMessage ?? DEFAULT_CHART_EMPTY}
          </p>
        ) : (
          children
        )}
      </div>
    </div>
  );
}

function chartSegmentBlock<T extends Record<string, unknown>>(
  data: T | null | undefined,
  segment: ResponderEvalSegment
): T[keyof T] | undefined {
  return data?.[segment as keyof T];
}

export function ResponderEvalCharts({
  summary,
  grades,
  timeseries,
  buckets,
  resolutions,
  topIssues,
  loading,
  days,
  granularity,
  segment,
  onDaysChange,
  onGranularityChange,
  onSegmentChange,
}: Props) {
  const segmentLabel = RESPONDER_EVAL_SEGMENTS.find((s) => s.id === segment)?.label ?? "Composite";
  const scoreLine = SEGMENT_LINE[segment] ?? SEGMENT_LINE.composite;

  const pieData = useMemo(() => {
    const block = grades?.[segment];
    const slices = [...(block?.slices ?? [])];
    slices.sort((a, b) => responderGradeSortKey(b.grade) - responderGradeSortKey(a.grade));
    return slices.map((s) => ({
      name: responderGradeLabel(s.grade || "—"),
      value: s.count,
      fill: responderGradeChartFill(s.grade),
    }));
  }, [grades, segment]);

  const tsData = useMemo(() => {
    const volumeKey = scoreLine.volumeKey;
    return (timeseries?.points ?? []).map((p) => ({
      ...p,
      segment_total: p[volumeKey] ?? 0,
      label: formatBucketLabel(p.bucket_start, granularity),
    }));
  }, [timeseries, granularity, scoreLine.volumeKey]);

  const bucketBlock = chartSegmentBlock(buckets, segment) as
    | { buckets: { range: string; count: number }[] }
    | undefined;
  const bucketData = bucketBlock?.buckets ?? [];
  const hasBucketData = bucketData.some((b) => b.count > 0);

  const resolutionBlock = chartSegmentBlock(resolutions, segment) as
    | { slices: { resolution: string; count: number }[] }
    | undefined;
  const resolutionData = (resolutionBlock?.slices ?? []).map((s) => ({
    resolution: resolutionMetric(s.resolution).value,
    count: s.count,
  }));
  const hasResolutionData = resolutionData.some((r) => r.count > 0);

  const issuesByGrade = useMemo(() => {
    const raw = chartSegmentBlock(topIssues, segment) as
      | Record<
          ResponderEvalIssueGrade,
          {
            issue: string;
            count: number;
            attributes?: { id: string; count: number }[];
          }[]
        >
      | undefined;
    const out = {} as Record<
      ResponderEvalIssueGrade,
      {
        id: string;
        label: string;
        count: number;
        attributes?: { id: string; label: string; count: number }[];
      }[]
    >;
    for (const g of RESPONDER_EVAL_ISSUE_GRADES) {
      out[g] = (raw?.[g] ?? []).map((item) => {
        const attributes = [...(item.attributes ?? [])].sort((a, b) => {
          const aSev = a.id.startsWith("severity_") ? 0 : a.id.startsWith("lifecycle") ? 2 : 1;
          const bSev = b.id.startsWith("severity_") ? 0 : b.id.startsWith("lifecycle") ? 2 : 1;
          if (aSev !== bSev) return aSev - bSev;
          return b.count - a.count;
        });
        return {
          id: item.issue,
          label: hallucinationParentLabel(item.issue, attributes),
          count: item.count,
          attributes: attributes.map((a) => ({
            id: a.id,
            label: evalIssueAttributeLabel(item.issue, a.id),
            count: a.count,
          })),
        };
      });
    }
    return out;
  }, [topIssues, segment]);

  const hasTimeseries = tsData.some((p) => p.segment_total > 0);

  const botClosedN = summary?.graded_bot_closed_runs ?? 0;
  const botPreN = summary?.graded_bot_pre_handoff_runs ?? 0;
  const humanN = summary?.graded_human_runs ?? 0;

  return (
    <section
      className="space-y-4"
      aria-labelledby="responder-eval-charts-heading"
      aria-busy={loading}
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
        <StatCard
          label={`Evaluated (${days}d)`}
          value={loading ? "—" : (summary?.evaluated ?? 0)}
          hint="Graded + not graded in window"
          accent="emerald"
        />
        <StatCard
          label="Graded"
          value={loading ? "—" : (summary?.graded ?? summary?.evaluated ?? 0)}
          hint="Eval status completed (scored)"
          accent="sky"
        />
        <StatCard
          label="Not graded"
          value={loading ? "—" : (summary?.not_graded ?? 0)}
          hint="Eval status not graded"
          accent="violet"
        />
        <StatCard
          label="Eval queue"
          value={loading ? "—" : (summary?.pending_dumps ?? 0)}
          hint={
            loading
              ? undefined
              : `${summary?.waiting_chats ?? 0} waiting for chat close · all time`
          }
          accent="amber"
        />
        <StatCard
          label="Avg composite"
          value={
            loading || summary?.graded === 0 || summary?.avg_composite_score == null
              ? "—"
              : summary.avg_composite_score
          }
          hint="Mean composite · graded chats"
          accent="sky"
        />
        <StatCard
          label="Avg AI Bot · closed"
          value={
            loading || botClosedN === 0 || summary?.avg_bot_closed_score == null
              ? "—"
              : summary.avg_bot_closed_score
          }
          hint={
            loading
              ? "AI Bot score · no human segment"
              : `${botClosedN.toLocaleString()} graded chats · AI Bot only`
          }
          accent="violet"
        />
        <StatCard
          label="Avg AI Bot · with hand-off"
          value={
            loading || botPreN === 0 || summary?.avg_bot_pre_handoff_score == null
              ? "—"
              : summary.avg_bot_pre_handoff_score
          }
          hint={
            loading
              ? "AI Bot score · before human joined"
              : `${botPreN.toLocaleString()} graded chats · AI Bot then human`
          }
          accent="violet"
        />
        <StatCard
          label="Avg human"
          value={
            loading || humanN === 0 || summary?.avg_human_score == null
              ? "—"
              : summary.avg_human_score
          }
          hint={
            loading
              ? "Human agent score"
              : `${humanN.toLocaleString()} graded chats · human segment`
          }
          accent="amber"
        />
      </div>

      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm sm:p-4">
        <div className="flex flex-col gap-3 border-b border-[var(--border)]/80 pb-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <p
              id="responder-eval-charts-heading"
              className="text-[10px] font-semibold uppercase tracking-[0.14em] text-violet-700/90 "
            >
              Analytics
            </p>
            <h2 className="mt-0.5 text-[15px] font-semibold tracking-tight text-[var(--text-primary)]">
              Quality trends
            </h2>
            <p className="mt-1 max-w-2xl text-[11px] leading-relaxed text-[var(--text-muted)]">
              Segment applies to charts and the table. AI Bot · closed = bot-only chats (no human);
              with hand-off = bot score on chats where a human also joined. Counts are graded chats,
              not messages. Nested issue attrs are multi-label (one chat can carry several).
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <fieldset className="flex flex-wrap items-center gap-1 border-0 p-0">
              <legend className="sr-only">Score segment</legend>
              {RESPONDER_EVAL_SEGMENTS.map((s) => (
                <label
                  key={s.id}
                  className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                    segment === s.id
                      ? "border-violet-600/50 bg-violet-100 text-violet-950 "
                      : "border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                  }`}
                >
                  <input
                    type="radio"
                    className="sr-only"
                    name="responder-segment"
                    checked={segment === s.id}
                    onChange={() => onSegmentChange(s.id)}
                  />
                  {s.label}
                </label>
              ))}
            </fieldset>
            <label className="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
              <span className="whitespace-nowrap">Window</span>
              <select
                className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-xs outline-none focus:border-violet-500/50 focus:ring-1 focus:ring-violet-500/30"
                value={String(days)}
                onChange={(e) => onDaysChange(Number(e.target.value))}
                aria-label="Chart time window"
              >
                <option value="7">7 days</option>
                <option value="15">15 days</option>
                <option value="30">30 days</option>
              </select>
            </label>
            <fieldset className="flex items-center gap-1.5 border-0 p-0">
              <legend className="sr-only">Bucket size</legend>
              {(["day", "week"] as const).map((g) => (
                <label
                  key={g}
                  className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                    granularity === g
                      ? "border-violet-600/50 bg-violet-100 text-violet-950 "
                      : "border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                  }`}
                >
                  <input
                    type="radio"
                    className="sr-only"
                    name="responder-granularity"
                    checked={granularity === g}
                    onChange={() => onGranularityChange(g)}
                  />
                  {g === "day" ? "Daily" : "Weekly"}
                </label>
              ))}
            </fieldset>
          </div>
        </div>

        {loading ? (
          <p className="mt-4 text-xs text-[var(--text-muted)]">Loading charts…</p>
        ) : (
          <>
            <div className="mt-4 grid gap-4 lg:grid-cols-3">
              <ChartPanel
                title="Grade distribution"
                subtitle={`${segmentLabel} letter grades A–E`}
                empty={pieData.length === 0}
              >
                <div className="h-[220px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={pieData}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="50%"
                        innerRadius={48}
                        outerRadius={78}
                        paddingAngle={2}
                      >
                        {pieData.map((e, i) => (
                          <Cell key={`${e.name}-${i}`} fill={e.fill} stroke="var(--bg-card)" />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={tooltipStyle} />
                      <Legend layout="horizontal" verticalAlign="bottom" wrapperStyle={{ fontSize: 11 }} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              </ChartPanel>

              <ChartPanel title="Score buckets" subtitle={`${segmentLabel} score ranges`} empty={!hasBucketData}>
                <div className="h-[220px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={bucketData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="range" tick={{ fontSize: 10 }} stroke="var(--text-muted)" />
                      <YAxis allowDecimals={false} tick={{ fontSize: 10 }} stroke="var(--text-muted)" width={28} />
                      <Tooltip contentStyle={tooltipStyle} />
                      <Bar dataKey="count" name="Chats" fill={CHART.bucket} radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </ChartPanel>

              <ChartPanel title="Resolution" subtitle={`${segmentLabel} · graded chats`} empty={!hasResolutionData}>
                <div className="h-[220px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={resolutionData} layout="vertical" margin={{ top: 4, right: 12, left: 4, bottom: 0 }}>
                      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" horizontal={false} />
                      <XAxis type="number" allowDecimals={false} tick={{ fontSize: 10 }} stroke="var(--text-muted)" />
                      <YAxis
                        type="category"
                        dataKey="resolution"
                        width={88}
                        tick={{ fontSize: 10 }}
                        stroke="var(--text-muted)"
                      />
                      <Tooltip contentStyle={tooltipStyle} />
                      <Bar dataKey="count" name="Chats" fill={CHART.resolution} radius={[0, 4, 4, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </ChartPanel>
            </div>

            <div className="mt-4 grid gap-4 lg:grid-cols-3">
              {RESPONDER_EVAL_ISSUE_GRADES.map((g) => {
                const items = issuesByGrade[g] ?? [];
                const empty = items.length === 0 || !items.some((i) => i.count > 0);
                const gradeCount =
                  grades?.[segment]?.slices?.find((s) => s.grade === g)?.count ?? 0;
                const emptyMessage =
                  gradeCount > 0 ? ISSUES_EMPTY_NO_TAGS : ISSUES_EMPTY_NO_GRADE;
                return (
                  <ChartPanel
                    key={g}
                    title={`Top issues · grade ${g}`}
                    subtitle={`${segmentLabel} · ${g}-graded chats · nested attrs are multi-label`}
                    empty={empty}
                    emptyMessage={emptyMessage}
                    scrollCap
                  >
                    <div className="w-full">
                      <TopIssuesBarList data={items} />
                    </div>
                  </ChartPanel>
                );
              })}
            </div>

            <ChartPanel
              title="Volume & average score"
              subtitle={`Evals per ${granularity} with ${segmentLabel.toLowerCase()} trend`}
              empty={!hasTimeseries}
            >
              <div className="h-[240px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={tsData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 10 }} stroke="var(--text-muted)" />
                    <YAxis yAxisId="left" allowDecimals={false} tick={{ fontSize: 10 }} stroke="var(--text-muted)" width={32} />
                    <YAxis
                      yAxisId="right"
                      orientation="right"
                      domain={[0, 100]}
                      tick={{ fontSize: 10 }}
                      stroke="var(--text-muted)"
                      width={32}
                    />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar
                      yAxisId="left"
                      dataKey="segment_total"
                      name={`${scoreLine.name} evals`}
                      fill={CHART.volume}
                      radius={[4, 4, 0, 0]}
                    />
                    <Line
                      yAxisId="right"
                      type="monotone"
                      dataKey={scoreLine.dataKey}
                      name={scoreLine.name}
                      stroke={scoreLine.stroke}
                      strokeWidth={2}
                      dot={{ r: 3, fill: scoreLine.stroke }}
                      connectNulls
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </ChartPanel>
          </>
        )}
      </div>
    </section>
  );
}
