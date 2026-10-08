/** Plant code prefix ↔ purchasing org (matches backend ``plant_org.py``). */

const ORG_BY_PLANT_PREFIX: Record<string, string> = {
  H: "1MGH",
  L: "1LFS",
  T: "1MGT",
};

export function plantMatchesPurchasingOrg(plantCode: string, purchasingOrg: string): boolean {
  const plant = String(plantCode ?? "").trim();
  const org = String(purchasingOrg ?? "")
    .trim()
    .toUpperCase();
  if (!plant) return !org;
  if (!org) return true;
  const prefix = plant[0]!.toUpperCase();
  const scopedOrg = ORG_BY_PLANT_PREFIX[prefix];
  if (!scopedOrg) return true;
  return scopedOrg.toUpperCase() === org;
}

export function filterPlantsForOrg<T extends { code: string }>(
  rows: T[],
  purchasingOrg: string
): T[] {
  const org = String(purchasingOrg ?? "").trim();
  if (!org) return rows;
  return rows.filter((r) => plantMatchesPurchasingOrg(r.code, org));
}

/** Sloc code from header storage location (``H001|3021`` → ``3021``). Matches backend ``storage_location_sloc_code``. */
export function storageLocationSlocCode(raw: string): string {
  const v = String(raw ?? "").trim();
  if (!v) return "";
  const pipe = v.indexOf("|");
  if (pipe >= 0) return v.slice(pipe + 1).trim();
  return v;
}
