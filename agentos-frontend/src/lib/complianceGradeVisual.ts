/**
 * Shared grade → color / tone for compliance call-quality UI (charts + run detail).
 * Letter grades may include +/- (e.g. D+, A-).
 */

const CHART_FILL: Record<string, string> = {
  A: "#059669",
  B: "#0284c7",
  C: "#ca8a04",
  D: "#ea580c",
  F: "#b91c1c",
  unknown: "#64748b",
};

/** Recharts / SVG fill from API grade label. */
export function complianceGradeChartFill(grade: string): string {
  const g = grade.trim().toUpperCase();
  if (!g || g === "—" || g === "-") return CHART_FILL.unknown;
  const letter = g.charAt(0);
  if (letter === "A") return CHART_FILL.A;
  if (letter === "B") return CHART_FILL.B;
  if (letter === "C") return CHART_FILL.C;
  if (letter === "D") return CHART_FILL.D;
  if (letter === "F") return CHART_FILL.F;
  return CHART_FILL.unknown;
}

/** Sort grades for legend: worst → best (compliance lens). */
export function complianceGradeSortKey(grade: string): number {
  const g = grade.trim().toUpperCase();
  if (!g || g === "—" || g === "-") return 999;
  const letter = g.charAt(0);
  const delta = g.includes("+") ? 0.3 : g.includes("-") ? -0.3 : 0;
  const base =
    letter === "F"
      ? 0
      : letter === "D"
        ? 10
        : letter === "C"
          ? 20
          : letter === "B"
            ? 30
            : letter === "A"
              ? 40
              : 500;
  if (base === 500) return 500;
  return base + delta;
}

function parseCompositePct(raw: unknown): number | undefined {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (typeof raw === "string") {
    const n = Number.parseFloat(raw);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

/** Human-readable composite for the score line (handles string JSON from API). */
export function formatCompositePctDisplay(raw: unknown): string {
  const n = parseCompositePct(raw);
  if (n == null) return "—";
  const rounded = Math.round(n * 10) / 10;
  return Number.isInteger(rounded) ? `${Math.round(n)}%` : `${rounded}%`;
}

/**
 * Tailwind classes for the run-detail score card.
 * Always use dark foreground on pastel / neutral fills. Do not add `dark:` text
 * variants: with `darkMode: 'media'`, OS dark mode would apply light text while
 * unprefixed `bg-*-50` still wins, yielding invisible copy on every grade.
 */
export function complianceScoreCardTone(grade: string | undefined, compositeRaw: unknown): string {
  const pct = parseCompositePct(compositeRaw);
  const g = (grade || "").trim().toUpperCase();
  const letter = g.charAt(0);

  if (letter === "A") {
    return "border-emerald-600/35 bg-emerald-50 !text-emerald-950";
  }
  if (letter === "B") {
    return "border-sky-600/35 bg-sky-50 !text-sky-950";
  }
  if (letter === "C") {
    return "border-amber-600/35 bg-amber-50 !text-amber-950";
  }
  if (letter === "D") {
    return "border-orange-600/40 bg-orange-50 !text-orange-950";
  }
  if (letter === "F") {
    return "border-red-600/45 bg-red-50 !text-red-950";
  }
  if (pct != null && pct < 40) {
    return "border-red-600/45 bg-red-50 !text-red-950";
  }
  if (pct != null && pct < 70) {
    return "border-amber-600/35 bg-amber-50 !text-amber-950";
  }
  return "border-[var(--border)] bg-[var(--bg-primary)] !text-[var(--text-primary)]";
}

export function complianceHasRubricSummary(evalObj: Record<string, unknown> | undefined): boolean {
  if (!evalObj || typeof evalObj !== "object") return false;
  if (evalObj.grade != null && String(evalObj.grade).trim() !== "") return true;
  return parseCompositePct(evalObj.composite_pct) != null;
}
