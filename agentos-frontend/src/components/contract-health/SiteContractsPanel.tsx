"use client";

import type { ClientTreeSite } from "./siteUtils";

import { CtvCard } from "./CtvCard";
import { statusLabel } from "./statusUi";

type Props = {
  site: ClientTreeSite;
  periodStart: string;
  periodEnd: string;
  selectedCtvId: string;
  onSelectCtv: (id: string) => void;
};

/** Contracts for one site (middle column). Site name shown in list; banner explains MIS state. */
export function SiteContractsPanel({
  site,
  periodStart,
  periodEnd,
  selectedCtvId,
  onSelectCtv,
}: Props) {
  const approved = site.terms.filter((t) => String(t.status).toLowerCase() === "approved");
  const other = site.terms.filter((t) => String(t.status).toLowerCase() !== "approved");

  const pickerTerm = site.picker_ctv_id
    ? site.terms.find((t) => t.contract_terms_version_id === site.picker_ctv_id)
    : null;

  return (
    <div className="flex flex-col border border-[var(--border)] rounded-xl bg-[var(--bg-card)] overflow-hidden min-h-[280px] max-h-[75vh]">
      <div className="px-3 py-2 border-b border-[var(--border)] bg-[var(--bg-elev)] shrink-0">
        <h3 className="text-sm font-semibold leading-snug">{site.site_name}</h3>
        {site.overlap_conflict ? (
          <p className="text-[11px] text-red-700 mt-1 font-medium leading-relaxed">
            Two or more approved contracts overlap {periodStart}–{periodEnd}. Expire the wrong one
            before MIS.
          </p>
        ) : pickerTerm ? (
          <p className="text-[11px] text-emerald-800 mt-1 leading-relaxed">
            MIS uses: <span className="font-medium">{pickerTerm.title || "Approved contract"}</span> (
            {statusLabel(String(pickerTerm.status))})
          </p>
        ) : site.terms.length === 0 ? (
          <p className="text-[11px] text-[var(--text-muted)] mt-1">No contracts linked to this site.</p>
        ) : (
          <p className="text-[11px] text-[var(--text-muted)] mt-1 leading-relaxed">
            No billable contract for this month — fix dates or approve on MIS.
          </p>
        )}
      </div>

      <div className="overflow-y-auto flex-1 p-2 space-y-3">
        {site.terms.length === 0 ? (
          <p className="text-xs text-[var(--text-muted)] px-1 py-4 text-center">Nothing to review here.</p>
        ) : (
          <>
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
          </>
        )}
      </div>
    </div>
  );
}
