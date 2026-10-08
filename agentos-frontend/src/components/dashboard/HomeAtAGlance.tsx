"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  DashboardContractRenewalRow,
  DashboardContractRenewalSummary,
  DashboardContractStageSlice,
  DashboardMisMonthRow,
  DashboardO2cPeriod,
  DashboardO2cTopClient,
  DashboardO2cTopClientsByMonth,
  DashboardO2cTopSite,
  DashboardO2cTopSitesByMonth,
  DashboardProcurementThroughput,
  DashboardSummary,
} from "@/lib/api";
import { canAccessWorkflowQueue } from "@/lib/workflowQueueAccess";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

/**
 * Home dashboard — a finance-ops control tower.
 *
 * Every section is driven off the single `/api/dashboard/summary` aggregate —
 * no additional client round-trips, no email-specific widgets:
 *
 *   1. "Is this billing month on track?"     → `BillingMonthHero`
 *   2. "What needs me now?"                  → `ExceptionsStrip`
 *   3. "Who is making us the most money?"    → `TopClients` + `TopSites`
 *   4. "How is the month trending?"          → 6-month billing trend (INR + run counts)
 *   5. "Contract portfolio & renewals?"      → stage pie + renewal table
 *   6. "Procurement over time?"              → 6-month chart + 30d KPIs
 */

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

function safeInt(n: unknown): number {
  const x = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(x) || x < 0) return 0;
  return Math.floor(x);
}

/** "2026-04" → "Apr 2026" (user locale). */
function formatMonthKey(monthKey: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(monthKey);
  if (!m) return monthKey;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, 1));
  return Number.isNaN(d.getTime())
    ? monthKey
    : d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

/** Indian short form: ₹1.23 Cr / ₹12.3 L / ₹9,999. */
function formatCurrencyShort(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "₹0";
  const crore = n / 1e7;
  const lakh = n / 1e5;
  if (Math.abs(crore) >= 1) return `₹${crore.toFixed(crore >= 10 ? 0 : 2)} Cr`;
  if (Math.abs(lakh) >= 1) return `₹${lakh.toFixed(lakh >= 10 ? 0 : 2)} L`;
  return `₹${Math.round(n).toLocaleString("en-IN")}`;
}

function deltaPct(current: number, prior: number): number | null {
  if (!Number.isFinite(prior) || prior <= 0) return null;
  return ((current - prior) / prior) * 100;
}

// ---------------------------------------------------------------------------
// Atoms
// ---------------------------------------------------------------------------

/** Period-over-period % change chip; renders nothing when the comparison is meaningless. */
function DeltaChip({
  current,
  prior,
  lowerIsBetter = false,
  suffix = "vs prior",
}: {
  current: number;
  prior: number;
  lowerIsBetter?: boolean;
  suffix?: string;
}) {
  if (!Number.isFinite(prior) || prior <= 0) {
    if (current > 0) {
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
          <span aria-hidden>●</span>
          <span>new</span>
        </span>
      );
    }
    return null;
  }
  const pct = deltaPct(current, prior);
  if (pct === null) return null;
  const rounded = Math.round(pct * 10) / 10;
  const up = rounded > 0;
  const flat = rounded === 0;
  const good = flat ? null : lowerIsBetter ? !up : up;
  const tone =
    good === null
      ? "text-[var(--text-muted)]"
      : good
        ? "text-emerald-700 dark:text-emerald-300"
        : "text-rose-700 dark:text-rose-300";
  const arrow = flat ? "→" : up ? "▲" : "▼";
  const pretty =
    Math.abs(rounded) >= 10
      ? Math.round(Math.abs(rounded))
      : Math.abs(rounded).toFixed(1);
  return (
    <span className={`inline-flex items-center gap-1 text-[11px] font-medium ${tone}`}>
      <span aria-hidden>{arrow}</span>
      <span>
        {pretty}% {suffix}
      </span>
    </span>
  );
}

