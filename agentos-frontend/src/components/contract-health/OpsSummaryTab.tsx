"use client";

import { useCallback, useEffect, useState } from "react";

import { getO2CContractHealthSummary, type ContractHealthOpsSummary } from "@/lib/api";

type Props = {
  periodStart: string;
  periodEnd: string;
  onOpenClient: (billingClientId: string, ctvId?: string) => void;
};

export function OpsSummaryTab({ periodStart, periodEnd, onOpenClient }: Props) {
  const [data, setData] = useState<ContractHealthOpsSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!periodStart || !periodEnd || periodStart > periodEnd) {
      setError("Invalid billing period");
      return;
    }
    setLoading(true);
    setError("");
    try {
      setData(await getO2CContractHealthSummary(periodStart, periodEnd));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load summary");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [periodStart, periodEnd]);

  useEffect(() => {
    void load();
  }, [load]);

  const pending = (data?.by_status?.pending ?? 0) + (data?.by_status?.draft ?? 0);

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="px-3 py-1.5 rounded-lg border border-[var(--border)] text-xs disabled:opacity-50"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}

      {data && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[
              { label: "Pending / draft CTVs", value: pending },
              { label: "Overlap sites (period)", value: data.overlap_site_count },
              { label: "Parse failures", value: data.parse_failure_count },
              { label: "Pending w/ data issues", value: data.queues.pending_data_issues.length },
            ].map((t) => (
              <div key={t.label} className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-3">
                <div className="text-xl font-bold">{t.value}</div>
                <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wide mt-1">{t.label}</div>
              </div>
            ))}
          </div>

          <div className="text-xs text-[var(--text-muted)]">
            By status:{" "}
            {Object.entries(data.by_status)
              .map(([k, v]) => `${k} ${v}`)
              .join(" · ")}
          </div>

          <section className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
            <h3 className="text-sm font-semibold mb-2">Overlapping approved terms (sites)</h3>
            {data.queues.overlap_sites.length === 0 ? (
              <p className="text-xs text-[var(--text-muted)]">None for this period.</p>
            ) : (
              <ul className="space-y-2">
                {data.queues.overlap_sites.map((row) => (
                  <li key={row.service_site_id} className="flex justify-between gap-2 text-sm">
                    <span>
                      {row.client_name} — {row.site_name}{" "}
                      <span className="text-red-500">({row.approved_ctv_count} approved)</span>
                    </span>
                    <button
                      type="button"
                      className="text-xs text-[var(--accent-blue)] hover:underline shrink-0"
                      onClick={() => onOpenClient(row.billing_client_id)}
                    >
                      Open client →
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
            <h3 className="text-sm font-semibold mb-2">Pending contracts with data issues</h3>
            {data.queues.pending_data_issues.length === 0 ? (
              <p className="text-xs text-[var(--text-muted)]">None.</p>
            ) : (
              <ul className="space-y-2">
                {data.queues.pending_data_issues.map((row) => (
                  <li key={row.contract_terms_version_id} className="flex justify-between gap-2 text-sm">
                    <span>
                      {row.client_name}: {row.title || "Untitled"}{" "}
                      {row.null_rate_count > 0 && (
                        <span className="text-red-500">· {row.null_rate_count} null rates</span>
                      )}
                      {row.null_site_count > 0 && (
                        <span className="text-amber-500">· {row.null_site_count} unmapped</span>
                      )}
                    </span>
                    <button
                      type="button"
                      className="text-xs text-[var(--accent-blue)] hover:underline shrink-0"
                      onClick={() => onOpenClient(row.billing_client_id, row.contract_terms_version_id)}
                    >
                      Fix →
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
