/**
 * Chart tooltip component for NewsPage - Prosight N Dashboard
 */

import { COLORS } from "./constants";
import type { ChartTooltipProps } from "./types";

export function ChartTooltip({
  active,
  payload,
  label,
  selIds,
  ts,
  infoOf,
}: ChartTooltipProps) {
  if (!active || !payload?.length || !label) return null;

  const entries = selIds
    .map((fid, i) => {
      const pt = (ts[fid] ?? []).find((p) => p.date.slice(5) === label);
      if (!pt) return null;
      const name = infoOf(fid).label;
      const delta = pt.actual - pt.predicted;
      const status =
        pt.actual < pt.lower
          ? { text: "↓ below band", color: "#dc2626" }
          : pt.actual > pt.upper
            ? { text: "↑ above band", color: "#f59e0b" }
            : { text: "✓ in band", color: "#16a34a" };
      return { fid, i, name, pt, delta, status };
    })
    .filter(Boolean);

  if (!entries.length) return null;

  return (
    <div className="bg-white border border-[#d1d5db] rounded-lg p-2.5 px-3.5 shadow-lg text-[11px] min-w-[200px]">
      <div className="font-bold text-[#374151] mb-2 text-xs">{label}</div>
      {entries.map((entry) => {
        if (!entry) return null;
        const { fid, i, name, pt, delta, status } = entry;
        return (
          <div key={fid} className={entries.length > 1 ? "mb-2.5" : ""}>
            {entries.length > 1 && (
              <div
                className="font-semibold mb-1 border-b border-[#f3f4f6] pb-1"
                style={{ color: COLORS[i % COLORS.length] }}
              >
                {name}
              </div>
            )}
            <div className="grid grid-cols-[80px_1fr] gap-x-2 gap-y-0.5">
              <span className="text-[#9ca3af]">Actual</span>
              <span className="font-mono font-bold text-[#111827]">
                {Math.round(pt.actual).toLocaleString("en-IN")}
              </span>
              <span className="text-[#9ca3af]">Predicted</span>
              <span className="font-mono text-[#374151]">
                {Math.round(pt.predicted).toLocaleString("en-IN")}
              </span>
              <span className="text-[#9ca3af]">Δ</span>
              <span
                className="font-mono font-semibold"
                style={{
                  color:
                    delta < 0 ? "#dc2626" : delta > 0 ? "#f59e0b" : "#6b7280",
                }}
              >
                {delta >= 0 ? "+" : ""}
                {Math.round(delta).toLocaleString("en-IN")}
              </span>
              <span className="text-[#9ca3af]">Band</span>
              <span className="font-mono text-[#6b7280] text-[10px]">
                [{Math.round(pt.lower).toLocaleString("en-IN")} –{" "}
                {Math.round(pt.upper).toLocaleString("en-IN")}]
              </span>
              <span className="text-[#9ca3af]">Status</span>
              <span className="font-semibold" style={{ color: status.color }}>
                {status.text}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
