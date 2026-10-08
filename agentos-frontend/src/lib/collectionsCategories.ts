export type CategoryMeta = {
  label: string;
  nextStep: string;
  whatItMeans: string;
  /** Tailwind bg class applied to the table row in the breakdown. */
  rowBgClass: string;
};

/**
 * Static metadata for each GmailIntelligence category slug.
 * Keys match the `category` field returned by the backend classifier.
 * Unknown slugs fall back to `getCategoryMeta`'s generated fallback.
 */
export const COLLECTIONS_CATEGORY_META: Record<string, CategoryMeta> = {
  internal_processing: {
    label: "Internal processing",
    nextStep: "Track WoW",
    whatItMeans: "Client forwarded internally to AP/finance team",
    rowBgClass: "bg-yellow-50/70",
  },
  recon_pending: {
    label: "Recon pending",
    nextStep: "Added to the finance recon sheet",
    whatItMeans: "Client shared UTR/payment proof; knock-off delayed from our end",
    rowBgClass: "bg-green-50/70",
  },
  request_for_more_info: {
    label: "Request for more info",
    nextStep: "Relevant KAM to track and revert",
    whatItMeans: "Client asked for invoice copy, PO number, or clarification",
    rowBgClass: "bg-amber-50/60",
  },
  discrepancy_or_issues: {
    label: "Discrepancy or issues",
    nextStep: "Relevant KAM to track and revert",
    whatItMeans: "Dispute on amount, service, or invoice validity",
    rowBgClass: "bg-orange-50/60",
  },
  email_poc_issues: {
    label: "Email/POC issues",
    nextStep: "Relevant KAM to track and revert",
    whatItMeans: "Wrong contact / redirected to new POC",
    rowBgClass: "bg-rose-50/60",
  },
  automated_reply: {
    label: "Automated reply",
    nextStep: "Await human revert",
    whatItMeans: "Out-of-office or system-generated acknowledgement",
    rowBgClass: "bg-stone-50/60",
  },

  // ── Outreach reply categories (v2 — matches categorizer_prompt.txt) ───────
  schedule_meeting: {
    label: "Schedule Meeting",
    nextStep: "Book meeting / send calendar invite",
    whatItMeans: "Prospect asked to connect, demo, or set up a call",
    rowBgClass: "bg-teal-50/70",
  },
  share_proposal: {
    label: "Share Proposal",
    nextStep: "Send proposal / company deck",
    whatItMeans: "Prospect requested pricing, proposal, or capabilities info",
    rowBgClass: "bg-emerald-50/70",
  },
  marked_team_member: {
    label: "Team Member Added",
    nextStep: "Identify correct POC and follow up",
    whatItMeans: "Prospect routed internally or introduced another stakeholder",
    rowBgClass: "bg-amber-50/60",
  },
  follow_up_no_response: {
    label: "Follow Up Later",
    nextStep: "Follow up in 30 days or next quarter",
    whatItMeans: "Prospect deferred — OOO, busy, or timing-based postponement",
    rowBgClass: "bg-stone-50/60",
  },
  wrong_contact: {
    label: "Wrong Contact",
    nextStep: "Find correct SPOC and update sheet",
    whatItMeans: "Recipient rejected ownership — not the right person",
    rowBgClass: "bg-rose-50/60",
  },
  not_interested: {
    label: "Not Interested",
    nextStep: "Stop outreach",
    whatItMeans: "Prospect explicitly declined or disqualified the opportunity",
    rowBgClass: "bg-red-50/60",
  },
};

/** Formats an unrecognised category slug into title-case display text. */
export function formatCategorySlug(slug: string): string {
  return slug.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Resolves metadata for a category slug, with a safe fallback for unknown values. */
export function getCategoryMeta(slug: string): CategoryMeta {
  const meta = COLLECTIONS_CATEGORY_META[slug];
  if (!meta) {
    return {
      label: formatCategorySlug(slug),
      nextStep: "—",
      whatItMeans: "—",
      rowBgClass: "bg-[var(--bg-card)]",
    };
  }
  return meta;
}
