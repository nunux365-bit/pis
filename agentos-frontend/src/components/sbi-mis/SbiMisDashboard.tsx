"use client";

import { useCallback, useEffect, useState } from "react";
import {
  apiFetch,
  sbiActivateRun,
  sbiDownloadUrl,
  sbiGetBillingSummary,
  sbiGetRuns,
  sbiGetStatus,
  SbiBillingSummary,
  SbiRun,
  SbiStatusResponse,
} from "@/lib/api";
import { SbiMisUploadModal } from "./SbiMisUpload";
import { notifySbiRerun } from "./SbiMisLeftNav";

function inr(v: number) {
  return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function BillingCard({
  label,
  badge,
  badgeColor,
  gmv,
  orders,
}: {
  label: string;
  badge: string;
  badgeColor: "green" | "red";
  gmv: number;
  orders: number;
}) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wider text-[var(--text-secondary)]">
          {label}
        </p>
        <span
          className={[
            "text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full",
            badgeColor === "green"
              ? "bg-emerald-100 text-emerald-700"
              : "bg-red-100 text-red-600",
          ].join(" ")}
        >
          {badge}
        </span>
      </div>
      <div className="space-y-0.5">
        <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
          Total GMV <span className="normal-case">(GMV List Price)</span>
        </p>
        <p className="text-xl font-bold tabular-nums text-[var(--text-primary)]">{inr(gmv)}</p>
        <p className="text-xs text-[var(--text-muted)]">
          {orders.toLocaleString("en-IN")} orders
        </p>
      </div>
    </div>
  );
}

/** Animated spinner SVG used inside the download button */
function Spinner() {
  return (
    <svg
      className="animate-spin h-3.5 w-3.5"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle
        className="opacity-25"
        cx="12" cy="12" r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
      />
    </svg>
  );
}

