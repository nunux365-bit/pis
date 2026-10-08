"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorBanner } from "@/components/ErrorBanner";
import { ProcurementBlocksForm } from "@/components/procurement/ProcurementBlocksForm";
import { LinkedPrRequestFiles } from "@/components/procurement/LinkedPrRequestFiles";
import { ParentPrPicker } from "@/components/procurement/ParentPrPicker";
import {
  ProcurementBackLink,
  ProcurementButtonCancel,
  ProcurementButtonSubmit,
  ProcurementFlowSteps,
  ProcurementFormDock,
} from "@/components/procurement/ProcurementChrome";
import {
  createPo,
  getProcurementSchema,
  getTicket,
  humanizeProcurementUnknownError,
  listParentPRs,
  prefillPo,
  ProcurementLinkedPoConflictError,
  type LinkedTicketSummary,
  type ProcurementTicket,
  type SchemaResponse,
} from "@/lib/procurementApi";
import { ProcurementFilePicker } from "@/components/procurement/ProcurementFilePicker";
import {
  applyPoRequestorEmailDefault,
  emptyTicketForm,
  focusFirstProcurementClientIssue,
  hydrateProcurementFormForEditor,
  parentsListLoadErrorMessage,
  procurementPoHeaderFields,
  procurementSchemaTypesKey,
  schemaLoadErrorMessage,
} from "@/lib/procurementEditorShared";
import { useAuth } from "@/contexts/AuthContext";
import { validateProcurementForm, type ClientFormIssue } from "@/lib/procurementFormValidation";
import { mergePoDraft, persistPoDraft, clearPoDraft } from "@/lib/lastPoFormStorage";
import { PROCUREMENT_HOME_HREF } from "@/lib/procurementRoutes";

const LAST_PO_DOC_KEY = "agentos_procurement_last_po_doc_type";

