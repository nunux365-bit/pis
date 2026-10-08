"use client";

import type { MetricDisplay } from "@/lib/responderEvalLabels";

type Props = {
  label: string;
  metric: MetricDisplay;
  hint?: string;
};

export function ResponderEvalMetricCard({ label, metric, hint }: Props) {
  return (
    <div className="rounded-lg border border-black/5 bg-white/40 px-3 py-2 ">
      <p className="text-[10px] font-medium uppercase tracking-wide opacity-70">{label}</p>
      <p className="mt-0.5 font-semibold tabular-nums">{metric.value}</p>
      {metric.meaning ? (
        <p className="mt-0.5 text-[11px] font-medium opacity-75">{metric.meaning}</p>
      ) : null}
      {hint ? <p className="mt-1 text-[10px] opacity-60">{hint}</p> : null}
    </div>
  );
}
