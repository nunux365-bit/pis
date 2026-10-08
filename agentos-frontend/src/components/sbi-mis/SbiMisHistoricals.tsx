"use client";

import { useCallback, useEffect, useState } from "react";
import {
  sbiDeleteHistoricals,
  sbiGetHistoricals,
  sbiGetHistoricalStats,
  SbiMonthStats,
  SbiPfHistorical,
} from "@/lib/api";
import { SbiMisUploadModal } from "./SbiMisUpload";

function MonthCard({
  month,
  stats,
  onDelete,
  deleting,
}: {
  month: string;
  stats: SbiMonthStats;
  onDelete: (m: string) => void;
  deleting: boolean;
}) {
  const fmt = (v: number) =>
    "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });

  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-secondary)]">
          {month}
        </h3>
        <button
          onClick={() => onDelete(month)}
          disabled={deleting}
          className="text-[11px] text-red-500 hover:underline disabled:opacity-50"
        >
          {deleting ? "Deleting…" : "delete"}
        </button>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <span className="text-[var(--text-muted)]">PF count</span>
        <span className="font-semibold tabular-nums text-[var(--text-primary)] text-right">
          {stats.pf_count.toLocaleString("en-IN")}
        </span>
        <span className="text-[var(--text-muted)]">Pharma total</span>
        <span className="font-semibold tabular-nums text-[var(--text-primary)] text-right">
          {fmt(stats.pharma_total)}
        </span>
        <span className="text-[var(--text-muted)]">AHC total</span>
        <span className="font-semibold tabular-nums text-[var(--text-primary)] text-right">
          {fmt(stats.ahc_total)}
        </span>
        <span className="text-[var(--text-muted)]">Wallets filled</span>
        <span className="font-semibold tabular-nums text-[var(--text-primary)] text-right">
          {stats.wallets_filled.toLocaleString("en-IN")}
        </span>
      </div>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="rounded-xl border border-[var(--border)] overflow-hidden animate-pulse">
      <div className="bg-[var(--bg-elev)] px-4 py-2.5 flex gap-8">
        {["w-12", "w-16", "w-10", "w-24", "w-20", "w-20"].map((w, i) => (
          <div key={i} className={`h-2.5 rounded bg-[var(--border)] ${w}`} />
        ))}
      </div>
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="border-t border-[var(--border)]/40 px-4 py-3 flex gap-8">
          {["w-20", "w-16", "w-10", "w-28", "w-24", "w-24"].map((w, j) => (
            <div key={j} className={`h-2 rounded bg-[var(--bg-elev)] ${w}`} />
          ))}
        </div>
      ))}
      <p className="px-4 py-3 text-xs text-[var(--text-muted)]">Loading rows…</p>
    </div>
  );
}

