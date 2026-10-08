"use client";

import Link from "next/link";
import { API_BASE } from "@/lib/api";

export default function AdminPage() {
  const healthUrl = `${API_BASE}/health`;

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">System admin</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Operational shortcuts — O2C tools, API health, automations, audit trail, and more.
      </p>
      <div className="grid grid-cols-2 gap-4">
        <Link
          href="/dashboard"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Dashboard</div>
          <div className="text-xs text-[var(--text-muted)]">
            Overview metrics and key performance indicators
          </div>
        </Link>
        <Link
          href="/agents"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Agents</div>
          <div className="text-xs text-[var(--text-muted)]">
            Manage and monitor AI agents across the platform
          </div>
        </Link>
        <Link
          href="/analytics"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Analytics</div>
          <div className="text-xs text-[var(--text-muted)]">
            Usage trends, session data, and performance insights
          </div>
        </Link>
        <Link
          href="/admin/o2c-contract-review"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Contract Health</div>
          <div className="text-xs text-[var(--text-muted)]">
            Contract quality dashboard — missing rates, unmapped sites, parsing failures, and rate line editor
          </div>
        </Link>
        <Link
          href="/admin/o2c-site-alias"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Attendance site names</div>
          <div className="text-xs text-[var(--text-muted)]">
            Link unknown names to sites or add new sites — optional billing draft refresh
          </div>
        </Link>
        <div
          className="cursor-not-allowed select-none rounded-xl border border-dashed border-[var(--border)] bg-[var(--bg-elev)]/50 p-5 opacity-80"
          aria-disabled="true"
          title="Coming soon"
        >
          <div className="mb-1 flex items-start justify-between gap-2">
            <div className="text-sm font-semibold text-[var(--text-secondary)]">Integration health</div>
            <span className="shrink-0 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-px text-[8px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
              Soon
            </span>
          </div>
          <div className="text-xs text-[var(--text-muted)]">
            SAP, Darwinbox, TrueIn, ODIN, Google, Qdrant
          </div>
        </div>
        <a
          href={healthUrl}
          target="_blank"
          rel="noreferrer"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors block focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">API health</div>
          <div className="text-xs text-[var(--text-muted)] break-all">
            GET {healthUrl} (opens in new tab)
          </div>
        </a>
        <div
          className="cursor-not-allowed select-none rounded-xl border border-dashed border-[var(--border)] bg-[var(--bg-elev)]/50 p-5 opacity-80"
          aria-disabled="true"
          title="Coming soon"
        >
          <div className="mb-1 flex items-start justify-between gap-2">
            <div className="text-sm font-semibold text-[var(--text-secondary)]">Agent registry</div>
            <span className="shrink-0 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-px text-[8px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
              Soon
            </span>
          </div>
          <div className="text-xs text-[var(--text-muted)]">
            Status, uptime, Ring 2 graphs
          </div>
        </div>
        <Link
          href="/admin/automations"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Automations</div>
          <div className="text-xs text-[var(--text-muted)]">
            Rules, toggles, and AI suggestions for playbooks
          </div>
        </Link>
        <Link
          href="/admin/audit"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Audit trail</div>
          <div className="text-xs text-[var(--text-muted)]">
            Decision traces, actions, and eval judges (MLflow)
          </div>
        </Link>
        <Link
          href="/compliance/call-quality"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Call quality</div>
          <div className="text-xs text-[var(--text-muted)]">
            GLP compliance runs — transcripts, rubric scores, Drive links
          </div>
        </Link>
        <Link
          href="/admin/optimus"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Optimus</div>
          <div className="text-xs text-[var(--text-muted)]">
            User access management and document knowledge base
          </div>
        </Link>
        <Link
          href="/admin/prosight"
          className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 hover:border-[var(--border-active)]/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          <div className="text-sm font-semibold mb-1">Prosight</div>
          <div className="text-xs text-[var(--text-muted)]">
            User access management and Databricks data sync
          </div>
        </Link>
      </div>
    </div>
  );
}
