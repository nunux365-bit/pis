"use client";

import type { ContractHealthClientTree } from "@/lib/api";

import { CtvCard } from "./CtvCard";
import { statusLabel } from "./statusUi";

type Site = ContractHealthClientTree["sites"][number];

type Props = {
  site: Site;
  periodStart: string;
  periodEnd: string;
  selectedCtvId: string;
  onSelectCtv: (id: string) => void;
};

export function SiteTermsSection({ site, periodStart, periodEnd, selectedCtvId, onSelectCtv }: Props) {
  const approved = site.terms.filter((t) => String(t.status).toLowerCase() === "approved");
  const other = site.terms.filter((t) => String(t.status).toLowerCase() !== "approved");

  const pickerTerm = site.picker_ctv_id
    ? site.terms.find((t) => t.contract_terms_version_id === site.picker_ctv_id)
    : null;

  return (
    <section className="border border-[var(--border)] rounded-xl overflow-hidden bg-[var(--bg-card)]">
      <div className="px-3 py-2 bg-[var(--bg-elev)] border-b border-[var(--border)]">
        <h3 className="text-sm font-semibold">{site.site_name}</h3>
        {site.overlap_conflict ? (
          <p className="text-[11px] text-red-700 mt-1 font-medium">
            Two or more approved contracts overlap {periodStart}–{periodEnd}. MIS cannot run until you Expire
            the older/wrong one.
          </p>
        ) : pickerTerm ? (
          <p className="text-[11px] text-emerald-800 mt-1">
            MIS billing uses: <span className="font-medium">{pickerTerm.title || "Approved contract"}</span> (
            {statusLabel(String(pickerTerm.status))})
          </p>
        ) : site.terms.length === 0 ? (
          <p className="text-[11px] text-[var(--text-muted)] mt-1">No contract terms linked to this site.</p>
        ) : (
          <p className="text-[11px] text-[var(--text-muted)] mt-1">
            No billable approved contract for this period — fix dates or approve via MIS.
          </p>
        )}
      </div>

      <div className="p-2 space-y-3">
        {approved.length > 0 && (
          <div>
            <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] font-semibold px-1 mb-1">
              Approved ({approved.length})
            </p>
            {approved.map((ctv) => (
              <CtvCard
                key={ctv.contract_terms_version_id}
                ctv={ctv}
                selected={selectedCtvId === ctv.contract_terms_version_id}
                isMisPick={site.picker_ctv_id === ctv.contract_terms_version_id}
                onSelect={() => onSelectCtv(ctv.contract_terms_version_id)}
              />
            ))}
          </div>
        )}
        {other.length > 0 && (
          <div>
            <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] font-semibold px-1 mb-1">
              Pending / other ({other.length})
            </p>
            {other.map((ctv) => (
              <CtvCard
                key={ctv.contract_terms_version_id}
                ctv={ctv}
                selected={selectedCtvId === ctv.contract_terms_version_id}
                isMisPick={site.picker_ctv_id === ctv.contract_terms_version_id}
                onSelect={() => onSelectCtv(ctv.contract_terms_version_id)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