export function SbiMisHistoricals() {
  // Phase 1 — stats (fast, renders month cards)
  const [months, setMonths] = useState<string[]>([]);
  const [monthStats, setMonthStats] = useState<Record<string, SbiMonthStats>>({});
  const [totalRecords, setTotalRecords] = useState<number>(0);
  const [statsLoading, setStatsLoading] = useState(true);

  // Phase 2 — rows (slow, renders detail table)
  const [rows, setRows] = useState<SbiPfHistorical[]>([]);
  const [rowsLoading, setRowsLoading] = useState(true);
  const [activeMonth, setActiveMonth] = useState<string | undefined>(undefined);

  const [deleting, setDeleting] = useState<string | null>(null);
  const [showJson, setShowJson] = useState(false);
  const [uploadPfOpen, setUploadPfOpen] = useState(false);

  // Load stats once on mount
  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    try {
      const data = await sbiGetHistoricalStats();
      setMonths(data.months);
      setMonthStats(data.month_stats ?? {});
      setTotalRecords(data.total_records ?? 0);
    } finally {
      setStatsLoading(false);
    }
  }, []);

  // Load rows whenever month filter changes (or on mount after stats)
  const loadRows = useCallback(async (month?: string) => {
    setRowsLoading(true);
    try {
      const data = await sbiGetHistoricals(month);
      setRows(data.rows);
    } finally {
      setRowsLoading(false);
    }
  }, []);

  useEffect(() => { loadStats(); }, [loadStats]);
  // Start row fetch as soon as the component mounts (in parallel with stats)
  useEffect(() => { loadRows(activeMonth); }, [activeMonth, loadRows]);

  const deleteMonth = async (month: string) => {
    if (!confirm(`Delete all historicals for ${month}? This triggers a pipeline rerun.`)) return;
    setDeleting(month);
    try {
      await sbiDeleteHistoricals(month);
      await Promise.all([loadStats(), loadRows(activeMonth)]);
    } finally {
      setDeleting(null);
    }
  };

  const fmt = (v: number | null) =>
    v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });

  return (
    <div className="p-6 space-y-5 max-w-5xl">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-muted)]">
            CUMULATIVE RECORD
          </p>
          <h1 className="text-base font-bold text-[var(--text-primary)] mt-0.5">
            PF Historicals
          </h1>
          {!statsLoading && (
            <p className="text-xs text-[var(--text-muted)] mt-0.5">
              {totalRecords.toLocaleString("en-IN")} records across {months.length} month
              {months.length !== 1 ? "s" : ""} · auto-used by PF Summary
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => setShowJson(true)}
            disabled={rowsLoading}
            className="px-3 py-1.5 rounded border border-[var(--border)] text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] transition-colors disabled:opacity-50"
          >
            View JSON
          </button>
          <button
            onClick={() => setUploadPfOpen(true)}
            className="px-3 py-1.5 rounded bg-[var(--accent-green)]/10 text-[var(--accent-green)] text-xs font-medium hover:bg-[var(--accent-green)]/20 transition-colors"
          >
            + Upload PF Summary
          </button>
        </div>
      </div>

      {/* Phase 1: Month summary cards — show as soon as stats arrive */}
      {statsLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div
              key={i}
              className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 animate-pulse space-y-3"
            >
              <div className="h-2.5 w-20 rounded bg-[var(--bg-elev)]" />
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, j) => (
                  <div key={j} className="flex justify-between">
                    <div className="h-2 w-16 rounded bg-[var(--bg-elev)]" />
                    <div className="h-2 w-20 rounded bg-[var(--bg-elev)]" />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : months.length > 0 ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {months.map((m) =>
            monthStats[m] ? (
              <MonthCard
                key={m}
                month={m}
                stats={monthStats[m]}
                onDelete={deleteMonth}
                deleting={deleting === m}
              />
            ) : null
          )}
        </div>
      ) : (
        <p className="text-sm text-[var(--text-muted)]">No historicals found.</p>
      )}

      {/* Phase 2: Row-level detail table — loads in background, shows skeleton */}
      <div className="space-y-3">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold text-[var(--text-primary)]">Detail</h2>
          <select
            value={activeMonth ?? ""}
            onChange={(e) => setActiveMonth(e.target.value || undefined)}
            disabled={statsLoading}
            className="border border-[var(--border)] rounded px-3 py-1 text-xs bg-[var(--bg-primary)] text-[var(--text-primary)] disabled:opacity-50"
          >
            <option value="">All months</option>
            {months.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {rowsLoading && (
            <span className="text-[11px] text-[var(--text-muted)]">loading rows…</span>
          )}
        </div>

        {rowsLoading ? (
          <TableSkeleton />
        ) : (
          <div className="rounded-xl border border-[var(--border)] overflow-auto">
            <table className="w-full text-sm">
              <thead className="bg-[var(--bg-elev)]">
                <tr>
                  {["PF", "Month", "Type", "Pharma total", "AHC total", "Wallet limit"].map(
                    (h) => (
                      <th
                        key={h}
                        className="px-4 py-2 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider"
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={`${r.pf}-${r.month}`}
                    className="border-t border-[var(--border)]/40 hover:bg-[var(--bg-elev)]/50"
                  >
                    <td className="px-4 py-2 font-mono text-xs text-[var(--text-primary)]">{r.pf}</td>
                    <td className="px-4 py-2 tabular-nums text-[var(--text-secondary)]">{r.month}</td>
                    <td className="px-4 py-2 text-[var(--text-muted)]">{r.pf_type ?? "—"}</td>
                    <td className="px-4 py-2 tabular-nums text-right text-[var(--text-secondary)]">{fmt(r.pharma_total)}</td>
                    <td className="px-4 py-2 tabular-nums text-right text-[var(--text-secondary)]">{fmt(r.ahc_total)}</td>
                    <td className="px-4 py-2 tabular-nums text-right text-[var(--text-secondary)]">{fmt(r.wallet_limit)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && (
              <p className="p-6 text-sm text-[var(--text-muted)]">No records for this filter.</p>
            )}
          </div>
        )}
      </div>

      {/* View JSON modal */}
      {showJson && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-[var(--bg-card)] rounded-xl shadow-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
            <div className="p-4 border-b border-[var(--border)] flex items-center justify-between">
              <h2 className="font-semibold text-[var(--text-primary)] text-sm">
                Historicals JSON ({rows.length} rows)
              </h2>
              <button
                onClick={() => setShowJson(false)}
                className="text-[var(--text-muted)] hover:text-[var(--text-primary)] text-lg"
              >
                ✕
              </button>
            </div>
            <pre className="flex-1 overflow-auto p-4 text-xs text-[var(--text-secondary)] font-mono whitespace-pre leading-relaxed">
              {JSON.stringify(rows, null, 2)}
            </pre>
          </div>
        </div>
      )}

      {/* Upload PF Summary modal */}
      {uploadPfOpen && (
        <SbiMisUploadModal
          onDone={() => {
            setUploadPfOpen(false);
            Promise.all([loadStats(), loadRows(activeMonth)]);
          }}
          onCancel={() => setUploadPfOpen(false)}
        />
      )}
    </div>
  );
}
