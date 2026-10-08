/**
 * Client-side PR/PO validation — mirrors ``field_schema.validate_form`` for Phase 1 blocks + allocations.
 */
import type { FieldSpec, ProcurementTicket } from "./procurementApi";
import { catalogGroupField, lineCatalogGroup } from "./procurementCatalogGroup";
import { lineTaxCode } from "./procurementLineTax";
import { procurementFieldDomId } from "./procurementFieldId";
import { storageLocationMatchesPlant } from "./procurementFormSanitize";

export type ProcurementFormSchemaSlice = {
  code: string;
  header_fields: FieldSpec[];
  line_fields: FieldSpec[];
  block_fields?: FieldSpec[];
  allocation_fields?: FieldSpec[];
};

export type ClientFormIssue = { message: string } & (
  | { scope: "header"; key: string }
  | { scope: "block"; blockIndex: number; key: string }
  | { scope: "allocation"; blockIndex: number; allocationIndex: number; key: string }
);

/** YAST account splits are asset+qty (not cost centre). Keep in sync with backend ``allocation_field_specs``. */
export const YAST_ALLOCATION_FIELD_SPECS: FieldSpec[] = [
  {
    key: "asset",
    label: "Asset",
    scope: "allocation",
    widget: "search",
    reference_domain: "asset",
    required_pr: true,
    required_po: true,
    help_text:
      "Search by asset number or description. Scoped to the purchasing organisation (company code). Each split can use a different asset.",
    ui_visible: true,
  },
  {
    key: "qty",
    label: "Quantity",
    scope: "allocation",
    widget: "number",
    reference_domain: null,
    required_pr: true,
    required_po: true,
    help_text: "Quantity for this asset split.",
    ui_visible: true,
  },
];

/**
 * Prefer schema allocation fields when they already describe assets; otherwise force the YAST
 * asset/qty shape so a stale API schema cannot render cost-centre splits on Asset workflows.
 */
export function resolveProcurementAllocationFields(
  documentType: string,
  schemaFields: FieldSpec[] | undefined | null
): FieldSpec[] {
  const dt = (documentType || "").toUpperCase();
  const fields = Array.isArray(schemaFields) ? schemaFields : [];
  if (dt !== "YAST") return fields;
  const hasAsset = fields.some(
    (f) => f.key === "asset" || f.reference_domain === "asset"
  );
  const hasCostCenter = fields.some(
    (f) => f.key === "cost_center" || f.reference_domain === "cost_center"
  );
  if (hasAsset && !hasCostCenter) return fields;
  return YAST_ALLOCATION_FIELD_SPECS;
}

function norm(v: unknown): string {
  if (v === null || v === undefined) return "";
  return String(v).trim();
}

/** Mirror backend ``asset_codes_equal`` — leading zeros do not make assets distinct. */
function assetCodesEqual(left: string, right: string): boolean {
  const a = norm(left);
  const b = norm(right);
  if (a === b) return true;
  return (a.replace(/^0+/, "") || "0") === (b.replace(/^0+/, "") || "0");
}

function isPositiveInt(s: string): boolean {
  if (!s) return false;
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) && n > 0 && String(n) === s.trim();
}

/** Positive qty — whole or decimal (YSER SAP hydrate may return e.g. 0.999). */
function isPositiveQty(s: string): boolean {
  if (!s) return false;
  const v = Number.parseFloat(s.replace(",", "."));
  return Number.isFinite(v) && v > 0;
}

const PO_REQUESTOR_EMAIL_MAX_LEN = 30;
const HEADER_NOTE_PR_MAX_LEN = 40;
const HEADER_NOTE_PO_MAX_LEN = 12;
const PO_TEXT_FIELD_MAX_LEN = 2500;

const PO_TEXT_HEADER_KEYS = [
  "po_remarks",
  "po_deadlines",
  "po_terms_of_delivery",
] as const;

function isValidRequestorEmail(val: string): boolean {
  const s = norm(val);
  if (!s) return true;
  if (s.length > PO_REQUESTOR_EMAIL_MAX_LEN) return false;
  if (!s.includes("@")) return false;
  const [local, domain] = s.split("@");
  return Boolean(local && domain && domain.includes("."));
}

function required(f: FieldSpec, kind: "PR" | "PO"): boolean {
  return kind === "PR" ? f.required_pr : f.required_po;
}

function appendHeaderNoteIssues(
  kind: "PR" | "PO",
  header: Record<string, unknown>,
  issues: ClientFormIssue[]
): void {
  const note = norm(header.header_note);
  const maxLen = kind === "PO" ? HEADER_NOTE_PO_MAX_LEN : HEADER_NOTE_PR_MAX_LEN;
  if (note.length > maxLen) {
    issues.push({
      scope: "header",
      key: "header_note",
      message: `Header note must be at most ${maxLen} characters${kind === "PO" ? " on purchase orders" : ""}.`,
    });
  }
}

