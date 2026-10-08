/**
 * Procurement (PR/PO) API client — thin wrappers over ``/api/procurement/*``.
 *
 * **Design:** Pages/components own UI state; this module owns transport (``apiFetch`` / ``apiJson``),
 * typed DTOs, and **human-readable errors** via ``humanizeProcurementEditorError`` / ``humanizeProcurementUnknownError``.
 * Shared editor bootstrap helpers live in ``procurementEditorShared.ts``.
 */

import { apiFetch, apiJson, messageFromFailedResponse } from "./api";

/** Kept in sync with ``app.procurement.multipart`` (25 MB per file, 10 files, 100 MB total). */
export const PROCUREMENT_MAX_FILES = 10;
export const PROCUREMENT_MAX_FILE_BYTES = 25 * 1024 * 1024;
export const PROCUREMENT_MAX_TOTAL_BYTES = 100 * 1024 * 1024;

/** HTML file input accept + client-side MIME/extension gate (matches server). */
export const PROCUREMENT_ATTACHMENT_ACCEPT =
  ".pdf,.jpg,.jpeg,.png,.xls,.xlsx,application/pdf,image/jpeg,image/png,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** Read picked files synchronously, then reset the input (must not run inside ``setState`` updaters). */
export function readProcurementFileInput(input: HTMLInputElement): File[] {
  const picked = input.files ? Array.from(input.files) : [];
  input.value = "";
  return picked;
}

function procurementFilePickKey(file: File): string {
  return `${file.name}\0${file.size}\0${file.lastModified}`;
}

export type ProcurementFilePickResult = {
  files: File[];
  errors: string[];
};

/** Validate and append file-picker results to an existing staged list (pure — safe inside ``setState``). */
export function appendProcurementFilePicks(prev: File[], picked: File[]): ProcurementFilePickResult {
  if (picked.length === 0) return { files: prev, errors: [] };

  const errors: string[] = [];
  const accepted: File[] = [];
  const seen = new Set(prev.map(procurementFilePickKey));

  for (const file of picked) {
    const name = (file.name || "").trim() || "An attachment";
    const key = procurementFilePickKey(file);
    if (seen.has(key)) {
      errors.push(`${name} is already in the list.`);
      continue;
    }
    if (!procurementAttachmentTypeAllowed(file.name, file.type || "")) {
      errors.push(`${name} must be a PDF, JPG, PNG, XLS, or XLSX.`);
      continue;
    }
    if (file.size > PROCUREMENT_MAX_FILE_BYTES) {
      const mb = Math.round(PROCUREMENT_MAX_FILE_BYTES / (1024 * 1024));
      errors.push(`${name} exceeds ${mb} MB.`);
      continue;
    }
    seen.add(key);
    accepted.push(file);
  }

  if (accepted.length === 0) return { files: prev, errors };

  const room = PROCUREMENT_MAX_FILES - prev.length;
  if (room <= 0) {
    errors.push(`You can attach at most ${PROCUREMENT_MAX_FILES} files.`);
    return { files: prev, errors };
  }

  const toAdd = accepted.slice(0, room);
  if (toAdd.length < accepted.length) {
    errors.push(`Only ${room} more file(s) allowed (limit ${PROCUREMENT_MAX_FILES}).`);
  }

  let next = [...prev, ...toAdd];
  let totalBytes = next.reduce((sum, file) => sum + file.size, 0);
  const totalCapMb = Math.round(PROCUREMENT_MAX_TOTAL_BYTES / (1024 * 1024));
  while (next.length > prev.length && totalBytes > PROCUREMENT_MAX_TOTAL_BYTES) {
    const skipped = next.pop()!;
    totalBytes -= skipped.size;
    errors.push(
      `${(skipped.name || "").trim() || "A file"} skipped — combined size exceeds ${totalCapMb} MB.`
    );
  }

  return { files: next, errors };
}