export default function NewPoPage() {
  const router = useRouter();
  const params = useSearchParams();
  const parentFromUrl = params.get("parent");
  const { user } = useAuth();

  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [parents, setParents] = useState<ProcurementTicket[]>([]);
  const [parentId, setParentId] = useState<string | null>(parentFromUrl);
  const [documentType, setDocumentType] = useState<string | null>(null);
  const [form, setForm] = useState<ProcurementTicket["form"]>({ header: {}, lines: [] });
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [prefillNotice, setPrefillNotice] = useState<string | null>(null);
  const [schemaErr, setSchemaErr] = useState<string | null>(null);
  const [parentsErr, setParentsErr] = useState<string | null>(null);
  const [parentsLoading, setParentsLoading] = useState(false);
  const [clientIssues, setClientIssues] = useState<ClientFormIssue[]>([]);
  const [prefillLoading, setPrefillLoading] = useState(false);
  const [linkedPoConflict, setLinkedPoConflict] = useState<LinkedTicketSummary | null>(null);
  /** Full PR row (for attachment list) — from parent picker list or ``getTicket`` when deep-linking. */
  const [linkedPrResolved, setLinkedPrResolved] = useState<ProcurementTicket | null>(null);

  const formRef = useRef<ProcurementTicket["form"]>({ header: {}, lines: [] });
  formRef.current = form;
  /** Standalone drafts per workflow (switching YSER ↔ YUNB keeps each form until send). */
  const draftsRef = useRef<Record<string, ProcurementTicket["form"]>>({});

  /** Avoid duplicate parent-list fetches (deep link vs standalone auto-pick). */
  const parentsRequest = useRef<"idle" | "loading" | "done">("idle");

  const loadSchema = useCallback(() => {
    setSchemaErr(null);
    getProcurementSchema()
      .then(setSchema)
      .catch((e) => setSchemaErr(schemaLoadErrorMessage(e)));
  }, []);

  const fetchParents = useCallback(async (force = false) => {
    if (force) parentsRequest.current = "idle";
    if (parentsRequest.current === "loading") return;
    if (parentsRequest.current === "done") return;
    parentsRequest.current = "loading";
    setParentsErr(null);
    setParentsLoading(true);
    try {
      const rows = await listParentPRs();
      setParents(rows);
      parentsRequest.current = "done";
    } catch (e) {
      parentsRequest.current = "idle";
      setParentsErr(parentsListLoadErrorMessage(e));
    } finally {
      setParentsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSchema();
  }, [loadSchema]);

  /** Load SAP-linked PRs as soon as schema is ready so step-1 autocomplete has data (independent of workflow pick). */
  useEffect(() => {
    if (!schema) return;
    void fetchParents();
  }, [schema, fetchParents]);

  useEffect(() => {
    if (parentFromUrl) setParentId(parentFromUrl);
  }, [parentFromUrl]);

  useEffect(() => {
    if (!parentId) {
      setLinkedPrResolved(null);
      return;
    }
    const fromList = parents.find((p) => p.id === parentId);
    if (fromList) {
      setLinkedPrResolved(fromList);
      return;
    }
    let cancelled = false;
    getTicket(parentId)
      .then((t) => {
        if (!cancelled) setLinkedPrResolved(t);
      })
      .catch(() => {
        if (!cancelled) setLinkedPrResolved(null);
      });
    return () => {
      cancelled = true;
    };
  }, [parentId, parents]);

  const types = schema?.document_types ?? [];
  const multiType = types.length > 1;
  const singleTypeLabel = types.length === 1 ? types[0]!.label : null;
  const active =
    schema && documentType ? schema.document_types.find((d) => d.code === documentType) : undefined;
  const poHeaderFields = active ? procurementPoHeaderFields(active) : [];

  const schemaTypesKey = useMemo(() => procurementSchemaTypesKey(schema), [schema]);

  /** Single workflow only → select automatically. Multi-type standalone: user must pick (no localStorage preselect). */
  useEffect(() => {
    if (!schema?.document_types.length || parentId) return;
    setDocumentType((current) => {
      if (current) return current;
      const list = schema.document_types;
      if (list.length === 1) return list[0]!.code;
      return current;
    });
  }, [schema, parentId]);

  useEffect(() => {
    if (!parentId) {
      setPrefillLoading(false);
      setLinkedPoConflict(null);
      return;
    }
    const prId = parentId;
    let cancelled = false;

    async function loadLinkedPoSummary(poId: string, documentTypeHint: string): Promise<LinkedTicketSummary> {
      try {
        const po = await getTicket(poId);
        return {
          id: po.id,
          kind: po.kind,
          sap_id: po.sap_id,
          document_type: po.document_type,
        };
      } catch {
        return { id: poId, kind: "PO", sap_id: null, document_type: documentTypeHint };
      }
    }

    async function run() {
      setPrefillLoading(true);
      setErr(null);
      setLinkedPoConflict(null);
      setPrefillNotice(null);
      try {
        const pr = await getTicket(prId);
        if (cancelled) return;
        setLinkedPrResolved(pr);
        const existingPo = pr.linked_pos?.[0];
        if (existingPo) {
          setLinkedPoConflict(existingPo);
          return;
        }
        const p = await prefillPo(prId);
        if (cancelled) return;
        setDocumentType(p.document_type);
        const notices: string[] = [];
        if (p.sap_prefill_message) notices.push(p.sap_prefill_message);
        const formCopy =
          p.form && typeof p.form === "object"
            ? (structuredClone(p.form) as ProcurementTicket["form"])
            : p.form;
        const hdr =
          formCopy?.header && typeof formCopy.header === "object"
            ? (formCopy.header as Record<string, unknown>)
            : null;
        if (hdr) {
          const note = String(hdr.header_note ?? "");
          if (note.length > 12) {
            hdr.header_note = note.slice(0, 12);
            notices.push("PR header note was longer than 12 characters and was shortened for the PO.");
          }
        }
        if (notices.length) setPrefillNotice(notices.join(" "));
        const hydrated = hydrateProcurementFormForEditor(
          applyPoRequestorEmailDefault(formCopy, user?.email),
          {
            kind: "PO",
            documentType: p.document_type,
            formSource: p.form_source ?? (p.sap_prefill_fallback ? "db" : "sap"),
          }
        );
        setForm(hydrated);
        try {
          draftsRef.current[p.document_type] = structuredClone(hydrated);
        } catch {
          draftsRef.current[p.document_type] = hydrated;
        }
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ProcurementLinkedPoConflictError) {
          if (e.linkedPoId) {
            const summary = await loadLinkedPoSummary(e.linkedPoId, documentType ?? "YSER");
            if (!cancelled) setLinkedPoConflict(summary);
          } else {
            try {
              const pr = await getTicket(prId);
              const po = pr.linked_pos?.[0];
              if (!cancelled) {
                if (po) setLinkedPoConflict(po);
                else setErr(e.message);
              }
            } catch {
              if (!cancelled) setErr(e.message);
            }
          }
          return;
        }
        setErr(humanizeProcurementUnknownError(e, "Could not load PR for PO draft."));
      } finally {
        if (!cancelled) setPrefillLoading(false);
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [parentId, user?.email, documentType]);

  /** Fill requestor email once auth resolves (prefill/empty form may run before ``user`` is loaded). */
  useEffect(() => {
    if (!user?.email) return;
    setForm((prev) => applyPoRequestorEmailDefault(prev, user.email));
  }, [user?.email]);

  /** Sole workflow type + standalone: load empty template once (multi-type uses ``pickDocumentType``). */
  useEffect(() => {
    if (parentId || !documentType || !schema) return;
    if (schema.document_types.length !== 1) return;
    const dt = schema.document_types[0]!;
    if (dt.code !== documentType) return;
    const saved = draftsRef.current[documentType];
    if (saved) {
      setForm(structuredClone(saved));
      return;
    }
    const keys = (dt.block_fields ?? dt.line_fields).map((f) => f.key);
    const base = emptyTicketForm(procurementPoHeaderFields(dt), dt.line_fields, dt.code);
    const merged = applyPoRequestorEmailDefault(
      mergePoDraft(base, documentType, keys),
      user?.email
    );
    draftsRef.current[documentType] = structuredClone(merged);
    setForm(merged);
  }, [parentId, documentType, schema, schemaTypesKey, user?.email]);

  useEffect(() => {
    setClientIssues([]);
  }, [parentId, documentType]);

  /** Persist unsaved standalone PO draft (not when linked to a PR). */
  useEffect(() => {
    if (!documentType || parentId) return;
    const t = window.setTimeout(() => {
      persistPoDraft(documentType, formRef.current);
      try {
        draftsRef.current[documentType] = structuredClone(formRef.current);
      } catch {
        draftsRef.current[documentType] = formRef.current;
      }
    }, 400);
    return () => window.clearTimeout(t);
  }, [form, documentType, parentId]);

  const pickDocumentType = useCallback(
    (code: string) => {
      if (code === documentType) return;
      if (documentType) {
        if (!parentId) persistPoDraft(documentType, formRef.current);
        try {
          draftsRef.current[documentType] = structuredClone(formRef.current);
        } catch {
          draftsRef.current[documentType] = formRef.current;
        }
      }
      setParentId(null);
      setDocumentType(code);
      const incoming = draftsRef.current[code];
      if (incoming) {
        setForm(structuredClone(incoming));
      } else {
        const dt = schema?.document_types.find((d) => d.code === code);
        if (!dt) return;
        const keys = (dt.block_fields ?? dt.line_fields).map((f) => f.key);
        const base = emptyTicketForm(procurementPoHeaderFields(dt), dt.line_fields, dt.code);
        const merged = applyPoRequestorEmailDefault(mergePoDraft(base, code, keys), user?.email);
        draftsRef.current[code] = structuredClone(merged);
        setForm(merged);
      }
      try {
        window.localStorage.setItem(LAST_PO_DOC_KEY, code);
      } catch {
        /* ignore */
      }
      setClientIssues([]);
    },
    [documentType, schema, user?.email]
  );

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!documentType || !active || linkedPoConflict) return;
    setErr(null);
    const local = validateProcurementForm("PO", form, {
      ...active,
      header_fields: poHeaderFields,
    });
    if (local.length > 0) {
      setClientIssues(local);
      focusFirstProcurementClientIssue("PO", documentType, local);
      return;
    }
    setClientIssues([]);
    setBusy(true);
    try {
      const t = await createPo({
        parent_pr_id: parentId ?? undefined,
        document_type: documentType,
        form,
        files,
      });
      if (!parentId) {
        clearPoDraft(documentType);
        delete draftsRef.current[documentType];
      }
      router.push(`/procurement/tickets/${t.id}?syncing=1`);
    } catch (x) {
      if (x instanceof ProcurementLinkedPoConflictError && x.linkedPoId) {
        try {
          const po = await getTicket(x.linkedPoId);
          setLinkedPoConflict({
            id: po.id,
            kind: po.kind,
            sap_id: po.sap_id,
            document_type: po.document_type,
          });
        } catch {
          setErr(x.message);
        }
        return;
      }
      setErr(humanizeProcurementUnknownError(x, "Submit failed"));
    } finally {
      setBusy(false);
    }
  }

  const formActive = Boolean(active);
  const flowStep: 1 | 2 = formActive ? 2 : 1;

  const workflowTypePicker = useMemo(() => {
    if (!multiType || types.length === 0) return null;
    const compactToolbar = formActive;
    return (
      <div className="flex flex-wrap gap-2" role="group" aria-label="Order workflow type">
        {types.map((d) => (
          <button
            key={d.code}
            type="button"
            onClick={() => pickDocumentType(d.code)}
            className={`rounded-full border px-3 py-1.5 text-left font-semibold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-green)] ${
              compactToolbar
                ? "min-h-[32px] text-xs sm:min-h-[34px] sm:px-3.5 sm:text-sm"
                : "min-h-[34px] text-xs sm:min-h-[38px] sm:px-4 sm:py-2 sm:text-sm"
            } ${
              documentType === d.code
                ? "border-[var(--accent-green)] bg-[rgba(21,163,115,0.12)] text-[var(--accent-green)] shadow-sm"
                : "border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-[var(--border-active)]"
            }`}
          >
            <span className="block leading-tight">{d.label}</span>
            <span className="mt-0.5 block font-mono text-[11px] font-medium uppercase tracking-wide text-[var(--text-muted)] sm:text-xs">
              {d.code}
            </span>
          </button>
        ))}
      </div>
    );
  }, [multiType, types, formActive, documentType, pickDocumentType]);

  return (
    <div
      className={`mx-auto flex w-full max-w-[1600px] flex-1 min-h-0 flex-col sm:-mt-2 ${formActive ? "gap-1.5 sm:gap-2" : "gap-2 sm:gap-3"}`}
    >
      <header
        className={`flex shrink-0 flex-col xl:flex-row xl:items-start xl:justify-between xl:gap-4 ${formActive ? "gap-1" : "gap-2"}`}
      >
        <div className="min-w-0 flex-1">
          {formActive ? (
            <>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 sm:gap-x-3">
                <ProcurementBackLink href={PROCUREMENT_HOME_HREF} dense>
                  Procurement home
                </ProcurementBackLink>
                <h1 className="text-lg font-bold tracking-tight text-[var(--text-primary)] sm:text-xl">
                  New purchase order
                </h1>
              </div>
              {multiType ? (
                <ProcurementFlowSteps
                  compact
                  denseStrip
                  step={flowStep}
                  labels={["Pick workflow", "Fill & send"]}
                />
              ) : types.length === 1 ? (
                <p className="mt-0.5 text-xs font-medium text-[var(--text-muted)]">
                  Workflow: <span className="text-[var(--text-primary)]">{singleTypeLabel}</span>
                  <span className="ml-1.5 rounded-md bg-[var(--bg-elev)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--text-secondary)] sm:text-xs">
                    {types[0]!.code}
                  </span>
                </p>
              ) : null}
              <p className="mt-0.5 max-w-xl text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
                {parentId
                  ? "Lines were copied from your request — confirm amounts and vendor, then send."
                  : "On a wide screen, lines and purchasing details sit side by side. Search fields support typing to find a row, then choose it from the list."}{" "}
                <kbd className="rounded border border-[var(--border)] bg-[var(--bg-elev)] px-1 py-0.5 font-mono text-[10px] sm:text-xs">
                  ⌘↵
                </kbd>{" "}
                / Ctrl+Enter sends when focus is not inside a multi-line note.
              </p>
            </>
          ) : (
            <>
              <ProcurementBackLink href={PROCUREMENT_HOME_HREF}>Procurement home</ProcurementBackLink>
              {multiType ? (
                <ProcurementFlowSteps
                  compact
                  denseStrip={false}
                  step={flowStep}
                  labels={["Pick workflow", "Fill in and send"]}
                />
              ) : types.length === 1 ? (
                <p className="mb-3 text-xs font-medium text-[var(--text-muted)]">
                  Workflow: <span className="text-[var(--text-primary)]">{singleTypeLabel}</span>
                  <span className="ml-2 rounded-md bg-[var(--bg-elev)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]">
                    {types[0]!.code}
                  </span>
                </p>
              ) : null}
              <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)] sm:text-3xl">
                New purchase order
              </h1>
              <p className="mt-1 max-w-3xl text-xs leading-snug text-[var(--text-secondary)]">
                Pick a workflow in the header to open the form. Unsaved standalone orders are kept in this browser
                session until you send. Linked orders copy from the request. Expand{" "}
                <span className="font-medium">Optional: link a PR</span> below only if you need copied lines. Lists with
                only one valid choice fill automatically.{" "}
                <kbd className="rounded border border-[var(--border)] bg-[var(--bg-elev)] px-1 py-0.5 font-mono text-[11px] sm:text-xs">
                  ⌘↵
                </kbd>{" "}
                / Ctrl+Enter sends from the form when focus is not in a long text box.
              </p>
            </>
          )}
        </div>

        {multiType ? (
          <div
            className="flex shrink-0 flex-wrap gap-2 xl:max-w-[min(100%,28rem)] xl:justify-end"
            role="group"
            aria-label="Order workflow category"
          >
            {workflowTypePicker}
          </div>
        ) : null}
      </header>

      {schemaErr ? (
        <div className="shrink-0">
          <ErrorBanner onRetry={loadSchema}>{schemaErr}</ErrorBanner>
        </div>
      ) : null}
      {parentsErr ? (
        <div className="shrink-0">
          <ErrorBanner onRetry={() => void fetchParents(true)}>{parentsErr}</ErrorBanner>
        </div>
      ) : null}
      {err ? (
        <div className="shrink-0">
          <ErrorBanner>{err}</ErrorBanner>
        </div>
      ) : null}
      {linkedPoConflict ? (
        <div
          className="shrink-0 rounded-lg border border-[var(--accent-orange)]/35 bg-[rgba(245,158,11,0.1)] px-4 py-3 text-sm text-[var(--text-secondary)]"
          role="alert"
        >
          <p>
            This purchase request already has a linked purchase order. Open that order to update it, or raise a new
            purchase request.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Link
              href={`/procurement/tickets/${linkedPoConflict.id}`}
              className="inline-flex min-h-[40px] items-center rounded-lg bg-app-gradient-1 px-4 text-sm font-bold text-white shadow-sm transition hover:opacity-[0.97]"
            >
              Open order {linkedPoConflict.sap_id?.trim() || "→"}
            </Link>
            <Link
              href="/procurement/pr/new"
              className="inline-flex min-h-[40px] items-center rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-4 text-sm font-semibold text-[var(--text-primary)] transition hover:border-[var(--border-active)]"
            >
              New purchase request
            </Link>
          </div>
        </div>
      ) : null}
      {prefillNotice ? (
        <div className="shrink-0 rounded-lg border border-amber-200/60 bg-amber-50/80 px-4 py-3 text-sm text-amber-900">
          {prefillNotice}
        </div>
      ) : null}

      {!schema && !schemaErr ? (
        <div className="grid shrink-0 gap-4 xl:grid-cols-12" aria-busy="true" aria-label="Loading form">
          <div className="h-48 animate-pulse rounded-2xl bg-[var(--bg-secondary)] xl:col-span-5" />
          <div className="h-64 animate-pulse rounded-2xl bg-[var(--bg-secondary)] xl:col-span-7" />
        </div>
      ) : null}

      {schema && types.length === 0 ? (
        <p className="shrink-0 rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-6 text-sm text-[var(--text-muted)]">
          No document types are configured for procurement. Contact your administrator.
        </p>
      ) : null}

      {parentId && prefillLoading && !err && !linkedPoConflict ? (
        <div
          className="shrink-0 flex items-center gap-2 rounded-lg border border-dashed border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-[11px] text-[var(--text-muted)] sm:text-xs"
          aria-live="polite"
        >
          <span
            className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--accent-green)]"
            aria-hidden
          />
          Loading linked request…
        </div>
      ) : null}

      {schema && multiType && !documentType && !parentId && !schemaErr ? (
        <div className="shrink-0 rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 text-sm text-[var(--text-secondary)] shadow-sm ring-1 ring-black/[0.02]">
          <p className="font-semibold text-[var(--text-primary)]">Select an order workflow</p>
          <p className="mt-1.5 text-xs leading-relaxed">
            Choose a workflow in the header to continue. Each workflow keeps its own draft if you switch before you send.
          </p>
        </div>
      ) : null}

      {!formActive && schema && !schemaErr ? (
        <details className="group shrink-0 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-xs font-semibold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden sm:px-4 sm:text-sm">
            <span>
              Optional: link a PR <span className="font-normal text-[var(--text-muted)]">(SAP number)</span>
            </span>
            <span className="text-xs font-medium text-[var(--accent-blue)] group-open:hidden">Show</span>
            <span className="hidden text-xs font-medium text-[var(--accent-blue)] group-open:inline">Hide</span>
          </summary>
          <div className="border-t border-[var(--border)] px-3 pb-3 pt-2 sm:px-4">
            <p className="mb-2 text-[11px] leading-snug text-[var(--text-secondary)] sm:text-xs">
              SAP-linked PRs only — replaces your draft with copied lines until you clear.{" "}
              <Link href="/procurement/tickets" className="font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline">
                My tickets
              </Link>
            </p>
            {!parentsErr ? (
              <ParentPrPicker
                parents={parents}
                value={parentId}
                onChange={setParentId}
                disabled={!!parentsErr || parentsLoading}
                parentsLoading={parentsLoading}
                dense
              />
            ) : null}
            {!parentsErr && !parentsLoading && parents.length === 0 ? (
              <p className="mt-2 text-xs text-[var(--text-secondary)]">
                No SAP-backed PRs yet.{" "}
                <Link href="/procurement/pr/new" className="font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline">
                  Create a purchase request
                </Link>
                .
              </p>
            ) : null}
          </div>
        </details>
      ) : null}

      {formActive ? (
        <form
          id="proc-po-new-form"
          noValidate
          onSubmit={onSubmit}
          onKeyDown={(e) => {
            if (e.defaultPrevented) return;
            if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
            const t = e.target as HTMLElement | null;
            if (t?.closest("textarea")) return;
            e.preventDefault();
            (e.currentTarget as HTMLFormElement).requestSubmit();
          }}
          className="flex min-h-0 flex-1 flex-col overflow-hidden"
        >
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-y-contain pb-2 sm:space-y-4 sm:pb-3">
            {active!.block_fields?.length ? (
              <ProcurementBlocksForm
                kind="PO"
                documentType={documentType!}
                active={{
                  code: active!.code,
                  header_fields: poHeaderFields,
                  block_fields: active!.block_fields,
                  allocation_fields: active!.allocation_fields ?? [],
                }}
                form={form}
                setForm={setForm}
                clientIssues={clientIssues}
                density="compact"
                autoFillSingleOptionRefs
                linkedToPr={Boolean(parentId)}
              />
            ) : (
              <ErrorBanner>
                This workflow has no block-based form in the schema. Fix server configuration or contact support.
              </ErrorBanner>
            )}

            {parentId && linkedPrResolved ? (
              <LinkedPrRequestFiles prId={parentId} attachments={linkedPrResolved.attachments} dense />
            ) : null}

            <details className="group rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-xs font-semibold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden sm:px-4 sm:text-sm">
                <span>
                  Optional: link a PR <span className="font-normal text-[var(--text-muted)]">(SAP number)</span>
                </span>
                <span className="text-xs font-medium text-[var(--accent-blue)] group-open:hidden">Show</span>
                <span className="hidden text-xs font-medium text-[var(--accent-blue)] group-open:inline">Hide</span>
              </summary>
              <div className="border-t border-[var(--border)] px-3 pb-3 pt-2 sm:px-4">
                <p className="mb-2 text-[11px] leading-snug text-[var(--text-secondary)] sm:text-xs">
                  SAP-linked PRs only — replaces this draft with copied lines until you clear.
                </p>
                {!parentsErr ? (
                  <ParentPrPicker
                    parents={parents}
                    value={parentId}
                    onChange={setParentId}
                    disabled={!!parentsErr || parentsLoading}
                    parentsLoading={parentsLoading}
                    dense
                  />
                ) : null}
                {!parentsErr && !parentsLoading && parents.length === 0 ? (
                  <p className="mt-2 text-xs text-[var(--text-secondary)]">
                    No SAP-backed PRs yet.{" "}
                    <Link href="/procurement/pr/new" className="font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline">
                      Create a purchase request
                    </Link>
                    .
                  </p>
                ) : null}
              </div>
            </details>

            <details className="group rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ring-1 ring-black/[0.02]">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-xs font-semibold text-[var(--text-primary)] marker:content-none [&::-webkit-details-marker]:hidden sm:px-4 sm:text-sm">
                <span>
                  Attachments{" "}
                  <span className="font-normal text-[var(--text-muted)]">(optional)</span>
                </span>
                <span className="text-xs font-medium text-[var(--accent-blue)] group-open:hidden">Show</span>
                <span className="hidden text-xs font-medium text-[var(--accent-blue)] group-open:inline">Hide</span>
              </summary>
              <div className="border-t border-[var(--border)] px-4 pb-4 pt-2">
                <p className="mb-3 text-xs text-[var(--text-muted)]">
                  PDF, JPG, PNG, XLS, or XLSX — up to 10 files, 25 MB each. Sent to SAP after the PO is created.
                </p>
                <ProcurementFilePicker
                  id="po-new-file-upload"
                  files={files}
                  onChange={setFiles}
                  disabled={busy}
                  uploadLabel="Attach files to this order"
                  inputClassName="block w-full text-sm text-[var(--text-secondary)] file:mr-4 file:rounded-lg file:border-0 file:bg-[var(--accent-green)] file:px-4 file:py-2 file:text-xs file:font-semibold file:text-white"
                />
              </div>
            </details>
          </div>

          <ProcurementFormDock
            variant="pinned"
            hint={
              <>
                ⌘/Ctrl+Enter sends. After save you&apos;ll open the ticket page — SAP document number and attachments
                update there automatically.
              </>
            }
          >
            <ProcurementButtonCancel href={PROCUREMENT_HOME_HREF}>Cancel</ProcurementButtonCancel>
            <ProcurementButtonSubmit busy={busy} busyLabel="Saving ticket…">
              Send order
            </ProcurementButtonSubmit>
          </ProcurementFormDock>
        </form>
      ) : null}
    </div>
  );
}
