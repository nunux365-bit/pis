import type { ProcurementTicket } from "./procurementApi";
import { emptyLineFromKeys, emptyPhase1Block } from "./procurementEditorShared";
import { sanitizeProcurementHeader } from "./procurementFormSanitize";

const MAX_LINES = 25;

type StoredDraft = {
  v: 3;
  document_type: string;
  header: Record<string, unknown>;
  lines: Record<string, unknown>[];
};

function mergeNonEmptyRow(
  target: Record<string, unknown>,
  source: Record<string, unknown>,
  keys: string[]
): Record<string, unknown> {
  const out = { ...target };
  for (const k of keys) {
    const v = source[k];
    if (v !== undefined && v !== null && String(v).trim().length > 0) out[k] = v;
  }
  return out;
}

function mergeBlockRow(
  seed: Record<string, unknown>,
  saved: Record<string, unknown>,
  blockKeys: string[]
): Record<string, unknown> {
  const out = mergeNonEmptyRow(seed, saved, blockKeys);
  const al = (saved as { allocations?: unknown }).allocations;
  if (Array.isArray(al) && al.length > 0) {
    out.allocations = al;
  }
  return out;
}

function mergeHeader(
  base: Record<string, unknown>,
  stored: Record<string, unknown> | undefined
): Record<string, unknown> {
  if (!stored || typeof stored !== "object") return base;
  const h = { ...base };
  for (const [k, v] of Object.entries(stored)) {
    if (v !== undefined && v !== null && String(v).trim().length > 0) h[k] = v;
  }
  return sanitizeProcurementHeader(h);
}

/** Merge unsaved session draft into a blank template (header + lines). */
export function mergeProcurementDraft(
  base: ProcurementTicket["form"],
  documentType: string,
  lineFieldKeys: string[],
  rawJson: string | null
): ProcurementTicket["form"] {
  if (!rawJson) return base;
  try {
    const o = JSON.parse(rawJson) as StoredDraft;
    if (
      !o ||
      typeof o !== "object" ||
      o.v !== 3 ||
      String(o.document_type ?? "").toUpperCase() !== documentType.toUpperCase()
    ) {
      return base;
    }

    const header = mergeHeader(
      { ...(base.header && typeof base.header === "object" ? base.header : {}) },
      o.header
    );

    const saved = (o.lines || []).filter((row) => row && typeof row === "object") as Record<string, unknown>[];
    if (saved.length === 0) return { header, lines: base.lines };

    const lines: Record<string, unknown>[] = [];
    const baseLines = base.lines?.length ? base.lines : [emptyLineFromKeys(lineFieldKeys)];
    const isBlock = Boolean(
      baseLines[0] &&
        typeof baseLines[0] === "object" &&
        Array.isArray((baseLines[0] as { allocations?: unknown }).allocations)
    );
    for (let i = 0; i < saved.length; i++) {
      const seed =
        i < baseLines.length && typeof baseLines[i] === "object" && baseLines[i]
          ? { ...(baseLines[i] as Record<string, unknown>) }
          : isBlock
            ? { ...emptyPhase1Block(documentType) }
            : emptyLineFromKeys(lineFieldKeys);
      lines.push(
        isBlock ? mergeBlockRow(seed, saved[i]!, lineFieldKeys) : mergeNonEmptyRow(seed, saved[i]!, lineFieldKeys)
      );
    }
    return { header, lines };
  } catch {
    return base;
  }
}

export function serializeProcurementDraft(
  documentType: string,
  form: ProcurementTicket["form"]
): StoredDraft {
  const lines = (form.lines || [])
    .filter((row) => row && typeof row === "object")
    .slice(0, MAX_LINES) as Record<string, unknown>[];
  const headerRaw =
    form.header && typeof form.header === "object" ? (form.header as Record<string, unknown>) : {};
  return {
    v: 3,
    document_type: documentType,
    header: sanitizeProcurementHeader(headerRaw),
    lines,
  };
}
