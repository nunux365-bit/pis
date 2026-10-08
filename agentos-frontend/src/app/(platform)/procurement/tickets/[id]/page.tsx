"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ErrorBanner } from "@/components/ErrorBanner";
import { ProcurementBlocksForm } from "@/components/procurement/ProcurementBlocksForm";
import { ProcurementBackLink, ProcurementFormDock, btnGhost, btnPrimary } from "@/components/procurement/ProcurementChrome";
import { TicketLinksDetailPanel } from "@/components/procurement/TicketLinkSummary";
import {
  downloadProcurementTicketAttachment,
  getProcurementSchema,
  deleteProcurementTicketAttachment,
  retryProcurementTicketAttachment,
  getTicket,
  getTicketSyncStatus,
  getTicketAudit,
  humanizeProcurementUnknownError,
  patchTicket,
  procurementAttachmentRef,
  procurementAttachmentSapStatus,
  procurementAttachmentStorage,
  ticketProcurementAttachmentSyncInFlight,
  ticketProcurementSyncInFlight,
  ticketProcurementSapFormSyncInFlight,
  type ProcurementAuditEntry,
  type ProcurementTicket,
  type SchemaResponse,
} from "@/lib/procurementApi";
import { ProcurementFilePicker } from "@/components/procurement/ProcurementFilePicker";
import { ticketSapListStatus } from "@/lib/procurementTicketUi";

