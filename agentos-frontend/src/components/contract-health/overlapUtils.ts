import type { ContractHealthCtvSummary } from "@/lib/api";

import type { ClientTreeSite } from "./siteUtils";

export type OverlapSuggestion = {
  keep: ContractHealthCtvSummary;
  expire: ContractHealthCtvSummary;
  keepReasons: string[];
  expireReasons: string[];
  confidence: "high" | "medium" | "low";
  remainingApproved: number;
};

function scoreCtv(ctv: ContractHealthCtvSummary): { score: number; reasons: string[] } {
  const reasons: string[] = [];
  let score = 0;
  const title = (ctv.title ?? "").toLowerCase();

  if (/\bloi\b|letter of intent|intent for/.test(title)) {
    score -= 40;
    reasons.push("interim LOI");
  }
  if (/service agreement|master service|\bmsa\b|statement of work|\bsow\b|fy20/.test(title)) {
    score += 25;
    reasons.push("service agreement");
  }

  score += Math.min(ctv.rate_line_count, 30) * 2;
  if (ctv.rate_line_count > 0) reasons.push(`${ctv.rate_line_count} rate lines`);

  if (ctv.null_rate_count > 0) {
    score -= ctv.null_rate_count * 10;
    reasons.push(`${ctv.null_rate_count} missing rate`);
  }
  if (ctv.null_site_count > 0) {
    score -= ctv.null_site_count * 6;
  }

  if (ctv.created_at) {
    score += new Date(ctv.created_at).getTime() / 1e15;
  }

  return { score, reasons: reasons.slice(0, 3) };
}

/** Heuristic: which approved duplicate to keep vs expire (operator can override). */
export function suggestOverlapResolution(site: ClientTreeSite): OverlapSuggestion | null {
  const approved = site.terms.filter((t) => String(t.status).toLowerCase() === "approved");
  if (approved.length < 2) return null;

  const scored = approved.map((ctv) => ({ ctv, ...scoreCtv(ctv) }));
  scored.sort((a, b) => b.score - a.score);

  const keep = scored[0].ctv;
  const expire = scored[scored.length - 1].ctv;
  const gap = scored[0].score - scored[scored.length - 1].score;
  const confidence = gap > 25 ? "high" : gap > 8 ? "medium" : "low";

  return {
    keep,
    expire,
    keepReasons: scored[0].reasons,
    expireReasons: scored[scored.length - 1].reasons,
    confidence,
    remainingApproved: approved.length - 1,
  };
}

export function shortCtvTitle(ctv: ContractHealthCtvSummary, max = 56): string {
  const t = (ctv.title ?? "Untitled").trim();
  return t.length <= max ? t : `${t.slice(0, max - 1)}…`;
}
