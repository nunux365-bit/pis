"use client";

import type { ContractHealthCtvSummary } from "@/lib/api";

import { CtvCard } from "./CtvCard";

type Props = {
  terms: ContractHealthCtvSummary[];
  selectedCtvId: string;
  onSelectCtv: (id: string) => void;
};

export function GlobalContractsPanel({ terms, selectedCtvId, onSelectCtv }: Props) {
  return (
    <div className="flex flex-col border border-dashed border-slate-300 rounded-xl bg-slate-50/40 overflow-hidden min-h-[280px] max-h-[75vh]">
      <div className="px-3 py-2 border-b border-slate-200 shrink-0">
        <h3 className="text-sm font-semibold text-slate-800">Shared across all sites</h3>
        <p className="text-[11px] text-slate-600 mt-1 leading-relaxed">
          Client-wide contracts with <strong>global rate lines</strong>. Each site still has its own
          billing contract for MIS — open a site on the left to review site billing.
        </p>
      </div>
      <div className="overflow-y-auto flex-1 p-2">
        {terms.length === 0 ? (
          <p className="text-xs text-[var(--text-muted)] px-1 py-4 text-center">No shared contracts.</p>
        ) : (
          terms.map((ctv) => (
            <CtvCard
              key={ctv.contract_terms_version_id}
              ctv={ctv}
              selected={selectedCtvId === ctv.contract_terms_version_id}
              isMisPick={false}
              globalBucket
              onSelect={() => onSelectCtv(ctv.contract_terms_version_id)}
            />
          ))
        )}
      </div>
    </div>
  );
}
