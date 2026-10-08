/** Grade colors for responder eval dashboard (A–E). */

const CHART_FILL: Record<string, string> = {
  A: "#059669",
  B: "#0284c7",
  C: "#ca8a04",
  D: "#ea580c",
  E: "#b91c1c",
  "-": "#64748b",
  unknown: "#64748b",
};

export function isResponderEvalGraded(row: { letter_grade?: string; eval_status?: string }): boolean {
  if (row.eval_status === "not_graded") return false;
  return (row.letter_grade || "").trim() !== "-";
}

export function responderGradeLabel(grade: string): string {
  const g = (grade || "").trim();
  return g === "-" ? "N/A" : g.toUpperCase();
}

export function responderGradeChartFill(grade: string): string {
  const g = (grade || "").trim();
  if (g === "-") return CHART_FILL["-"];
  return CHART_FILL[g.toUpperCase()] ?? CHART_FILL.unknown;
}

export function responderGradeSortKey(grade: string): number {
  const order: Record<string, number> = { A: 5, B: 4, C: 3, D: 2, E: 1 };
  return order[(grade || "").trim().toUpperCase()] ?? 0;
}

export function responderScoreCardTone(grade: string): string {
  const g = (grade || "").trim();
  // No dark: variants — OS night mode must not change Chat Eval colors/fonts.
  if (g === "-") return "border-slate-500/30 bg-slate-50 !text-slate-800";
  const upper = g.toUpperCase();
  if (upper === "A") return "border-emerald-600/35 bg-emerald-50 !text-emerald-950";
  if (upper === "B") return "border-sky-600/35 bg-sky-50 !text-sky-950";
  if (upper === "C") return "border-amber-600/35 bg-amber-50 !text-amber-950";
  if (upper === "D") return "border-orange-600/40 bg-orange-50 !text-orange-950";
  if (upper === "E") return "border-red-600/45 bg-red-50 !text-red-950";
  return "border-[var(--border)] bg-[var(--bg-primary)] !text-[var(--text-primary)]";
}
