import type { ClientTreeSite } from "./siteUtils";

/** Client-side guard aligned with backend expire validation (MIS must keep one billable approved). */
export function canSafelyExpireCtv(site: ClientTreeSite | null, ctvId: string): {
  safe: boolean;
  reason?: string;
} {
  if (!site) return { safe: true };

  const approved = site.terms.filter((t) => String(t.status).toLowerCase() === "approved");

  if (site.overlap_conflict && approved.length >= 2) {
    return { safe: true };
  }

  if (site.picker_ctv_id === ctvId) {
    return {
      safe: false,
      reason:
        "This is the only contract MIS can bill for this site this month. Expiring it will block MIS.",
    };
  }

  if (approved.length === 1 && approved[0].contract_terms_version_id === ctvId) {
    return {
      safe: false,
      reason: "Only approved contract for this site — use overlap flow or fix dates instead.",
    };
  }

  return { safe: true };
}
