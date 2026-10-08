import type { ProcurementTicket } from "./procurementApi";
import { mergeProcurementDraft, serializeProcurementDraft } from "./procurementDraftMerge";
import { recalcFormValuationPrices } from "./procurementEditorShared";

const DRAFT_KEY_PREFIX = "agentos_procurement_pr_draft:";
/** Legacy: submitted-form cache (v2) — cleared on read; no longer written. */
const LEGACY_SUBMITTED_KEY = "agentos_procurement_last_pr_form";
const LEGACY_SUBMITTED_V1_KEY = "agentos_procurement_last_pr_form_v1";

function draftKey(documentType: string): string {
  return `${DRAFT_KEY_PREFIX}${documentType.toUpperCase()}`;
}

function clearLegacySubmittedStorage(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(LEGACY_SUBMITTED_KEY);
    window.localStorage.removeItem(LEGACY_SUBMITTED_V1_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * Restore an **unsaved** PR draft from sessionStorage (survives refresh; not used after successful submit).
 */
export function mergePrDraft(
  base: ProcurementTicket["form"],
  documentType: string,
  lineFieldKeys: string[]
): ProcurementTicket["form"] {
  if (typeof window === "undefined") return base;
  clearLegacySubmittedStorage();
  const raw = window.sessionStorage.getItem(draftKey(documentType));
  const merged = mergeProcurementDraft(base, documentType, lineFieldKeys, raw);
  return recalcFormValuationPrices(merged);
}

/** Persist in-progress PR draft (debounced from the new-PR page). */
export function persistPrDraft(documentType: string, form: ProcurementTicket["form"]): void {
  if (typeof window === "undefined") return;
  try {
    const payload = serializeProcurementDraft(documentType, form);
    const hasHeader = Object.values(payload.header).some((v) => String(v ?? "").trim().length > 0);
    const hasLines = payload.lines.some((row) =>
      Object.entries(row).some(([k, v]) => {
        if (k === "allocations") return false;
        return String(v ?? "").trim().length > 0;
      })
    );
    if (!hasHeader && !hasLines) {
      window.sessionStorage.removeItem(draftKey(documentType));
      return;
    }
    window.sessionStorage.setItem(draftKey(documentType), JSON.stringify(payload));
  } catch {
    /* quota / private mode */
  }
}

/** Drop unsaved draft after a successful create. */
export function clearPrDraft(documentType: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(draftKey(documentType));
    clearLegacySubmittedStorage();
  } catch {
    /* ignore */
  }
}

/** @deprecated Use ``mergePrDraft`` — kept for imports during transition. */
export const mergeLastPrForm = mergePrDraft;

/** @deprecated Successful submit now calls ``clearPrDraft`` instead. */
export function persistLastPrForm(documentType: string, form: ProcurementTicket["form"]): void {
  persistPrDraft(documentType, form);
}
