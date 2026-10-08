"use client";

import { useMemo } from "react";
import type { ProsightRankedFeature } from "./types";
import { parseDriver, type GateRow, type CfRow } from "./utils/parseDriver";
import { SurfaceSection } from "./surfaces";

// ─────────────────────────────────────────────────────────────────────────────
// Helper Functions
// ─────────────────────────────────────────────────────────────────────────────

function fmtSigned(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const r = Math.round(v);
  return `${r > 0 ? "+" : ""}${r.toLocaleString("en-IN")}`;
}

interface GateLookupEntry {
  gate_pct: number;
  kind: string;
  suffix: string | null;
}

function buildGateLookup(
  ranked: ProsightRankedFeature[] | null,
  parsedGate: GateRow[]
): Map<string, GateLookupEntry> {
  const map = new Map<string, GateLookupEntry>();
  if (Array.isArray(ranked)) {
    for (const r of ranked) {
      if (!map.has(r.feature)) {
        map.set(r.feature, { gate_pct: r.pct, kind: r.kind, suffix: r.suffix });
      }
    }
  }
  // Fallback: if `ranked` was not provided, populate from the parsed gate
  // rows (still better than nothing - they cover only top-3 features).
  if (!map.size && Array.isArray(parsedGate)) {
    for (const g of parsedGate) {
      if (!map.has(g.feature)) {
        map.set(g.feature, {
          gate_pct: g.gate_pct,
          kind: g.kind,
          suffix: g.suffix ? `_${g.suffix}` : null,
        });
      }
    }
  }
  return map;
}

interface MergedRow {
  rank: number;
  feature: string;
  today_val: string;
  baseline_val: string;
  impact: number | null;
  gate_pct: number | null;
  kind: string | null;
  suffix: string | null;
}

function mergeRows(
  cf: CfRow[],
  gateLookup: Map<string, GateLookupEntry>,
  ranked: ProsightRankedFeature[] | null
): MergedRow[] {
  const cfFeats = new Set(cf.map((c) => c.feature));
  const merged: MergedRow[] = [];
  for (const c of cf) {
    const g = gateLookup.get(c.feature);
    merged.push({
      rank: 0,
      feature: c.feature,
      today_val: c.today_val,
      baseline_val: c.baseline_val,
      impact: c.impact,
      gate_pct: g?.gate_pct ?? null,
      kind: g?.kind ?? null,
      suffix: g?.suffix ?? null,
    });
  }
  // Top features by gate% that aren't already in CF - surface up to 5 rows total.
  if (Array.isArray(ranked)) {
    for (const r of ranked) {
      if (cfFeats.has(r.feature)) continue;
      if (merged.length >= 5) break;
      merged.push({
        rank: 0,
        feature: r.feature,
        today_val: "n/a",
        baseline_val: "n/a",
        impact: null,
        gate_pct: r.pct,
        kind: r.kind,
        suffix: r.suffix,
      });
    }
  }
  return merged.slice(0, 5).map((r, i) => ({ ...r, rank: i + 1 }));
}

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

interface CounterfactualImpactProps {
  driver: string | null | undefined;
  ranked: ProsightRankedFeature[] | null;
}

export default function CounterfactualImpact({
  driver,
  ranked,
}: CounterfactualImpactProps) {
  const parsed = useMemo(() => parseDriver(driver), [driver]);
  const gateLookup = useMemo(
    () => buildGateLookup(ranked, parsed.gate),
    [ranked, parsed.gate]
  );
  const rows = useMemo(
    () => mergeRows(parsed.cf, gateLookup, ranked),
    [parsed.cf, gateLookup, ranked]
  );

  if (!rows.length) {
    return (
      <SurfaceSection title="Feature impact - top model features today" collapsible>
        <div className="text-[11px] text-ink-400 italic">
          No model attribution available for this day.
        </div>
      </SurfaceSection>
    );
  }

  const hasCF = parsed.cf.length > 0;

  return (
    <SurfaceSection title="Feature impact - top model features today" collapsible>
      {hasCF && (
        <p className="text-[11px] text-ink-500 mb-2">
          orders if the feature&apos;s today-value were swapped to its baseline;{" "}
          <span className="font-mono">Feature importance</span> is the model&apos;s
          daily attention share.
        </p>
      )}
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-[9px] uppercase tracking-wide text-ink-400 border-b border-ink-100">
            <th className="text-left py-1 px-1 font-medium">#</th>
            <th className="text-left py-1 px-1 font-medium">feature</th>
            <th className="text-right py-1 px-1 font-medium">today</th>
            <th className="text-right py-1 px-1 font-medium">vs</th>
            <th className="text-right py-1 px-1 font-medium">impact</th>
            <th
              className="text-right py-1 px-1 font-medium"
              title="The model's daily attention share for this feature."
            >
              Feature importance
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={`${r.rank}-${r.feature}`}
              className="border-b border-ink-50 hover:bg-blue-50/40"
            >
              <td className="py-1 px-1 font-mono text-ink-500">{r.rank}</td>
              <td className="py-1 px-1">
                <span className="font-mono">{r.feature}</span>
              </td>
              <td className="py-1 px-1 text-right font-mono">{r.today_val}</td>
              <td className="py-1 px-1 text-right font-mono text-ink-600">
                {r.baseline_val}
              </td>
              <td
                className={`py-1 px-1 text-right font-mono ${r.impact == null ? "text-ink-400" : r.impact < 0 ? "text-red-700" : r.impact > 0 ? "text-amber-700" : ""}`}
              >
                {r.impact == null ? "—" : `${fmtSigned(r.impact)} orders`}
              </td>
              <td className="py-1 px-1 text-right font-mono">
                {r.gate_pct == null ? "—" : `${r.gate_pct.toFixed(1)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </SurfaceSection>
  );
}
