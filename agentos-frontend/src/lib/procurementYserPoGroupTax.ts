/**
 * YSER PO: SAP stores one TaxCode per PO item (service group). UI lines may disagree.
 * Mirrors backend ``yser_po_group_*`` helpers in ``line_tax_code.py``.
 */

import { lineCatalogGroup } from "./procurementCatalogGroup";
import { lineTaxCode } from "./procurementLineTax";

export type YserPoGroupTaxHint = {
  groupKey: string;
  lineIndices: number[];
  resolvedTax: string;
  /** 0-based line index whose tax is sent to SAP for this group. */
  sourceLineIndex: number;
  hasConflict: boolean;
};

export function yserPoLineGroupBuckets(
  lines: Record<string, unknown>[],
  header: Record<string, unknown>
): { groupKey: string; lineIndices: number[] }[] {
  const order: string[] = [];
  const map = new Map<string, number[]>();
  lines.forEach((row, i) => {
    if (!row || typeof row !== "object") return;
    const grp = lineCatalogGroup(row, header, "YSER");
    const key = grp || "__header__";
    if (!map.has(key)) {
      map.set(key, []);
      order.push(key);
    }
    map.get(key)!.push(i);
  });
  return order.map((groupKey) => ({ groupKey, lineIndices: map.get(groupKey)! }));
}

/** Canonical SAP item tax for one service-group bucket (last line wins when codes differ). */
export function yserPoGroupItemTaxResolution(
  lineIndices: number[],
  lines: Record<string, unknown>[],
  header: Record<string, unknown>
): { tax: string; sourceLineIndex: number; hasConflict: boolean } {
  const taxes: { tax: string; idx: number }[] = [];
  for (const idx of lineIndices) {
    const row = lines[idx];
    if (!row || typeof row !== "object") continue;
    const tax = lineTaxCode(row, header);
    if (tax) taxes.push({ tax, idx });
  }
  if (taxes.length === 0) {
    const fallback = String(header.tax_code ?? "").trim();
    const idx = lineIndices[0] ?? -1;
    return { tax: fallback, sourceLineIndex: idx, hasConflict: false };
  }
  const unique = new Set(taxes.map((t) => t.tax));
  if (unique.size === 1) {
    return { tax: taxes[0].tax, sourceLineIndex: taxes[0].idx, hasConflict: false };
  }
  const last = taxes[taxes.length - 1];
  return { tax: last.tax, sourceLineIndex: last.idx, hasConflict: true };
}

/** One hint per service group with 2+ lines (PO editor banners). */
export function yserPoGroupTaxHints(
  lines: Record<string, unknown>[],
  header: Record<string, unknown>
): YserPoGroupTaxHint[] {
  const hints: YserPoGroupTaxHint[] = [];
  for (const { groupKey, lineIndices } of yserPoLineGroupBuckets(lines, header)) {
    if (lineIndices.length < 2) continue;
    const { tax, sourceLineIndex, hasConflict } = yserPoGroupItemTaxResolution(
      lineIndices,
      lines,
      header
    );
    if (!tax) continue;
    hints.push({
      groupKey: groupKey === "__header__" ? "" : groupKey,
      lineIndices,
      resolvedTax: tax,
      sourceLineIndex,
      hasConflict,
    });
  }
  return hints;
}

/** Map first line index in each multi-line group → hint (banner placement). */
export function yserPoGroupTaxHintByFirstLine(
  lines: Record<string, unknown>[],
  header: Record<string, unknown>
): Map<number, YserPoGroupTaxHint> {
  const map = new Map<number, YserPoGroupTaxHint>();
  for (const hint of yserPoGroupTaxHints(lines, header)) {
    const anchor = hint.hasConflict ? hint.sourceLineIndex : Math.min(...hint.lineIndices);
    map.set(anchor, hint);
  }
  return map;
}

function formatLineList(lineNumbers: number[]): string {
  if (lineNumbers.length === 0) return "";
  if (lineNumbers.length === 1) return String(lineNumbers[0]);
  if (lineNumbers.length === 2) return `${lineNumbers[0]} and ${lineNumbers[1]}`;
  const last = lineNumbers[lineNumbers.length - 1];
  return `${lineNumbers.slice(0, -1).join(", ")}, and ${last}`;
}

export type YserPoGroupTaxBannerContent = {
  title: string;
  body: string;
};

export function yserPoGroupTaxBannerContent(hint: YserPoGroupTaxHint): YserPoGroupTaxBannerContent {
  const lineNums = hint.lineIndices.map((i) => i + 1).sort((a, b) => a - b);
  const linesLabel = formatLineList(lineNums);
  const grpLabel = hint.groupKey ? ` in service group ${hint.groupKey}` : "";
  const sourceLine = hint.sourceLineIndex + 1;
  const otherLineNums = lineNums.filter((n) => n !== sourceLine);

  if (hint.hasConflict) {
    const ignored =
      otherLineNums.length === 1
        ? `Tax on line ${otherLineNums[0]} is not sent to SAP.`
        : `Tax on lines ${formatLineList(otherLineNums)} is not sent to SAP.`;
    return {
      title: "These lines use different tax codes",
      body: `Lines ${linesLabel}${grpLabel} are one combined item in SAP, which allows only one tax code. When you save, SAP will use ${hint.resolvedTax} from line ${sourceLine}. ${ignored}`,
    };
  }

  return {
    title: "Shared tax for this service group",
    body: `Lines ${linesLabel}${grpLabel} will all sync to SAP with tax code ${hint.resolvedTax}.`,
  };
}

/** @deprecated Use yserPoGroupTaxBannerContent */
export function yserPoGroupTaxBannerMessage(hint: YserPoGroupTaxHint): string {
  const { title, body } = yserPoGroupTaxBannerContent(hint);
  return `${title}. ${body}`;
}
