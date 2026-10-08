"use client";

import { useCallback, useEffect, useState } from "react";
import { HandoffAnalysisDashboard } from "@/components/responder-eval/HandoffAnalysisDashboard";
import { getHandoffAnalysis, type HandoffAnalysis } from "@/lib/responderEval";

const EMPTY: HandoffAnalysis = {
  reference_date: "",
  focus_date: "",
  kpis: {
    handoffs_d1: 0,
    handoffs_vs_7d_avg_pct: null,
    handoff_rate_d1: 0,
    handoff_rate_vs_d2_pp: null,
    chats_landed_d1: 0,
    largest_bucket: null,
  },
  movers: { rising: [], falling: [] },
  day_headers: [],
  buckets: [],
};

export default function ResponderHandOffAnalysisPage() {
  const [data, setData] = useState<HandoffAnalysis>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(await getHandoffAnalysis());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load hand-off analysis");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mx-auto flex w-full max-w-[110rem] flex-col gap-4 px-3 py-4 sm:px-5 sm:py-5">
      <header className="border-b border-[var(--border)]/70 pb-4">
        <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-violet-700/90 ">
          Support quality
        </p>
        <h1 className="mt-0.5 text-xl font-semibold tracking-tight text-[var(--text-primary)] sm:text-2xl">
          Hand-Off Analysis
        </h1>
        <p className="mt-1.5 max-w-3xl text-xs leading-relaxed text-[var(--text-secondary)]">
          Daily bucket / sub-bucket breakdown of why chats escalated to a human agent.
        </p>
      </header>

      {err ? (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {err}
          <button type="button" className="ml-3 underline" onClick={() => void load()}>
            Retry
          </button>
        </div>
      ) : null}

      <HandoffAnalysisDashboard data={data} loading={loading} />
    </div>
  );
}