/** @deprecated Prefer ``readProcurementFileInput`` + ``appendProcurementFilePicks`` in the event handler. */
export function mergeProcurementFileInputSelection(prev: File[], input: HTMLInputElement): File[] {
  return appendProcurementFilePicks(prev, readProcurementFileInput(input)).files;
}

/** Remove one staged file before upload (by list index). */
export function removeProcurementFileAtIndex(files: File[], index: number): File[] {
  if (index < 0 || index >= files.length) return files;
  return files.filter((_, i) => i !== index);
}

const PROCUREMENT_ATTACHMENT_MIME_ALLOW = new Set([
  "application/pdf",
  "image/jpeg",
  "image/png",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]);

const PROCUREMENT_ATTACHMENT_MIME_ALIASES: Record<string, string> = {
  "image/jpg": "image/jpeg",
  "image/pjpeg": "image/jpeg",
  "application/excel": "application/vnd.ms-excel",
  "application/x-excel": "application/vnd.ms-excel",
  "application/x-msexcel": "application/vnd.ms-excel",
  "application/msexcel": "application/vnd.ms-excel",
  "application/haansoftxlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

const PROCUREMENT_ATTACHMENT_EXT_ALLOW = new Set([
  ".pdf",
  ".jpg",
  ".jpeg",
  ".png",
  ".xls",
  ".xlsx",
]);

const PROCUREMENT_DANGEROUS_INNER_EXTS = new Set([
  ".exe",
  ".bat",
  ".cmd",
  ".com",
  ".msi",
  ".scr",
  ".js",
  ".vbs",
  ".ps1",
  ".dll",
]);

function normalizeClientMime(mime: string): string {
  return (mime || "").trim().toLowerCase().split(";", 1)[0]!.trim();
}

function attachmentExt(name: string): string {
  const n = (name || "").trim();
  if (!n.includes(".")) return "";
  return `.${n.split(".").pop()!.toLowerCase()}`;
}

function filenameHasDangerousInnerExt(name: string): boolean {
  const base = (name || "").trim().split(/[/\\]/).pop() || "";
  const parts = base.toLowerCase().split(".");
  if (parts.length < 3) return false;
  for (let i = 1; i < parts.length - 1; i++) {
    if (PROCUREMENT_DANGEROUS_INNER_EXTS.has(`.${parts[i]}`)) return true;
  }
  return false;
}

/** Exported for unit tests — mirrors server resolve rules (MIME only; no magic bytes). */
export function procurementAttachmentTypeAllowed(filename: string, fileType: string): boolean {
  if (filenameHasDangerousInnerExt(filename)) return false;
  let t = normalizeClientMime(fileType);
  if (t && PROCUREMENT_ATTACHMENT_MIME_ALIASES[t]) {
    t = PROCUREMENT_ATTACHMENT_MIME_ALIASES[t]!;
  }
  const ext = attachmentExt(filename);
  if (
    t === "application/vnd.ms-excel" ||
    t === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  ) {
    if (ext === ".xls" || ext === ".xlsx") return true;
  }
  if (t && PROCUREMENT_ATTACHMENT_MIME_ALLOW.has(t)) return true;
  if ((!t || t === "application/octet-stream") && PROCUREMENT_ATTACHMENT_EXT_ALLOW.has(ext)) {
    return true;
  }
  return false;
}

async function parseLinkedPoConflictFromResponse(
  res: Response,
  fallback: string
): Promise<ProcurementLinkedPoConflictError | null> {
  if (res.status !== 409) return null;
  try {
    const j = (await res.clone().json()) as { detail?: unknown };
    const d = j.detail;
    if (!d || typeof d !== "object") return null;
    const o = d as Record<string, unknown>;
    const message =
      typeof o.message === "string" && o.message.trim()
        ? o.message.trim()
        : fallback;
    if (!/already has a linked purchase order/i.test(message)) return null;
    const linkedPoId = typeof o.linked_po_id === "string" ? o.linked_po_id : null;
    return new ProcurementLinkedPoConflictError(message, linkedPoId);
  } catch {
    return null;
  }
}

async function requireOk(res: Response, fallback: string): Promise<void> {
  if (res.ok) return;
  const conflict = await parseLinkedPoConflictFromResponse(res, fallback);
  if (conflict) throw conflict;
  throw new Error(await messageFromFailedResponse(res, fallback));
}

/** Preflight that mirrors the server-side caps. Keeps failed large uploads off the wire. */
function assertAttachmentsWithinCaps(files: File[]): void {
  if (files.length > PROCUREMENT_MAX_FILES) {
    throw new Error(`Too many attachments — limit is ${PROCUREMENT_MAX_FILES}.`);
  }
  let total = 0;
  for (const f of files) {
    if (f.size > PROCUREMENT_MAX_FILE_BYTES) {
      const mb = Math.round(PROCUREMENT_MAX_FILE_BYTES / (1024 * 1024));
      throw new Error(`${f.name || "An attachment"} exceeds ${mb} MB.`);
    }
    total += f.size;
  }
  if (total > PROCUREMENT_MAX_TOTAL_BYTES) {
    const mb = Math.round(PROCUREMENT_MAX_TOTAL_BYTES / (1024 * 1024));
    throw new Error(`Attachments combined exceed ${mb} MB for a single request.`);
  }
}

/** Matches server: PDF, JPG/JPEG, PNG, XLS, XLSX (aliases + extension fallback). */
function assertProcurementAttachmentMimeTypes(files: File[]): void {
  for (const f of files) {
    const name = (f.name || "").trim();
    if (procurementAttachmentTypeAllowed(name, f.type || "")) continue;
    throw new Error(
      `${name || "An attachment"} must be a PDF, JPG, PNG, XLS, or XLSX. Other file types are not accepted.`
    );
  }
}

function appendMultipartFiles(fd: FormData, files: File[]): void {
  assertAttachmentsWithinCaps(files);
  assertProcurementAttachmentMimeTypes(files);
  for (const f of files) fd.append("files", f);
}

async function postMultipartTicket(
  path: "/api/procurement/tickets/pr" | "/api/procurement/tickets/po",
  payload: Record<string, unknown>,
  files: File[],
  errorFallback: string
): Promise<ProcurementTicket> {
  const fd = new FormData();
  fd.append("payload", JSON.stringify(payload));
  appendMultipartFiles(fd, files);
  const res = await apiFetch(path, { method: "POST", body: fd });
  await requireOk(res, errorFallback);
  try {
    return (await res.json()) as ProcurementTicket;
  } catch {
    throw new Error("The server returned an unreadable response. Try again or contact support.");
  }
}

export type LinkedTicketSummary = {
  id: string;
  kind: string;
  sap_id: string | null;
  document_type: string;
};

/** PO create / prefill blocked because the PR already has a linked PO ticket. */
export class ProcurementLinkedPoConflictError extends Error {
  readonly linkedPoId: string | null;

  constructor(message: string, linkedPoId: string | null) {
    super(message);
    this.name = "ProcurementLinkedPoConflictError";
    this.linkedPoId = linkedPoId;
  }
}

export type ProcurementTicket = {
  id: string;
  kind: "PR" | "PO";
  parent_pr_id: string | null;
  parent_pr_summary?: LinkedTicketSummary | null;
  linked_pos?: LinkedTicketSummary[];
  document_type: string;
  form: { header: Record<string, unknown>; lines: Record<string, unknown>[] };
  attachments: Array<{
    id?: string;
    sap_document_id?: string | null;
    name?: string;
    mime_type?: string;
    size?: number;
    uploaded_at?: string;
    sap_synced_at?: string | null;
    sap_sync_error?: string | null;
    can_retry_upload?: boolean;
    /** ``sap`` = file is in SAP; ``local`` = AgentOS only (pending upload). */
    storage?: "local" | "sap";
  }>;
  drive_folder_id?: string | null;
  sap_id: string | null;
  sap_sync: {
    attempt_count?: number;
    next_retry_at?: string | null;
    last_error?: string | null;
    create_submitted?: boolean;
    sync_pending?: boolean;
    attachments_pending?: boolean;
    attachment_last_error?: string | null;
  };
  /** ``sap`` when form was loaded from SAP on this response; ``db`` when using stored ticket only. */
  form_source?: "sap" | "db";
  sap_form_read_error?: string | null;
  /** Set when SAP attachment list merge failed on this response. */
  sap_attachment_hydrate_error?: string | null;
  /** True when SAP document exists but every line item is deleted — editor shows empty lines. */
  sap_no_active_lines?: boolean;
  version: number;
  created_at: string;
  updated_at: string;
};

export type ProcurementTicketSyncStatus = Pick<
  ProcurementTicket,
  "id" | "sap_id" | "sap_sync" | "attachments" | "version" | "updated_at" | "sap_attachment_hydrate_error"
>;

export type FieldSpec = {
  key: string;
  label: string;
  scope: "header" | "line" | "block" | "allocation";
  widget: string;
  reference_domain: string | null;
  required_pr: boolean;
  required_po: boolean;
  help_text?: string | null;
  max_length?: number | null;
  ui_visible?: boolean;
};

export type SchemaResponse = {
  document_types: Array<{
    code: string;
    label: string;
    header_fields: FieldSpec[];
    /** PO editor headers (includes PO-only fields such as requestor email). */
    po_header_fields?: FieldSpec[];
    line_fields: FieldSpec[];
    block_fields?: FieldSpec[];
    allocation_fields?: FieldSpec[];
  }>;
  /** SAP sync retry cap from server (aligns ``Needs help`` with backend). */
  sap_max_attempts?: number;
};

export async function getProcurementSchema(): Promise<SchemaResponse> {
  return apiJson<SchemaResponse>("/api/procurement/schema");
}

export type ReferenceValueRow = {
  code: string;
  label: string;
  /** Raw catalogue description (search only); use for SAP short text, not display ``label``. */
  description?: string;
  document_type: string | null;
  applies_to_kind?: string | null;
  extra: unknown;
};

export async function getReferenceValues(
  domain: string,
  documentType: string,
  opts?: { ticketKind?: "PR" | "PO"; purchasingOrg?: string; plant?: string }
): Promise<ReferenceValueRow[]> {
  const q = new URLSearchParams({ domain, document_type: documentType });
  if (opts?.ticketKind) q.set("ticket_kind", opts.ticketKind);
  if (opts?.purchasingOrg) q.set("purchasing_org", opts.purchasingOrg);
  if (opts?.plant) q.set("plant", opts.plant);
  return apiJson(`/api/procurement/reference-values?${q.toString()}`);
}

export async function searchReferenceValues(
  domain: string,
  documentType: string,
  opts: {
    ticketKind?: "PR" | "PO";
    q?: string;
    companyCode?: string;
    materialGroup?: string;
    serviceGroup?: string;
    ccEntity?: string;
    ccProfitCenter?: string;
    ccDepartment?: string;
    ccBusinessArea?: string;
    /** Cost centre: Business Area must match any storage location under this plant. */
    plant?: string;
    limit?: number;
    offset?: number;
    /** When aborted, rejects — use in debounced search to drop stale responses. */
    signal?: AbortSignal;
  }
): Promise<{ items: ReferenceValueRow[]; total: number; limit: number; offset: number }> {
  const q = new URLSearchParams({ domain, document_type: documentType });
  if (opts.ticketKind) q.set("ticket_kind", opts.ticketKind);
  if (opts.q != null && opts.q !== "") q.set("q", opts.q);
  if (opts.companyCode) q.set("company_code", opts.companyCode);
  if (opts.materialGroup) q.set("material_group", opts.materialGroup);
  if (opts.serviceGroup) q.set("service_group", opts.serviceGroup);
  if (opts.ccEntity) q.set("cc_entity", opts.ccEntity);
  if (opts.ccProfitCenter) q.set("cc_profit_center", opts.ccProfitCenter);
  if (opts.ccDepartment) q.set("cc_department", opts.ccDepartment);
  if (opts.ccBusinessArea) q.set("cc_business_area", opts.ccBusinessArea);
  if (opts.plant) q.set("plant", opts.plant);
  if (opts.limit != null) q.set("limit", String(opts.limit));
  if (opts.offset != null) q.set("offset", String(opts.offset));
  return apiJson(`/api/procurement/reference-values/search?${q.toString()}`, opts.signal ? { signal: opts.signal } : {});
}

export type CostCenterFacetOption = { value: string; label: string };

export type CostCenterFacetScope = {
  resolved_entity: string | null;
  needs_purchasing_organisation: boolean;
  no_directory_match_for_purchasing_org: boolean;
};

export type CostCenterFacetsResponse = {
  entity: CostCenterFacetOption[];
  profit_center: CostCenterFacetOption[];
  department: CostCenterFacetOption[];
  has_rich_extra: boolean;
  /** Present when ``purchasing_org`` was sent on the request (scoped facet mode). */
  facet_scope?: CostCenterFacetScope | null;
};

export async function getCostCenterFacetOptions(
  documentType: string,
  opts?: { ticketKind?: "PR" | "PO"; purchasingOrg?: string }
): Promise<CostCenterFacetsResponse> {
  const q = new URLSearchParams({ document_type: documentType });
  if (opts?.ticketKind) q.set("ticket_kind", opts.ticketKind);
  if (opts?.purchasingOrg !== undefined) q.set("purchasing_org", opts.purchasingOrg);
  return apiJson(`/api/procurement/reference-values/cost-center-facets?${q.toString()}`);
}

export async function listMyTickets(
  kind?: "PR" | "PO",
  opts?: { limit?: number; offset?: number }
): Promise<ProcurementTicket[]> {
  const q = new URLSearchParams();
  if (kind) q.set("kind", kind);
  if (opts?.limit != null) q.set("limit", String(opts.limit));
  if (opts?.offset != null) q.set("offset", String(opts.offset));
  const qs = q.toString();
  return apiJson<ProcurementTicket[]>(`/api/procurement/tickets${qs ? `?${qs}` : ""}`);
}

export type ProcurementAuditEntry = {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  actor_user_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
};

export async function getTicketAudit(ticketId: string, limit = 100): Promise<ProcurementAuditEntry[]> {
  return apiJson<ProcurementAuditEntry[]>(
    `/api/procurement/tickets/${encodeURIComponent(ticketId)}/audit?limit=${limit}`
  );
}

export async function listParentPRs(): Promise<ProcurementTicket[]> {
  return apiJson<ProcurementTicket[]>("/api/procurement/tickets/parent-prs");
}

export async function getTicket(id: string): Promise<ProcurementTicket> {
  return apiJson<ProcurementTicket>(`/api/procurement/tickets/${id}`);
}

/** Lightweight poll while only attachments are syncing (skips SAP form hydrate). */
export async function getTicketSyncStatus(id: string): Promise<ProcurementTicketSyncStatus> {
  return apiJson<ProcurementTicketSyncStatus>(
    `/api/procurement/tickets/${encodeURIComponent(id)}/sync-status`
  );
}

/** Stable id for download/delete routes (`id` or `sap_document_id`). */
export function procurementAttachmentRef(
  a: ProcurementTicket["attachments"][number]
): string {
  return (a.id || a.sap_document_id || "").trim();
}

export function procurementAttachmentRowUploading(
  a: ProcurementTicket["attachments"][number]
): boolean {
  if (a.sap_document_id) return false;
  if (a.can_retry_upload) return true;
  return !a.sap_sync_error;
}

export function procurementAttachmentStorage(
  a: ProcurementTicket["attachments"][number]
): "local" | "sap" {
  if (a.storage === "sap" || a.storage === "local") return a.storage;
  return a.sap_document_id ? "sap" : "local";
}

export function procurementAttachmentSapStatus(
  a: ProcurementTicket["attachments"][number]
): "sap" | "pending" | "failed" {
  if (a.sap_document_id) return "sap";
  if (a.sap_sync_error && !a.can_retry_upload) return "failed";
  if (a.sap_sync_error && a.can_retry_upload) return "pending";
  return "pending";
}

/** True while staged attachments are still uploading to SAP (or retrying). */
export function ticketProcurementAttachmentSyncInFlight(t: ProcurementTicket): boolean {
  const sapId = (t.sap_id ?? "").trim();
  if (!sapId) return false;
  const rows = t.attachments ?? [];
  if (rows.some(procurementAttachmentRowUploading)) return true;
  const sync = t.sap_sync ?? {};
  if (sync.attachments_pending) {
    return rows.some(procurementAttachmentRowUploading);
  }
  return false;
}

/**
 * True while SAP document create/resubmit may still be running.
 * Does not block on attachment-only background work once ``sap_id`` exists.
 */
export function ticketProcurementSapFormSyncInFlight(
  t: ProcurementTicket,
  sapMaxAttempts = 6
): boolean {
  const sync = t.sap_sync ?? {};
  const lastErr = (sync.last_error ?? "").trim();
  const sapId = (t.sap_id ?? "").trim();
  const attempts = sync.attempt_count ?? 0;
  const exhausted = attempts >= sapMaxAttempts;

  if (!sapId && !exhausted && !lastErr) return true;
  if (!sapId && attempts > 0 && !exhausted) return true;

  if (sync.sync_pending && !lastErr && sapId) {
    return !ticketProcurementAttachmentSyncInFlight(t);
  }

  return false;
}

/** True while any background SAP work (form or attachments) may still be running. */
export function ticketProcurementSyncInFlight(
  t: ProcurementTicket,
  sapMaxAttempts = 6
): boolean {
  return (
    ticketProcurementSapFormSyncInFlight(t, sapMaxAttempts) ||
    ticketProcurementAttachmentSyncInFlight(t)
  );
}

/** Download one PDF from SAP (or staging while upload is pending). */
export async function downloadProcurementTicketAttachment(
  ticketId: string,
  attachmentId: string,
  filename: string
): Promise<void> {
  const res = await apiFetch(
    `/api/procurement/tickets/${encodeURIComponent(ticketId)}/attachments/${encodeURIComponent(attachmentId)}/download`
  );
  if (!res.ok) {
    throw new Error(await messageFromFailedResponse(res, "Download failed."));
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename.trim() || "download";
    a.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function prefillPo(prId: string): Promise<{
  form: ProcurementTicket["form"];
  document_type: string;
  form_source?: "sap" | "db";
  sap_prefill_fallback?: boolean;
  sap_prefill_message?: string;
}> {
  const res = await apiFetch(`/api/procurement/tickets/${encodeURIComponent(prId)}/prefill-po`);
  await requireOk(res, "Could not load PR for PO draft.");
  return res.json() as Promise<{
    form: ProcurementTicket["form"];
    document_type: string;
    form_source?: "sap" | "db";
    sap_prefill_fallback?: boolean;
    sap_prefill_message?: string;
  }>;
}

export async function createPr(payload: {
  document_type: string;
  form: ProcurementTicket["form"];
  files: File[];
}): Promise<ProcurementTicket> {
  return postMultipartTicket(
    "/api/procurement/tickets/pr",
    { document_type: payload.document_type, form: payload.form },
    payload.files,
    "Could not create purchase requisition."
  );
}

export async function createPo(payload: {
  /** When set, lines are copied from this PR after server checks (SAP id, ownership). */
  parent_pr_id?: string | null;
  document_type: string;
  form: ProcurementTicket["form"];
  files: File[];
}): Promise<ProcurementTicket> {
  const body: Record<string, unknown> = {
    document_type: payload.document_type,
    form: payload.form,
  };
  const parent = payload.parent_pr_id;
  if (parent != null && String(parent).trim() !== "") {
    body.parent_pr_id = String(parent).trim();
  }
  return postMultipartTicket("/api/procurement/tickets/po", body, payload.files, "Could not create purchase order.");
}

export async function patchTicket(
  id: string,
  body: { form?: ProcurementTicket["form"]; version: number; resync_sap?: boolean; files?: File[] }
): Promise<ProcurementTicket> {
  const files = (body.files ?? []).filter(Boolean);
  if (files.length > 0) {
    const fd = new FormData();
    fd.append("version", String(body.version));
    fd.append(
      "payload",
      JSON.stringify({
        form: body.form,
        resync_sap: body.resync_sap ?? false,
      })
    );
    appendMultipartFiles(fd, files);
    const res = await apiFetch(`/api/procurement/tickets/${id}`, { method: "PATCH", body: fd });
    await requireOk(res, "Could not save this ticket.");
    try {
      return (await res.json()) as ProcurementTicket;
    } catch {
      throw new Error("The server returned an unreadable response. Try again or contact support.");
    }
  }
  return apiJson<ProcurementTicket>(`/api/procurement/tickets/${id}`, {
    method: "PATCH",
    body: JSON.stringify({
      form: body.form,
      version: body.version,
      resync_sap: body.resync_sap ?? false,
    }),
  });
}

export async function deleteProcurementTicketAttachment(
  ticketId: string,
  attachmentId: string
): Promise<ProcurementTicket> {
  return apiJson<ProcurementTicket>(
    `/api/procurement/tickets/${encodeURIComponent(ticketId)}/attachments/${encodeURIComponent(attachmentId)}`,
    { method: "DELETE" }
  );
}

export async function retryProcurementTicketAttachment(
  ticketId: string,
  attachmentId: string
): Promise<ProcurementTicket> {
  return apiJson<ProcurementTicket>(
    `/api/procurement/tickets/${encodeURIComponent(ticketId)}/attachments/${encodeURIComponent(attachmentId)}/retry`,
    { method: "POST" }
  );
}

export function attachmentDownloadUrl(ticketId: string, attachmentId: string): string {
  return `/api/procurement/tickets/${ticketId}/attachments/${encodeURIComponent(attachmentId)}/download`;
}

/** Friendlier copy for common ticket save / load failures (still shows server detail when unmatched). */
export function humanizeProcurementEditorError(message: string): string {
  const m = message.trim();
  if (/version conflict/i.test(m) || /\b409\b/.test(m)) {
    return "This ticket was updated elsewhere. Refresh the page to load the latest version, then try again.";
  }
  if (/not authenticated|invalid or expired token|session expired|401/i.test(m)) {
    return "Your session expired. Sign in again, then return to this ticket.";
  }
  if (/not found|404/i.test(m)) {
    return "This ticket no longer exists or you do not have access.";
  }
  if (/failed to fetch|networkerror|load failed|network request failed|ecconn|enotfound/i.test(m)) {
    return "We could not reach the server. Check your connection, VPN, or try again in a moment.";
  }
  return m;
}

/** Map unknown thrown values (often ``Error`` from ``apiJson``) through ``humanizeProcurementEditorError``. */
export function humanizeProcurementUnknownError(e: unknown, fallback = "Something went wrong."): string {
  return humanizeProcurementEditorError(e instanceof Error ? e.message : fallback);
}
