"use client";

import type { ContractHealthCtvSummary } from "@/lib/api";

import { formatDateRange, statusBadgeClass, statusLabel } from "./statusUi";

type Props = {
  ctv: ContractHealthCtvSummary;
  selected: boolean;
  isMisPick: boolean;
  onSelect: () => void;
  /** Shown under title when this card lives in the global bucket */
  globalBucket?: boolean;
};

export function CtvCard({ ctv, selected, isMisPick, onSelect, globalBucket }: Props) {
  const issues: string[] = [];
  if (ctv.null_rate_count > 0) issues.push(`${ctv.null_rate_count} missing rate`);
  if (ctv.null_site_count > 0) issues.push(`${ctv.null_site_count} line(s) need site mapping`);
  if (ctv.empty_billing_rules_count > 0) issues.push(`${ctv.empty_billing_rules_count} empty rules`);

  const st = String(ctv.status ?? "");

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left p-2.5 rounded-lg border mb-1.5 transition-colors ${
        selected
          ? "border-[var(--accent-green)] bg-[rgba(21,163,115,0.10)] ring-1 ring-[var(--accent-green)]"
          : "border-[var(--border)] hover:bg-[var(--bg-elev)]"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium leading-snug line-clamp-2">
            {ctv.title || "Untitled contract"}
          </div>
          <div className="text-[11px] text-[var(--text-muted)] mt-0.5">
            {formatDateRange(ctv.effective_from, ctv.effective_to)}
          </div>
        </div>
        <span
          className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] font-semibold border ${statusBadgeClass(st)}`}
        >
          {statusLabel(st)}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {isMisPick && (
          <span className="px-1.5 py-0.5 rounded bg-emerald-600 text-white text-[10px] font-medium">
            Used for MIS this period
          </span>
        )}
        {globalBucket && !isMisPick && (
          <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px]">
            Global lines only
          </span>
        )}
        {issues.map((w) => (
          <span key={w} className="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 text-[10px]">
            {w}
          </span>
        ))}
      </div>
    </button>
  );
}
