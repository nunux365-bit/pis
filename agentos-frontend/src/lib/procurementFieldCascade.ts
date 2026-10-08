/**
 * When a header or block field changes, clear dependent fields so stale catalogue values do not linger.
 * Only runs when the value actually changes (same string → no-op).
 */

import { plantMatchesPurchasingOrg } from "./procurementPlantOrg";
import { DEFAULT_PROCUREMENT_ORDER_UNIT } from "./procurementEditorShared";

export type ProcurementFormLines = Record<string, unknown>[];

function clearAllocationAssignments(
  lines: ProcurementFormLines,
  documentType: string
): ProcurementFormLines {
  const dt = (documentType || "").toUpperCase();
  return lines.map((row) => {
    const r = { ...(typeof row === "object" && row ? row : {}) } as Record<string, unknown>;
    const allocs = Array.isArray(r.allocations) ? r.allocations : [];
    if (!allocs.length) return r;
    r.allocations = allocs.map((a) => {
      const alloc = { ...(typeof a === "object" && a ? a : {}) } as Record<string, unknown>;
      if (dt === "YAST") {
        alloc.asset = "";
      } else {
        alloc.cost_center = "";
      }
      return alloc;
    });
    if (dt === "YAST") {
      r.asset = "";
    }
    return r;
  });
}

export function applyProcurementHeaderCascade(
  documentType: string,
  headerKey: string,
  prevValue: string,
  nextValue: string,
  header: Record<string, unknown>,
  lines: ProcurementFormLines
): { header: Record<string, unknown>; lines: ProcurementFormLines } | null {
  if (String(prevValue ?? "").trim() === String(nextValue ?? "").trim()) {
    return null;
  }

  const nextHeader: Record<string, unknown> = { ...header, [headerKey]: nextValue };
  const dt = (documentType || "").toUpperCase();
  let anyChange = false;
  let nextLines = lines.map((row) => ({ ...(typeof row === "object" && row ? row : {}) }));

  if (headerKey === "purchasing_org" || headerKey === "company_code") {
    nextLines = clearAllocationAssignments(nextLines, dt);
    anyChange = true;
    const org = String(nextHeader.purchasing_org ?? nextHeader.company_code ?? "").trim();
    const plant = String(nextHeader.plant ?? "").trim();
    if (plant && org && !plantMatchesPurchasingOrg(plant, org)) {
      nextHeader.plant = "";
      nextHeader.storage_location = "";
      anyChange = true;
    }
  }

  if (headerKey === "plant") {
    if (String(nextHeader.storage_location ?? "").trim()) {
      nextHeader.storage_location = "";
      anyChange = true;
    }
    // YUNB/YSER cost centres are plant-scoped (any sloc under plant); clear on plant change.
    if (dt !== "YAST") {
      nextLines = clearAllocationAssignments(nextLines, dt);
      anyChange = true;
    }
  }

  // Legacy: header group change still clears all lines (old tickets / API clients).
  nextLines = nextLines.map((row) => {
    const r = { ...(typeof row === "object" && row ? row : {}) } as Record<string, unknown>;

    if (headerKey === "material_group" && dt !== "YSER") {
      if (!String(r.material_group ?? "").trim()) {
        r.material_group = nextValue;
        anyChange = true;
      }
      if (String(r.material ?? "").trim()) {
        r.material = "";
        anyChange = true;
      }
      if (String(r.short_text ?? "").trim()) {
        r.short_text = "";
        anyChange = true;
      }
      if (String(r.order_unit ?? "").trim() !== DEFAULT_PROCUREMENT_ORDER_UNIT) {
        r.order_unit = DEFAULT_PROCUREMENT_ORDER_UNIT;
        anyChange = true;
      }
    }

    if (headerKey === "service_group" && dt === "YSER") {
      if (!String(r.service_group ?? "").trim()) {
        r.service_group = nextValue;
        anyChange = true;
      }
      if (String(r.service ?? "").trim()) {
        r.service = "";
        anyChange = true;
      }
      if (String(r.short_text ?? "").trim()) {
        r.short_text = "";
        anyChange = true;
      }
    }

    return r;
  });

  if (!anyChange) {
    return null;
  }

  return { header: nextHeader, lines: nextLines };
}

/** Clear catalogue fields on one line when its group changes. */
export function applyProcurementBlockCascade(
  documentType: string,
  blockKey: string,
  prevValue: string,
  nextValue: string,
  block: Record<string, unknown>
): Record<string, unknown> | null {
  if (String(prevValue ?? "").trim() === String(nextValue ?? "").trim()) {
    return null;
  }

  const dt = (documentType || "").toUpperCase();
  const next: Record<string, unknown> = { ...block, [blockKey]: nextValue };
  let changed = true;

  if (blockKey === "material_group" && dt !== "YSER") {
    if (String(block.material ?? "").trim()) {
      next.material = "";
    }
    if (String(block.short_text ?? "").trim()) {
      next.short_text = "";
    }
    if (String(block.order_unit ?? "").trim() !== DEFAULT_PROCUREMENT_ORDER_UNIT) {
      next.order_unit = DEFAULT_PROCUREMENT_ORDER_UNIT;
    }
  }

  if (blockKey === "service_group" && dt === "YSER") {
    if (String(block.service ?? "").trim()) {
      next.service = "";
    }
    if (String(block.short_text ?? "").trim()) {
      next.short_text = "";
    }
  }

  return changed ? next : null;
}
