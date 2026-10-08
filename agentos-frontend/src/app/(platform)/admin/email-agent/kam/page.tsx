"use client";

import { useEffect, useState } from "react";
import {
  getKamDashboardMetrics,
  type KamDashboardResponse,
} from "@/lib/api";

/** Humanize a mean reply gap (seconds) to a compact label like `16h` / `4d` / `45m`. */
function formatReplyTime(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

function formatPct(value: number): string {
  return `${value.toFixed(value % 1 === 0 ? 0 : 1)}%`;
}

export default function EmailAgentKamPage() {
  const [data, setData] = useState<KamDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Dashboard tracks the last 7 days (L7D) only.
  useEffect(() => {
    let cancelled = false;
    getKamDashboardMetrics("7d")
      .then((res) => {
        if (cancelled) return;
        setError(null);
        setData(res);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load KAM metrics.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const rows = data?.rows ?? [];

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto overscroll-y-none p-3 sm:p-4">
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
        {/* ── Header ── */}
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
            KAM Performance
          </p>
          <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
            Per-KAM reply tracking on payment-reminder threads over the last 7 days.
            Accuracy is an LLM 0/1 score (did the KAM correctly handle every open ask);
            reply rate is KAM follow-ups on threads the client genuinely replied to.
          </p>
        </div>

        {error && (
          <div
            className="mt-3 rounded-lg border border-red-200/90 bg-red-50/80 px-3 py-2 text-[13px] text-red-950 ring-1 ring-red-900/10"
            role="alert"
          >
            {error}
          </div>
        )}

        {/* Skeleton on first load */}
        {loading && !error && rows.length === 0 && (
          <div
            className="mt-3 h-48 animate-pulse rounded-lg bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
            aria-busy="true"
            aria-label="Loading KAM metrics"
          />
        )}

        {!loading && !error && rows.length === 0 && (
          <p className="py-4 text-center text-[13px] text-[var(--text-muted)]">
            No KAM activity in the last 7 days.
          </p>
        )}

        {rows.length > 0 && (
          <div
            className={`mt-3 transition-opacity duration-150 ${
              loading ? "opacity-60" : "opacity-100"
            }`}
          >
            <div className="overflow-hidden rounded-lg border border-[var(--border)]">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[860px] table-fixed text-left text-[13px]">
                  <caption className="sr-only">Per-KAM reply-tracking metrics</caption>
                  <colgroup>
                    <col />
                    <col style={{ width: "140px" }} />
                    <col style={{ width: "160px" }} />
                    <col style={{ width: "120px" }} />
                    <col style={{ width: "130px" }} />
                    <col style={{ width: "140px" }} />
                  </colgroup>
                  <thead className="border-b border-[var(--border)] bg-[var(--bg-elev)]/95 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)] shadow-[0_1px_0_0_var(--border)] backdrop-blur-sm">
                    <tr>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Name</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Total Overdue (₹ L)</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Client Mails Received</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Reply Rate</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Accuracy Rate</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Avg Reply time</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, idx) => (
                      <tr
                        key={row.kam_name + idx}
                        className={
                          idx % 2 === 0
                            ? "border-b border-[var(--border)]/50 bg-[var(--bg-card)] hover:bg-[var(--bg-elev)]/50"
                            : "border-b border-[var(--border)]/50 bg-[var(--bg-primary)]/40 hover:bg-[var(--bg-elev)]/50"
                        }
                      >
                        <td
                          className="overflow-hidden px-3 py-2.5"
                          title={row.kam_name}
                        >
                          <span className="block truncate font-medium text-[var(--text-primary)]">
                            {row.kam_name}
                          </span>
                        </td>
                        <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-primary)]">
                          {row.total_overdue_lakh.toFixed(2)}
                        </td>
                        <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-primary)]">
                          {row.client_replied_threads}
                        </td>
                        <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-primary)]">
                          {formatPct(row.reply_rate)}
                        </td>
                        <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-primary)]">
                          {formatPct(row.accuracy_rate)}
                        </td>
                        <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-secondary)]">
                          {formatReplyTime(row.avg_reply_seconds)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
