import type { ContractHealthClientTree } from "@/lib/api";

/** All CTV ids present in a client tree (sites + global). */
export function collectCtvIdsFromTree(tree: ContractHealthClientTree | null): Set<string> {
  const ids = new Set<string>();
  if (!tree) return ids;
  for (const c of tree.global_terms) {
    ids.add(c.contract_terms_version_id);
  }
  for (const site of tree.sites) {
    for (const c of site.terms) {
      ids.add(c.contract_terms_version_id);
    }
  }
  return ids;
}

export function resolveCtvSelection(
  tree: ContractHealthClientTree,
  preferredId: string | undefined
): string {
  const ids = collectCtvIdsFromTree(tree);
  if (preferredId && ids.has(preferredId)) return preferredId;
  for (const site of tree.sites) {
    if (site.overlap_conflict && site.terms[0]) {
      return site.terms[0].contract_terms_version_id;
    }
  }
  for (const site of tree.sites) {
    if (site.terms[0]) return site.terms[0].contract_terms_version_id;
  }
  if (tree.global_terms[0]) return tree.global_terms[0].contract_terms_version_id;
  return "";
}
