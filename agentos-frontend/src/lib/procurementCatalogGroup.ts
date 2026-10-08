/** Per-line catalogue group (line wins; header fallback for legacy forms). */

export function catalogGroupField(documentType: string): "material_group" | "service_group" {
  return (documentType || "").toUpperCase() === "YSER" ? "service_group" : "material_group";
}

export function lineCatalogGroup(
  block: Record<string, unknown>,
  header: Record<string, unknown>,
  documentType: string
): string {
  const key = catalogGroupField(documentType);
  const line = String(block[key] ?? "").trim();
  if (line) return line;
  return String(header[key] ?? "").trim();
}

/** Mirror backend sync_header_catalog_group — header follows first line with a group. */
export function syncHeaderCatalogGroup(
  header: Record<string, unknown>,
  lines: Record<string, unknown>[],
  documentType: string
): void {
  const key = catalogGroupField(documentType);
  for (const row of lines) {
    const grp = String(row[key] ?? "").trim();
    if (grp) {
      header[key] = grp;
      return;
    }
  }
  if (!String(header[key] ?? "").trim()) {
    header[key] = "";
  }
}

/** Promote legacy header-only group onto lines; sync header from lines. */
export function ensureFormLineCatalogGroups<T extends { header?: unknown; lines?: unknown }>(
  form: T,
  documentType: string
): T {
  const dt = (documentType || "").toUpperCase();
  const key = catalogGroupField(documentType);
  const header =
    form.header && typeof form.header === "object"
      ? { ...(form.header as Record<string, unknown>) }
      : ({} as Record<string, unknown>);
  const rawLines = Array.isArray(form.lines) ? form.lines : [];
  const lines = rawLines.map((row) =>
    row && typeof row === "object" ? { ...(row as Record<string, unknown>) } : row
  );
  const headerGrp = String(header[key] ?? "").trim();
  for (const row of lines) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    if (!String(r[key] ?? "").trim() && headerGrp) {
      r[key] = headerGrp;
    }
  }
  syncHeaderCatalogGroup(
    header,
    lines.filter((r): r is Record<string, unknown> => Boolean(r && typeof r === "object")),
    dt
  );
  return { ...form, header, lines };
}

export function catalogLineSearchBlocked(
  lineFieldKey: string,
  documentType: string,
  block: Record<string, unknown>,
  header: Record<string, unknown> = {}
): boolean {
  const dt = (documentType || "").toUpperCase();
  if (lineFieldKey === "material" && (dt === "YUNB" || dt === "YAST")) {
    return !lineCatalogGroup(block, header, documentType);
  }
  if (lineFieldKey === "service" && dt === "YSER") {
    return !lineCatalogGroup(block, header, documentType);
  }
  return false;
}

export function catalogLineSearchPlaceholder(lineFieldKey: string): string | undefined {
  if (lineFieldKey === "service") return "Select service group on this line first";
  if (lineFieldKey === "material") return "Select material group on this line first";
  return undefined;
}