export function SbiMisDashboard() {
  const [status, setStatus] = useState<SbiStatusResponse | null>(null);
  const [runs, setRuns] = useState<SbiRun[]>([]);
  const [billing, setBilling] = useState<SbiBillingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activating, setActivating] = useState<string | null>(null);

  const [downloading, setDownloading] = useState(false);
  const [downloadInfo, setDownloadInfo] = useState<string | null>(null);

  // Upload modal state
  const [uploadOpen, setUploadOpen] = useState(false);
  // After upload, poll eagerly for up to 8s to catch computing=true before
  // the normal computing-dependent poll loop takes over.
  const [eagerPolling, setEagerPolling] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([sbiGetStatus(), sbiGetRuns()]);
      setStatus(s);
      setRuns(r);
      setError(null);
      if (s.has_upload && !s.computing) {
        sbiGetBillingSummary()
          .then(setBilling)
          .catch(() => setBilling(null));
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while computing
  useEffect(() => {
    if (!status?.computing) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [status?.computing, refresh]);

  // Eager post-upload poll: FastAPI BackgroundTask fires after the HTTP response,
  // so computing=false on the first refresh. Poll every 800ms for up to 8s to
  // catch computing=true without requiring a manual page reload.
  useEffect(() => {
    if (!eagerPolling) return;
    let ticks = 0;
    const id = setInterval(async () => {
      ticks++;
      await refresh();
      if (ticks >= 10) { clearInterval(id); setEagerPolling(false); }
    }, 800);
    return () => clearInterval(id);
  }, [eagerPolling, refresh]);

  // Auto-activate the latest run when dashboard opens with no active month.
  useEffect(() => {
    if (!loading && status && !status.active_month && runs.length > 0) {
      sbiActivateRun(runs[0].month).then(refresh).catch(() => {});
    }
  // Only run once after the initial load resolves.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);

  const activate = async (month: string) => {
    setActivating(month);
    try {
      await sbiActivateRun(month);
      notifySbiRerun(); // activating a run triggers a pipeline rerun
      await refresh();
    } finally {
      setActivating(null);
    }
  };

  const handleDownload = async () => {
    setDownloading(true);
    setDownloadInfo(null);
    try {
      const check = await apiFetch(sbiDownloadUrl(), { method: "HEAD" });
      if (!check.ok) {
        const text = await check.text().catch(() => "");
        let msg = `Download failed (${check.status})`;
        try { msg = JSON.parse(text)?.detail ?? msg; } catch { /* use default */ }
        // 503 means the file is being generated — treat as informational, not an error
        if (check.status === 503) {
          setDownloadInfo(msg);
          return;
        }
        throw new Error(msg);
      }
      const a = document.createElement("a");
      a.href = sbiDownloadUrl();
      a.download = `SBI_MIS_${activeMonth ?? "export"}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    } catch (e) {
      setError(String(e));
    } finally {
      setDownloading(false);
    }
  };

  if (loading) {
    return <div className="p-6 text-[var(--text-muted)]">Loading…</div>;
  }

  const rowCount = status?.active_run?.row_count ?? billing?.row_count ?? null;
  const activeMonth = status?.active_month;

  return (
    <div className="p-6 space-y-6 max-w-4xl">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-base font-bold text-[var(--text-primary)]">
            Monthly Billing
            {activeMonth && (
              <span className="ml-2 font-normal text-[var(--text-secondary)]">
                / {activeMonth}
              </span>
            )}
          </h1>
          {rowCount != null && (
            <p className="text-xs text-[var(--text-muted)] mt-0.5">
              {rowCount.toLocaleString("en-IN")} line items
              {billing && !billing.empty && billing.grand_total.rows > 0 && (
                <> · {billing.grand_total.rows.toLocaleString("en-IN")} orders</>
              )}
            </p>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={refresh}
            className="px-3 py-1.5 rounded border border-[var(--border)] text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] transition-colors"
          >
            ↻ Refresh
          </button>

          {/* Upload */}
          <button
            onClick={() => setUploadOpen(true)}
            className="px-3 py-1.5 rounded bg-[var(--accent-green)]/10 text-[var(--accent-green)] text-xs font-medium hover:bg-[var(--accent-green)]/20 transition-colors"
          >
            Upload Input
          </button>

          {/* Download — disabled while pipeline is generating */}
          {status?.has_upload && (
            <button
              onClick={handleDownload}
              disabled={downloading || !!status?.computing}
              title={status?.computing ? "Output file is being generated — try again once the pipeline finishes" : undefined}
              className={[
                "flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-all",
                (downloading || status?.computing)
                  ? "bg-[var(--accent-green)] text-white cursor-not-allowed opacity-60"
                  : "bg-[var(--accent-green)] text-white hover:opacity-90",
              ].join(" ")}
            >
              {downloading ? (
                <>
                  <Spinner />
                  <span>Preparing…</span>
                </>
              ) : status?.computing ? (
                <>
                  <Spinner />
                  <span>Generating…</span>
                </>
              ) : (
                <>
                  <svg
                    className="h-3.5 w-3.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2.5}
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M12 4v12m0 0l-4-4m4 4l4-4"
                    />
                  </svg>
                  Download .xlsx
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Error banners */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}
      {downloadInfo && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800 flex items-center justify-between gap-3">
          <span>{downloadInfo}</span>
          <button
            onClick={() => setDownloadInfo(null)}
            className="text-blue-500 hover:text-blue-700 text-xs shrink-0"
          >
            Dismiss
          </button>
        </div>
      )}
      {status?.error && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          Pipeline error: {status.error}
        </div>
      )}

      {/* Computing indicator — prominent banner so it's always visible */}
      {status?.computing && (
        <div className="rounded-xl border-2 border-blue-400 bg-blue-600 px-5 py-4 text-white flex items-center gap-4 shadow-lg">
          <svg className="w-8 h-8 animate-spin shrink-0" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-30" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
            <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <div>
            <p className="font-bold text-base">Pipeline running…</p>
            <p className="text-sm opacity-80 mt-0.5">Processing uploaded sheet. Results will appear below when complete.</p>
          </div>
        </div>
      )}

      {/* Billing breakdown — shown only after pipeline has run */}
      {billing && !billing.empty && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <BillingCard
              label="Permissible"
              badge="to be billed"
              badgeColor="green"
              gmv={billing.permissible.gmv_list}
              orders={billing.permissible.rows}
            />
            <BillingCard
              label="Non-permissible"
              badge="not billed"
              badgeColor="red"
              gmv={billing.non_permissible.gmv_list}
              orders={billing.non_permissible.rows}
            />
          </div>

          {/* Grand total bar */}
          <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-elev)] px-5 py-3 flex items-center justify-between">
            <p className="text-xs font-semibold uppercase tracking-wider text-[var(--text-secondary)]">
              Grand Total
            </p>
            <div className="flex items-center gap-6 text-sm">
              <span className="font-bold tabular-nums text-[var(--text-primary)]">
                {inr(billing.grand_total.gmv_list)} GMV
              </span>
              <span className="text-[var(--text-muted)]">
                {billing.grand_total.rows.toLocaleString("en-IN")} orders
              </span>
            </div>
          </div>
        </div>
      )}

      {/* No data prompt */}
      {!status?.has_upload && (
        <p className="text-sm text-[var(--text-muted)]">
          No data uploaded yet.{" "}
          <button
            onClick={() => setUploadOpen(true)}
            className="text-[var(--accent-green)] underline underline-offset-2 hover:opacity-80"
          >
            Upload a Metabase Dump .xlsx
          </button>{" "}
          to get started.
        </p>
      )}

      {/* Upload history */}
      {runs.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-[var(--text-primary)] mb-2">Upload history</h2>
          <div className="rounded-xl border border-[var(--border)] overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-[var(--bg-elev)]">
                <tr>
                  {["Month", "Rows", "Uploaded", ""].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2 text-left text-xs font-medium text-[var(--text-muted)] uppercase tracking-wider"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const isActive = r.month === status?.active_month;
                  return (
                    <tr
                      key={r.month}
                      className={[
                        "border-t border-[var(--border)]",
                        isActive
                          ? "bg-[var(--accent-green)]/5"
                          : "hover:bg-[var(--bg-elev)]/50",
                      ].join(" ")}
                    >
                      <td className="px-4 py-2 font-mono text-[var(--text-primary)]">
                        {r.month}
                        {isActive && (
                          <span className="ml-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--accent-green)] bg-[var(--accent-green)]/10 px-1.5 py-0.5 rounded">
                            active
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2 tabular-nums text-[var(--text-secondary)]">
                        {r.row_count?.toLocaleString("en-IN") ?? "—"}
                      </td>
                      <td className="px-4 py-2 text-[var(--text-muted)] text-xs">
                        {new Date(r.uploaded_at).toLocaleString("en-IN")}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <button
                          onClick={() => !isActive && activate(r.month)}
                          disabled={isActive || !!activating || !!status?.computing}
                          className="text-xs text-[var(--accent-green)] hover:underline disabled:opacity-40 disabled:cursor-not-allowed disabled:no-underline"
                          title={
                            isActive ? "Already the active month" :
                            status?.computing ? "Pipeline is computing — please wait" :
                            undefined
                          }
                        >
                          {activating === r.month ? "Activating…" : "Activate"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Upload modal */}
      {uploadOpen && (
        <SbiMisUploadModal
          initialMonth={activeMonth}
          onDone={() => { setUploadOpen(false); refresh(); setEagerPolling(true); notifySbiRerun(); }}
          onCancel={() => setUploadOpen(false)}
        />
      )}
    </div>
  );
}
