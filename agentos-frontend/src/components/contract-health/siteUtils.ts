import type { ContractHealthClientTree } from "@/lib/api";

export const GLOBAL_BUCKET_ID = "__global__";

export type ClientTreeSite = ContractHealthClientTree["sites"][number];

export function siteNeedsAttention(site: ClientTreeSite): boolean {
  if (site.overlap_conflict) return true;
  return site.terms.some(
    (t) =>
      t.null_rate_count > 0 ||
      t.null_site_count > 0 ||
      String(t.status).toLowerCase() === "pending"
  );
}

export function globalNeedsAttention(tree: ContractHealthClientTree): boolean {
  return tree.global_terms.some(
    (t) =>
      t.null_rate_count > 0 ||
      t.null_site_count > 0 ||
      String(t.status).toLowerCase() === "pending"
  );
}

export function findBucketForCtv(tree: ContractHealthClientTree, ctvId: string): string | null {
  if (tree.global_terms.some((t) => t.contract_terms_version_id === ctvId)) {
    return GLOBAL_BUCKET_ID;
  }
  for (const site of tree.sites) {
    if (site.terms.some((t) => t.contract_terms_version_id === ctvId)) {
      return site.service_site_id;
    }
  }
  return null;
}

export function defaultCtvForBucket(
  tree: ContractHealthClientTree,
  bucketId: string
): string {
  if (bucketId === GLOBAL_BUCKET_ID) {
    return tree.global_terms[0]?.contract_terms_version_id ?? "";
  }
  const site = tree.sites.find((s) => s.service_site_id === bucketId);
  if (!site) return "";
  return site.picker_ctv_id || site.terms[0]?.contract_terms_version_id || "";
}

export function sortSitesForList(sites: ClientTreeSite[]): ClientTreeSite[] {
  return [...sites].sort((a, b) => {
    const aScore = siteListScore(a);
    const bScore = siteListScore(b);
    if (aScore !== bScore) return bScore - aScore;
    return a.site_name.localeCompare(b.site_name);
  });
}

function siteListScore(site: ClientTreeSite): number {
  if (site.overlap_conflict) return 100;
  if (siteNeedsAttention(site)) return 50;
  if (!site.picker_ctv_id && site.terms.length > 0) return 25;
  return 0;
}

export function nextOverlapSiteId(sites: ClientTreeSite[], afterSiteId: string): string | null {
  const ordered = sortSitesForList(sites).filter((s) => s.overlap_conflict);
  const idx = ordered.findIndex((s) => s.service_site_id === afterSiteId);
  if (idx < 0) return ordered[0]?.service_site_id ?? null;
  return ordered[idx + 1]?.service_site_id ?? null;
}

export function siteIssueSummary(site: ClientTreeSite): string[] {
  const parts: string[] = [];
  if (site.overlap_conflict) parts.push("overlap");
  const issueTerms = site.terms.filter(
    (t) => t.null_rate_count > 0 || t.null_site_count > 0
  ).length;
  if (issueTerms > 0) parts.push(`${issueTerms} data issue${issueTerms > 1 ? "s" : ""}`);
  const pending = site.terms.filter((t) => String(t.status).toLowerCase() === "pending").length;
  if (pending > 0) parts.push(`${pending} pending`);
  return parts;
}