function appendPoRequestorEmailIssues(
  kind: "PR" | "PO",
  header: Record<string, unknown>,
  issues: ClientFormIssue[]
): void {
  if (kind !== "PO") return;
  const reqEmail = norm(header.requestor_email);
  if (reqEmail && !isValidRequestorEmail(reqEmail)) {
    issues.push({
      scope: "header",
      key: "requestor_email",
      message: `Requestor email id must be a valid email address (max ${PO_REQUESTOR_EMAIL_MAX_LEN} characters).`,
    });
  }
}

function appendPoTextFieldIssues(
  kind: "PR" | "PO",
  header: Record<string, unknown>,
  issues: ClientFormIssue[]
): void {
  if (kind !== "PO") return;
  for (const key of PO_TEXT_HEADER_KEYS) {
    const val = norm(header[key]);
    if (val.length > PO_TEXT_FIELD_MAX_LEN) {
      issues.push({
        scope: "header",
        key,
        message: `Must be at most ${PO_TEXT_FIELD_MAX_LEN} characters.`,
      });
    }
  }
}

/** Phase 1: header + blocks + allocations. */
export function validateProcurementForm(
  kind: "PR" | "PO",
  form: ProcurementTicket["form"],
  active: ProcurementFormSchemaSlice
): ClientFormIssue[] {
  const issues: ClientFormIssue[] = [];
  const header = form.header && typeof form.header === "object" ? (form.header as Record<string, unknown>) : {};
  const lines = Array.isArray(form.lines) ? form.lines : [];
  const blockSpecs = active.block_fields ?? [];
  const allocSpecs = resolveProcurementAllocationFields(active.code, active.allocation_fields);

  if (blockSpecs.length > 0) {
    if (lines.length === 0) {
      issues.push({ scope: "block", blockIndex: 0, key: "material", message: "Add at least one block." });
      return issues;
    }

    for (const f of active.header_fields) {
      if (f.ui_visible === false) continue;
      if (!required(f, kind)) continue;
      if (!norm(header[f.key])) {
        issues.push({ scope: "header", key: f.key, message: `Enter ${f.label}.` });
      }
    }

    appendHeaderNoteIssues(kind, header, issues);
    appendPoRequestorEmailIssues(kind, header, issues);
    appendPoTextFieldIssues(kind, header, issues);

    const plant = norm(header.plant);
    const sloc = norm(header.storage_location);
    if (plant && sloc && !storageLocationMatchesPlant(sloc, plant)) {
      issues.push({
        scope: "header",
        key: "storage_location",
        message: `Storage location must belong to plant ${plant} (use a ${plant}|… location, not ${sloc}).`,
      });
    }

    const seen = new Set<string>();
    for (let bi = 0; bi < lines.length; bi++) {
      const raw = lines[bi];
      if (!raw || typeof raw !== "object") {
        issues.push({ scope: "block", blockIndex: bi, key: "material", message: "Invalid block." });
        continue;
      }
      const row = raw as Record<string, unknown>;
      const dt = active.code.toUpperCase();
      // YSER: same service on multiple lines is valid (different CC splits / SAP service refs).
      // YUNB/YAST: duplicate material codes across lines are not allowed.
      if (dt !== "YSER") {
        const code = norm(row.material);
        if (code) {
          if (seen.has(code)) {
            issues.push({
              scope: "block",
              blockIndex: bi,
              key: "material",
              message: "Duplicate material or service is not allowed.",
            });
          }
          seen.add(code);
        }
      }
      for (const f of blockSpecs) {
        if (f.ui_visible === false) continue;
        // YAST: assets are only on allocation rows (line.asset is mirrored).
        if (f.key === "asset") continue;
        if (!required(f, kind)) continue;
        const grpKey = catalogGroupField(dt);
        const val =
          f.key === grpKey
            ? lineCatalogGroup(row, header, active.code)
            : f.key === "tax_code"
              ? lineTaxCode(row, header)
              : norm(row[f.key]);
        if (!val) {
          issues.push({ scope: "block", blockIndex: bi, key: f.key, message: `Enter ${f.label}.` });
        }
      }

      const shortHidden = blockSpecs.find((s) => s.key === "short_text" && s.ui_visible === false);
      if (shortHidden && required(shortHidden, kind)) {
        const primary = dt === "YSER" ? norm(row.service) : norm(row.material);
        if (primary && !norm(row.short_text)) {
          issues.push({
            scope: "block",
            blockIndex: bi,
            key: dt === "YSER" ? "service" : "material",
            message:
              dt === "YSER"
                ? "Pick the service again from search so its description is saved on the line."
                : "Pick the material again from search so its description is saved on the line.",
          });
        }
      }

      const allocs = row.allocations;
      if (!Array.isArray(allocs) || allocs.length === 0) {
        issues.push({
          scope: "allocation",
          blockIndex: bi,
          allocationIndex: 0,
          key: dt === "YAST" ? "asset" : "cost_center",
          message:
            dt === "YAST"
              ? "Add at least one asset allocation."
              : "Add at least one cost centre allocation.",
        });
        continue;
      }
      const ccSeen = new Set<string>();
      const assetSeen: string[] = [];
      for (let ai = 0; ai < allocs.length; ai++) {
        const a = allocs[ai];
        if (!a || typeof a !== "object") {
          issues.push({
            scope: "allocation",
            blockIndex: bi,
            allocationIndex: ai,
            key: dt === "YAST" ? "asset" : "cost_center",
            message: "Invalid allocation row.",
          });
          continue;
        }
        const ad = a as Record<string, unknown>;
        for (const f of allocSpecs) {
          if (!required(f, kind)) continue;
          if (!norm(ad[f.key])) {
            issues.push({
              scope: "allocation",
              blockIndex: bi,
              allocationIndex: ai,
              key: f.key,
              message: `Enter ${f.label}.`,
            });
          }
        }
        const cc = norm(ad.cost_center);
        if (dt !== "YAST") {
          if (cc && ccSeen.has(cc)) {
            issues.push({
              scope: "allocation",
              blockIndex: bi,
              allocationIndex: ai,
              key: "cost_center",
              message: "Duplicate cost centre in this block.",
            });
          }
          if (cc) ccSeen.add(cc);
        } else {
          const asset = norm(ad.asset);
          if (asset && assetSeen.some((seen) => assetCodesEqual(asset, seen))) {
            issues.push({
              scope: "allocation",
              blockIndex: bi,
              allocationIndex: ai,
              key: "asset",
              message: "Duplicate asset in this block.",
            });
          }
          if (asset) assetSeen.push(asset);
        }
        const qv = norm(ad.qty);
        if (qv && !isPositiveQty(qv)) {
          issues.push({
            scope: "allocation",
            blockIndex: bi,
            allocationIndex: ai,
            key: "qty",
            message: "Quantity must be a positive number.",
          });
        }
      }
    }
    return issues;
  }

  /* Legacy flat line */
  if (lines.length === 0) {
    const k0 = active.line_fields[0]?.key ?? "quantity";
    issues.push({ scope: "block", blockIndex: 0, key: k0, message: "Add at least one line item." });
    return issues;
  }
  for (const f of active.header_fields) {
    if (!required(f, kind)) continue;
    if (!norm(header[f.key])) {
      issues.push({ scope: "header", key: f.key, message: `Enter ${f.label}.` });
    }
  }
  const dtLegacy = active.code.toUpperCase();
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] && typeof lines[i] === "object" ? (lines[i] as Record<string, unknown>) : {};
    for (const f of active.line_fields) {
      if (f.ui_visible === false) continue;
      const val = norm(line[f.key]);
      const req = required(f, kind);
      if (f.key === "item_category") {
        if (req && val !== "" && val.toUpperCase() !== "D") {
          issues.push({
            scope: "block",
            blockIndex: i,
            key: f.key,
            message: "Choose Standard (leave blank) or Service (D).",
          });
        }
        continue;
      }
      if (req && !val) {
        issues.push({ scope: "block", blockIndex: i, key: f.key, message: `Enter ${f.label}.` });
      }
    }
    const shortLineHidden = active.line_fields.find((s) => s.key === "short_text" && s.ui_visible === false);
    if (shortLineHidden && required(shortLineHidden, kind)) {
      const primary = dtLegacy === "YSER" ? norm(line.service) : norm(line.material);
      if (primary && !norm(line.short_text)) {
        issues.push({
          scope: "block",
          blockIndex: i,
          key: dtLegacy === "YSER" ? "service" : "material",
          message:
            dtLegacy === "YSER"
              ? "Pick the service again from search so its description is saved on the line."
              : "Pick the material again from search so its description is saved on the line.",
        });
      }
    }
  }
  appendHeaderNoteIssues(kind, header, issues);
  appendPoRequestorEmailIssues(kind, header, issues);
  appendPoTextFieldIssues(kind, header, issues);
  return issues;
}

export type LinkedValidationIssue = { elementId: string; text: string };

export function linkedValidationIssues(
  kind: "PR" | "PO",
  documentType: string,
  raw: ClientFormIssue[]
): LinkedValidationIssue[] {
  return raw.map((issue) => {
    let elementId: string;
    if (issue.scope === "header") {
      elementId = procurementFieldDomId(kind, documentType, `h-${issue.key}`);
    } else if (issue.scope === "block") {
      elementId = procurementFieldDomId(kind, documentType, `B${issue.blockIndex}-b-${issue.key}`);
    } else {
      elementId = procurementFieldDomId(
        kind,
        documentType,
        `B${issue.blockIndex}-a${issue.allocationIndex}-${issue.key}`
      );
    }
    return { elementId, text: issue.message };
  });
}
