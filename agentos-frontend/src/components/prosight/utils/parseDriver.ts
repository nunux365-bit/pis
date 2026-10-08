/**
 * Parse the driver text produced by TFT model attribution
 *
 * On L0 anomaly days the driver has two pipe-separated blocks:
 *   <Spike|Drop> - TFT today: <gate1> | <gate2> | <gate3>. | CF (perturb top-VSN): <cf1>; <cf2>; <cf3>
 *
 * Gate row:  <feature>=<today> (vs <baseline> LW) - gate <pct>% [<kind>,<suffix>]
 * CF row:    <feature>=<today>-><baseline> <+-impact> orders (<+-pct>% of miss)
 *
 * On normal days only the gate block is present (no CF clause).
 */

export interface GateRow {
  rank: number;
  feature: string;
  today_val: string;
  baseline_val: string;
  gate_pct: number;
  kind: string;
  suffix: string;
}

export interface CfRow {
  rank: number;
  feature: string;
  today_val: string;
  baseline_val: string;
  impact: number;
  pct_of_miss: number;
}

export interface ParsedDriver {
  gate: GateRow[];
  cf: CfRow[];
}

const GATE_RE =
  /([\w.-]+)=([^()]+?)\s*\(vs\s+(.+?)\s+LW\)\s*[—-]\s*gate\s+([\d.]+)%\s*\[([^,\]]+),([^\]]+)\]/;

const CF_RE =
  /([\w.-]+)=([^→]+?)→([^\s]+)\s+([+-][\d.,]+)\s+orders\s+\(([+-]?[\d.]+)%\s+of miss\)/;

export function parseDriver(driver: string | null | undefined): ParsedDriver {
  if (typeof driver !== "string" || !driver.trim() || driver.trim() === "—") {
    return { gate: [], cf: [] };
  }
  const text = driver;

  // Split into the TFT-today half and the CF half (if present).
  const cfMarker = text.indexOf("CF (perturb");
  const gateHalf = cfMarker === -1 ? text : text.slice(0, cfMarker);
  const cfHalf = cfMarker === -1 ? "" : text.slice(cfMarker);

  const gate: GateRow[] = [];
  // Gate clauses are separated by ` | ` after "TFT today:". Strip the prefix.
  const gateBody = gateHalf
    .replace(/^[^:]*TFT today:\s*/, "")
    .replace(/\.\s*$/, "");
  for (const clause of gateBody.split(/\s+\|\s+/)) {
    const m = clause.match(GATE_RE);
    if (!m) continue;
    gate.push({
      rank: gate.length + 1,
      feature: m[1].trim(),
      today_val: m[2].trim(),
      baseline_val: m[3].trim(),
      gate_pct: parseFloat(m[4]),
      kind: m[5].trim(),
      suffix: m[6].trim(),
    });
  }

  const cf: CfRow[] = [];
  if (cfHalf) {
    const cfBody = cfHalf.replace(/^CF \(perturb top-VSN\):\s*/, "");
    for (const clause of cfBody.split(/;\s*/)) {
      const m = clause.match(CF_RE);
      if (!m) continue;
      cf.push({
        rank: cf.length + 1,
        feature: m[1].trim(),
        today_val: m[2].trim(),
        baseline_val: m[3].trim(),
        impact: parseFloat(m[4].replace(/,/g, "")),
        pct_of_miss: parseFloat(m[5]),
      });
    }
  }

  return { gate, cf };
}
