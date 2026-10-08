/** Client-side header checks aligned with backend ``storage_location_matches_plant``. */

export function storageLocationPlantCode(raw: string): string {
  const v = String(raw ?? "").trim();
  if (!v) return "";
  const pipe = v.indexOf("|");
  if (pipe >= 0) return v.slice(0, pipe).trim();
  return "";
}

export function storageLocationMatchesPlant(storageLocation: string, plant: string): boolean {
  const pl = String(plant ?? "").trim();
  const slocRaw = String(storageLocation ?? "").trim();
  if (!pl || !slocRaw) return true;
  const seg = storageLocationPlantCode(slocRaw);
  if (!seg) return false;
  return seg.toUpperCase() === pl.toUpperCase();
}

export function sanitizeProcurementHeader(header: Record<string, unknown>): Record<string, unknown> {
  const h = { ...header };
  const plant = String(h.plant ?? "").trim();
  const sloc = String(h.storage_location ?? "").trim();
  if (plant && sloc && !storageLocationMatchesPlant(sloc, plant)) {
    h.storage_location = "";
  }
  return h;
}
