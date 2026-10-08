"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getDashboardSummary, type DashboardSummary } from "@/lib/api";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

export default function AnalyticsPage() {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await getDashboardSummary();
        if (!cancelled) {
          setData(d);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setData(null);
          setErr(e instanceof Error ? e.message : "Failed to load");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadTick]);

  return (
    <div className="mx-auto w-full max-w-5xl pb-8">
      <h1 className="text-2xl font-semibold text-[var(--text-primary)]">Analytics</h1>
      <p className="mt-2 text-sm text-[var(--text-secondary)]">
        Compact tabular view of the same aggregate snapshot as{" "}
        <Link className="font-semibold text-[var(--accent-blue)] hover:underline" href="/dashboard">
          Home
        </Link>{" "}
        (one API call). Home has the charts, trends, and month-close pulse.
      </p>

      {err && <p className="mt-4 text-sm text-[var(--accent-red)]">{err}</p>}
      {!data && !err && <p className="mt-6 text-sm text-[var(--text-muted)]">Loading…</p>}

      {data ? (
        <div className="mt-6 space-y-6">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Review queue (scope)</h3>
              <p className="mt-1 text-3xl font-bold tabular-nums">{data.pending_approvals}</p>
              <Link className="mt-2 inline-block text-sm text-[var(--accent-blue)] hover:underline" href={WORKFLOW_DEFAULT_PATH}>
                Open queue
              </Link>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Automation (30d)</h3>
              <p className="mt-1 text-3xl font-bold tabular-nums">{Math.round(data.automation_rate_30d * 1000) / 10}%</p>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Catalog agents</h3>
              <p className="mt-1 text-3xl font-bold tabular-nums">{data.active_agents}</p>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Email sent (14d / 30d)</h3>
              <p className="mt-1 text-3xl font-bold tabular-nums">
                {data.email.sends_by_status["sent"] ?? 0} / {data.email_30d.sends_by_status["sent"] ?? 0}
              </p>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Notifications unread</h3>
              <p className="mt-1 text-3xl font-bold tabular-nums">{data.operator.notifications_unread}</p>
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">Procurement (you)</h3>
              <p className="mt-1 text-xl font-bold tabular-nums">
                {data.operator.procurement.my_tickets} tickets · {data.operator.procurement.pending_sap_id} no SAP id
              </p>
            </div>
          </div>

          {data.department_automation.length > 0 ? (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5">
              <h3 className="text-sm font-semibold">Automation by department (30d)</h3>
              <div className="mt-3 flex flex-col gap-2 text-sm">
                {data.department_automation.map((d) => (
                  <div
                    key={d.dept}
                    className="flex justify-between border-b border-[var(--border)] pb-2 last:border-0"
                  >
                    <span>{d.dept}</span>
                    <span>
                      {d.auto_pct}% auto · {d.tasks} tasks
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-sm text-[var(--text-muted)]">No department breakdown in your role scope.</p>
          )}

          <p className="text-[11px] text-[var(--text-muted)]">
            Generated {new Date(data.generated_at).toLocaleString()} · <button type="button" className="text-[var(--accent-blue)] hover:underline" onClick={() => setReloadTick((n) => n + 1)}>Refresh</button>
          </p>
        </div>
      ) : null}
    </div>
  );
}
