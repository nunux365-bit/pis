"use client";

import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { HandoffChatIdsModal } from "@/components/responder-eval/HandoffChatIdsModal";
import {
  getHandoffChatIds,
  type HandoffAnalysis,
  type HandoffChatIds,
  type HandoffKpis,
  type HandoffMatrixBucket,
  type HandoffMatrixRow,
  type HandoffMover,
} from "@/lib/responderEval";

type DisplayMode = "count" | "pct";

const EMPTY_KPIS: HandoffKpis = {
  handoffs_d1: 0,
  handoffs_vs_7d_avg_pct: null,
  handoff_rate_d1: 0,
  handoff_rate_vs_d2_pp: null,
  chats_landed_d1: 0,
  largest_bucket: null,
};

function heatStyle(value: number, max: number): { background: string; color: string } {
  if (value <= 0 || max <= 0) {
    return { background: "transparent", color: "var(--text-muted)" };
  }
  const t = Math.min(1, value / max);
  const alpha = 0.12 + t * 0.72;
  return {
    background: `rgba(255, 107, 53, ${alpha.toFixed(3)})`,
    color: t > 0.55 ? "#3b1408" : "#5c2a12",
  };
}

function formatValue(count: number, pct: number, mode: DisplayMode): string {
  if (count === 0) return "";
  return mode === "count" ? String(count) : `${pct.toFixed(1)}%`;
}

function formatDelta(delta: number): { text: string; tone: "up" | "down" | "flat" } {
  if (delta > 0) return { text: `+${delta}`, tone: "up" };
  if (delta < 0) return { text: String(delta), tone: "down" };
  return { text: "0", tone: "flat" };
}

function deltaClass(tone: "up" | "down" | "flat"): string {
  if (tone === "up") return "text-[var(--accent-red)]";
  if (tone === "down") return "text-[var(--accent-green)]";
  return "text-[var(--text-muted)]";
}

function matchesFilter(bucket: HandoffMatrixBucket, q: string): boolean {
  if (!q) return true;
  if (bucket.label.toLowerCase().includes(q)) return true;
  return bucket.sub_buckets.some((s) => s.label.toLowerCase().includes(q) || s.id.toLowerCase().includes(q));
}

function visibleSubs(bucket: HandoffMatrixBucket, q: string): HandoffMatrixRow[] {
  if (!q) return bucket.sub_buckets;
  if (bucket.label.toLowerCase().includes(q)) return bucket.sub_buckets;
  return bucket.sub_buckets.filter(
    (s) => s.label.toLowerCase().includes(q) || s.id.toLowerCase().includes(q)
  );
}

function heatMax(buckets: HandoffMatrixBucket[], mode: DisplayMode): number {
  let max = 0;
  for (const b of buckets) {
    for (const s of b.sub_buckets) {
      const values = mode === "count" ? s.days : s.pct_days;
      for (const v of values) if (v > max) max = v;
    }
  }
  return max;
}

/** A table-cell value that's a link to the chat-ids popup when `onOpen` is given, plain content otherwise. */
function ClickableCount({
  onOpen,
  className,
  style,
  children,
  fallback,
}: {
  onOpen?: () => void;
  className: string;
  style?: CSSProperties;
  children: ReactNode;
  fallback: ReactNode;
}) {
  if (!onOpen) return <>{fallback}</>;
  return (
    <button type="button" onClick={onOpen} className={className} style={style}>
      {children}
    </button>
  );
}

function KpiCard({
  title,
  value,
  hint,
  accent,
  loading,
}: {
  title: string;
  value: string;
  hint: ReactNode;
  accent?: "up" | "down";
  loading?: boolean;
}) {
  const bar =
    accent === "up"
      ? "border-l-[3px] border-l-[var(--accent-red)]"
      : accent === "down"
        ? "border-l-[3px] border-l-[var(--accent-green)]"
        : "border-l-[3px] border-l-transparent";
  return (
    <div className={`rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-4 py-3 shadow-sm ${bar}`}>
      <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">{title}</p>
      <p className="mt-1 text-[28px] font-bold leading-none tabular-nums tracking-tight text-[var(--text-primary)]">
        {loading ? "—" : value}
      </p>
      <p className="mt-2 text-[11px] text-[var(--text-secondary)]">{hint}</p>
    </div>
  );
}