/** Simple horizontal bar — used inside the billing hero for day-of-month progress. */
function Meter({
  pct,
  tone = "var(--accent-blue)",
  className = "",
}: {
  pct: number;
  tone?: string;
  className?: string;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div className={`h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-elev)] ${className}`}>
      <div
        className="h-full rounded-full transition-[width] duration-500"
        style={{ width: `${clamped}%`, background: tone }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page sections — OHC MIS, Contracts, Procurement
// ---------------------------------------------------------------------------

function DashboardSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="scroll-mt-1 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 shadow-sm sm:p-5"
    >
      <h2
        id={`${id}-title`}
        className="mb-3 border-b border-[var(--border)] pb-2 text-base font-semibold tracking-tight text-[var(--text-primary)]"
      >
        {title}
      </h2>
      <div className="min-w-0 space-y-4">{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Billing month hero
// ---------------------------------------------------------------------------

function BillingMonthHero({
  period,
  priorMonthBilled,
  compact = false,
}: {
  period: DashboardO2cPeriod | null;
  priorMonthBilled: number | null;
  /** Denser layout: smaller ring, type — for dashboard that fits the viewport. */
  compact?: boolean;
}) {
  if (!period || !period.revenue || !period.progress) return null;
  const rev = period.revenue;
  const prog = period.progress;
  const totalSites = rev.sites_in_scope;
  const approvedSites = rev.sites_approved;
  const pct = totalSites > 0 ? (approvedSites / totalSites) * 100 : 0;
  const ringPct = Math.max(0, Math.min(100, pct));
  const dayPct =
    prog.days_in_month > 0
      ? Math.min(100, (prog.day_of_month / prog.days_in_month) * 100)
      : 0;
  const daysLeft = Math.max(0, prog.days_in_month - prog.day_of_month);

  const r = compact ? 32 : 56;
  const vb = compact ? 100 : 140;
  const cx = vb / 2;
  const strokeW = compact ? 7 : 12;
  const c = 2 * Math.PI * r;
  const offset = c - (ringPct / 100) * c;
  const ringColor =
    pct >= 75 ? "var(--accent-green)" : pct >= 40 ? "var(--accent-orange)" : "var(--accent-red)";

  return (
    <section
      className={`overflow-hidden rounded-lg border border-[var(--border)] bg-gradient-to-br from-[var(--bg-card)] to-[var(--bg-secondary)] shadow-sm ${
        compact ? "p-2.5" : "rounded-2xl p-5 shadow-sm sm:p-7"
      }`}
      aria-label="Billing month hero"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-1.5">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--text-muted)]">
          This billing month
        </p>
        <Link
          href={WORKFLOW_DEFAULT_PATH}
          className={`rounded border border-[var(--border)] bg-[var(--bg-card)] font-semibold text-[var(--text-primary)] transition hover:bg-[var(--bg-elev)] ${
            compact ? "px-2 py-0.5 text-xs" : "px-3 py-1.5 text-xs shadow-sm"
          }`}
        >
          OHC MIS →
        </Link>
      </div>

      <div
        className={
          compact
            ? "mt-2 grid grid-cols-1 items-center gap-2 sm:grid-cols-[auto_1fr_auto]"
            : "mt-4 grid grid-cols-1 gap-6 md:grid-cols-[auto_1fr_auto]"
        }
      >
        <div className="flex items-center justify-center sm:justify-start">
          <div
            className={`relative shrink-0 ${compact ? "h-[5.5rem] w-[5.5rem]" : "h-36 w-36"}`}
          >
            <svg viewBox={`0 0 ${vb} ${vb}`} className="h-full w-full -rotate-90">
              <circle
                cx={cx}
                cy={cx}
                r={r}
                fill="none"
                stroke="var(--bg-elev)"
                strokeWidth={strokeW}
              />
              <circle
                cx={cx}
                cy={cx}
                r={r}
                fill="none"
                stroke={ringColor}
                strokeWidth={strokeW}
                strokeLinecap="round"
                strokeDasharray={c}
                strokeDashoffset={offset}
                style={{ transition: "stroke-dashoffset 700ms ease" }}
              />
            </svg>
            <div className="absolute inset-0 flex flex-col items-center justify-center">
              <p
                className={`font-semibold leading-none tabular-nums text-[var(--text-primary)] ${
                  compact ? "text-base" : "text-[26px]"
                }`}
              >
                {Math.round(pct)}%
              </p>
              <p
                className={`text-[var(--text-muted)] ${compact ? "mt-px text-xs" : "mt-0.5 text-[11px]"}`}
              >
                sites
              </p>
              <p
                className={`font-medium tabular-nums text-[var(--text-secondary)] ${
                  compact ? "mt-px text-xs" : "mt-1 text-[11px]"
                }`}
              >
                {approvedSites}/{totalSites}
              </p>
            </div>
          </div>
        </div>

        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
            Billed
          </p>
          <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0">
            <p
              className={`font-semibold tabular-nums text-emerald-700 dark:text-emerald-300 ${
                compact ? "text-xl" : "text-4xl sm:text-[44px]"
              }`}
            >
              {formatCurrencyShort(rev.revenue_billed)}
            </p>
            {priorMonthBilled !== null ? (
              <DeltaChip
                current={rev.revenue_billed}
                prior={priorMonthBilled}
                suffix="vs last mo."
              />
            ) : null}
          </div>
          <div
            className={`mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0 text-[var(--text-secondary)] ${
              compact ? "text-xs" : "mt-4"
            }`}
          >
            <span className="font-semibold uppercase text-[var(--text-muted)]">At risk</span>
            <span
              className={`font-semibold tabular-nums text-amber-700 dark:text-amber-300 ${
                compact ? "" : "text-xl"
              }`}
            >
              {formatCurrencyShort(rev.revenue_at_risk)}
            </span>
            <span>· {rev.sites_pending} pending</span>
          </div>
        </div>

        <div className={compact ? "min-w-0 sm:min-w-[9rem]" : "min-w-[180px]"}>
          <p className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
            Month (IST)
          </p>
          <p
            className={`font-semibold tabular-nums text-[var(--text-primary)] ${
              compact ? "mt-0.5 text-sm" : "mt-1 text-2xl"
            }`}
          >
            D{prog.day_of_month}
            <span className="ml-0.5 font-normal text-[var(--text-muted)]">/{prog.days_in_month}</span>
          </p>
          <Meter pct={dayPct} tone="var(--accent-blue)" className={compact ? "mt-0.5" : "mt-2"} />
          <p className={`text-[var(--text-muted)] ${compact ? "mt-0.5 text-xs" : "mt-1 text-[11px]"}`}>
            {daysLeft}d left · EOM
          </p>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Section 2 — Exceptions (compact "Needs you now")
// ---------------------------------------------------------------------------

type ExceptionItem = {
  key: string;
  tone: "red" | "amber" | "slate";
  body: React.ReactNode;
};

function ExceptionsStrip({ items, compact = false }: { items: ExceptionItem[]; compact?: boolean }) {
  if (items.length === 0) {
    return (
      <div
        className={`flex items-center gap-1.5 rounded-md border border-emerald-500/25 bg-emerald-500/5 text-emerald-900 dark:text-emerald-100 ${
          compact ? "px-2 py-1.5 text-[11px]" : "rounded-xl px-4 py-2.5 text-sm"
        }`}
        role="status"
      >
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
        <p>
          <strong className="font-semibold">All clear.</strong>{" "}
          <span className="opacity-90">No threshold breaches right now.</span>
        </p>
      </div>
    );
  }
  return (
    <ul
      className={`grid ${compact ? "gap-1 sm:grid-cols-2" : "gap-2 md:grid-cols-2 xl:grid-cols-3"} grid-cols-1`}
      role="list"
    >
      {items.map((it) => {
        const toneCls =
          it.tone === "red"
            ? "border-rose-500/30 bg-rose-500/5"
            : it.tone === "amber"
              ? "border-amber-500/30 bg-amber-500/5"
              : "border-[var(--border)] bg-[var(--bg-card)]";
        return (
          <li
            key={it.key}
            className={`rounded-md border ${compact ? "px-2 py-1.5 text-xs" : "rounded-xl px-4 py-3"} ${toneCls}`}
          >
            {it.body}
          </li>
        );
      })}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Section 3 — 6-month billing trend (full-width, detail on demand)
// ---------------------------------------------------------------------------

function BillingTrend({
  rows,
  compact = false,
}: {
  rows: DashboardMisMonthRow[];
  compact?: boolean;
}) {
  const [showTable, setShowTable] = useState(false);
  const data = useMemo(
    () =>
      rows.map((r) => ({
        name: formatMonthKey(r.month_key),
        billed: Number(r.revenue_billed) || 0,
        at_risk: Number(r.revenue_at_risk) || 0,
        approved: safeInt(r.approved),
        rejected: safeInt(r.rejected),
        human_corrected: safeInt(r.human_corrected_runs),
      })),
    [rows],
  );
  const empty =
    data.length === 0 ||
    data.every(
      (d) =>
        d.billed + d.at_risk + d.approved + d.rejected + d.human_corrected === 0,
    );

  const totalBilled = rows.reduce((acc, r) => acc + (Number(r.revenue_billed) || 0), 0);
  const totalAtRisk = rows.reduce((acc, r) => acc + (Number(r.revenue_at_risk) || 0), 0);

  return (
    <section
      className={`border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${
        compact ? "rounded-lg p-2.5" : "rounded-2xl p-5"
      }`}
      aria-labelledby="dashboard-trend-heading"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2
            id="dashboard-trend-heading"
            className={`font-semibold text-[var(--text-primary)] ${compact ? "text-xs" : "text-sm"}`}
          >
            Billing trend · 6 mo (IST)
          </h2>
          {!compact ? (
            <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
              Billed and at-risk INR (bars); run counts: approved, rejected, and MIS runs with
              human-corrected line items (right axis).
            </p>
          ) : (
            <p className="mt-px line-clamp-1 text-xs text-[var(--text-muted)]">
              Bars: INR · Lines: runs (approved, rejected, human-corrected)
            </p>
          )}
        </div>
        <div className={`flex items-center gap-2 ${compact ? "text-xs" : "text-[11px]"}`}>
          <span className="inline-flex items-center gap-1.5 text-[var(--text-secondary)]">
            <span aria-hidden className="h-2 w-2 rounded-sm bg-[var(--accent-green)]" />
            Billed {formatCurrencyShort(totalBilled)} · 6mo
          </span>
          <span className="inline-flex items-center gap-1.5 text-[var(--text-secondary)]">
            <span aria-hidden className="h-2 w-2 rounded-sm bg-[var(--accent-orange)]" />
            At risk {formatCurrencyShort(totalAtRisk)}
          </span>
        </div>
      </div>

      {empty ? (
        <p className={`text-[var(--text-muted)] ${compact ? "mt-1 text-[11px]" : "mt-4 text-sm"}`}>
          No MIS activity in the last 6 months, or billing DB is unavailable.
        </p>
      ) : (
        <>
          <div className={`w-full min-w-0 ${compact ? "mt-1.5 h-40" : "mt-4 h-72"}`}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 12, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-[var(--border)]" />
                <XAxis dataKey="name" tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
                <YAxis
                  yAxisId="inr"
                  tickFormatter={(v: number) => formatCurrencyShort(Number(v))}
                  tick={{ fontSize: 10, fill: "var(--text-muted)" }}
                  width={56}
                />
                <YAxis
                  yAxisId="runs"
                  orientation="right"
                  allowDecimals={false}
                  tick={{ fontSize: 10, fill: "var(--text-muted)" }}
                  width={32}
                />
                <Tooltip
                  contentStyle={{
                    background: "var(--bg-card)",
                    border: "1px solid var(--border)",
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                  formatter={(value: unknown, name: unknown) => {
                    const key = String(name ?? "");
                    if (key === "Billed" || key === "At risk") {
                      return [formatCurrencyShort(Number(value)), key];
                    }
                    return [String(value), key];
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                <Bar
                  yAxisId="inr"
                  dataKey="billed"
                  name="Billed"
                  fill="var(--accent-green)"
                  radius={[3, 3, 0, 0]}
                />
                <Bar
                  yAxisId="inr"
                  dataKey="at_risk"
                  name="At risk"
                  fill="var(--accent-orange)"
                  radius={[3, 3, 0, 0]}
                />
                <Line
                  yAxisId="runs"
                  type="monotone"
                  dataKey="approved"
                  name="Approved runs"
                  stroke="var(--accent-blue)"
                  strokeWidth={2}
                  dot={{ r: 2 }}
                  isAnimationActive={false}
                />
                <Line
                  yAxisId="runs"
                  type="monotone"
                  dataKey="rejected"
                  name="Rejected"
                  stroke="var(--accent-red)"
                  strokeWidth={2}
                  strokeDasharray="4 3"
                  dot={{ r: 2 }}
                  isAnimationActive={false}
                />
                <Line
                  yAxisId="runs"
                  type="monotone"
                  dataKey="human_corrected"
                  name="Human-corrected"
                  stroke="var(--accent-purple)"
                  strokeWidth={2}
                  dot={{ r: 2 }}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          <button
            type="button"
            onClick={() => setShowTable((v) => !v)}
            className="mt-3 text-xs font-semibold text-[var(--accent-blue)] hover:underline"
            aria-expanded={showTable}
          >
            {showTable ? "Hide monthly detail" : "Show monthly detail"}
          </button>

          {showTable ? (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full min-w-[760px] border-collapse text-sm">
                <thead>
                  <tr className="text-[11px] uppercase tracking-wide text-[var(--text-muted)]">
                    <th className="py-2 text-left font-medium">Month</th>
                    <th className="py-2 text-right font-medium">Billed</th>
                    <th className="py-2 text-right font-medium">At risk</th>
                    <th className="py-2 text-right font-medium">Approved</th>
                    <th className="py-2 text-right font-medium">Pending</th>
                    <th className="py-2 text-right font-medium">Rejected</th>
                    <th className="py-2 text-right font-medium">Human-corrected</th>
                    <th className="py-2 text-right font-medium">Sites billed</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {[...rows].reverse().map((r, idx) => (
                    <tr key={r.month_key} className={idx === 0 ? "font-medium" : ""}>
                      <td className="py-2 text-[var(--text-primary)]">{formatMonthKey(r.month_key)}</td>
                      <td className="py-2 text-right tabular-nums text-emerald-700 dark:text-emerald-300">
                        {formatCurrencyShort(r.revenue_billed)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-amber-700 dark:text-amber-300">
                        {formatCurrencyShort(r.revenue_at_risk)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-[var(--text-primary)]">
                        {safeInt(r.approved)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-[var(--text-secondary)]">
                        {safeInt(r.pending_human)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-rose-700 dark:text-rose-300">
                        {safeInt(r.rejected)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-purple-700 dark:text-purple-300">
                        {safeInt(r.human_corrected_runs)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-[var(--text-secondary)]">
                        {safeInt(r.sites_approved)} / {safeInt(r.sites_in_scope)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Section — Top clients / top sites (horizontal bars, INR-first)
// ---------------------------------------------------------------------------

type RankedMoneyRow = {
  id: string;
  label: string;
  sublabel?: string | null;
  billed: number;
  atRisk: number;
  extra?: string | null;
};

function RankedMoneyList({
  rows,
  emptyMessage,
  variant = "default",
}: {
  rows: RankedMoneyRow[];
  emptyMessage: string;
  variant?: "default" | "dense";
}) {
  const dense = variant === "dense";
  if (rows.length === 0) {
    return (
      <p className={dense ? "text-sm text-[var(--text-muted)]" : "text-sm text-[var(--text-muted)]"}>
        {emptyMessage}
      </p>
    );
  }
  const max = Math.max(
    1,
    ...rows.map((r) => (r.billed || 0) + (r.atRisk || 0)),
  );
  return (
    <ul className={dense ? "space-y-2" : "space-y-3"}>
      {rows.map((r) => {
        const total = (r.billed || 0) + (r.atRisk || 0);
        const pctBilled = total > 0 ? (r.billed / max) * 100 : 0;
        const pctRisk = total > 0 ? (r.atRisk / max) * 100 : 0;
        return (
          <li
            key={r.id}
            className={dense ? "flex items-center gap-2 text-sm" : "flex items-center gap-3 text-sm"}
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span
                  className="truncate font-medium text-[var(--text-primary)]"
                  title={r.label}
                >
                  {r.label}
                </span>
                <span className="shrink-0 tabular-nums text-[var(--text-primary)]">
                  {formatCurrencyShort(total)}
                </span>
              </div>
              <div
                className={`mt-0.5 flex w-full overflow-hidden rounded-full bg-[var(--bg-elev)] ${
                  dense ? "h-1.5" : "mt-1 h-2"
                }`}
              >
                <span
                  className="h-full"
                  style={{
                    width: `${pctBilled}%`,
                    background: "var(--accent-green)",
                  }}
                  aria-hidden
                />
                <span
                  className="h-full"
                  style={{
                    width: `${pctRisk}%`,
                    background: "var(--accent-orange)",
                  }}
                  aria-hidden
                />
              </div>
              {!dense ? (
                <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-[var(--text-secondary)]">
                  <span className="truncate" title={r.sublabel ?? undefined}>
                    {r.sublabel || ""}
                  </span>
                  <span className="shrink-0 tabular-nums">
                    <span className="text-emerald-700 dark:text-emerald-300">
                      {formatCurrencyShort(r.billed)} billed
                    </span>
                    {r.atRisk > 0 ? (
                      <>
                        {" · "}
                        <span className="text-amber-700 dark:text-amber-300">
                          {formatCurrencyShort(r.atRisk)} at-risk
                        </span>
                      </>
                    ) : null}
                    {r.extra ? (
                      <>
                        {" · "}
                        <span>{r.extra}</span>
                      </>
                    ) : null}
                  </span>
                </div>
              ) : (
                <div className="mt-0.5 truncate text-sm text-[var(--text-secondary)]" title={r.sublabel ?? ""}>
                  {[r.sublabel, r.extra].filter(Boolean).join(" · ")}
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function mapClientRowsToRanked(rows: DashboardO2cTopClient[]): RankedMoneyRow[] {
  return rows.map((r) => ({
    id: r.client_id || r.client_name,
    label: r.client_name,
    sublabel: `${r.sites} site${r.sites === 1 ? "" : "s"}`,
    billed: Number(r.revenue_billed) || 0,
    atRisk: Number(r.revenue_at_risk) || 0,
    extra:
      r.approved_runs + r.pending_runs > 0
        ? `${r.approved_runs}/${r.approved_runs + r.pending_runs} runs approved`
        : null,
  }));
}

function mapSiteRowsToRanked(rows: DashboardO2cTopSite[]): RankedMoneyRow[] {
  return rows.map((r) => ({
    id: r.site_id || r.site_name,
    label: r.site_name,
    sublabel: [r.client_name, r.city].filter(Boolean).join(" · "),
    billed: Number(r.revenue_billed) || 0,
    atRisk: Number(r.revenue_at_risk) || 0,
    extra: r.last_status ? r.last_status.replace(/_/g, " ") : null,
  }));
}

type RevenueDimension = "clients" | "sites";

/** One surface: same metric and time range, two analytical lenses (parent org vs. location). */
function RevenueLeadersCard({
  clientsByMonth,
  sitesByMonth,
}: {
  clientsByMonth: DashboardO2cTopClientsByMonth[];
  sitesByMonth: DashboardO2cTopSitesByMonth[];
}) {
  const [dim, setDim] = useState<RevenueDimension>("clients");
  const byMonth = dim === "clients" ? clientsByMonth : sitesByMonth;

  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-elev)]/30 p-3 shadow-sm sm:p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-[var(--text-primary)]">Revenue leaders</h3>
          <p className="mt-1 text-sm leading-relaxed text-[var(--text-muted)]">
            Last 6 IST months — ranked by <strong className="font-medium text-[var(--text-secondary)]">billed + at-risk</strong>{" "}
            INR (same definition as the billing trend). Switch lens below.
          </p>
          <p className="mt-2 text-sm leading-relaxed text-[var(--text-muted)]">
            <span className="font-medium text-[var(--text-secondary)]">By client</span> rolls up all service sites
            under each billing parent.&nbsp;
            <span className="font-medium text-[var(--text-secondary)]">By site</span> shows individual locations
            (one client can place several sites in the list).
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-stretch gap-1.5 sm:items-end">
          <span className="self-end text-xs font-medium uppercase tracking-wider text-[var(--text-muted)]">
            IST
          </span>
          <div
            className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-0.5 shadow-inner"
            role="tablist"
            aria-label="Revenue ranking dimension"
          >
            {(
              [
                { id: "clients" as const, label: "By client" },
                { id: "sites" as const, label: "By site" },
              ] as const
            ).map(({ id, label }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={dim === id}
                className={`rounded-md px-3 py-1.5 text-xs font-semibold transition ${
                  dim === id
                    ? "bg-[var(--bg-elev)] text-[var(--text-primary)] shadow-sm"
                    : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                }`}
                onClick={() => setDim(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {byMonth.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--text-muted)]">No billing data in this window yet.</p>
      ) : (
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-6">
          {byMonth.map((m) => (
            <div
              key={m.month_key}
              className="min-w-0 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-2.5"
            >
              <div className="text-sm font-semibold text-[var(--text-secondary)]">
                {formatMonthKey(m.month_key)}
              </div>
              <div className="mt-2">
                <RankedMoneyList
                  variant="dense"
                  rows={
                    dim === "clients"
                      ? mapClientRowsToRanked((m as DashboardO2cTopClientsByMonth).rows)
                      : mapSiteRowsToRanked((m as DashboardO2cTopSitesByMonth).rows)
                  }
                  emptyMessage="—"
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Contract stages (pie)
// ---------------------------------------------------------------------------

const STAGE_PIE_COLORS: Record<string, string> = {
  expired: "var(--accent-red)",
  expiring_0_90d: "var(--accent-orange)",
  expiring_91_180d: "#ca8a04",
  active_open_ended: "var(--accent-green)",
  pending: "var(--accent-blue)",
  no_end_date: "var(--text-muted)",
  other: "var(--accent-cyan)",
};

function stageLabel(stage: string): string {
  const m: Record<string, string> = {
    expired: "Expired",
    expiring_0_90d: "Expiring ≤90d",
    expiring_91_180d: "Expiring 91–180d",
    active_open_ended: "Active (future)",
    pending: "Pending",
    no_end_date: "No end date",
  };
  return m[stage] ?? stage.replace(/_/g, " ");
}

function ContractStagesPie({
  slices,
  compact = false,
}: {
  slices: DashboardContractStageSlice[];
  compact?: boolean;
}) {
  const data = useMemo(
    () =>
      slices
        .filter((s) => s.count > 0)
        .map((s) => ({
          name: stageLabel(s.stage),
          value: s.count,
          key: s.stage,
        })),
    [slices],
  );
  if (data.length === 0) {
    return (
      <div
        className={`flex items-center justify-center rounded-lg border border-dashed border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-muted)] ${
          compact ? "h-28 p-2 text-[11px]" : "h-48 p-4 text-sm"
        }`}
      >
        No site-mapped contract terms in the billing database.
      </div>
    );
  }
  return (
    <div
      className={`border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${
        compact ? "rounded-lg p-2" : "rounded-2xl p-4"
      }`}
    >
      <h3 className={`font-semibold text-[var(--text-primary)] ${compact ? "text-sm" : "text-base"}`}>
        Portfolio by stage
      </h3>
      <p
        className={`mt-1 text-[var(--text-muted)] ${compact ? "text-sm leading-snug" : "text-sm"}`}
      >
        Site-mapped terms only: active rate line on a service site. IST date bands first; &quot;Pending&quot; is
        for end dates beyond 180d. Rejected terms excluded.
      </p>
      <div
        className={`mt-1 flex flex-col items-center gap-1 sm:flex-row sm:items-start ${compact ? "" : "mt-2 gap-2"}`}
      >
        <div className={`shrink-0 ${compact ? "h-28 w-28" : "h-44 w-44"}`}>
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                innerRadius={compact ? 22 : 40}
                outerRadius={compact ? 40 : 68}
                paddingAngle={1}
                stroke="var(--bg-card)"
                strokeWidth={2}
                isAnimationActive={false}
              >
                {data.map((d) => (
                  <Cell
                    key={d.key}
                    fill={STAGE_PIE_COLORS[d.key] ?? STAGE_PIE_COLORS.other}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  fontSize: 12,
                }}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <ul className={`min-w-0 flex-1 space-y-0.5 ${compact ? "text-sm" : "space-y-1 text-sm"}`}>
          {data.map((d) => (
            <li key={d.key} className="flex items-center justify-between gap-1">
              <span className="flex min-w-0 items-center gap-2">
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{
                    background: STAGE_PIE_COLORS[d.key] ?? STAGE_PIE_COLORS.other,
                  }}
                  aria-hidden
                />
                <span className="truncate text-[var(--text-secondary)]">{d.name}</span>
              </span>
              <span className="shrink-0 tabular-nums font-medium text-[var(--text-primary)]">
                {d.value}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section — Contract renewal watch
// ---------------------------------------------------------------------------

function rowUrgencyBorder(u: string): string {
  if (u === "expired") return "border-l-4 border-l-rose-500 bg-rose-500/5";
  if (u === "orange") return "border-l-4 border-l-orange-500 bg-orange-500/5";
  if (u === "yellow") return "border-l-4 border-l-amber-500 bg-amber-500/5";
  return "border-l-4 border-l-transparent";
}

function RenewalWatchCard({
  summary,
  rows,
  compact = false,
}: {
  summary: DashboardContractRenewalSummary | null;
  rows: DashboardContractRenewalRow[];
  compact?: boolean;
}) {
  return (
    <section
      aria-labelledby="dashboard-ren-heading"
      className={`border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${
        compact ? "rounded-lg p-2" : "rounded-2xl p-5"
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-1.5">
        <div>
          <h3
            id="dashboard-ren-heading"
            className={`font-semibold text-[var(--text-primary)] ${compact ? "text-xs" : "text-sm"}`}
          >
            Up for renewal
          </h3>
          {!compact ? (
            <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
              <span className="text-rose-600 dark:text-rose-400">Red</span> = expired
              (past end) ·{" "}
              <span className="text-orange-600 dark:text-orange-300">Orange</span> = 0–90
              days · <span className="text-amber-600 dark:text-amber-300">Yellow</span> =
              91–180 days. Monthly estimate from active rate lines.
            </p>
          ) : (
            <p className="text-xs text-[var(--text-muted)]">Red / orange / yellow by horizon</p>
          )}
        </div>
        {summary ? (
          <div className="text-right">
            <p
              className={`font-semibold tabular-nums text-[var(--text-primary)] ${
                compact ? "text-sm" : "text-[20px]"
              }`}
            >
              {formatCurrencyShort(summary.monthly_estimate_total)}{" "}
              <span className="text-[11px] font-normal text-[var(--text-muted)]">
                / month
              </span>
            </p>
            <p className={`text-[var(--text-secondary)] ${compact ? "text-xs" : "text-[11px]"}`}>
              {summary.count_total} in scope
              {summary.count_expired > 0 ? (
                <>
                  {" · "}
                  <span className="text-rose-600 dark:text-rose-400">
                    {summary.count_expired} expired
                  </span>
                </>
              ) : null}
              {summary.count_orange > 0 ? (
                <>
                  {" · "}
                  <span className="text-orange-600 dark:text-orange-300">
                    {summary.count_orange} ≤90d
                  </span>
                </>
              ) : null}
              {summary.count_yellow > 0 ? (
                <>
                  {" · "}
                  <span className="text-amber-600 dark:text-amber-300">
                    {summary.count_yellow} 91–180d
                  </span>
                </>
              ) : null}
            </p>
          </div>
        ) : null}
      </div>

      {rows.length === 0 ? (
        <p className={`text-[var(--text-muted)] ${compact ? "mt-1.5 text-[11px]" : "mt-4 text-sm"}`}>
          No contracts in the next 6 months (or no recently expired) — you&apos;re
          clear.
        </p>
      ) : (
        <div className={`overflow-x-auto ${compact ? "mt-2" : "mt-4"}`}>
          <table className={`min-w-full ${compact ? "text-xs" : "text-sm"}`}>
            <thead>
              <tr
                className={`text-left font-semibold uppercase text-[var(--text-muted)] ${
                  compact ? "text-xs" : "text-[11px]"
                }`}
              >
                <th className={compact ? "py-1 pr-2" : "py-2 pr-3"}>Client</th>
                <th className={compact ? "py-1 pr-2" : "py-2 pr-3"}>Expires</th>
                <th className={compact ? "py-1 pr-2" : "py-2 pr-3"}>In</th>
                <th className={compact ? "py-1 pr-2" : "py-2 pr-3"}>Sites</th>
                <th className={`text-right ${compact ? "py-1 pr-2" : "py-2 pr-3"}`}>
                  {compact ? "Mo. est." : "Monthly est."}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {rows.map((r) => {
                const u = r.urgency || "ok";
                const expired = u === "expired";
                const cp = compact ? "py-1 pr-2" : "py-2 pr-3";
                return (
                  <tr
                    key={r.contract_id || `${r.client_name}-${r.effective_to}`}
                    className={rowUrgencyBorder(u)}
                  >
                    <td
                      className={`${cp} font-medium text-[var(--text-primary)]`}
                      title={r.client_name}
                    >
                      {r.client_name}
                    </td>
                    <td className={`${cp} text-[var(--text-secondary)]`}>{r.effective_to}</td>
                    <td
                      className={`${cp} font-medium ${
                        expired
                          ? "text-rose-700 dark:text-rose-300"
                          : u === "orange"
                            ? "text-orange-700 dark:text-orange-300"
                            : u === "yellow"
                              ? "text-amber-700 dark:text-amber-300"
                              : "text-[var(--text-secondary)]"
                      }`}
                    >
                      {expired
                        ? `${Math.abs(r.days_remaining)}d overdue`
                        : `${r.days_remaining}d`}
                    </td>
                    <td className={`${cp} tabular-nums text-[var(--text-secondary)]`}>
                      {r.sites || "—"}
                    </td>
                    <td className={`${cp} text-right tabular-nums text-[var(--text-primary)]`}>
                      {r.monthly_estimate > 0
                        ? formatCurrencyShort(r.monthly_estimate)
                        : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Section — Procurement (30d KPIs + 6-month trend)
// ---------------------------------------------------------------------------

function ProcurementSection({
  stats,
  compact = false,
}: {
  stats: DashboardProcurementThroughput;
  compact?: boolean;
}) {
  const chartData = useMemo(() => {
    const monthly = stats.monthly_6m ?? [];
    return [...monthly].reverse().map((m) => ({
      name: formatMonthKey(m.month_key),
      created: m.created,
      posted: m.posted,
      posted_pct: m.posted_pct,
    }));
  }, [stats.monthly_6m]);
  const kpis = [
    {
      label: "Created (30d)",
      value: stats.created,
      tone: "text-[var(--text-primary)]",
    },
    {
      label: "Posted to SAP",
      value: stats.posted,
      tone: "text-emerald-700 dark:text-emerald-300",
      suffix:
        stats.created > 0 ? (
          <span className="ml-1 text-[11px] text-[var(--text-muted)]">
            ({stats.posted_pct.toFixed(0)}%)
          </span>
        ) : null,
    },
    {
      label: "Pending SAP id",
      value: stats.pending_sap,
      tone:
        stats.pending_sap > 0
          ? "text-amber-700 dark:text-amber-300"
          : "text-[var(--text-secondary)]",
    },
    {
      label: "Stuck ≥ 7 days",
      value: stats.stuck_ge_7d,
      tone:
        stats.stuck_ge_7d > 0
          ? "text-rose-700 dark:text-rose-300"
          : "text-[var(--text-secondary)]",
    },
  ] as const;

  return (
    <section
      aria-labelledby="dashboard-proc-heading"
      className={`border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${
        compact ? "rounded-lg p-2" : "rounded-2xl p-5"
      }`}
    >
      <div className="flex items-baseline justify-between gap-2">
        {!compact ? (
          <h3
            id="dashboard-proc-heading"
            className="text-sm font-semibold text-[var(--text-primary)]"
          >
            Procurement
          </h3>
        ) : (
          <p
            id="dashboard-proc-heading"
            className="text-xs font-medium text-[var(--text-muted)]"
          >
            30d snapshot · 6 mo / UTC
          </p>
        )}
        <Link
          href="/procurement/tickets"
          className={`font-semibold text-[var(--accent-blue)] hover:underline ${
            compact ? "text-xs" : "text-[11px]"
          }`}
        >
          Tickets →
        </Link>
      </div>
      {!compact ? (
        <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
          Left: last 30 days snapshot. Right: tickets created per UTC month and how
          many received a SAP id.
        </p>
      ) : null}
      <div className={`grid grid-cols-2 sm:grid-cols-4 ${compact ? "mt-1.5 gap-1" : "mt-4 gap-3"}`}>
        {kpis.map((k) => (
          <div
            key={k.label}
            className={`rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] ${
              compact ? "p-1.5" : "rounded-xl p-3"
            }`}
          >
            <p
              className={`font-semibold uppercase text-[var(--text-muted)] ${
                compact ? "text-[11px] leading-tight" : "text-xs tracking-wide"
              }`}
            >
              {k.label}
            </p>
            <p
              className={`font-semibold tabular-nums ${k.tone} ${
                compact ? "mt-0.5 text-base" : "mt-1 text-[22px]"
              }`}
            >
              {k.value}
              {"suffix" in k ? k.suffix : null}
            </p>
          </div>
        ))}
      </div>

      {chartData.length > 0 ? (
        <div className={`w-full min-w-0 ${compact ? "mt-1.5" : "mt-5"}`}>
          <p className={`font-semibold text-[var(--text-muted)] ${compact ? "text-[11px]" : "text-[11px]"}`}>
            6 months (UTC)
          </p>
          <div className={compact ? "mt-0.5 h-28" : "mt-2 h-48"}>
            <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-[var(--border)]" />
              <XAxis dataKey="name" tick={{ fontSize: 10, fill: "var(--text-muted)" }} />
              <YAxis
                yAxisId="c"
                allowDecimals={false}
                tick={{ fontSize: 10, fill: "var(--text-muted)" }}
                width={28}
              />
              <YAxis
                yAxisId="pct"
                domain={[0, 100]}
                orientation="right"
                tick={{ fontSize: 10, fill: "var(--text-muted)" }}
                unit="%"
                width={32}
              />
              <Tooltip
                contentStyle={{
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  fontSize: 12,
                }}
              />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              <Bar
                yAxisId="c"
                dataKey="created"
                name="Created"
                fill="var(--accent-blue)"
                radius={[2, 2, 0, 0]}
                maxBarSize={28}
              />
              <Bar
                yAxisId="c"
                dataKey="posted"
                name="SAP posted"
                fill="var(--accent-green)"
                radius={[2, 2, 0, 0]}
                maxBarSize={28}
              />
              <Line
                yAxisId="pct"
                type="monotone"
                dataKey="posted_pct"
                name="Posted %"
                stroke="var(--accent-orange)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
          </div>
        </div>
      ) : null}

      {stats.by_kind.length > 0 ? (
        <div className={compact ? "mt-1.5" : "mt-4"}>
          <p
            className={`font-semibold uppercase text-[var(--text-muted)] ${
              compact ? "text-[11px]" : "text-[11px] tracking-wide"
            }`}
          >
            By kind (30d)
          </p>
          <ul
            className={`mt-1 flex flex-wrap gap-1 ${compact ? "text-xs" : "mt-2 gap-2 text-[12px]"}`}
          >
            {stats.by_kind.map((k) => (
              <li
                key={k.kind}
                className={`inline-flex items-center gap-0.5 rounded-full border border-[var(--border)] bg-[var(--bg-elev)] ${
                  compact ? "px-1 py-0" : "px-2 py-0.5"
                }`}
                title={`${k.posted}/${k.count} posted`}
              >
                <span className="font-mono text-[11px] text-[var(--text-secondary)]">
                  {k.kind}
                </span>
                <span className="tabular-nums text-[var(--text-primary)]">
                  {k.posted}/{k.count}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

type Props = {
  data: DashboardSummary;
  userRoles: string[] | undefined;
};

export function HomeAtAGlance({ data, userRoles }: Props) {
  const showQueue = canAccessWorkflowQueue(userRoles);

  // ------- Headline numbers -------

  const queuePending = safeInt(data.pending_approvals);
  const unreadN = safeInt(data.operator.notifications_unread);
  const wfFailed = safeInt(data.operator.workflow.failed_30d);
  const penSap = safeInt(data.operator.procurement.pending_sap_id);

  // ------- Approvals -------

  const overdue = safeInt(data.approvals.aging.lt_7d) + safeInt(data.approvals.aging.gte_7d);

  // ------- MIS / O2C -------

  const misMonthly = data.mis?.monthly ?? [];
  const misCur = data.mis?.current_month ?? null;
  const priorMonthBilled =
    misMonthly.length >= 2
      ? Number(misMonthly[misMonthly.length - 2].revenue_billed) || 0
      : null;
  const o2cCur = data.o2c?.current_month ?? null;

  // ------- Exceptions -------

  const misPending = misCur ? safeInt(misCur.pending_human) : 0;

  const exceptions: ExceptionItem[] = [];
  if (overdue > 0) {
    exceptions.push({
      key: "overdue",
      tone: "red",
      body: (
        <>
          <p className="text-sm font-medium text-rose-900 dark:text-rose-200">
            {overdue} approval{overdue === 1 ? "" : "s"} aged 3+ days
          </p>
          <p className="mt-0.5 text-[11px] text-rose-800/80 dark:text-rose-100/80">
            Clear these first — aging beyond 3 days is typically past SLA.
          </p>
          {showQueue ? (
            <Link
              href={WORKFLOW_DEFAULT_PATH}
              className="mt-1 inline-block text-xs font-semibold text-rose-800 hover:underline dark:text-rose-200"
            >
              Open queue →
            </Link>
          ) : null}
        </>
      ),
    });
  }
  if (showQueue && queuePending > 0 && overdue === 0) {
    exceptions.push({
      key: "queue",
      tone: "slate",
      body: (
        <>
          <p className="text-sm font-medium text-[var(--text-primary)]">
            {queuePending} approval{queuePending === 1 ? "" : "s"} waiting in your queue
          </p>
          <Link
            href={WORKFLOW_DEFAULT_PATH}
            className="mt-1 inline-block text-xs font-semibold text-[var(--accent-blue)] hover:underline"
          >
            Open queue →
          </Link>
        </>
      ),
    });
  }
  if (misPending > 0) {
    exceptions.push({
      key: "mis",
      tone: "amber",
      body: (
        <>
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            {misPending} MIS run{misPending === 1 ? "" : "s"} pending human review
          </p>
          <p className="mt-0.5 text-[11px] text-amber-900/80 dark:text-amber-100/80">
            Current IST month — finish these to bill the remaining sites.
          </p>
          {showQueue ? (
            <Link
              href={WORKFLOW_DEFAULT_PATH}
              className="mt-1 inline-block text-xs font-semibold text-amber-800 hover:underline dark:text-amber-200"
            >
              Open OHC MIS →
            </Link>
          ) : null}
        </>
      ),
    });
  }
  if (wfFailed > 0) {
    exceptions.push({
      key: "wf",
      tone: "red",
      body: (
        <>
          <p className="text-sm font-medium text-rose-900 dark:text-rose-200">
            {wfFailed} workflow run{wfFailed === 1 ? "" : "s"} failed in last 30 days
          </p>
          <Link
            href="/sessions"
            className="mt-1 inline-block text-xs font-semibold text-rose-800 hover:underline dark:text-rose-200"
          >
            Open Sessions →
          </Link>
        </>
      ),
    });
  }
  if (penSap > 0) {
    exceptions.push({
      key: "sap",
      tone: "amber",
      body: (
        <>
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            {penSap} requisition ticket{penSap === 1 ? "" : "s"} missing a SAP id
          </p>
          <Link
            href="/procurement/tickets"
            className="mt-1 inline-block text-xs font-semibold text-amber-800 hover:underline dark:text-amber-200"
          >
            View tickets →
          </Link>
        </>
      ),
    });
  }
  if (unreadN > 0) {
    exceptions.push({
      key: "bell",
      tone: "slate",
      body: (
        <>
          <p className="text-sm font-medium text-[var(--text-primary)]">
            {unreadN} unread notification{unreadN === 1 ? "" : "s"}
          </p>
          <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
            Open the bell in the top bar to read and clear them.
          </p>
        </>
      ),
    });
  }

  const hasMisData = misMonthly.length > 0;

  const topClients = data.o2c?.top_clients ?? [];
  const topSites = data.o2c?.top_sites ?? [];
  const renewalSummary = data.contracts?.summary ?? null;
  const renewalRows = data.contracts?.contracts ?? [];
  const contractStages = data.contracts?.stages ?? [];
  const procurement = data.procurement;

  // ------- Render: OHC MIS, Contracts, Procurement — spaced for readability, avoid inner scrollbars

  return (
    <div className="flex w-full flex-col gap-5 sm:gap-6">
      <DashboardSection id="section-ohc" title="OHC MIS">
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
          <div className="min-w-0 space-y-3 xl:col-span-5">
            {o2cCur && o2cCur.revenue && o2cCur.progress ? (
              <BillingMonthHero
                period={o2cCur}
                priorMonthBilled={priorMonthBilled}
                compact
              />
            ) : (
              <div className="rounded-lg border border-dashed border-[var(--border)] bg-[var(--bg-elev)]/40 p-2 text-center text-[11px] text-[var(--text-muted)]">
                <p className="font-medium text-[var(--text-primary)]">No billing snapshot yet</p>
                <p className="mt-0.5">MIS data will fill this in.</p>
              </div>
            )}
            <div>
              <p
                id="dashboard-exc-heading"
                className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]"
              >
                Needs you
              </p>
              <div className="mt-1.5">
                <ExceptionsStrip items={exceptions} />
              </div>
            </div>
          </div>
          <div className="min-w-0 xl:col-span-7">
            {hasMisData ? <BillingTrend rows={misMonthly} compact /> : null}
          </div>
        </div>
        <RevenueLeadersCard clientsByMonth={topClients} sitesByMonth={topSites} />
      </DashboardSection>

      <DashboardSection id="section-contracts" title="Contracts">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="min-w-0 lg:col-span-4">
            <ContractStagesPie slices={contractStages} compact />
          </div>
          <div className="min-w-0 lg:col-span-8">
            <RenewalWatchCard summary={renewalSummary} rows={renewalRows} compact />
          </div>
        </div>
      </DashboardSection>

      <DashboardSection id="section-procurement" title="Procurement">
        {procurement ? (
          <ProcurementSection stats={procurement} compact />
        ) : (
          <p className="text-center text-[11px] text-[var(--text-muted)]">
            Procurement summary unavailable.
          </p>
        )}
      </DashboardSection>
    </div>
  );
}