const SAP_STATUS_POLL_MS = 2500;
const SAP_STATUS_POLL_MAX_MS = 120_000;
const ATTACHMENT_STATUS_POLL_MAX_MS = 1_800_000;
import { focusFirstProcurementClientIssue, hydrateProcurementFormForEditor, procurementPoHeaderFields } from "@/lib/procurementEditorShared";
import { validateProcurementForm, type ClientFormIssue } from "@/lib/procurementFormValidation";
function formatUpdatedHuman(iso: string): string {
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} hr ago`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
    return d.toLocaleDateString(undefined, { dateStyle: "medium" });
  } catch {
    return iso;
  }
}

function friendlyKind(kind: string): string {
  return kind === "PR" ? "Purchase request" : kind === "PO" ? "Purchase order" : kind;
}

function friendlyAuditAction(action: string): string {
  if (action === "procurement.ticket.create") return "Ticket created";
  if (action === "procurement.ticket.update") return "Ticket updated";
  if (action === "procurement.ticket.attachment.remove") return "Attachment removed";
  return action;
}

function humanAuditDetailLines(a: ProcurementAuditEntry): string[] {
  const d = a.details;
  if (!d || typeof d !== "object") return [];
  const o = d as Record<string, unknown>;
  const lines: string[] = [];
  if (a.action === "procurement.ticket.create") {
    if (typeof o.kind === "string" && o.kind) lines.push(`Document kind: ${o.kind}`);
    if (typeof o.document_type === "string" && o.document_type) lines.push(`Workflow: ${o.document_type}`);
    const ac = o.attachment_count;
    if (typeof ac === "number" && ac > 0) lines.push(`${ac} attachment(s) uploaded`);
    if (o.parent_pr_id) lines.push("Linked to a purchase request");
    if (typeof o.sap_id === "string" && o.sap_id) lines.push(`SAP document: ${o.sap_id}`);
  } else if (a.action === "procurement.ticket.update") {
    if (typeof o.version === "number") lines.push(`Saved as version ${o.version}`);
    if (o.resync_sap === true) lines.push("Resubmit to SAP was requested");
    if (o.had_new_attachments === true) lines.push("New file(s) attached");
    if (typeof o.sap_id === "string" && o.sap_id) lines.push(`SAP document: ${o.sap_id}`);
  }
  return lines;
}

export default function TicketDetailPage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const openedAfterSubmit = searchParams.get("syncing") === "1";
  const id = typeof params.id === "string" ? params.id : Array.isArray(params.id) ? params.id[0] : undefined;
  const [ticket, setTicket] = useState<ProcurementTicket | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [form, setForm] = useState<ProcurementTicket["form"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [audit, setAudit] = useState<ProcurementAuditEntry[]>([]);
  const [attachmentErr, setAttachmentErr] = useState<string | null>(null);
  const [deletingFileId, setDeletingFileId] = useState<string | null>(null);
  const [retryingFileId, setRetryingFileId] = useState<string | null>(null);
  const [syncPollTimedOut, setSyncPollTimedOut] = useState(false);

  function attachmentStatusLabel(a: ProcurementTicket["attachments"][number]): string {
    if (procurementAttachmentStorage(a) === "sap") return "In SAP";
    const st = procurementAttachmentSapStatus(a);
    if (st === "failed") return "Local — upload failed";
    if (a.can_retry_upload && a.sap_sync_error) return "Local — retrying upload";
    return "Local — not in SAP yet";
  }
  const [clientIssues, setClientIssues] = useState<ClientFormIssue[]>([]);
  /** True after AgentOS-only save on a ticket that already has a SAP document id. */
  const [changesNotInSap, setChangesNotInSap] = useState(false);
  const [sapMaxAttempts, setSapMaxAttempts] = useState(6);
  const [sapSyncPolling, setSapSyncPolling] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollStartedRef = useRef(0);
  const pollCancelledRef = useRef(false);
  /** JSON snapshot of the form after load or successful save — drives Save / Resubmit enablement. */
  const formBaselineRef = useRef<string | null>(null);
  const formEditorRef = useRef<ProcurementTicket["form"] | null>(null);
  const ticketRef = useRef<ProcurementTicket | null>(null);
  formEditorRef.current = form;
  ticketRef.current = ticket;

  const applyLoadedTicket = useCallback((t: ProcurementTicket) => {
    const hydrated = hydrateProcurementFormForEditor(t.form, {
      kind: t.kind,
      documentType: t.document_type,
      formSource: t.form_source,
    });
    setForm(hydrated);
    formBaselineRef.current = JSON.stringify(hydrated);
    setChangesNotInSap(false);
  }, []);

  const load = useCallback(async () => {
    if (!id) return;
    setLoadErr(null);
    try {
      const [t, s] = await Promise.all([getTicket(id), getProcurementSchema()]);
      setTicket(t);
      setSchema(s);
      const cap = s.sap_max_attempts;
      if (typeof cap === "number" && cap >= 1) setSapMaxAttempts(cap);
      applyLoadedTicket(t);
      setClientIssues([]);
      try {
        setAudit(await getTicketAudit(id));
      } catch {
        setAudit([]);
      }
    } catch (e) {
      setLoadErr(humanizeProcurementUnknownError(e, "Could not load this ticket."));
      setTicket(null);
      setSchema(null);
      setForm(null);
      setAudit([]);
      setChangesNotInSap(false);
    }
  }, [id, applyLoadedTicket]);

  useEffect(() => {
    load();
  }, [load]);

  const stopSapPoll = useCallback((timedOut = false) => {
    pollCancelledRef.current = true;
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setSapSyncPolling(false);
    if (timedOut) setSyncPollTimedOut(true);
  }, []);

  useEffect(() => () => stopSapPoll(), [stopSapPoll]);

  const refreshTicketSapStatus = useCallback(async () => {
    if (!id) return;
    const current = ticketRef.current;
    const lightweight =
      Boolean((current?.sap_id || "").trim()) &&
      current !== null &&
      !ticketProcurementSapFormSyncInFlight(current, sapMaxAttempts) &&
      ticketProcurementAttachmentSyncInFlight(current);

    const fetched = lightweight ? await getTicketSyncStatus(id) : await getTicket(id);
    const t: ProcurementTicket = lightweight && current
      ? { ...current, ...fetched }
      : (fetched as ProcurementTicket);
    const docSyncComplete =
      Boolean((t.sap_id || "").trim()) && !ticketProcurementSapFormSyncInFlight(t, sapMaxAttempts);
    setTicket((prev) => {
      if (!prev) return t;
      const hydratedForm =
        docSyncComplete && !lightweight && t.form
          ? hydrateProcurementFormForEditor(t.form, {
              kind: t.kind === "PR" ? "PR" : "PO",
              documentType: t.document_type,
              formSource: t.form_source,
            })
          : prev.form;
      return {
        ...prev,
        sap_id: t.sap_id,
        sap_sync: t.sap_sync,
        attachments: t.attachments,
        version: t.version,
        updated_at: t.updated_at,
        ...(lightweight
          ? { sap_attachment_hydrate_error: t.sap_attachment_hydrate_error }
          : {
              form_source: t.form_source,
              sap_form_read_error: t.sap_form_read_error,
              sap_no_active_lines: t.sap_no_active_lines,
              sap_attachment_hydrate_error: t.sap_attachment_hydrate_error,
            }),
        ...(docSyncComplete && !lightweight ? { form: hydratedForm } : {}),
      };
    });
    if (
      !lightweight &&
      docSyncComplete &&
      t.form &&
      (t.sap_no_active_lines || t.form_source === "sap")
    ) {
      const baseline = formBaselineRef.current;
      const editorDirty =
        formEditorRef.current !== null &&
        baseline !== null &&
        JSON.stringify(formEditorRef.current) !== baseline;
      if (!editorDirty) {
        const hydrated = hydrateProcurementFormForEditor(t.form, {
          kind: t.kind === "PR" ? "PR" : "PO",
          documentType: t.document_type,
          formSource: t.form_source,
        });
        setForm(hydrated);
        formBaselineRef.current = JSON.stringify(hydrated);
        setChangesNotInSap(false);
      }
    }
    return t;
  }, [id, sapMaxAttempts]);

  const sapSyncInFlight = ticket ? ticketProcurementSyncInFlight(ticket, sapMaxAttempts) : false;
  const sapFormSyncInFlight = ticket ? ticketProcurementSapFormSyncInFlight(ticket, sapMaxAttempts) : false;
  const attachmentSyncInFlight = ticket ? ticketProcurementAttachmentSyncInFlight(ticket) : false;
  const sapReady = Boolean((ticket?.sap_id || "").trim());
  const sapSyncLastError = (ticket?.sap_sync?.last_error || "").trim();
  const failedAttachments = (ticket?.attachments ?? []).filter((a) => procurementAttachmentSapStatus(a) === "failed");
  const pollMaxMs =
    sapFormSyncInFlight || !sapReady ? SAP_STATUS_POLL_MAX_MS : ATTACHMENT_STATUS_POLL_MAX_MS;
  const showSapDocSyncBanner =
    (sapSyncPolling || (openedAfterSubmit && !sapReady && !loadErr)) &&
    sapFormSyncInFlight &&
    !sapSyncLastError;
  const showAttachmentSyncBanner =
    sapReady && attachmentSyncInFlight && !sapFormSyncInFlight;

  useEffect(() => {
    if (!sapSyncInFlight) setSyncPollTimedOut(false);
  }, [sapSyncInFlight]);

  useEffect(() => {
    if (!id || !openedAfterSubmit || sapSyncInFlight) return;
    router.replace(`/procurement/tickets/${id}`);
  }, [id, openedAfterSubmit, sapSyncInFlight, router]);

  useEffect(() => {
    if (!id || busy || !sapSyncInFlight) {
      stopSapPoll();
      return;
    }
    pollCancelledRef.current = false;
    pollStartedRef.current = Date.now();
    setSapSyncPolling(true);

    const tick = async () => {
      if (pollCancelledRef.current) return;
      if (Date.now() - pollStartedRef.current > pollMaxMs) {
        stopSapPoll(true);
        return;
      }
      try {
        const t = await refreshTicketSapStatus();
        if (pollCancelledRef.current || !t) return;
        if (!ticketProcurementSyncInFlight(t, sapMaxAttempts)) stopSapPoll();
      } catch {
        /* keep polling until timeout */
      }
    };

    void tick();
    pollRef.current = setInterval(() => void tick(), SAP_STATUS_POLL_MS);
    return () => stopSapPoll();
  }, [id, busy, sapSyncInFlight, sapMaxAttempts, pollMaxMs, refreshTicketSapStatus, stopSapPoll]);

  const active = schema?.document_types.find((d) => d.code === ticket?.document_type);
  const formSchemaSlice = useMemo(() => {
    if (!active) return null;
    if (ticket?.kind === "PO") {
      return { ...active, header_fields: procurementPoHeaderFields(active) };
    }
    return active;
  }, [active, ticket?.kind]);

  async function save(resync: boolean) {
    if (!ticket || !form || !formSchemaSlice) return;
    if (ticket.sap_no_active_lines) return;
    setErr(null);
    const local = validateProcurementForm(ticket.kind === "PR" ? "PR" : "PO", form, formSchemaSlice);
    if (local.length > 0) {
      setClientIssues(local);
      focusFirstProcurementClientIssue(ticket.kind === "PR" ? "PR" : "PO", ticket.document_type, local);
      return;
    }
    setClientIssues([]);
    setBusy(true);
    try {
      const staged = files.length ? [...files] : [];
      const t = await patchTicket(ticket.id, {
        form,
        version: ticket.version,
        resync_sap: resync,
        files: staged.length ? staged : undefined,
      });
      const latest = t;
      const syncPending =
        Boolean(t.sap_sync?.sync_pending) || Boolean(t.sap_sync?.attachments_pending);
      const sapErr = (t.sap_sync?.last_error || "").trim();
      setTicket(t);
      if (resync) {
        const hydrated = hydrateProcurementFormForEditor(t.form, {
          kind: t.kind,
          documentType: t.document_type,
          formSource: t.form_source,
        });
        setForm(hydrated);
        formBaselineRef.current = JSON.stringify(hydrated);
        if (!sapErr) setChangesNotInSap(false);
      } else if ((t.sap_id || "").trim()) {
        /* Keep in-memory form — PATCH response is SAP-hydrated and would revert edits. */
        formBaselineRef.current = JSON.stringify(form);
        setChangesNotInSap(true);
      } else {
        formBaselineRef.current = JSON.stringify(form);
        setChangesNotInSap(false);
      }
      if (staged.length) setFiles([]);
      try {
        setAudit(await getTicketAudit(latest.id));
      } catch {
        /* ignore */
      }
      if (sapErr) {
        setErr(
          resync
            ? `Saved in AgentOS, but SAP resubmit failed: ${sapErr}`
            : `Saved in AgentOS, but SAP sync failed: ${sapErr}`
        );
      } else if (!syncPending) {
        setErr(null);
      }
    } catch (e) {
      const raw = e instanceof Error ? e.message : "Save failed";
      const conflict = /409|version conflict/i.test(raw);
      if (conflict) {
        await load();
        setErr(
          "Latest version loaded. Another save won this round — reapply any changes you still need, then save again."
        );
      } else {
        setErr(humanizeProcurementUnknownError(e, "Save failed"));
      }
    } finally {
      setBusy(false);
    }
  }

  async function downloadFile(driveFileId: string, name: string) {
    if (!id) return;
    setAttachmentErr(null);
    try {
      await downloadProcurementTicketAttachment(id, driveFileId, name || "download");
    } catch (e) {
      setAttachmentErr(humanizeProcurementUnknownError(e, "Could not download the file."));
    }
  }

  async function retryAttachmentUpload(attachmentId: string) {
    if (!id || !ticket) return;
    setAttachmentErr(null);
    setRetryingFileId(attachmentId);
    try {
      const t = await retryProcurementTicketAttachment(id, attachmentId);
      setTicket(t);
      try {
        setAudit(await getTicketAudit(id));
      } catch {
        /* ignore */
      }
    } catch (e) {
      setAttachmentErr(humanizeProcurementUnknownError(e, "Could not retry the upload."));
    } finally {
      setRetryingFileId(null);
    }
  }

  async function removeAttachment(attachmentId: string) {
    if (!id || !ticket) return;
    setAttachmentErr(null);
    setDeletingFileId(attachmentId);
    try {
      const t = await deleteProcurementTicketAttachment(id, attachmentId);
      setTicket(t);
      /* Keep in-memory form (may have unsaved edits); only ticket.version + attachments change. */
      try {
        setAudit(await getTicketAudit(id));
      } catch {
        /* ignore */
      }
    } catch (e) {
      setAttachmentErr(humanizeProcurementUnknownError(e, "Could not remove the file."));
    } finally {
      setDeletingFileId(null);
    }
  }

  if (!id) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <ProcurementBackLink href="/procurement/tickets" dense>
          Your tickets
        </ProcurementBackLink>
        <ErrorBanner>Open a ticket from your list — this link is missing a ticket id.</ErrorBanner>
      </div>
    );
  }

  if (loadErr) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <ProcurementBackLink href="/procurement/tickets" dense>
          Your tickets
        </ProcurementBackLink>
        <ErrorBanner onRetry={() => load()}>{loadErr}</ErrorBanner>
      </div>
    );
  }

  if (ticket === null || form === null || schema === null) {
    return (
      <div className="mx-auto max-w-6xl space-y-3" aria-busy="true" aria-label="Loading ticket">
        <div className="h-5 w-36 animate-pulse rounded-lg bg-[var(--bg-secondary)]" />
        <div className="h-24 animate-pulse rounded-xl bg-[var(--bg-secondary)]" />
        <div className="grid gap-3 lg:grid-cols-12">
          <div className="h-56 animate-pulse rounded-xl bg-[var(--bg-secondary)] lg:col-span-3" />
          <div className="h-72 animate-pulse rounded-xl bg-[var(--bg-secondary)] lg:col-span-9" />
        </div>
      </div>
    );
  }

  if (!active) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <ProcurementBackLink href="/procurement/tickets" dense>
          Your tickets
        </ProcurementBackLink>
        <ErrorBanner onRetry={() => load()}>
          This ticket uses workflow type{" "}
          <span className="font-mono font-semibold">{ticket.document_type}</span>, which is not in the form schema
          returned by the server. You cannot edit it here until configuration is fixed. Try reload, or contact an
          administrator.
        </ErrorBanner>
      </div>
    );
  }

  const sapLastError = (ticket.sap_sync?.last_error || "").trim();
  const sapAttempts = ticket.sap_sync?.attempt_count ?? 0;
  const sapNextRetryAt = (ticket.sap_sync?.next_retry_at || "").trim();
  const sapNeedsAttention = sapLastError.length > 0;
  const sapPendingRetry =
    sapLastError.length > 0 &&
    Boolean(sapNextRetryAt) &&
    sapAttempts > 0 &&
    sapAttempts < sapMaxAttempts;
  const sapChip = ticketSapListStatus(ticket, sapMaxAttempts);

  const showPoContinue =
    ticket.kind === "PR" &&
    sapReady &&
    !ticket.sap_no_active_lines &&
    (ticket.linked_pos?.length ?? 0) === 0;
  const linkedPo = ticket.kind === "PR" ? ticket.linked_pos?.[0] : undefined;
  const showPoOpenExisting =
    ticket.kind === "PR" &&
    sapReady &&
    !ticket.sap_no_active_lines &&
    Boolean(linkedPo);
  const formFromSap = ticket.form_source === "sap";
  const sapFormReadWarn = (ticket.sap_form_read_error || "").trim();
  const sapNoActiveLines = Boolean(ticket.sap_no_active_lines);
  const formDirty =
    form !== null && formBaselineRef.current !== null && JSON.stringify(form) !== formBaselineRef.current;
  const hasPendingFiles = files.length > 0;
  const hasUnsavedWork = formDirty || hasPendingFiles;
  /** One action at a time: save locally first, then resubmit when in sync with AgentOS. */
  const canSaveLocally = !sapNoActiveLines && !sapFormSyncInFlight && hasUnsavedWork;
  const canResubmitToSap =
    !sapNoActiveLines &&
    !sapFormSyncInFlight &&
    !hasUnsavedWork &&
    (changesNotInSap || !sapReady || sapNeedsAttention);
  const saveDisabled = busy || !canSaveLocally;
  const resubmitDisabled = busy || !canResubmitToSap;

  return (
    <div className="mx-auto max-w-6xl space-y-3 pb-36 sm:pb-40">
      <ProcurementBackLink href="/procurement/tickets" dense>
        Your tickets
      </ProcurementBackLink>

      {formFromSap && sapReady && !changesNotInSap ? (
        <p className="rounded-lg border border-[var(--accent-green)]/30 bg-[rgba(21,163,115,0.08)] px-3 py-2 text-sm text-[var(--text-secondary)]">
          Line details are loaded from SAP document <span className="font-mono font-semibold">{ticket.sap_id}</span> (not
          the last saved copy in AgentOS). Save will store your edits here, then resubmit pushes to SAP.
        </p>
      ) : null}
      {changesNotInSap && sapReady ? (
        <p
          className="rounded-lg border border-[var(--accent-orange)]/35 bg-[rgba(245,158,11,0.1)] px-3 py-2 text-sm text-[var(--text-secondary)]"
          role="status"
          aria-live="polite"
        >
          Changes saved in AgentOS only — not yet in SAP document{" "}
          <span className="font-mono font-semibold">{ticket.sap_id}</span>. Press{" "}
          <strong className="font-semibold text-[var(--text-primary)]">Resubmit to SAP</strong> to apply.
        </p>
      ) : null}
      {sapNoActiveLines ? (
        <div
          className="rounded-lg border border-[rgba(217,79,69,0.35)] bg-[rgba(217,79,69,0.06)] px-3 py-2.5"
          role="alert"
        >
          <p className="text-sm font-bold text-[var(--text-primary)]">
            SAP document <span className="font-mono font-semibold">{ticket.sap_id}</span> has no active line items
          </p>
          <p className="mt-1.5 text-xs leading-relaxed text-[var(--text-secondary)]">
            Every line in SAP is deleted or inactive. The form below is empty — AgentOS may still store an older copy in
            the database, but it no longer matches SAP. You cannot save or resubmit this ticket; create a new purchase
            request instead.
          </p>
          {ticket.kind === "PR" ? (
            <Link
              href="/procurement/pr/new"
              className="mt-3 inline-flex min-h-[40px] items-center rounded-lg bg-app-gradient-1 px-4 text-sm font-bold text-white shadow-sm transition hover:opacity-[0.97]"
            >
              Create new purchase request →
            </Link>
          ) : null}
        </div>
      ) : sapFormReadWarn ? (
        <ErrorBanner>
          Could not refresh from SAP — showing last saved form. {sapFormReadWarn}
        </ErrorBanner>
      ) : null}
      {(ticket.sap_attachment_hydrate_error || "").trim() ? (
        <ErrorBanner>
          Could not refresh attachments from SAP — showing saved files only.{" "}
          {ticket.sap_attachment_hydrate_error}
        </ErrorBanner>
      ) : null}

      {showSapDocSyncBanner ? (
        <p
          className="rounded-lg border border-[var(--accent-blue)]/30 bg-[rgba(59,130,246,0.08)] px-3 py-2 text-sm text-[var(--text-secondary)]"
          role="status"
          aria-live="polite"
        >
          Saving to SAP… The document number will update here automatically (usually within a few seconds).
        </p>
      ) : null}

      {showAttachmentSyncBanner ? (
        <p
          className="rounded-lg border border-[var(--accent-orange)]/30 bg-[rgba(249,115,22,0.08)] px-3 py-2 text-sm text-[var(--text-secondary)]"
          role="status"
          aria-live="polite"
        >
          Uploading attachments to SAP… Status updates here automatically. The PR/PO is already in SAP — you can keep
          editing and save again without resubmitting the whole document.
          {ticket.sap_sync?.attachment_last_error
            ? ` Latest issue: ${ticket.sap_sync.attachment_last_error}`
            : null}
        </p>
      ) : null}

      {syncPollTimedOut && sapSyncInFlight ? (
        <p
          className="rounded-lg border border-[var(--accent-orange)]/35 bg-[rgba(245,158,11,0.1)] px-3 py-2 text-sm text-[var(--text-secondary)]"
          role="status"
          aria-live="polite"
        >
          Live status updates paused — background sync may still be running. Refresh this page or check file status in
          Files and uploads below.
        </p>
      ) : null}

      <section
        className={`relative overflow-hidden rounded-xl border bg-[var(--bg-card)] px-4 py-3.5 shadow-sm ring-1 ring-black/[0.04] sm:px-4 sm:py-3.5 ${
          showPoContinue || showPoOpenExisting
            ? "border-[var(--accent-green)]/40 ring-[var(--accent-green)]/15"
            : "border-[var(--border)]"
        }`}
      >
        <div
          className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-[rgba(21,163,115,0.06)] blur-xl"
          aria-hidden
        />
        <div className="relative flex flex-col gap-2.5 lg:flex-row lg:items-start lg:justify-between lg:gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="rounded-full bg-[rgba(21,163,115,0.14)] px-2.5 py-1 text-[11px] font-bold uppercase tracking-wide text-[var(--accent-green)]">
                {friendlyKind(ticket.kind)}
              </span>
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-elev)] px-2.5 py-1 text-[11px] font-semibold text-[var(--text-secondary)]">
                {ticket.document_type}
              </span>
            </div>
            <h1 className="mt-2 text-xl font-bold tracking-tight text-[var(--text-primary)] sm:text-2xl">
              {sapReady ? "Track this ticket" : "Finish or fix this ticket"}
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--text-secondary)]">
              Edit below, then save. Use <strong className="font-semibold text-[var(--text-primary)]">Resubmit to SAP</strong>{" "}
              to push your current form to SAP again for the same document id (SAP may accept only part of the payload).
            </p>
            <p className="mt-3 break-all font-mono text-xs leading-normal text-[var(--text-muted)]">
              <span className="font-sans font-semibold text-[var(--text-secondary)]">Reference id</span>
              <span className="mx-1.5 font-sans text-[var(--text-muted)]">·</span>
              {ticket.id}
            </p>
          </div>
          <div className="flex w-full shrink-0 flex-col sm:w-auto sm:min-w-[11rem] lg:max-w-[18rem]">
            <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]/90 px-3 py-2.5 shadow-inner sm:text-right">
              <div className="flex flex-wrap items-center justify-between gap-2 sm:justify-end">
                <p className="text-[11px] font-bold uppercase tracking-wide text-[var(--text-muted)]">SAP document</p>
                <span className={`inline-flex rounded-full px-2.5 py-0.5 text-[10px] font-bold ${sapChip.tone}`}>
                  {sapChip.label}
                </span>
              </div>
              <p className="mt-1 break-all font-mono text-sm font-bold leading-snug text-[var(--text-primary)] sm:text-base">
                {ticket.sap_id ?? "—"}
              </p>
              <p className="mt-1.5 text-xs text-[var(--text-muted)]">Updated {formatUpdatedHuman(ticket.updated_at)}</p>
              {sapNeedsAttention ? (
                <p className="mt-2 text-xs font-semibold text-[var(--accent-red)]">
                  {sapPendingRetry ? "Retry scheduled" : "See error below"}
                </p>
              ) : sapSyncInFlight ? (
                <p className="mt-2 text-xs font-semibold text-[var(--accent-blue)]">Sync in progress…</p>
              ) : null}
            </div>
          </div>
        </div>

        <TicketLinksDetailPanel ticket={ticket} />

        {sapNeedsAttention ? (
          <div
            className="relative mt-3 rounded-lg border border-[rgba(217,79,69,0.35)] bg-[rgba(217,79,69,0.06)] px-3 py-2.5"
            role="alert"
          >
            <p className="text-sm font-bold text-[var(--text-primary)]">Saved in AgentOS — SAP did not accept the document</p>
            <p className="mt-1.5 text-xs leading-relaxed text-[var(--text-secondary)]">
              Your form is stored. Fix the issue below, then use <strong className="font-semibold">Resubmit to SAP</strong>{" "}
              {sapReady ? "for the same PR number." : "once you have a SAP document id, or after fixing validation errors."}
            </p>
            {sapAttempts > 0 ? (
              <p className="mt-1.5 text-xs text-[var(--text-muted)]">
                Failed SAP attempts: <span className="font-mono font-semibold">{sapAttempts}</span>
                {sapPendingRetry ? (
                  <>
                    {" "}
                    · next retry{" "}
                    <span className="font-mono font-semibold">{formatUpdatedHuman(sapNextRetryAt)}</span>
                  </>
                ) : null}
              </p>
            ) : null}
            <p className="mt-2 break-words font-mono text-xs leading-relaxed text-[var(--accent-red)]">{sapLastError}</p>
          </div>
        ) : null}

        {showPoContinue ? (
          <div className="relative mt-3 flex flex-col gap-2 border-t border-[var(--border)]/80 pt-3 sm:flex-row sm:items-center sm:justify-between sm:gap-3 sm:pt-3">
            <div className="min-w-0 flex-1 rounded-lg bg-[rgba(21,163,115,0.07)] px-3 py-2 sm:px-3 sm:py-2">
              <p className="text-sm font-bold text-[var(--text-primary)]">Continue to purchase order</p>
              <p className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                Lines copy forward — you confirm quantities and vendor on the PO screen.
              </p>
            </div>
            <Link
              href={`/procurement/po/new?parent=${ticket.id}`}
              className="inline-flex min-h-[44px] shrink-0 items-center justify-center self-stretch rounded-lg bg-app-gradient-1 px-5 text-sm font-bold text-white shadow-sm transition hover:opacity-[0.97] focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/45 sm:self-center sm:min-h-0 sm:px-6"
            >
              Open PO screen →
            </Link>
          </div>
        ) : null}
        {showPoOpenExisting && linkedPo ? (
          <div className="relative mt-3 flex flex-col gap-2 border-t border-[var(--border)]/80 pt-3 sm:flex-row sm:items-center sm:justify-between sm:gap-3 sm:pt-3">
            <div className="min-w-0 flex-1 rounded-lg bg-[rgba(21,163,115,0.07)] px-3 py-2 sm:px-3 sm:py-2">
              <p className="text-sm font-bold text-[var(--text-primary)]">Purchase order already created</p>
              <p className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                Open the linked order to update it. If that order can no longer be changed in SAP, raise a new purchase
                request instead of creating another order from this one.
              </p>
            </div>
            <Link
              href={`/procurement/tickets/${linkedPo.id}`}
              className="inline-flex min-h-[44px] shrink-0 items-center justify-center self-stretch rounded-lg bg-app-gradient-1 px-5 text-sm font-bold text-white shadow-sm transition hover:opacity-[0.97] focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/45 sm:self-center sm:min-h-0 sm:px-6"
            >
              Open order {linkedPo.sap_id?.trim() || "→"}
            </Link>
          </div>
        ) : null}
      </section>

      {err ? (
        <ErrorBanner
          onRetry={() => {
            setErr(null);
          }}
        >
          {err}
        </ErrorBanner>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-12 lg:items-start">
        <section
          className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 shadow-sm ring-1 ring-black/[0.02] lg:col-span-3"
          aria-labelledby="ticket-activity-title"
        >
          <h2 id="ticket-activity-title" className="text-xs font-bold text-[var(--text-primary)] sm:text-sm">
            What happened
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-[var(--text-muted)]">Saves, uploads, SAP — newest first.</p>
          {audit.length === 0 ? (
            <p className="mt-4 text-xs text-[var(--text-muted)]">No activity recorded yet.</p>
          ) : (
            <ul className="relative mt-4 space-y-0 border-l-2 border-[var(--border)] pl-4">
              {audit.map((a) => (
                <li key={a.id} className="relative pb-5 last:pb-0">
                  <span
                    className="absolute -left-[calc(1rem+5px)] top-1.5 h-2.5 w-2.5 rounded-full bg-[var(--accent-green)] ring-4 ring-[var(--bg-card)]"
                    aria-hidden
                  />
                  <p className="text-xs font-semibold text-[var(--text-primary)]">{friendlyAuditAction(a.action)}</p>
                  <p className="mt-0.5 text-xs text-[var(--text-muted)]">{formatUpdatedHuman(a.created_at)}</p>
                  {humanAuditDetailLines(a).map((line, li) => (
                    <p key={`${a.id}-d${li}`} className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                      {line}
                    </p>
                  ))}
                  {a.details && Object.keys(a.details).length > 0 ? (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-xs font-semibold text-[var(--accent-blue)] hover:underline">
                        Technical details
                      </summary>
                      <pre className="mt-1 max-h-40 overflow-auto rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-2 font-mono text-xs leading-snug text-[var(--text-muted)]">
                        {JSON.stringify(a.details, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="min-w-0 space-y-4 lg:col-span-9">
          {formSchemaSlice?.block_fields?.length ? (
            <ProcurementBlocksForm
              kind={ticket.kind}
              documentType={ticket.document_type}
              active={{
                code: formSchemaSlice.code,
                header_fields: formSchemaSlice.header_fields,
                block_fields: formSchemaSlice.block_fields,
                allocation_fields: formSchemaSlice.allocation_fields ?? [],
              }}
              form={form}
              setForm={setForm as Dispatch<SetStateAction<ProcurementTicket["form"]>>}
              clientIssues={clientIssues}
              density="compact"
              linkedToPr={ticket.kind === "PO" && Boolean(ticket.parent_pr_id)}
              formLocked={sapNoActiveLines}
            />
          ) : (
            <ErrorBanner>
              This workflow has no block-based form in the schema. Fix server configuration or contact support.
            </ErrorBanner>
          )}

          <details className="group rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-bold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden hover:bg-[var(--bg-elev)]/50">
              <span>
                Files and uploads <span className="font-normal text-[var(--text-muted)]">(optional)</span>
              </span>
              <span className="text-xs font-semibold text-[var(--accent-blue)] group-open:hidden">Show</span>
              <span className="hidden text-xs font-semibold text-[var(--accent-blue)] group-open:inline">Hide</span>
            </summary>
            <div className="border-t border-[var(--border)] px-4 py-3">
              <label htmlFor="ticket-detail-file-upload" className="sr-only">
                Add files to this ticket
              </label>
              {attachmentErr ? (
                <div className="mb-3">
                  <ErrorBanner
                    onRetry={() => {
                      setAttachmentErr(null);
                    }}
                    retryLabel="Dismiss"
                  >
                    {attachmentErr}
                  </ErrorBanner>
                </div>
              ) : null}
              <p className="mb-3 text-xs leading-relaxed text-[var(--text-muted)]">
                PDF, JPG, PNG, XLS, or XLSX — up to 10 files, 25 MB each. Files upload to SAP after the document
                exists. <strong className="font-semibold text-[var(--text-secondary)]">Local</strong> files can be
                removed here; <strong className="font-semibold text-[var(--text-secondary)]">In SAP</strong> files
                cannot be deleted until SAP enables attachment delete.
              </p>
              {failedAttachments.length > 0 ? (
                <p className="mb-3 rounded-lg border border-[var(--accent-red)]/25 bg-[rgba(239,68,68,0.06)] px-3 py-2 text-xs leading-relaxed text-[var(--text-secondary)]">
                  <strong className="font-semibold text-[var(--text-primary)]">
                    {failedAttachments.length === 1 ? "1 file" : `${failedAttachments.length} files`} did not reach
                    SAP.
                  </strong>{" "}
                  This {ticket.kind === "PR" ? "purchase request" : "purchase order"} is already in SAP. Use{" "}
                  <strong className="font-semibold">Retry upload</strong> when available, or remove the file, attach it
                  again, and save. You do not need to resubmit the whole document.
                </p>
              ) : null}
              {ticket.sap_sync?.attachments_pending && !showAttachmentSyncBanner ? (
                <p className="mb-2 text-xs font-semibold text-[var(--accent-orange)]">
                  Some attachments are still uploading to SAP
                  {ticket.sap_sync?.attachment_last_error
                    ? ` — ${ticket.sap_sync.attachment_last_error}`
                    : null}
                </p>
              ) : null}
              <ul className="space-y-2 text-sm">
                {(ticket.attachments || []).length === 0 ? (
                  <li className="text-xs text-[var(--text-muted)]">No files uploaded yet.</li>
                ) : (
                  (ticket.attachments || []).map((a, idx) => {
                    const ref = procurementAttachmentRef(a);
                    const status = procurementAttachmentSapStatus(a);
                    const failed = status === "failed";
                    const inSap = procurementAttachmentStorage(a) === "sap";
                    return (
                    <li
                      key={ref ? `${ref}-${idx}` : `att-${idx}-${a.name ?? "file"}`}
                      className="flex flex-wrap items-center justify-between gap-2"
                    >
                      <span className="min-w-0 text-[var(--text-primary)]">
                        {a.name}
                        <span
                          className={`ml-2 text-[10px] font-semibold ${
                            failed ? "text-[var(--accent-red)]" : "text-[var(--text-muted)]"
                          }`}
                        >
                          {attachmentStatusLabel(a)}
                          {failed && a.sap_sync_error ? ` — ${a.sap_sync_error}` : null}
                        </span>
                      </span>
                      {ref ? (
                        <span className="flex shrink-0 items-center gap-2">
                          {failed && a.can_retry_upload ? (
                            <button
                              type="button"
                              onClick={() => retryAttachmentUpload(ref)}
                              disabled={Boolean(busy || deletingFileId || retryingFileId)}
                              className="text-xs font-bold text-[var(--accent-green)] hover:underline disabled:opacity-40"
                            >
                              {retryingFileId === ref ? "Retrying…" : "Retry upload"}
                            </button>
                          ) : failed ? (
                            <span className="text-[10px] font-semibold text-[var(--text-muted)]">
                              Re-attach required
                            </span>
                          ) : null}
                          <button
                            type="button"
                            onClick={() => downloadFile(ref, a.name || "file")}
                            disabled={Boolean(deletingFileId || retryingFileId)}
                            className="text-xs font-bold text-[var(--accent-blue)] hover:underline disabled:opacity-40"
                          >
                            Download
                          </button>
                          {inSap ? (
                            <span
                              className="text-[10px] font-semibold text-[var(--text-muted)]"
                              title="SAP attachment delete is not available on this system yet."
                            >
                              Delete not available
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => removeAttachment(ref)}
                              disabled={Boolean(busy || deletingFileId || retryingFileId)}
                              className="text-xs font-bold text-[var(--accent-red)] hover:underline disabled:opacity-40"
                            >
                              {deletingFileId === ref ? "Removing…" : "Remove"}
                            </button>
                          )}
                        </span>
                      ) : null}
                    </li>
                  );
                  })
                )}
              </ul>
              <ProcurementFilePicker
                id="ticket-detail-file-upload"
                files={files}
                onChange={setFiles}
                disabled={sapNoActiveLines || busy}
                caption="These files will upload when you save."
                uploadLabel="Attach more files"
                inputClassName="mt-4 block w-full text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-[var(--accent-green)] file:px-4 file:py-2 file:text-xs file:font-bold file:text-white disabled:opacity-50"
              />
            </div>
          </details>
        </div>
      </div>

      <ProcurementFormDock
        hint={
          sapNoActiveLines ? (
            <>This ticket is read-only because SAP has no active lines. Create a new purchase request to continue.</>
          ) : (
            <>
              {hasUnsavedWork ? (
                <>
                  You have unsaved edits — use <strong className="font-semibold text-[var(--text-secondary)]">Save changes</strong>{" "}
                  first. Resubmit unlocks once your form matches what is stored in AgentOS.
                </>
              ) : (
                <>
                  <strong className="font-semibold text-[var(--text-secondary)]">Save changes</strong> stores edits in
                  AgentOS only.{" "}
                  <strong className="font-semibold text-[var(--text-secondary)]">Resubmit to SAP</strong> saves to
                  AgentOS <em>and</em> sends the current stored form to SAP.
                </>
              )}
            </>
          )
        }
      >
        <button
          type="button"
          disabled={saveDisabled}
          onClick={() => save(false)}
          className={btnGhost}
          title={
            saveDisabled && !busy && !sapNoActiveLines
              ? hasUnsavedWork
                ? undefined
                : "No unsaved edits — change the form or add files first"
              : undefined
          }
        >
          Save changes
        </button>
        <button
          type="button"
          disabled={resubmitDisabled}
          onClick={() => save(true)}
          className={btnPrimary}
          title={
            resubmitDisabled && !busy && !sapNoActiveLines
              ? hasUnsavedWork
                ? "Save your edits in AgentOS first, then resubmit to SAP"
                : sapReady && !changesNotInSap && !sapNeedsAttention
                  ? "No changes to send — edit the form and save first"
                  : undefined
              : undefined
          }
        >
          Resubmit to SAP
        </button>
      </ProcurementFormDock>
    </div>
  );
}