function MoverList({
  title,
  tone,
  items,
}: {
  title: string;
  tone: "up" | "down";
  items: HandoffMover[];
}) {
  const dot = tone === "up" ? "bg-[var(--accent-red)]" : "bg-[var(--accent-green)]";
  return (
    <div>
      <p className="flex items-center gap-1.5 px-4 py-2 text-[10px] font-bold uppercase tracking-[0.12em] text-[var(--text-secondary)]">
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${dot}`} />
        {title}
      </p>
      {items.length === 0 ? (
        <p className="px-4 py-6 text-[12px] text-[var(--text-muted)]">None today.</p>
      ) : (
        <ul>
          {items.map((item) => {
            const d = formatDelta(item.delta);
            const pct =
              item.delta_pct == null
                ? ""
                : ` (${item.delta_pct > 0 ? "+" : ""}${item.delta_pct}%)`;
            return (
              <li
                key={item.id}
                className="flex items-start justify-between gap-3 border-t border-[var(--border)] px-4 py-2.5"
              >
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold text-[var(--text-primary)]">{item.label}</p>
                  <p className="text-[11px] text-[var(--text-muted)]">{item.bucket_label}</p>
                </div>
                <p className="shrink-0 text-right text-[13px] tabular-nums">
                  <span className="font-semibold text-[var(--text-primary)]">{item.count}</span>
                  <span className={`ml-2 font-semibold ${deltaClass(d.tone)}`}>
                    {d.text}
                    {pct}
                  </span>
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

type Props = {
  data: HandoffAnalysis;
  loading?: boolean;
};

export function HandoffAnalysisDashboard({ data, loading }: Props) {
  const [mode, setMode] = useState<DisplayMode>("count");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [seeded, setSeeded] = useState(false);
  const [chatIdsLabel, setChatIdsLabel] = useState<string | null>(null);
  const [chatIdsData, setChatIdsData] = useState<HandoffChatIds | null>(null);
  const [chatIdsLoading, setChatIdsLoading] = useState(false);
  const [chatIdsError, setChatIdsError] = useState<string | null>(null);
  const chatIdsRequestRef = useRef(0);
  const kpis = data.kpis ?? EMPTY_KPIS;
  const q = query.trim().toLowerCase();
  const headers = data.day_headers ?? [];
  const buckets = useMemo(
    () => (data.buckets ?? []).filter((b) => matchesFilter(b, q)),
    [data.buckets, q]
  );
  const max = useMemo(() => heatMax(buckets, mode), [buckets, mode]);

  useEffect(() => {
    if (seeded || !data.buckets[0]) return;
    setOpen(new Set([data.buckets[0].id]));
    setSeeded(true);
  }, [data.buckets, seeded]);

  const vsAvg = kpis.handoffs_vs_7d_avg_pct;
  const ratePp = kpis.handoff_rate_vs_d2_pp;

  function toggle(id: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function expandAll() {
    setOpen(new Set(buckets.map((b) => b.id)));
  }

  function collapseAll() {
    setOpen(new Set());
  }

  async function openChatIds(
    bucketId: string,
    subBucketId: string | null,
    label: string,
    opts: { scope: "day" | "last_7" | "mtd"; day?: string }
  ) {
    const requestId = ++chatIdsRequestRef.current;
    setChatIdsLabel(label);
    setChatIdsData(null);
    setChatIdsError(null);
    setChatIdsLoading(true);
    try {
      const res = await getHandoffChatIds(bucketId, subBucketId ?? undefined, opts);
      if (requestId !== chatIdsRequestRef.current) return; // a newer click superseded this one
      setChatIdsData(res);
    } catch (e) {
      if (requestId !== chatIdsRequestRef.current) return;
      setChatIdsError(e instanceof Error ? e.message : "Failed to load chat IDs");
    } finally {
      if (requestId === chatIdsRequestRef.current) setChatIdsLoading(false);
    }
  }

  function closeChatIds() {
    setChatIdsLabel(null);
  }

  const colCount = headers.length + 4;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.14em] text-[var(--text-muted)]">Show</span>
          <div
            className="inline-flex rounded-md border border-[var(--border)] bg-[var(--bg-card)] p-0.5 text-[12px]"
            role="group"
            aria-label="Display mode"
          >
            <button
              type="button"
              onClick={() => setMode("count")}
              className={`rounded px-2.5 py-1 font-medium ${
                mode === "count"
                  ? "bg-[var(--text-primary)] text-white"
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              Count
            </button>
            <button
              type="button"
              onClick={() => setMode("pct")}
              className={`rounded px-2.5 py-1 font-medium ${
                mode === "pct"
                  ? "bg-[var(--text-primary)] text-white"
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              % of chats landed
            </button>
          </div>
        </div>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter buckets or sub-buckets"
          aria-label="Filter buckets or sub-buckets"
          className="min-w-[14rem] flex-1 rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--border-active)]"
        />
        <div className="flex items-center gap-3 text-[12px]">
          <button type="button" className="text-[var(--accent-blue)] hover:underline" onClick={expandAll}>
            Expand all
          </button>
          <button type="button" className="text-[var(--accent-blue)] hover:underline" onClick={collapseAll}>
            Collapse all
          </button>
        </div>
      </div>

      <section className="grid gap-3 sm:grid-cols-2">
        <KpiCard
          title="Hand-offs · D-1"
          value={kpis.handoffs_d1.toLocaleString()}
          loading={loading}
          accent={vsAvg == null || vsAvg === 0 ? undefined : vsAvg > 0 ? "up" : "down"}
          hint={
            vsAvg == null || loading ? (
              "vs 7-day average"
            ) : (
              <span className={vsAvg > 0 ? "text-[var(--accent-red)]" : vsAvg < 0 ? "text-[var(--accent-green)]" : ""}>
                {vsAvg > 0 ? "▲" : vsAvg < 0 ? "▼" : "·"} {Math.abs(vsAvg).toFixed(1)}% vs 7-day average
              </span>
            )
          }
        />
        <KpiCard
          title="Hand-off rate · D-1"
          value={`${kpis.handoff_rate_d1.toFixed(1)}%`}
          loading={loading}
          accent={ratePp == null || ratePp === 0 ? undefined : ratePp > 0 ? "up" : "down"}
          hint={
            ratePp == null || loading ? (
              "vs D-2"
            ) : (
              <span className={ratePp > 0 ? "text-[var(--accent-red)]" : ratePp < 0 ? "text-[var(--accent-green)]" : ""}>
                {ratePp > 0 ? "▲" : ratePp < 0 ? "▼" : "·"} {Math.abs(ratePp).toFixed(2)}pp vs D-2
              </span>
            )
          }
        />
        <KpiCard
          title="Chats landed on responder"
          value={kpis.chats_landed_d1.toLocaleString()}
          loading={loading}
          hint="D-1 denominator for all % views"
        />
        <KpiCard
          title="Largest bucket"
          value={kpis.largest_bucket ? `${Math.round(kpis.largest_bucket.pct)}%` : "—"}
          loading={loading}
          hint={
            kpis.largest_bucket
              ? `${kpis.largest_bucket.label} · ${kpis.largest_bucket.count}`
              : "No classified hand-offs on D-1"
          }
        />
      </section>

      <section className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-[var(--border)] px-4 py-3">
          <h2 className="text-[15px] font-semibold text-[var(--text-primary)]">Where to look today</h2>
          <p className="text-[12px] text-[var(--text-muted)]">
            Top 5 sub-buckets on D-1 vs their last-7-day average. Under 5 hand-offs are excluded as noise.
          </p>
        </div>
        <div className="grid md:grid-cols-2">
          <MoverList title="Rising — worth a look" tone="up" items={data.movers?.rising ?? []} />
          <div className="border-t border-[var(--border)] md:border-l md:border-t-0">
            <MoverList title="Falling — working" tone="down" items={data.movers?.falling ?? []} />
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-[var(--border)] px-4 py-3">
          <h2 className="text-[15px] font-semibold text-[var(--text-primary)]">Bucket detail</h2>
          <p className="text-[12px] text-[var(--text-muted)]">
            Click a bucket to open it. Last 7 days and MTD are totals; Δ compares D-1 against the 7-day
            total ÷ 7.
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse text-left">
            <thead>
              <tr className="border-b border-[var(--border)]">
                <th className="sticky left-0 z-10 min-w-[18rem] bg-[var(--bg-card)] px-4 py-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--text-muted)]">
                  Bucket / sub-bucket
                </th>
                {headers.map((h) => (
                  <th
                    key={h.date}
                    className="min-w-[4.5rem] px-2 py-2 text-right text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]"
                  >
                    {h.header}
                  </th>
                ))}
                <th className="min-w-[5.5rem] px-2 py-2 text-right text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                  Last 7 days total
                </th>
                <th className="min-w-[5rem] px-2 py-2 text-right text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                  MTD total
                </th>
                <th className="min-w-[5.5rem] px-2 py-2 text-right text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                  Δ D-1 vs 7d avg
                </th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={colCount} className="px-4 py-10 text-center text-sm text-[var(--text-secondary)]">
                    Loading…
                  </td>
                </tr>
              ) : buckets.length === 0 ? (
                <tr>
                  <td colSpan={colCount} className="px-4 py-10 text-center text-sm text-[var(--text-secondary)]">
                    No classified hand-off chats in the last 7 complete UTC days.
                  </td>
                </tr>
              ) : (
                buckets.flatMap((bucket) => {
                  const expanded = open.has(bucket.id);
                  const subs = expanded ? visibleSubs(bucket, q) : [];
                  const bucketLast7Display =
                    mode === "count" ? bucket.last_7.toLocaleString() : `${bucket.pct_last_7.toFixed(1)}%`;
                  const bucketMtdDisplay =
                    mode === "count" ? bucket.mtd.toLocaleString() : `${bucket.pct_mtd.toFixed(1)}%`;
                  const bucketRow = (
                    <tr key={bucket.id} className="border-b border-[var(--border)] bg-[var(--bg-elev)]">
                      <td className="sticky left-0 z-10 bg-[var(--bg-elev)] px-4 py-2">
                        <button
                          type="button"
                          onClick={() => toggle(bucket.id)}
                          className="flex w-full items-center gap-2 text-left text-[13px] font-semibold text-[var(--text-primary)]"
                          aria-expanded={expanded}
                        >
                          <span className="w-3 text-[10px] text-[var(--text-muted)]">
                            {expanded ? "▼" : "▶"}
                          </span>
                          {bucket.label}
                          <span className="font-normal text-[var(--text-muted)]">{bucket.sub_count}</span>
                        </button>
                      </td>
                      {bucket.days.map((n, i) => {
                        const display = formatValue(n, bucket.pct_days[i] ?? 0, mode) || "—";
                        const day = headers[i]?.date;
                        return (
                          <td key={headers[i]?.date ?? i} className="px-2 py-2 text-right text-[12px] font-semibold tabular-nums">
                            <ClickableCount
                              onOpen={
                                n > 0 && day
                                  ? () =>
                                      void openChatIds(bucket.id, null, `${bucket.label} · ${headers[i].header}`, {
                                        scope: "day",
                                        day,
                                      })
                                  : undefined
                              }
                              className="text-[var(--accent-blue)] hover:underline"
                              fallback={display}
                            >
                              {display}
                            </ClickableCount>
                          </td>
                        );
                      })}
                      <td className="px-2 py-2 text-right text-[12px] font-semibold tabular-nums">
                        <ClickableCount
                          onOpen={
                            bucket.last_7 > 0
                              ? () =>
                                  void openChatIds(bucket.id, null, `${bucket.label} · Last 7 days`, {
                                    scope: "last_7",
                                  })
                              : undefined
                          }
                          className="text-[var(--accent-blue)] hover:underline"
                          fallback={bucketLast7Display}
                        >
                          {bucketLast7Display}
                        </ClickableCount>
                      </td>
                      <td className="px-2 py-2 text-right text-[12px] font-semibold tabular-nums">
                        <ClickableCount
                          onOpen={
                            bucket.mtd > 0
                              ? () =>
                                  void openChatIds(bucket.id, null, `${bucket.label} · Month to date`, {
                                    scope: "mtd",
                                  })
                              : undefined
                          }
                          className="text-[var(--accent-blue)] hover:underline"
                          fallback={bucketMtdDisplay}
                        >
                          {bucketMtdDisplay}
                        </ClickableCount>
                      </td>
                      <td className={`px-2 py-2 text-right text-[12px] font-semibold tabular-nums ${deltaClass(formatDelta(bucket.delta).tone)}`}>
                        {formatDelta(bucket.delta).text}
                      </td>
                    </tr>
                  );
                  const subRows = subs.map((sub) => (
                    <tr key={`${bucket.id}:${sub.id}`} className="border-b border-[var(--border)]/80">
                      <td className="sticky left-0 z-10 bg-[var(--bg-card)] px-4 py-1.5 pl-10 text-[12px] text-[var(--text-secondary)]">
                        {sub.label}
                      </td>
                      {sub.days.map((n, i) => {
                        const pct = sub.pct_days[i] ?? 0;
                        const v = mode === "count" ? n : pct;
                        const day = headers[i]?.date;
                        const display = formatValue(n, pct, mode);
                        return (
                          <td key={headers[i]?.date ?? i} className="px-1 py-1">
                            <ClickableCount
                              onOpen={
                                n > 0 && day
                                  ? () =>
                                      void openChatIds(bucket.id, sub.id, `${sub.label} · ${headers[i].header}`, {
                                        scope: "day",
                                        day,
                                      })
                                  : undefined
                              }
                              className="w-full rounded-sm px-1.5 py-0.5 text-right text-[12px] tabular-nums underline-offset-2 hover:underline"
                              style={heatStyle(v, max)}
                              fallback={
                                <div
                                  className="rounded-sm px-1.5 py-0.5 text-right text-[12px] tabular-nums"
                                  style={heatStyle(v, max)}
                                >
                                  {display}
                                </div>
                              }
                            >
                              {display}
                            </ClickableCount>
                          </td>
                        );
                      })}
                      <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-[var(--text-secondary)]">
                        <ClickableCount
                          onOpen={
                            sub.last_7 > 0
                              ? () =>
                                  void openChatIds(bucket.id, sub.id, `${sub.label} · Last 7 days`, {
                                    scope: "last_7",
                                  })
                              : undefined
                          }
                          className="text-[var(--accent-blue)] hover:underline"
                          fallback=""
                        >
                          {mode === "count" ? sub.last_7 : `${sub.pct_last_7.toFixed(1)}%`}
                        </ClickableCount>
                      </td>
                      <td className="px-2 py-1.5 text-right text-[12px] tabular-nums text-[var(--text-secondary)]">
                        <ClickableCount
                          onOpen={
                            sub.mtd > 0
                              ? () =>
                                  void openChatIds(bucket.id, sub.id, `${sub.label} · Month to date`, {
                                    scope: "mtd",
                                  })
                              : undefined
                          }
                          className="text-[var(--accent-blue)] hover:underline"
                          fallback=""
                        >
                          {mode === "count" ? sub.mtd : `${sub.pct_mtd.toFixed(1)}%`}
                        </ClickableCount>
                      </td>
                      <td
                        className={`px-2 py-1.5 text-right text-[12px] font-medium tabular-nums ${deltaClass(formatDelta(sub.delta).tone)}`}
                      >
                        {formatDelta(sub.delta).text}
                      </td>
                    </tr>
                  ));
                  return [bucketRow, ...subRows];
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      <HandoffChatIdsModal
        open={chatIdsLabel != null}
        onClose={closeChatIds}
        label={chatIdsLabel}
        data={chatIdsData}
        loading={chatIdsLoading}
        error={chatIdsError}
      />
    </div>
  );
}
