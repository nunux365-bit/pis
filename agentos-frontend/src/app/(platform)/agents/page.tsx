"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  listWorkflowCatalog,
  type WorkflowCatalogItem,
} from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

function StatIcon({ kind }: { kind: "workflow" | "agents" | "avg" }) {
  if (kind === "workflow") {
    return (
      <svg viewBox="0 0 24 24" className="w-4 h-4" aria-hidden="true">
        <path
          fill="currentColor"
          d="M5 4a3 3 0 1 1 0 6 3 3 0 0 1 0-6Zm14 5H9.83a4.98 4.98 0 0 0-1.5-2H19a1 1 0 1 1 0 2ZM5 14a3 3 0 1 1 0 6 3 3 0 0 1 0-6Zm14 5H9.83a4.98 4.98 0 0 0-1.5-2H19a1 1 0 1 1 0 2Z"
        />
      </svg>
    );
  }
  if (kind === "agents") {
    return (
      <svg viewBox="0 0 24 24" className="w-4 h-4" aria-hidden="true">
        <path
          fill="currentColor"
          d="M16 11a4 4 0 1 0-3.999-4A4 4 0 0 0 16 11Zm-8 2a3 3 0 1 0-3-3 3 3 0 0 0 3 3Zm0 2c-2.67 0-8 1.34-8 4v1h10.26A6.94 6.94 0 0 1 10 18c0-1.13.39-2.16 1.02-3H8Zm8 0c-2.67 0-8 1.34-8 4v1h16v-1c0-2.66-5.33-4-8-4Z"
        />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className="w-4 h-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M3 17h3.5v4H3v-4Zm7.25-7H13.75v11h-3.5V10ZM17.5 3H21v18h-3.5V3Z"
      />
    </svg>
  );
}

export default function AgentsPage() {
  const { refreshUser } = useAuth();
  const [workflows, setWorkflows] = useState<WorkflowCatalogItem[]>([]);
  const [workflowsErr, setWorkflowsErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(12);
  const [expandedWorkflow, setExpandedWorkflow] = useState<string | null>(null);

  useEffect(() => {
    const isAuthError = (msg: string | null | undefined) =>
      !!msg && /not authenticated|invalid or expired token/i.test(msg);

    async function loadData() {
      setWorkflowsErr(null);
      setLoading(true);

      const runLoad = async () => {
        const result = await Promise.allSettled([listWorkflowCatalog()]);
        const workflowsRes = result[0];
        const nextWorkflowsErr =
          workflowsRes.status === "rejected"
            ? workflowsRes.reason instanceof Error
              ? workflowsRes.reason.message
              : "Failed to load workflow catalog"
            : null;
        return { workflowsRes, nextWorkflowsErr };
      };

      let { workflowsRes, nextWorkflowsErr } = await runLoad();

      // Token can be stale briefly after redirects/reloads; refresh and retry once.
      if (isAuthError(nextWorkflowsErr)) {
        await refreshUser();
        ({ workflowsRes, nextWorkflowsErr } = await runLoad());
      }

      if (workflowsRes.status === "fulfilled") setWorkflows(workflowsRes.value.workflows ?? []);
      else setWorkflows([]);

      setWorkflowsErr(nextWorkflowsErr);
      setLoading(false);
    }

    void loadData();
  }, [refreshUser]);

  const activeAgents = new Set(
    workflows.flatMap((wf) => wf.nodes.map((node) => `${wf.key}:${node.id}`))
  ).size;
  const averageAgentsPerWorkflow =
    workflows.length > 0
      ? Math.round(
          (workflows.reduce((sum, wf) => sum + wf.nodes.length, 0) / workflows.length) * 10
        ) / 10
      : 0;

  const statusOptions = useMemo(() => {
    const values = new Set(workflows.map((wf) => wf.status.toLowerCase()));
    return ["all", ...Array.from(values)];
  }, [workflows]);

  const filteredWorkflows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return workflows
      .filter((wf) => (statusFilter === "all" ? true : wf.status.toLowerCase() === statusFilter))
      .filter((wf) => {
        if (!q) return true;
        return (
          wf.display_name.toLowerCase().includes(q) ||
          wf.key.toLowerCase().includes(q) ||
          wf.summary.toLowerCase().includes(q) ||
          wf.nodes.some(
            (node) =>
              node.title.toLowerCase().includes(q) || node.description.toLowerCase().includes(q)
          )
        );
      })
      .sort((a, b) => a.display_name.localeCompare(b.display_name));
  }, [workflows, statusFilter, query]);

  const totalPages = Math.max(1, Math.ceil(filteredWorkflows.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pagedWorkflows = useMemo(() => {
    const start = (safePage - 1) * pageSize;
    return filteredWorkflows.slice(start, start + pageSize);
  }, [filteredWorkflows, safePage, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [query, statusFilter, pageSize]);

  useEffect(() => {
    if (expandedWorkflow && !pagedWorkflows.some((wf) => wf.key === expandedWorkflow)) {
      setExpandedWorkflow(null);
    }
  }, [expandedWorkflow, pagedWorkflows]);

  const authError =
    !!workflowsErr &&
    /not authenticated|invalid or expired token/i.test(workflowsErr) &&
    workflows.length === 0;

  const statusClass = (status: string) => {
    const s = status.toLowerCase();
    if (s === "implemented" || s === "active") {
      return "bg-[rgba(16,185,129,0.15)] text-[var(--accent-green)]";
    }
    if (s === "beta") {
      return "bg-[rgba(59,130,246,0.15)] text-[var(--accent-blue)]";
    }
    return "bg-[rgba(148,163,184,0.16)] text-[var(--text-muted)]";
  };

  return (
    <div className="mx-auto w-full max-w-[1240px]">
      <section className="relative overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 mb-4">
        <div
          className="absolute inset-0 opacity-70 pointer-events-none"
          style={{
            background:
              "radial-gradient(circle at 0% 0%, rgba(47,127,210,0.14), transparent 46%), radial-gradient(circle at 100% 0%, rgba(21,163,115,0.14), transparent 40%)",
          }}
        />
        <div className="relative">
          <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] mb-1.5">
            Workflow Operations
          </p>
          <h1 className="text-[26px] leading-tight font-semibold tracking-tight mb-1">
            Workflow Intelligence
          </h1>
          <p className="text-[13px] text-[var(--text-secondary)] max-w-3xl">
            Centralized view of implemented workflows and participating agents.
          </p>
        </div>
      </section>

      {authError ? (
        <div
          className="mb-5 rounded-xl border border-[rgba(239,68,68,0.35)] bg-[rgba(239,68,68,0.08)] p-4"
          role="alert"
        >
          <p className="text-sm text-[var(--text-primary)] font-medium mb-1">
            Session expired or not authenticated
          </p>
          <p className="text-xs text-[var(--text-secondary)]">
            Please sign in again to load workflow data.
          </p>
          <Link href="/login" className="text-xs text-[var(--accent-blue)] hover:underline">
            Go to sign in
          </Link>
        </div>
      ) : null}

      <section className="grid grid-cols-1 md:grid-cols-3 gap-2.5 mb-4">
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg p-3.5 shadow-[0_1px_2px_rgba(16,24,40,0.04)]">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-1">
            <span className="text-[var(--accent-blue)]">
              <StatIcon kind="workflow" />
            </span>
            Workflows
          </div>
          <div className="text-xl font-semibold">{workflows.length}</div>
        </div>
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg p-3.5 shadow-[0_1px_2px_rgba(16,24,40,0.04)]">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-1">
            <span className="text-[var(--accent-green)]">
              <StatIcon kind="agents" />
            </span>
            Active Agents
          </div>
          <div className="text-xl font-semibold">{activeAgents}</div>
          <div className="text-[11px] text-[var(--text-muted)] mt-1">Across listed workflows</div>
        </div>
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg p-3.5 shadow-[0_1px_2px_rgba(16,24,40,0.04)]">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-[var(--text-muted)] mb-1">
            <span className="text-[var(--accent-purple)]">
              <StatIcon kind="avg" />
            </span>
            Avg Agents per Workflow
          </div>
          <div className="text-xl font-semibold">{averageAgentsPerWorkflow}</div>
        </div>
      </section>

      <section className="mb-3 bg-[var(--bg-card)] border border-[var(--border)] rounded-lg p-3.5">
        <div className="flex flex-col lg:flex-row gap-3 lg:items-center">
          <div className="flex-1">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search workflow, key, or agent node..."
              className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2 text-[13px] outline-none focus:border-[var(--border-active)]"
            />
          </div>
          <div className="flex flex-wrap gap-2">
            {statusOptions.map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => setStatusFilter(status)}
                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                  statusFilter === status
                    ? "border-[var(--border-active)] bg-[rgba(21,163,115,0.12)] text-[var(--accent-green)]"
                    : "border-[var(--border)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                }`}
              >
                {status === "all" ? "All statuses" : status}
              </button>
            ))}
          </div>
          <select
            value={String(pageSize)}
            onChange={(e) => setPageSize(Number(e.target.value))}
            className="rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] min-w-[92px]"
          >
            <option value="6">6 / page</option>
            <option value="12">12 / page</option>
            <option value="24">24 / page</option>
            <option value="48">48 / page</option>
          </select>
        </div>
      </section>

      <section className="mb-6">
        {workflowsErr && (
          <p className="text-xs text-[var(--accent-red)] mb-3" role="alert">
            {workflowsErr}
          </p>
        )}
        {loading && (
          <div className="text-xs text-[var(--text-muted)] mb-3">Loading workflows...</div>
        )}
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold">Workflows</h2>
          <span className="text-xs text-[var(--text-muted)]">
            Showing {pagedWorkflows.length} of {filteredWorkflows.length}
          </span>
        </div>
        <div className="grid grid-cols-1 gap-4">
          {pagedWorkflows.map((wf) => (
            <div
              key={wf.key}
              className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg p-4 shadow-[0_1px_2px_rgba(16,24,40,0.04)] hover:shadow-[0_8px_24px_rgba(16,24,40,0.08)] transition-shadow"
            >
              <div className="flex items-center gap-2 mb-2">
                <span className="inline-flex w-6 h-6 items-center justify-center rounded-md bg-[rgba(47,127,210,0.12)] text-[var(--accent-blue)]">
                  <StatIcon kind="workflow" />
                </span>
                <span className="font-semibold text-[14px]">{wf.display_name}</span>
                <span
                  className={`ml-auto text-[10px] uppercase tracking-wide px-2 py-0.5 rounded ${statusClass(
                    wf.status
                  )}`}
                >
                  {wf.status}
                </span>
              </div>
              <p className="text-[12px] text-[var(--text-secondary)] mb-2.5">{wf.summary}</p>
              <div className="flex flex-wrap items-center gap-3 text-[11px] text-[var(--text-muted)] mb-3">
                <span>Agents: {wf.nodes.length}</span>
                <span>Edges: {wf.edges.length}</span>
                <span>Key: {wf.key}</span>
              </div>

              <div className="overflow-x-auto pb-1 mb-2.5">
                <div className="flex items-center gap-2 min-w-max">
                  {wf.nodes.map((node, idx) => (
                    <div key={`${wf.key}-flow-${node.id}`} className="flex items-center gap-2">
                      <div className="text-[11px] px-2 py-1 rounded-md border border-[var(--border)] bg-[rgba(47,127,210,0.08)]">
                        {node.title}
                      </div>
                      {idx < wf.nodes.length - 1 && (
                        <span className="text-[var(--text-muted)] text-xs">→</span>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              <button
                type="button"
                onClick={() =>
                  setExpandedWorkflow((current) => (current === wf.key ? null : wf.key))
                }
                className="text-xs font-medium text-[var(--accent-blue)] border border-[var(--border)] rounded-md px-2 py-1 hover:bg-[var(--bg-elev)] mb-1.5"
              >
                {expandedWorkflow === wf.key ? "Hide agent details" : "View agent details"}
              </button>

              <div
                className={`grid transition-all ${
                  expandedWorkflow === wf.key ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
                }`}
              >
                <div className="overflow-hidden">
                  <div className="space-y-1.5 pt-1">
                    {wf.nodes.map((node) => (
                      <div
                        key={`${wf.key}-${node.id}`}
                        className="rounded-md border border-[var(--border)] px-2.5 py-2 bg-[var(--bg-secondary)]"
                      >
                        <div className="flex items-center gap-2 mb-1">
                          <div className="text-xs font-medium">{node.title}</div>
                          <span className="text-[10px] uppercase px-1.5 py-0.5 rounded bg-[var(--bg-elev)] text-[var(--text-muted)]">
                            {node.role.replace(/_/g, " ")}
                          </span>
                        </div>
                        <div className="text-[11px] text-[var(--text-muted)]">{node.description}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          ))}
          {!loading && filteredWorkflows.length === 0 && !workflowsErr && (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-8 text-center">
              <p className="text-sm font-medium mb-1">No workflows found</p>
              <p className="text-xs text-[var(--text-muted)]">
                Try clearing filters or using a different search term.
              </p>
            </div>
          )}
        </div>

        {!loading && filteredWorkflows.length > 0 && (
          <div className="mt-5 flex items-center justify-between border-t border-[var(--border)] pt-4">
            <p className="text-xs text-[var(--text-muted)]">
              Page {safePage} of {totalPages}
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={safePage <= 1}
                className="rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-xs disabled:opacity-50 hover:bg-[var(--bg-elev)]"
              >
                Previous
              </button>
              <button
                type="button"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={safePage >= totalPages}
                className="rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-xs disabled:opacity-50 hover:bg-[var(--bg-elev)]"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
