/**
 * Shared pieces for PR/PO new-editor pages — one source of truth so flows stay aligned.
 *
 * **Design:** Keep **bootstrap** (empty forms, schema keys) and **error copy** here; API calls stay in
 * ``procurementApi.ts``. Use ``humanizeProcurementUnknownError`` for consistent session/network messaging.
 */

import type { FieldSpec, ProcurementTicket, SchemaResponse } from "./procurementApi";
import { humanizeProcurementUnknownError } from "./procurementApi";
import { ensureFormLineCatalogGroups } from "./procurementCatalogGroup";
import { ensureFormLineTaxCodes } from "./procurementLineTax";
import { linkedValidationIssues, type ClientFormIssue } from "./procurementFormValidation";

export const DEFAULT_PROCUREMENT_ORDER_UNIT = "QT";

/** SAP order unit default: EA for service (YSER), QT for material (YUNB/YAST). */
export function defaultProcurementOrderUnit(documentType: string): string {
  return documentType.toUpperCase() === "YSER" ? "EA" : DEFAULT_PROCUREMENT_ORDER_UNIT;
}

export const PROCUREMENT_SCHEMA_LOAD_FALLBACK =
  "Could not load procurement options. Check your connection and try again.";

export const PROCUREMENT_PARENT_PRS_LOAD_FALLBACK =
  "Could not load the list of purchase requisitions. Try again.";

/** Header keys persisted server-side (including SAP-only fields filled on save). */
export const PROCUREMENT_HEADER_KEYS: readonly string[] = [
  "purchasing_org",
  "company_code",
  "purchasing_doc_type",
  "payment_terms",
  "purchasing_group",
  "plant",
  "storage_location",
  "tax_code",
  "vendor",
  "header_note",
  "material_group",
  "service_group",
  "requestor_email",
  "po_remarks",
  "po_deadlines",
  "po_terms_of_delivery",
];

/** PO editor header fields from schema (includes PO-only fields such as requestor email). */
export function procurementPoHeaderFields(
  dt: { po_header_fields?: FieldSpec[]; header_fields: FieldSpec[] }
): FieldSpec[] {
  return dt.po_header_fields ?? dt.header_fields;
}

const PO_REQUESTOR_EMAIL_MAX_LEN = 30;

/** Default requestor email on new PO forms when the field is empty. */
export function applyPoRequestorEmailDefault(
  form: ProcurementTicket["form"],
  email: string | null | undefined
): ProcurementTicket["form"] {
  const e = String(email ?? "").trim();
  if (!e || e.length > PO_REQUESTOR_EMAIL_MAX_LEN) return form;
  const header =
    form.header && typeof form.header === "object"
      ? ({ ...(form.header as Record<string, unknown>) } as Record<string, unknown>)
      : {};
  if (!String(header.requestor_email ?? "").trim()) {
    header.requestor_email = e;
    return { ...form, header };
  }
  return form;
}

export function schemaLoadErrorMessage(e: unknown): string {
  return humanizeProcurementUnknownError(e, PROCUREMENT_SCHEMA_LOAD_FALLBACK);
}

export function parentsListLoadErrorMessage(e: unknown): string {
  return humanizeProcurementUnknownError(e, PROCUREMENT_PARENT_PRS_LOAD_FALLBACK);
}

