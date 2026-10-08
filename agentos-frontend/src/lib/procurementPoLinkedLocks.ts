/** PO fields read-only when the order is linked to a purchase request (must match SAP PR). */

export const PO_LINKED_LOCKED_HEADER_KEYS = new Set([
  "purchasing_org",
  "purchasing_group",
  "plant",
  "storage_location",
  "material_group",
  "service_group",
]);

export const PO_LINKED_LOCKED_BLOCK_KEYS = new Set([
  "material",
  "service",
  "material_group",
  "service_group",
  "asset",
  "account_assignment_cat",
]);

export function isPoFieldLockedFromPr(
  linkedToPr: boolean,
  kind: "PR" | "PO",
  scope: "header" | "block",
  key: string
): boolean {
  if (!linkedToPr || kind !== "PO") return false;
  if (scope === "header") return PO_LINKED_LOCKED_HEADER_KEYS.has(key);
  return PO_LINKED_LOCKED_BLOCK_KEYS.has(key);
}
