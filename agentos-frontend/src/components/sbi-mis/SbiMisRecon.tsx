"use client";

import { useCallback, useEffect, useState } from "react";
import { sbiRunRecon, sbiPollJob, SbiReconResult } from "@/lib/api";

// ── helpers ──────────────────────────────────────────────────────────────────

function fmtNum(n: number) {
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function fmtDelta(raw: number, pipeline: number) {
  const d = pipeline - raw;
  if (Math.abs(d) < 0.01) return null;
  return { value: d, pct: raw !== 0 ? (d / raw) * 100 : null };
}

// ── sub-components ───────────────────────────────────────────────────────────

function MetricCard({
  label,
  raw,
  pipeline,
  ok,
  isAmount,
}: {
  label: string;
  raw: number;
  pipeline: number;
  ok: boolean;
  isAmount?: boolean;
}) {
  const delta = fmtDelta(raw, pipeline);
  const fmt = (n: number) =>
    isAmount
      ? `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`
      : fmtNum(n);

  return (
    <div
      className={[
        "rounded-xl border p-5 flex flex-col gap-4",
        ok
          ? "border-[var(--border)] bg-[var(--bg-card)]"
          : "border-red-200 bg-red-50/40",
      ].join(" ")}
    >
      {/* Label + badge */}
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-semibold uppercase tracking-widest text-[var(--text-muted)]">
          {label}
        </p>
        <span
          className={[
            "text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full",
            ok
              ? "bg-emerald-100 text-emerald-700"
              : "bg-red-100 text-red-600",
          ].join(" ")}
        >
          {ok ? "✓ Match" : "✗ Mismatch"}
        </span>
      </div>

      {/* Raw vs Pipeline */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-0.5">
          <p className="text-[10px] font-medium text-[var(--text-muted)] uppercase tracking-wide">
            Raw file
          </p>
          <p className="text-lg font-bold tabular-nums text-[var(--text-primary)] leading-tight">
            {fmt(raw)}
          </p>
        </div>
        <div className="space-y-0.5">
          <p className="text-[10px] font-medium text-[var(--text-muted)] uppercase tracking-wide">
            Pipeline
          </p>
          <p
            className={[
              "text-lg font-bold tabular-nums leading-tight",
              ok ? "text-[var(--text-primary)]" : "text-red-600",
            ].join(" ")}
          >
            {fmt(pipeline)}
          </p>
        </div>
      </div>

      {/* Delta */}
      {delta ? (
        <div className="rounded-lg bg-red-100/70 px-3 py-2 flex items-center justify-between">
          <span className="text-xs font-medium text-red-700">Delta</span>
          <span className="text-xs font-bold tabular-nums text-red-700">
            {delta.value > 0 ? "+" : ""}
            {fmt(delta.value)}
            {delta.pct !== null && (
              <span className="ml-1.5 font-normal opacity-70">
                ({delta.pct > 0 ? "+" : ""}
                {delta.pct.toFixed(2)}%)
              </span>
            )}
          </span>
        </div>
      ) : (
        <div className="rounded-lg bg-emerald-50 px-3 py-2 flex items-center gap-1.5">
          <span className="text-emerald-500 text-xs">✓</span>
          <span className="text-xs text-emerald-700 font-medium">Values identical</span>
        </div>
      )}
    </div>
  );
}

function LoadingPulse() {
  return (
    <div className="p-8 flex flex-col items-center gap-5">
      <div className="flex gap-2 items-center">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="w-2.5 h-2.5 rounded-full bg-[var(--accent-green)]"
            style={{ animation: `recon-dot 1.2s ease-in-out ${i * 0.18}s infinite` }}
          />
        ))}
      </div>
      <div className="text-center space-y-1">
        <p className="text-sm font-medium text-[var(--text-primary)]">Running reconciliation…</p>
        <p className="text-xs text-[var(--text-muted)]">
          Re-reading source file from disk and comparing with pipeline output.
          This takes a few seconds.
        </p>
      </div>
      <style>{`@keyframes recon-dot{0%,100%{opacity:.2;transform:scale(.75)}50%{opacity:1;transform:scale(1)}}`}</style>
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export function SbiMisRecon() {
  const [data, setData]     = useState<SbiReconResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState<string | null>(null);
  const [ran, setRan]       = useState(false);
  const [ranAt, setRanAt]   = useState<Date | null>(null);
  const [reconSecs, setReconSecs] = useState(0);

  useEffect(() => {
    if (!loading) { setReconSecs(0); return; }
    const id = setInterval(() => setReconSecs((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [loading]);

  const reload = useCallback(async () => {
    setLoading(true);
    setRan(true);
    setError(null);
    try {
      // POST starts the job immediately (no blocking 504-prone HTTP call).
      const { job_id } = await sbiRunRecon();
      // Poll until done — onUpdate keeps the timer ticking via loading state.
      const job = await sbiPollJob(job_id, () => {});
      if (job.status === "failed") throw new Error(job.error ?? "Recon job failed");
      if (!job.result) throw new Error("No result from recon job");
      setData(job.result as unknown as SbiReconResult);
      setRanAt(new Date());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // ── Landing ──
  if (!ran) {
    return (
      <div className="p-8 max-w-2xl flex flex-col gap-6">
        <div className="space-y-1.5">
          <h1 className="text-base font-bold text-[var(--text-primary)]">Reconciliation</h1>
          <p className="text-sm text-[var(--text-muted)] leading-relaxed">
            Compares the <strong>raw uploaded file</strong> directly against the{" "}
            <strong>pipeline output</strong> — row count, GMV MRP, and unique order IDs.
            Reads the source file from disk so it takes a few seconds.
          </p>
        </div>
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-elev)] px-5 py-4 space-y-3">
          <p className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">What's checked</p>
          <div className="space-y-2">
            {[
              ["Row count", "Total line items in raw file vs pipeline Dump sheet"],
              ["GMV MRP", "Sum of gmv_mrp across all line items"],
              ["Unique order IDs", "Distinct order IDs present in both"],
            ].map(([label, desc]) => (
              <div key={label} className="flex items-start gap-3">
                <span className="mt-0.5 w-1.5 h-1.5 rounded-full bg-[var(--accent-green)] shrink-0" />
                <p className="text-sm text-[var(--text-secondary)]">
                  <span className="font-medium">{label}</span>
                  <span className="text-[var(--text-muted)]"> — {desc}</span>
                </p>
              </div>
            ))}
          </div>
        </div>
        <button
          onClick={reload}
          className="self-start px-5 py-2 rounded-lg bg-[var(--accent-green)] text-white text-sm font-semibold hover:opacity-90 transition-opacity"
        >
          Run Reconciliation
        </button>
      </div>
    );
  }

  // ── Loading ──
  if (loading) return (
    <div className="p-8 flex flex-col items-center gap-5">
      <div className="flex items-center gap-2 text-sm text-[var(--text-muted)]">
        <svg className="w-4 h-4 animate-spin shrink-0" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3"/>
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
        </svg>
        <span>Running reconciliation{reconSecs > 0 ? ` — ${reconSecs}s` : "…"}</span>
      </div>
    </div>
  );

  // ── Error ──
  if (error) {
    return (
      <div className="p-6 max-w-2xl space-y-3">
        <div className="rounded-xl border border-red-200 bg-red-50 px-5 py-4 space-y-1">
          <p className="text-sm font-semibold text-red-700">Reconciliation failed</p>
          <p className="text-xs text-red-600 font-mono break-all">{error}</p>
        </div>
        <button onClick={reload} className="text-xs text-[var(--accent-green)] hover:underline">
          Try again
        </button>
      </div>
    );
  }

  // ── Empty / no data ──
  if (!data || data.empty) {
    return (
      <div className="p-6 max-w-2xl space-y-3">
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-5 py-4">
          <p className="text-sm text-amber-800">
            {data?.error ?? "No data loaded — upload a file first."}
          </p>
        </div>
        <button onClick={reload} className="text-xs text-[var(--accent-green)] hover:underline">
          Try again
        </button>
      </div>
    );
  }

  // ── Results ──
  const metrics = [
    { label: "Row count",       raw: data.raw.rows,             pipeline: data.pipeline.rows,             ok: data.match.rows,     isAmount: false },
    { label: "GMV MRP",         raw: data.raw.gmv_mrp,          pipeline: data.pipeline.gmv_mrp,          ok: data.match.gmv_mrp,  isAmount: true  },
    { label: "Unique order IDs",raw: data.raw.unique_order_ids, pipeline: data.pipeline.unique_order_ids, ok: data.match.order_ids, isAmount: false },
  ];

  const allOk = metrics.every((m) => m.ok);
  const mismatchCount = metrics.filter((m) => !m.ok).length;

  return (
    <div className="p-6 space-y-5 max-w-3xl">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-base font-bold text-[var(--text-primary)]">Reconciliation</h1>
          {ranAt && (
            <p className="text-xs text-[var(--text-muted)] mt-0.5">
              Last run {ranAt.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </p>
          )}
        </div>
        <button
          onClick={reload}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-[var(--border)] text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] transition-colors disabled:opacity-50"
        >
          <svg className="w-3 h-3" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M13.5 8A5.5 5.5 0 1 1 8 2.5c1.8 0 3.4.87 4.4 2.2" strokeLinecap="round"/>
            <polyline points="12,1 12.5,4.5 9,4.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          Re-run
        </button>
      </div>

      {/* Overall status banner */}
      <div
        className={[
          "rounded-xl px-5 py-4 flex items-center gap-3",
          allOk
            ? "bg-emerald-50 border border-emerald-200"
            : "bg-red-50 border border-red-200",
        ].join(" ")}
      >
        <div
          className={[
            "w-8 h-8 rounded-full flex items-center justify-center shrink-0 text-sm",
            allOk ? "bg-emerald-100 text-emerald-600" : "bg-red-100 text-red-600",
          ].join(" ")}
        >
          {allOk ? "✓" : "✗"}
        </div>
        <div>
          <p className={["text-sm font-semibold", allOk ? "text-emerald-800" : "text-red-800"].join(" ")}>
            {allOk
              ? "All checks passed — raw file and pipeline output are consistent."
              : `${mismatchCount} check${mismatchCount > 1 ? "s" : ""} failed — pipeline output differs from raw file.`}
          </p>
          <p className={["text-xs mt-0.5", allOk ? "text-emerald-600" : "text-red-600"].join(" ")}>
            {allOk
              ? "No data loss detected across row count, GMV, and order IDs."
              : "Review the metrics below to identify where the discrepancy occurred."}
          </p>
        </div>
      </div>

      {/* Metric cards */}
      <div className="grid grid-cols-1 gap-4">
        {metrics.map((m) => (
          <MetricCard key={m.label} {...m} />
        ))}
      </div>

    </div>
  );
}
