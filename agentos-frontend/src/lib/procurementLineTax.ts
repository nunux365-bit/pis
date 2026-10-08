/** Per-line tax code (line wins; header fallback for legacy forms). */

export function lineTaxCode(
  block: Record<string, unknown>,
  header: Record<string, unknown>
): string {
  const line = String(block.tax_code ?? "").trim();
  if (line) return line;
  return String(header.tax_code ?? "").trim();
}

/** Mirror backend sync_header_tax_code — header follows first line with a tax code. */
export function syncHeaderTaxCode(
  header: Record<string, unknown>,
  lines: Record<string, unknown>[]
): void {
  for (const row of lines) {
    const tc = String(row.tax_code ?? "").trim();
    if (tc) {
      header.tax_code = tc;
      return;
    }
  }
  if (!String(header.tax_code ?? "").trim()) {
    header.tax_code = "";
  }
}

/** Promote legacy header-only tax onto lines; sync header from lines. */
export function ensureFormLineTaxCodes<T extends { header?: unknown; lines?: unknown }>(form: T): T {
  const header =
    form.header && typeof form.header === "object"
      ? { ...(form.header as Record<string, unknown>) }
      : ({} as Record<string, unknown>);
  const rawLines = Array.isArray(form.lines) ? form.lines : [];
  const lines = rawLines.map((row) =>
    row && typeof row === "object" ? { ...(row as Record<string, unknown>) } : row
  );
  const headerTax = String(header.tax_code ?? "").trim();
  for (const row of lines) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    if (!String(r.tax_code ?? "").trim() && headerTax) {
      r.tax_code = headerTax;
    }
  }
  syncHeaderTaxCode(
    header,
    lines.filter((r): r is Record<string, unknown> => Boolean(r && typeof r === "object"))
  );
  return { ...form, header, lines };
}
