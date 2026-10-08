"use client";

import { useCallback, useEffect, useState } from "react";
import {
  createAgentSession,
  listAgentSessions,
  triggerWorkflow,
  type AgentSessionDto,
} from "@/lib/api";

export default function SessionsPage() {
  const [rows, setRows] = useState<AgentSessionDto[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    setErr(null);
    listAgentSessions()
      .then((r) => setRows(r.sessions || []))
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed"));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const runSample = async () => {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const session = await createAgentSession("demo_variance_check");
      const run = await triggerWorkflow("demo_variance_check", {
        source: "sessions_ui",
        thread_id: session.thread_id,
      });
      setMsg(
        `Started session **${session.thread_id}** and queued workflow ${run.workflow_id.slice(0, 8)}… Invoice 3-way match (with demo variance) runs, then open the **O2C / S2P review queue** for HITL.`
      );
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Start failed");
    } finally {
      setBusy(false);
    }
  };

  const runCleanInvoice = async () => {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const session = await createAgentSession("invoice_3way_match");
      const run = await triggerWorkflow("invoice_3way_match", {
        source: "sessions_ui",
        thread_id: session.thread_id,
        vendor: "UAT Clean Vendor Ltd",
        invoice_number: "INV-CLEAN-001",
        po_number: "4500100001",
        invoice_total: "118000.00",
        po_total: "118000.00",
        grn_received_value: "118000.00",
        taxable_value: "100000.00",
        gst_rate_pct: "18",
      });
      setMsg(
        `Queued **invoice_3way_match** ${run.workflow_id.slice(0, 8)}… (balanced PO/GRN). Check the **O2C / S2P review queue** for structured HITL.`
      );
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Start failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Sessions</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Agent sessions are stored in PostgreSQL. Each{" "}
        <strong className="text-[var(--text-primary)]">workflow trigger</strong> runs
        an automated phase (invoice 3-way match for finance keys), then opens a{" "}
        <strong className="text-[var(--text-primary)]">human-in-the-loop</strong>{" "}
        review step you must confirm in the O2C / S2P queue before the run completes.
      </p>

      <div className="flex flex-wrap gap-3 mb-6">
        <button
          type="button"
          disabled={busy}
          onClick={() => runSample()}
          className="px-4 py-2.5 rounded-xl text-sm font-medium bg-app-gradient-1 text-white border-none cursor-pointer hover:opacity-90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-primary)]"
        >
          {busy ? "Starting…" : "Demo variance (real 3-way skill)"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => runCleanInvoice()}
          className="px-4 py-2.5 rounded-xl text-sm font-medium bg-[var(--bg-card)] border border-[var(--border)] text-[var(--text-primary)] hover:border-[var(--border-active)]/50 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
        >
          {busy ? "Starting…" : "Clean 3-way match (sample payload)"}
        </button>
        <button
          type="button"
          onClick={() => load()}
          className="px-4 py-2.5 rounded-xl text-sm font-medium bg-[var(--bg-card)] border border-[var(--border)] text-[var(--text-primary)] hover:border-[var(--border-active)]/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
        >
          Refresh list
        </button>
      </div>

      {msg && (
        <p
          className="text-sm text-[var(--accent-green)] mb-4 [&_strong]:font-semibold"
          role="status"
        >
          {msg.split("**").map((part, i) =>
            i % 2 === 1 ? <strong key={i}>{part}</strong> : part
          )}
        </p>
      )}
      {err && <p className="text-sm text-[var(--accent-red)] mb-4">{err}</p>}

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-left text-[var(--text-muted)] text-xs uppercase tracking-wide">
              <th className="px-4 py-3 font-medium">Thread</th>
              <th className="px-4 py-3 font-medium">Workflow</th>
              <th className="px-4 py-3 font-medium">Progress</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.id}
                className="border-b border-[var(--border)] last:border-0 hover:bg-white/[0.02]"
              >
                <td className="px-4 py-3 font-mono text-xs text-[var(--text-muted)]">
                  {r.thread_id}
                </td>
                <td className="px-4 py-3 font-medium">{r.workflow_name}</td>
                <td className="px-4 py-3">{r.progress_pct}%</td>
                <td className="px-4 py-3">
                  <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-[rgba(59,130,246,0.15)] text-[var(--accent-blue)]">
                    {r.status}
                  </span>
                </td>
                <td className="px-4 py-3 text-[var(--text-muted)] text-xs">
                  {new Date(r.updated_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && !err && (
          <p className="p-6 text-sm text-[var(--text-muted)]">
            No sessions yet. Run a sample workflow above.
          </p>
        )}
      </div>
    </div>
  );
}
