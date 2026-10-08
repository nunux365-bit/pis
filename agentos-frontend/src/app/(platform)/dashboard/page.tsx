"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { HomeAtAGlance } from "@/components/dashboard/HomeAtAGlance";
import { getDashboardSummary, type DashboardSummary } from "@/lib/api";
import { canAccessWorkflowQueue } from "@/lib/workflowQueueAccess";
import { userHasAnyRole } from "@/lib/userAccess";
import { PROCUREMENT_HOME_HREF } from "@/lib/procurementRoutes";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

function greetingForNow(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diffSec = (Date.now() - d.getTime()) / 1000;
  if (diffSec < 30) return "just now";
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.round(diffSec / 3600)}h ago`;
  return `${Math.round(diffSec / 86400)}d ago`;
}

function formatMonthKey(key: string | undefined | null): string | null {
  if (!key) return null;
  const m = /^(\d{4})-(\d{2})$/.exec(key);
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, 1));
  return Number.isNaN(d.getTime())
    ? null
    : d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

export default function DashboardPage() {
  const { user } = useAuth();
  const showReviewQueue = canAccessWorkflowQueue(user?.roles);
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const d = await getDashboardSummary();
        if (!cancelled) {
          setData(d);
          setLoadError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setData(null);
          setLoadError(e instanceof Error ? e.message : "Could not load the dashboard.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadTick]);

  const firstName = user?.full_name?.split(/\s+/)[0] || "there";
  const greet = greetingForNow();
  const billingMonth = useMemo(
    () =>
      formatMonthKey(
        data?.o2c?.current_month?.progress?.month_label ??
        data?.mis?.monthly?.[data.mis.monthly.length - 1]?.month_key ??
        null,
      ),
    [data],
  );

  return (
    <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 pb-6 pt-1">
      {/* Header: keep short so the dashboard body can use vertical space. */}
      <header className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
          <p className="text-[9px] font-semibold uppercase tracking-[0.14em] text-[var(--text-muted)]">
            Home
          </p>
          {billingMonth ? (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-0.5 text-[11px] font-medium text-[var(--text-secondary)]">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-[var(--accent-blue)]" />
              Billing month · {billingMonth} (IST)
            </span>
          ) : null}
          <div className="ml-auto flex items-center gap-3 text-[11px] text-[var(--text-muted)]">
            {data ? (
              <span aria-live="polite">Updated {relativeTime(data.generated_at)}</span>
            ) : null}
            <button
              type="button"
              onClick={() => setReloadTick((n) => n + 1)}
              className="font-semibold text-[var(--accent-blue)] hover:underline disabled:opacity-50"
              disabled={loading}
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-end justify-between gap-2">
          <h1 className="text-lg font-semibold tracking-tight text-[var(--text-primary)] sm:text-xl">
            {greet}, {firstName}
          </h1>
          <div className="flex flex-wrap gap-1.5">
            {showReviewQueue ? (
              <Link
                href={WORKFLOW_DEFAULT_PATH}
                className="inline-flex items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1 text-xs font-semibold text-[var(--text-primary)] transition hover:bg-[var(--bg-elev)]"
              >
                O2C / S2P queue
              </Link>
            ) : null}
            <Link
              href={PROCUREMENT_HOME_HREF}
              className="inline-flex items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1 text-xs font-semibold text-[var(--text-primary)] transition hover:bg-[var(--bg-elev)]"
            >
              Finance requisition
            </Link>
            {userHasAnyRole(user?.roles, "system_admin", "email_agent_access") ? (
              <Link
                href="/admin/email-agent"
                className="inline-flex items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1 text-xs font-semibold text-[var(--text-primary)] transition hover:bg-[var(--bg-elev)]"
              >
                Email Agent
              </Link>
            ) : null}
          </div>
        </div>
      </header>

      {loadError ? (
        <div
          className="rounded-xl border border-[var(--accent-red)]/30 bg-[var(--bg-card)] px-4 py-4"
          role="alert"
        >
          <p className="text-sm text-[var(--accent-red)]">{loadError}</p>
          <button
            type="button"
            onClick={() => setReloadTick((n) => n + 1)}
            className="mt-2 text-sm font-semibold text-[var(--accent-blue)] hover:underline"
          >
            Retry
          </button>
        </div>
      ) : null}

      {!data && !loadError ? (
        <div
          className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-sm"
          aria-live="polite"
        >
          <div className="h-6 w-40 animate-pulse rounded-md bg-[var(--bg-elev)]" />
          <div className="mt-4 h-32 w-full animate-pulse rounded-xl bg-[var(--bg-elev)]" />
          <p className="mt-3 text-sm text-[var(--text-muted)]">Loading your dashboard…</p>
        </div>
      ) : null}

      {data ? <HomeAtAGlance data={data} userRoles={user?.roles} /> : null}
    </div>
  );
}
