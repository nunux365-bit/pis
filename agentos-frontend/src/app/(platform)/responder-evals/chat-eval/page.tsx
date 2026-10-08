"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ResponderEvalCharts } from "@/components/responder-eval/ResponderEvalCharts";
import { ResponderEvalDetailModal } from "@/components/responder-eval/ResponderEvalDetailModal";
import { ResponderEvalPendingPanel } from "@/components/responder-eval/ResponderEvalPendingPanel";
import {
  getResponderEval,
  getResponderEvalCharts,
  getResponderEvalPendingDumps,
  listResponderEvals,
  retryResponderEvalDump,
  RESPONDER_EVAL_SEGMENTS,
  buildResponderEvalReasonGroups,
  type ResponderEvalDetail,
  type ResponderEvalPendingDump,
  type ResponderEvalSegment,
  type ResponderEvalSummary,
} from "@/lib/responderEval";
import {
  isResponderEvalGraded,
  responderGradeChartFill,
  responderGradeLabel,
} from "@/lib/responderGradeVisual";

const PAGE_SIZES = [25, 50, 100] as const;

function GradeBadge({ grade }: { grade: string }) {
  return (
    <span
      className="inline-flex min-w-[1.75rem] justify-center rounded-md px-2 py-0.5 text-xs font-bold text-white shadow-sm"
      style={{ background: responderGradeChartFill(grade) }}
    >
      {responderGradeLabel(grade)}
    </span>
  );
}

