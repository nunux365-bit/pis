"use client";

import type { IntelligenceSummaryResponse, ReplyTrackerWindowDays } from "@/lib/api";
import { ReplyBreakdownTable } from "@/components/email-agent/ReplyBreakdownTable";

const VIEW_OPTIONS: ReplyTrackerWindowDays[] = [7, 14, 30];

type Props = {
  summary: IntelligenceSummaryResponse | null;
  loading: boolean;
  buList: string[];
  selectedBu: string;
  onBuChange: (bu: string) => void;
  onCategoryFilter: (category: string | null) => void;
  activeCategoryFilter: string | null;
  view: ReplyTrackerWindowDays;
  onViewChange: (view: ReplyTrackerWindowDays) => void;
};

function fmtLakh(n: number): string {
  return n.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function CollectionsExecutiveSummary({
  summary,
  loading,
  buList,
  selectedBu,
  onBuChange,
  onCategoryFilter,
  activeCategoryFilter,
  view,
  onViewChange,
}: Props) {
  return (
    <div className="flex flex-col gap-4">
      {/* ── Header row ── */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-[15px] font-bold tracking-tight text-[var(--text-primary)]">
            Collections Outreach — Executive Summary
          </h1>
          {summary?.snapshot_created_at && (
            <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
              Snapshot:{" "}
              {new Date(summary.snapshot_created_at).toLocaleDateString(undefined, {
                day: "numeric",
                month: "short",
                year: "numeric",
              })}
            </p>
          )}

          {/* View window — send-anchored 7 / 14 / 30 day (drives summary + party table) */}
          <div className="mt-2 flex items-center gap-1.5">
            <span className="text-[11px] font-medium text-[var(--text-muted)]">View</span>
            <div
              role="group"
              aria-label="Reply Tracker view window"
              className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-0.5 shadow-sm"
            >
              {VIEW_OPTIONS.map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => onViewChange(d)}
                  aria-pressed={view === d}
                  className={`rounded-md px-2.5 py-1 text-[12px] font-semibold transition ${
                    view === d
                      ? "bg-[var(--accent-green)]/10 text-[var(--accent-green)]"
                      : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                  }`}
                >
                  {d}D
                </button>
              ))}
            </div>
          </div>
        </div>

        {buList.length > 1 && (
          <label className="flex shrink-0 flex-col gap-1" htmlFor="summary-bu-select">
            <span className="text-[11px] font-medium text-[var(--text-muted)]">
              Select business unit
            </span>
            <select
              id="summary-bu-select"
              value={selectedBu}
              onChange={(e) => onBuChange(e.target.value)}
              className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[13px] text-[var(--text-primary)] shadow-sm outline-none transition focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/35"
            >
              {buList.map((bu) => (
                <option key={bu} value={bu}>
                  {bu === "All" ? "Total" : bu}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {/* ── Loading skeleton ── */}
      {loading && (
        <div
          className="h-64 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
          aria-busy="true"
          aria-label="Loading summary"
        />
      )}

      {!loading && summary && (
        <>
          {/* ── KPI Cards ── */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div
              className="flex flex-col gap-1 rounded-xl p-4"
              style={{ backgroundColor: "#7A1E1E" }}
            >
              <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-white/70">
                Total Overdue (Book)
              </p>
              <p className="text-[24px] font-extrabold leading-none text-white">
                ₹{fmtLakh(summary.total_overdue_book_lakh)} L
              </p>
              <p className="text-[11px] text-white/60">Full book — all HANAs (Lakh)</p>
            </div>

            <div
              className="flex flex-col gap-1 rounded-xl p-4"
              style={{ backgroundColor: "#7A1E1E" }}
            >
              <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-white/70">
                Total Overdue (Book, In Campaign)
              </p>
              <p className="text-[24px] font-extrabold leading-none text-white">
                ₹{fmtLakh(summary.total_overdue_in_campaign_lakh)} L
              </p>
              <p className="text-[11px] text-white/60">
                HANAs we emailed · last {summary.window_days}d (Lakh)
              </p>
            </div>

            <div
              className="flex flex-col gap-1 rounded-xl p-4"
              style={{ backgroundColor: "#1B2A4A" }}
            >
              <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-white/70">
                Total Overdue (Replies)
              </p>
              <p className="text-[24px] font-extrabold leading-none text-white">
                ₹{fmtLakh(summary.total_overdue_replies_lakh)} L
              </p>
              <p className="text-[11px] text-white/60">
                Clients who replied · last {summary.window_days}d (Lakh)
              </p>
            </div>
          </div>

          {/* ── Engagement Metrics ── */}
          <div className="grid grid-cols-3 divide-x divide-[var(--border)] overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-elev)]/60">
            {[
              {
                label: "Emails Delivered",
                value: summary.emails_delivered.toLocaleString("en-IN"),
                sub: `Emailed in last ${summary.window_days}d`,
              },
              {
                label: "Replies Received",
                value: summary.replies_received.toLocaleString("en-IN"),
                sub: `Replied to those emails · last ${summary.window_days}d`,
              },
              {
                label: "Reply Rate",
                value: `${summary.reply_rate.toFixed(1)}%`,
                sub: "Delivered emails replied",
              },
            ].map(({ label, value, sub }) => (
              <div key={label} className="flex flex-col gap-1 px-4 py-3">
                <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--text-muted)]">
                  {label}
                </p>
                <p className="text-[22px] font-extrabold tabular-nums leading-none text-[var(--text-primary)]">
                  {value}
                </p>
                <p className="text-[11px] text-[var(--text-muted)]">{sub}</p>
              </div>
            ))}
          </div>

          {/* ── Reply Breakdown by Category ── */}
          {summary.category_breakdown.length > 0 && (
            <div className="flex flex-col gap-2">
              <p className="text-[12px] font-bold text-[var(--text-primary)]">
                Reply Breakdown by Category
              </p>
              <ReplyBreakdownTable
                rows={summary.category_breakdown.map((r) => ({
                  category: r.category,
                  count: r.reply_count,
                  amount_lakh: r.amount_due_lakh,
                }))}
                totalCount={summary.replies_received}
                totalAmountLakh={summary.total_overdue_replies_lakh}
                showMoney={true}
                activeCategoryFilter={activeCategoryFilter}
                onCategoryFilter={onCategoryFilter}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
