"use client";

import { useState } from "react";
import { approveItem, rejectItem } from "@/lib/api";

export type ApprovalCardData = {
  /** Server approval row — required for API-backed Approve/Reject */
  approval_id: string;
  title: string;
  amount?: string;
  /** 0–100 */
  confidence: number;
  risk: "low" | "medium" | "high";
  summary?: [string, string, string];
  /** Shown when expanded; from approval payload when present */
  expanded_detail?: string | null;
};

function confidenceBadge(confidence: number, risk: ApprovalCardData["risk"]) {
  if (risk === "high" || confidence < 60)
    return { label: "Needs attention", className: "bg-[rgba(239,68,68,0.15)] text-[var(--accent-red)]" };
  if (risk === "medium" || confidence < 85)
    return { label: "Review", className: "bg-[rgba(245,158,11,0.15)] text-[var(--accent-orange)]" };
  return { label: "High confidence", className: "bg-[rgba(16,185,129,0.15)] text-[var(--accent-green)]" };
}

type Props = {
  data: ApprovalCardData;
  onApprove?: () => void;
  onReject?: () => void;
  onEdit?: () => void;
  onAskAgent?: () => void;
};

export function InlineApprovalCard({
  data,
  onApprove,
  onReject,
  onEdit,
  onAskAgent,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [acted, setActed] = useState<"approved" | "rejected" | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const badge = confidenceBadge(data.confidence, data.risk);

  const lines = data.summary?.length
    ? data.summary
    : [
        "Details for this item live in the O2C / S2P review queue.",
        "Review amount, risk, and policy before deciding.",
        "Use Approve / Reject below or open the full review queue (O2C / S2P).",
      ];

  const expandText =
    data.expanded_detail?.trim() ||
    "No line-item breakdown was attached. Open the review queue (O2C / S2P) for full payload and history.";

  async function handleApprove() {
    setActionError(null);
    if (!data.approval_id) {
      onApprove?.();
      setActed("approved");
      return;
    }
    setBusy(true);
    try {
      await approveItem(data.approval_id);
      setActed("approved");
      onApprove?.();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    setActionError(null);
    if (!data.approval_id) {
      onReject?.();
      setActed("rejected");
      return;
    }
    setBusy(true);
    try {
      await rejectItem(data.approval_id);
      setActed("rejected");
      onReject?.();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setBusy(false);
    }
  }

  if (acted) {
    return (
      <div
        className={`mt-3 flex items-center gap-2 px-3 py-2.5 rounded-xl border text-sm ${
          acted === "approved"
            ? "border-[rgba(16,185,129,0.35)] bg-[rgba(16,185,129,0.08)]"
            : "border-[rgba(239,68,68,0.35)] bg-[rgba(239,68,68,0.06)]"
        }`}
      >
        <span>{acted === "approved" ? "✓" : "✕"}</span>
        <span className="font-medium">
          {acted === "approved" ? "Approved" : "Rejected"}
        </span>
        <span className="text-[var(--text-secondary)]">— {data.title}</span>
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-xl border border-[rgba(59,130,246,0.2)] bg-[rgba(59,130,246,0.06)] overflow-hidden">
      <button
        type="button"
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/5 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span
          className={`text-[10px] font-bold px-2 py-0.5 rounded ${badge.className}`}
        >
          {badge.label}
        </span>
        <span className="text-[10px] text-[var(--text-muted)]">
          {data.confidence}%
        </span>
        <span className="ml-auto text-[11px] text-[var(--text-muted)]">
          {expanded ? "Hide detail" : "Expand"}
        </span>
      </button>
      <div className="px-3 pb-2">
        <div className="flex justify-between items-start gap-2 mb-2">
          <span className="text-sm font-semibold">{data.title}</span>
          {data.amount && (
            <span className="text-sm font-bold shrink-0">{data.amount}</span>
          )}
        </div>
        <div className="space-y-1 text-xs text-[var(--text-secondary)] leading-relaxed">
          {lines.map((line, i) => (
            <p key={i}>{line}</p>
          ))}
        </div>
        {expanded && (
          <div className="mt-2 p-2 rounded-lg bg-[var(--bg-secondary)] text-[11px] text-[var(--text-muted)] whitespace-pre-wrap">
            {expandText}
          </div>
        )}
        {actionError && (
          <div className="mt-2 text-[11px] text-[var(--accent-red)]">{actionError}</div>
        )}
        <div className="flex flex-wrap gap-2 mt-3">
          <button
            type="button"
            disabled={busy}
            className="flex-1 min-w-[88px] py-2 bg-[var(--accent-green)] text-white rounded-lg text-xs font-semibold disabled:opacity-50"
            onClick={() => void handleApprove()}
          >
            {busy ? "…" : "Approve"}
          </button>
          <button
            type="button"
            className="py-2 px-3 bg-transparent text-[var(--text-primary)] border border-[var(--border)] rounded-lg text-xs font-semibold"
            onClick={onEdit}
          >
            Edit
          </button>
          <button
            type="button"
            disabled={busy}
            className="py-2 px-3 bg-transparent text-[var(--accent-red)] border border-[rgba(239,68,68,0.3)] rounded-lg text-xs font-semibold disabled:opacity-50"
            onClick={() => void handleReject()}
          >
            Reject
          </button>
          <button
            type="button"
            className="py-2 px-3 bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg text-xs font-semibold text-[var(--text-secondary)]"
            onClick={onAskAgent}
          >
            Ask Agent
          </button>
        </div>
      </div>
    </div>
  );
}