export default function ResponderEvalsPage() {
  const [items, setItems] = useState<ResponderEvalSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [grade, setGrade] = useState("");
  const [reason, setReason] = useState("");
  const [evalStatus, setEvalStatus] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [searchApplied, setSearchApplied] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(50);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [chartDays, setChartDays] = useState(7);
  const [granularity, setGranularity] = useState<"day" | "week">("day");
  const [chartSegment, setChartSegment] = useState<ResponderEvalSegment>("composite");
  const [chartsLoading, setChartsLoading] = useState(true);
  const [chartsErr, setChartsErr] = useState<string | null>(null);
  const [summary, setSummary] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["summary"] | null
  >(null);
  const [grades, setGrades] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["grades"] | null
  >(null);
  const [timeseries, setTimeseries] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["timeseries"] | null
  >(null);
  const [buckets, setBuckets] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["buckets"] | null
  >(null);
  const [resolutions, setResolutions] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["resolutions"] | null
  >(null);
  const [topIssues, setTopIssues] = useState<
    Awaited<ReturnType<typeof getResponderEvalCharts>>["top_issues"] | null
  >(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ResponderEvalDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [pendingItems, setPendingItems] = useState<ResponderEvalPendingDump[]>([]);
  const [pendingTotal, setPendingTotal] = useState(0);
  const [pendingPage, setPendingPage] = useState(0);
  const [pendingLoading, setPendingLoading] = useState(true);
  const [pendingErr, setPendingErr] = useState<string | null>(null);
  const [retryingChatId, setRetryingChatId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const pendingPageSize = 25;

  function parseApiError(e: unknown): string {
    const msg = e instanceof Error ? e.message : "Request failed";
    if (msg.toLowerCase().includes("disabled") || msg.includes("503")) {
      return "Responder eval is disabled on this environment.";
    }
    return msg;
  }

  useEffect(() => {
    const t = window.setTimeout(() => setSearchApplied(searchInput.trim()), 400);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  const gradeCounts = useMemo(() => {
    const m = new Map<string, number>();
    const block = grades?.[chartSegment];
    const slices =
      block && typeof block === "object" && "slices" in block
        ? (block.slices as { grade: string; count: number }[])
        : [];
    for (const s of slices) m.set(s.grade, s.count);
    const na =
      block && typeof block === "object" && "na_count" in block
        ? Number(block.na_count ?? 0)
        : 0;
    m.set("-", na);
    return m;
  }, [grades, chartSegment]);

  const segmentGradeLabel =
    RESPONDER_EVAL_SEGMENTS.find((s) => s.id === chartSegment)?.label ?? "Composite";

  const reasonGroups = useMemo(
    () => buildResponderEvalReasonGroups(topIssues, chartSegment),
    [topIssues, chartSegment]
  );

  const loadCharts = useCallback(async () => {
    if (disabled) return;
    setChartsLoading(true);
    setChartsErr(null);
    try {
      const bundle = await getResponderEvalCharts(chartDays, granularity);
      setSummary(bundle.summary);
      setGrades(bundle.grades);
      setTimeseries(bundle.timeseries);
      setBuckets(bundle.buckets);
      setResolutions(bundle.resolutions);
      setTopIssues(bundle.top_issues);
    } catch (e) {
      const msg = parseApiError(e);
      if (msg.includes("disabled")) {
        setDisabled(true);
        setSummary(null);
        setGrades(null);
        setTimeseries(null);
        setBuckets(null);
        setResolutions(null);
        setTopIssues(null);
      }
      setChartsErr(msg);
    } finally {
      setChartsLoading(false);
    }
  }, [chartDays, granularity, disabled]);

  const loadPending = useCallback(async () => {
    if (disabled) return;
    setPendingLoading(true);
    setPendingErr(null);
    try {
      const data = await getResponderEvalPendingDumps(
        pendingPageSize,
        pendingPage * pendingPageSize
      );
      setPendingItems(data.items);
      setPendingTotal(data.total);
    } catch (e) {
      const msg = parseApiError(e);
      if (msg.includes("disabled")) {
        setDisabled(true);
        setPendingItems([]);
        setPendingTotal(0);
      }
      setPendingErr(msg);
    } finally {
      setPendingLoading(false);
    }
  }, [disabled, pendingPage]);

  const prevFiltersRef = useRef({ grade, reason, evalStatus, searchApplied, pageSize, chartDays, chartSegment });
  const effectivePage = useMemo(() => {
    const prev = prevFiltersRef.current;
    if (
      prev.grade !== grade ||
      prev.reason !== reason ||
      prev.evalStatus !== evalStatus ||
      prev.searchApplied !== searchApplied ||
      prev.pageSize !== pageSize ||
      prev.chartDays !== chartDays ||
      prev.chartSegment !== chartSegment
    ) {
      prevFiltersRef.current = { grade, reason, evalStatus, searchApplied, pageSize, chartDays, chartSegment };
      return 0;
    }
    return page;
  }, [grade, reason, evalStatus, searchApplied, pageSize, chartDays, chartSegment, page]);

  useEffect(() => {
    if (effectivePage === 0 && page !== 0) {
      setPage(0);
    }
  }, [effectivePage, page]);

  const loadTable = useCallback(async () => {
    if (disabled) return;
    setLoading(true);
    setErr(null);
    try {
      const data = await listResponderEvals({
        limit: pageSize,
        offset: effectivePage * pageSize,
        days: chartDays,
        grade: grade || undefined,
        segment: chartSegment,
        eval_status: evalStatus || undefined,
        search: searchApplied || undefined,
        reason: reason || undefined,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch (e) {
      const msg = parseApiError(e);
      if (msg.includes("disabled")) {
        setDisabled(true);
        setItems([]);
        setTotal(0);
      }
      setErr(msg);
    } finally {
      setLoading(false);
    }
  }, [chartDays, disabled, grade, reason, evalStatus, effectivePage, pageSize, searchApplied, chartSegment]);

  useEffect(() => {
    void loadCharts();
  }, [loadCharts]);

  useEffect(() => {
    void loadPending();
  }, [loadPending]);

  useEffect(() => {
    void loadTable();
  }, [loadTable]);

  const handleRetryDump = useCallback(
    async (chatId: string) => {
      setRetryingChatId(chatId);
      try {
        await retryResponderEvalDump(chatId);
        await Promise.all([loadPending(), loadCharts()]);
      } catch (e) {
        setPendingErr(e instanceof Error ? e.message : "Retry failed");
      } finally {
        setRetryingChatId(null);
      }
    },
    [loadCharts, loadPending]
  );

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await Promise.all([loadTable(), loadCharts(), loadPending()]);
    } finally {
      setRefreshing(false);
    }
  }, [loadCharts, loadPending, loadTable]);

  useEffect(() => {
    const maxPage = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (page > maxPage) {
      setPage(maxPage);
    }
  }, [total, pageSize, page]);

  useEffect(() => {
    const maxPage = Math.max(0, Math.ceil(pendingTotal / pendingPageSize) - 1);
    if (pendingPage > maxPage) {
      setPendingPage(maxPage);
    }
  }, [pendingTotal, pendingPage, pendingPageSize]);

  useEffect(() => {
    if (!detailId) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetail(null);
    setDetailLoading(true);
    setDetailErr(null);
    void getResponderEval(detailId)
      .then((data) => {
        if (active) setDetail(data);
      })
      .catch((e) => {
        if (active) {
          setDetailErr(e instanceof Error ? e.message : "Failed to load detail");
        }
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [detailId]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const fromIdx = total === 0 ? 0 : effectivePage * pageSize + 1;
  const toIdx = Math.min(total, effectivePage * pageSize + items.length);

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-3 py-4 sm:px-5 sm:py-5">
      <header className="border-b border-[var(--border)]/70 pb-4">
        <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-violet-700/90 ">
          Support quality
        </p>
        <h1 className="mt-0.5 text-xl font-semibold tracking-tight text-[var(--text-primary)] sm:text-2xl">
          Chat Eval
        </h1>
        <p className="mt-1.5 max-w-3xl text-xs leading-relaxed text-[var(--text-secondary)]">
          Automated rubric scoring for customer-support chats.
        </p>
      </header>

      {disabled ? (
        <div
          className="rounded-lg border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-sm text-amber-950 "
          role="alert"
        >
          Responder eval is disabled. Set <code className="text-xs">RESPONDER_EVAL_ENABLED=true</code> on the
          backend to enable this dashboard.
        </div>
      ) : null}

      {chartsErr && !disabled ? (
        <div
          className="rounded-lg border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-sm text-amber-950 "
          role="alert"
        >
          Charts could not load: {chartsErr}
        </div>
      ) : null}

      <ResponderEvalCharts
        summary={summary}
        grades={grades}
        timeseries={timeseries}
        buckets={buckets}
        resolutions={resolutions}
        topIssues={topIssues}
        loading={chartsLoading}
        days={chartDays}
        granularity={granularity}
        segment={chartSegment}
        onDaysChange={(d) => {
          setChartDays(d);
          setReason("");
          setPage(0);
        }}
        onGranularityChange={setGranularity}
        onSegmentChange={(s) => {
          setChartSegment(s);
          setGrade("");
          setReason("");
          setPage(0);
        }}
      />

      <ResponderEvalPendingPanel
        items={pendingItems}
        total={pendingTotal}
        loading={pendingLoading}
        error={pendingErr}
        page={pendingPage}
        pageSize={pendingPageSize}
        onPageChange={setPendingPage}
        onRetry={handleRetryDump}
        retryingId={retryingChatId}
      />

      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-3 py-3 shadow-sm sm:px-4">
        <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
          <div>
            <h2 className="text-[13px] font-semibold text-[var(--text-primary)]">Eval runs</h2>
            <p className="text-[11px] text-[var(--text-muted)]">
              Last {chartDays} days — filter by {segmentGradeLabel.toLowerCase()} grade, reason, status, or prefix-search chat / order id
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2 sm:gap-3">
            <label className="min-w-[10rem] flex-1 text-[11px] font-medium text-[var(--text-secondary)] sm:min-w-[14rem]">
              Search
              <input
                type="search"
                className="mt-1 block w-full rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-violet-500/50 focus:ring-1 focus:ring-violet-500/30"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Chat or order id (prefix)"
                aria-label="Search eval runs"
              />
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              {segmentGradeLabel} grade <span className="font-normal text-[var(--text-muted)]">({chartDays}d)</span>
              <select
                className="mt-1 block min-w-[6.5rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
                value={grade}
                onChange={(e) => {
                  setGrade(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">All</option>
                {["A", "B", "C", "D", "E"].map((g) => (
                  <option key={g} value={g}>
                    {g} ({gradeCounts.get(g) ?? 0})
                  </option>
                ))}
                <option value="-">N/A ({gradeCounts.get("-") ?? 0})</option>
              </select>
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Reason <span className="font-normal text-[var(--text-muted)]">({chartDays}d top issues)</span>
              <select
                className="mt-1 block min-w-[10rem] max-w-[14rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
                value={reason}
                onChange={(e) => {
                  setReason(e.target.value);
                  setPage(0);
                }}
                aria-label="Filter eval runs by issue reason"
              >
                <option value="">All</option>
                {reasonGroups.map((group) => (
                  <optgroup key={group.parentValue} label={`${group.parentLabel} (${group.parentCount})`}>
                    <option value={group.parentValue}>
                      {group.parentLabel} ({group.parentCount})
                    </option>
                    {group.options.map((opt) => (
                      <option key={`${group.parentValue}:${opt.value}`} value={opt.value}>
                        {opt.label} ({opt.count})
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Status <span className="font-normal text-[var(--text-muted)]">({chartDays}d)</span>
              <select
                className="mt-1 block min-w-[7.5rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
                value={evalStatus}
                onChange={(e) => {
                  setEvalStatus(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">All</option>
                <option value="completed">Graded</option>
                <option value="not_graded">Not graded</option>
              </select>
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Page size
              <select
                className="mt-1 block rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number]);
                  setPage(0);
                }}
              >
                {PAGE_SIZES.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium text-[var(--text-primary)] shadow-sm transition hover:bg-[var(--bg-secondary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/40 disabled:opacity-50"
              disabled={refreshing}
              onClick={() => void handleRefresh()}
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {err ? (
          <div
            className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 "
            role="alert"
          >
            {err}
          </div>
        ) : null}

        <div className="overflow-x-auto rounded-lg border border-[var(--border)]/80">
          <table className="w-full min-w-[860px] border-collapse text-left text-[13px]">
            <caption className="sr-only">Responder eval runs</caption>
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--bg-primary)]/60 text-[10px] uppercase tracking-wide text-[var(--text-muted)]">
                {["Chat", "Order", "Status", "Composite score", "Composite grade", "AI Bot", "Human", "Version", "Evaluated", ""].map((h) => (
                  <th key={h || "actions"} className="px-3 py-2.5 font-medium" scope="col">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={10} className="px-3 py-12 text-center text-sm text-[var(--text-muted)]">
                    Loading eval runs…
                  </td>
                </tr>
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={10} className="px-3 py-12 text-center">
                    <p className="text-sm font-medium text-[var(--text-secondary)]">No eval runs yet</p>
                    <p className="mx-auto mt-1 max-w-md text-xs text-[var(--text-muted)]">
                      Runs appear after the scheduler evaluates pending chats in the window.
                    </p>
                  </td>
                </tr>
              ) : (
                items.map((row) => {
                  const graded = isResponderEvalGraded(row);
                  return (
                  <tr
                    key={row.id}
                    className="cursor-pointer border-b border-[var(--border)]/50 transition-colors last:border-0 hover:bg-[var(--bg-secondary)]/60"
                    onClick={() => setDetailId(row.id)}
                  >
                    <td className="max-w-[12rem] truncate px-3 py-2.5 font-mono text-[11px] text-[var(--text-primary)]">
                      {row.chat_id}
                    </td>
                    <td className="px-3 py-2.5 text-[var(--text-secondary)]">
                      {row.order_id ? (
                        <Link
                          href={`/order-rca?po=${encodeURIComponent(row.order_id)}`}
                          className="text-violet-700 underline-offset-2 hover:underline "
                          onClick={(e) => e.stopPropagation()}
                        >
                          {row.order_id}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-[11px] text-[var(--text-secondary)]">
                      {row.eval_status === "not_graded" ? "Not graded" : "Graded"}
                    </td>
                    <td className="px-3 py-2.5 tabular-nums font-medium">
                      {graded ? row.composite_score : "—"}
                    </td>
                    <td className="px-3 py-2.5">
                      <GradeBadge grade={row.letter_grade} />
                    </td>
                    <td className="px-3 py-2.5">
                      <GradeBadge grade={row.bot_grade} />
                    </td>
                    <td className="px-3 py-2.5">
                      <GradeBadge grade={row.human_grade} />
                    </td>
                    <td className="px-3 py-2.5 font-mono text-[11px] text-[var(--text-muted)]">{row.eval_version}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 text-[var(--text-secondary)]">
                      {new Date(row.created_at).toLocaleString(undefined, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      })}
                    </td>
                    <td className="px-3 py-2.5">
                      <button
                        type="button"
                        className="rounded-md border border-violet-500/30 bg-violet-50 px-2.5 py-1 text-xs font-medium text-violet-900 transition hover:bg-violet-100 "
                        onClick={(e) => {
                          e.stopPropagation();
                          setDetailId(row.id);
                        }}
                      >
                        Details
                      </button>
                    </td>
                  </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="mt-3 flex flex-col gap-2 border-t border-[var(--border)]/60 pt-3 text-sm sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-[var(--text-muted)]">
            {total === 0 ? "0 runs" : `Showing ${fromIdx}–${toIdx} of ${total}`}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1 text-xs font-medium disabled:opacity-40"
              disabled={effectivePage <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </button>
            <span className="text-xs text-[var(--text-secondary)]">
              Page {effectivePage + 1} of {pageCount}
            </span>
            <button
              type="button"
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1 text-xs font-medium disabled:opacity-40"
              disabled={effectivePage + 1 >= pageCount}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </div>
      </div>

      <ResponderEvalDetailModal
        open={!!detailId}
        onClose={() => setDetailId(null)}
        detail={detail}
        loading={detailLoading}
        error={detailErr}
      />
    </div>
  );
}
