import type { ProcurementTicket } from "./procurementApi";
import { mergeProcurementDraft, serializeProcurementDraft } from "./procurementDraftMerge";
import { recalcFormValuationPrices } from "./procurementEditorShared";

const DRAFT_KEY_PREFIX = "agentos_procurement_po_draft:";
const LEGACY_SUBMITTED_KEY = "agentos_procurement_last_po_form";

function draftKey(documentType: string): string {
  return `${DRAFT_KEY_PREFIX}${documentType.toUpperCase()}`;
}

function clearLegacySubmittedStorage(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(LEGACY_SUBMITTED_KEY);
  } catch {
    /* ignore */
  }
}

/** Restore unsaved standalone PO draft from sessionStorage. */
export function mergePoDraft(
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

export function persistPoDraft(documentType: string, form: ProcurementTicket["form"]): void {
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

export function clearPoDraft(documentType: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(draftKey(documentType));
    clearLegacySubmittedStorage();
  } catch {
    /* ignore */
  }
}

/** @deprecated Use ``mergePoDraft``. */
export const mergeLastPoForm = mergePoDraft;

/** @deprecated Successful submit now calls ``clearPoDraft`` instead. */
export function persistLastPoForm(documentType: string, form: ProcurementTicket["form"]): void {
  persistPoDraft(documentType, form);
}