/** After validation fails, focus the first field with an error (inline messages only; no summary strip). */
export function focusFirstProcurementClientIssue(
  kind: "PR" | "PO",
  documentType: string,
  issues: ClientFormIssue[]
): void {
  if (issues.length === 0) return;
  const linked = linkedValidationIssues(kind, documentType, issues);
  const id = linked[0]?.elementId;
  if (!id) return;
  window.requestAnimationFrame(() => {
    const el = document.getElementById(id);
    if (!el) return;
    const focusable =
      el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement
        ? el
        : el.querySelector<HTMLElement>(
            "input:not([type='hidden']), textarea, select, button[role='combobox'], [tabindex]:not([tabindex='-1'])"
          );
    focusable?.focus();
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

/** One empty line row from field keys (legacy flat line). */
export function emptyLineFromKeys(keys: string[]): Record<string, unknown> {
  return Object.fromEntries(
    keys.map((k) => [k, k === "order_unit" ? DEFAULT_PROCUREMENT_ORDER_UNIT : ""])
  );
}

/** Phase 1: one material/service block with cost-centre allocations. */
export function emptyPhase1Block(documentType: string): Record<string, unknown> {
  const dt = documentType.toUpperCase();
  const lk = dt === "YSER" ? "service" : "material";
  return {
    line_kind: lk,
    material_group: "",
    service_group: "",
    material: "",
    service: "",
    short_text: "",
    order_unit: defaultProcurementOrderUnit(documentType),
    unit_price: "",
    valuation_price: "",
    delivery_date: "",
    tax_code: "",
    asset: "",
    account_assignment_cat: "",
    item_category: "",
    net_price: "",
    gross_price: "",
    allocations:
      (documentType || "").toUpperCase() === "YAST"
        ? [{ asset: "", qty: "" }]
        : [{ cost_center: "", qty: "" }],
  };
}

/** Phase 1 empty form (header + one block). */
export function emptyPhase1Form(documentType: string): ProcurementTicket["form"] {
  const header = Object.fromEntries(PROCUREMENT_HEADER_KEYS.map((k) => [k, ""])) as Record<string, unknown>;
  return { header, lines: [emptyPhase1Block(documentType)] };
}

function parsePrice(raw: unknown): number | null {
  const n = Number.parseFloat(String(raw ?? "").replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

function formatPrice(n: number): string {
  if (Math.abs(n - Math.round(n)) < 1e-9) return String(Math.round(n));
  return String(Math.round(n * 100) / 100);
}

/** Sum allocation qty for a YSER line (matches backend CC split totals). */
export function yserAllocationQtyTotal(allocs: unknown): number {
  if (!Array.isArray(allocs)) return 0;
  let sum = 0;
  for (const a of allocs) {
    if (!a || typeof a !== "object") continue;
    const q = Number.parseFloat(String((a as Record<string, unknown>).qty || "").replace(",", "."));
    if (Number.isFinite(q) && q > 0) sum += q;
  }
  return sum;
}

/**
 * YSER Z GET returns line total in GrossPrice/ValuationPrice — split to unit × total qty for the UI.
 */
export function normalizeYserLinePricesFromSap(line: Record<string, unknown>): Record<string, unknown> {
  const qtyTotal = yserAllocationQtyTotal(line.allocations);
  if (qtyTotal <= 0) return line;

  const val = parsePrice(line.valuation_price);
  const gross = parsePrice(line.gross_price);
  const unit = parsePrice(line.unit_price);
  const lineTotal = val ?? gross ?? (unit != null ? unit * qtyTotal : null);
  if (lineTotal == null || lineTotal <= 0) return line;

  const derivedUnit = lineTotal / qtyTotal;
  if (unit != null && Math.abs(unit - derivedUnit) < 0.001) {
    return {
      ...line,
      unit_price: formatPrice(derivedUnit),
      valuation_price: formatPrice(lineTotal),
      gross_price: formatPrice(lineTotal),
    };
  }
  if (unit != null && qtyTotal <= 1 && Math.abs(unit - lineTotal) < 0.001) {
    return line;
  }
  return {
    ...line,
    unit_price: formatPrice(derivedUnit),
    valuation_price: formatPrice(lineTotal),
    gross_price: formatPrice(lineTotal),
  };
}

export function normalizeYserFormFromSap(form: ProcurementTicket["form"]): ProcurementTicket["form"] {
  const lines = Array.isArray(form.lines)
    ? form.lines.map((row) =>
        row && typeof row === "object"
          ? normalizeYserLinePricesFromSap(row as Record<string, unknown>)
          : row
      )
    : form.lines;
  return { ...form, lines };
}

export function recalcLineValuationPrice(row: Record<string, unknown>): Record<string, unknown> {
  const up = parsePrice(row.unit_price);
  if (up == null) {
    if (String(row.valuation_price ?? "").trim()) {
      return { ...row, valuation_price: "" };
    }
    return row;
  }
  const allocs = Array.isArray(row.allocations) ? row.allocations : [];
  let sum = 0;
  for (const a of allocs) {
    if (!a || typeof a !== "object") continue;
    const q = Number.parseFloat(String((a as Record<string, unknown>).qty || "").replace(",", "."));
    if (Number.isFinite(q) && q > 0) sum += q * up;
  }
  if (sum <= 0) {
    if (String(row.valuation_price ?? "").trim()) {
      return { ...row, valuation_price: "" };
    }
    return row;
  }
  return { ...row, valuation_price: formatPrice(sum) };
}

/** Recompute line valuation totals after draft restore (unit price × allocation qty). */
export function recalcFormValuationPrices(form: ProcurementTicket["form"]): ProcurementTicket["form"] {
  const lines = Array.isArray(form.lines) ? form.lines : [];
  const next = lines.map((row) =>
    row && typeof row === "object" ? recalcLineValuationPrice(row as Record<string, unknown>) : row
  );
  return { ...form, lines: next };
}

/** Normalize SAP-hydrated forms for the editor (YSER totals; PO material line totals). */
export function hydrateProcurementFormForEditor(
  form: ProcurementTicket["form"],
  opts: { kind: string; documentType: string; formSource?: string | null }
): ProcurementTicket["form"] {
  const dt = (opts.documentType || "").toUpperCase();
  const kind = (opts.kind || "").toUpperCase();
  let out = form;
  if (dt === "YSER" && opts.formSource === "sap") {
    out = normalizeYserFormFromSap(out);
  } else if (kind === "PO" && dt !== "YSER") {
    out = recalcFormValuationPrices(out);
  }
  return ensureFormLineTaxCodes(ensureFormLineCatalogGroups(out, opts.documentType));
}

/**
 * Blank form from schema.
 * When ``line_fields`` is empty (Phase 1), builds block + allocation shape; else legacy flat line.
 */
export function emptyTicketForm(
  headerFields: FieldSpec[],
  lineFields: FieldSpec[],
  documentTypeCode?: string
): ProcurementTicket["form"] {
  if (lineFields.length === 0 && documentTypeCode) {
    return emptyPhase1Form(documentTypeCode);
  }
  if (lineFields.length === 0) {
    return { header: {}, lines: [] };
  }
  return {
    header: emptyLineFromKeys(headerFields.map((f) => f.key)),
    lines: [emptyLineFromKeys(lineFields.map((f) => f.key))],
  };
}

/** Stable key when only the set of workflow codes matters (avoids draft reset on schema refetch). */
export function procurementSchemaTypesKey(schema: SchemaResponse | null): string {
  return schema?.document_types.map((d) => d.code).join("\0") ?? "";
}
