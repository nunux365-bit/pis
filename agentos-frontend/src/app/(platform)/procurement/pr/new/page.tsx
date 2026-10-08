"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorBanner } from "@/components/ErrorBanner";
import { ProcurementBlocksForm } from "@/components/procurement/ProcurementBlocksForm";
import {
  ProcurementBackLink,
  ProcurementButtonCancel,
  ProcurementButtonSubmit,
  ProcurementFlowSteps,
  ProcurementFormDock,
} from "@/components/procurement/ProcurementChrome";
import { createPr, getProcurementSchema, humanizeProcurementUnknownError, type ProcurementTicket, type SchemaResponse } from "@/lib/procurementApi";
import { ProcurementFilePicker } from "@/components/procurement/ProcurementFilePicker";
import {
  emptyTicketForm,
  focusFirstProcurementClientIssue,
  procurementSchemaTypesKey,
  schemaLoadErrorMessage,
} from "@/lib/procurementEditorShared";
import { validateProcurementForm, type ClientFormIssue } from "@/lib/procurementFormValidation";
import { mergePrDraft, persistPrDraft, clearPrDraft } from "@/lib/lastPrFormStorage";
import { PROCUREMENT_HOME_HREF } from "@/lib/procurementRoutes";

const LAST_PR_DOC_KEY = "agentos_procurement_last_pr_doc_type";

export default function NewPrPage() {
  const router = useRouter();
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [documentType, setDocumentType] = useState<string | null>(null);
  const [form, setForm] = useState<ProcurementTicket["form"]>({ header: {}, lines: [] });
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [schemaErr, setSchemaErr] = useState<string | null>(null);
  const [clientIssues, setClientIssues] = useState<ClientFormIssue[]>([]);

  const formRef = useRef<ProcurementTicket["form"]>({ header: {}, lines: [] });
  formRef.current = form;
  /** In-memory draft per workflow code so switching YSER ↔ YUNB does not lose work. */
  const draftsRef = useRef<Record<string, ProcurementTicket["form"]>>({});
  const schemaTypesKey = useMemo(() => procurementSchemaTypesKey(schema), [schema]);

  const loadSchema = useCallback(() => {
    setSchemaErr(null);
    getProcurementSchema()
      .then(setSchema)
      .catch((e) => setSchemaErr(schemaLoadErrorMessage(e)));
  }, []);

  useEffect(() => {
    loadSchema();
  }, [loadSchema]);

  /** Only one workflow configured → select it automatically (nothing to pick). */
  useEffect(() => {
    if (!schema?.document_types.length) return;
    if (schema.document_types.length !== 1) return;
    const only = schema.document_types[0]!.code;
    setDocumentType((prev) => prev ?? only);
  }, [schema]);

  /** Initialise merged template when the sole workflow type becomes active. */
  useEffect(() => {
    if (!documentType || !schema) return;
    if (schema.document_types.length !== 1) return;
    const dt = schema.document_types[0]!;
    if (dt.code !== documentType) return;
    const saved = draftsRef.current[documentType];
    if (saved) {
      setForm(structuredClone(saved));
      return;
    }
    const keys = (dt.block_fields ?? dt.line_fields).map((f) => f.key);
    const base = emptyTicketForm(dt.header_fields, dt.line_fields, dt.code);
    const merged = mergePrDraft(base, documentType, keys);
    draftsRef.current[documentType] = structuredClone(merged);
    setForm(merged);
  }, [documentType, schema, schemaTypesKey]);

  const pickDocumentType = useCallback(
    (code: string) => {
      if (code === documentType) return;
      if (documentType) {
        persistPrDraft(documentType, formRef.current);
        draftsRef.current[documentType] = structuredClone(formRef.current);
      }
      setDocumentType(code);
      const incoming = draftsRef.current[code];
      if (incoming) {
        setForm(structuredClone(incoming));
      } else {
        const dt = schema?.document_types.find((d) => d.code === code);
        if (!dt) return;
        const keys = (dt.block_fields ?? dt.line_fields).map((f) => f.key);
        const base = emptyTicketForm(dt.header_fields, dt.line_fields, dt.code);
        const merged = mergePrDraft(base, code, keys);
        draftsRef.current[code] = structuredClone(merged);
        setForm(merged);
      }
      try {
        window.localStorage.setItem(LAST_PR_DOC_KEY, code);
      } catch {
        /* ignore */
      }
      setClientIssues([]);
    },
    [documentType, schema]
  );

  const active = schema?.document_types.find((d) => d.code === documentType);
  const types = schema?.document_types ?? [];
  const multiType = types.length > 1;
  const singleTypeLabel = types.length === 1 ? types[0]!.label : null;

  useEffect(() => {
    setClientIssues([]);
  }, [documentType]);

  /** Persist unsaved draft to sessionStorage (survives refresh; cleared on successful submit). */
  useEffect(() => {
    if (!documentType) return;
    const t = window.setTimeout(() => {
      persistPrDraft(documentType, formRef.current);
      try {
        draftsRef.current[documentType] = structuredClone(formRef.current);
      } catch {
        draftsRef.current[documentType] = formRef.current;
      }
    }, 400);
    return () => window.clearTimeout(t);
  }, [form, documentType]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!documentType || !active) return;
    setErr(null);
    const local = validateProcurementForm("PR", form, active);
    if (local.length > 0) {
      setClientIssues(local);
      focusFirstProcurementClientIssue("PR", documentType, local);
      return;
    }
    setClientIssues([]);
    setBusy(true);
    try {
      const t = await createPr({ document_type: documentType, form, files });
      clearPrDraft(documentType);
      delete draftsRef.current[documentType];
      router.push(`/procurement/tickets/${t.id}?syncing=1`);
    } catch (x) {
      setErr(humanizeProcurementUnknownError(x, "Submit failed"));
    } finally {
      setBusy(false);
    }
  }

  const flowStep: 1 | 2 = active ? 2 : 1;

  return (
    <div
      className={`mx-auto flex w-full max-w-[1600px] flex-1 min-h-0 flex-col sm:-mt-2 ${active ? "gap-1.5 sm:gap-2" : "gap-2 sm:gap-3"}`}
    >
      <header
        className={`flex shrink-0 flex-col xl:flex-row xl:items-start xl:justify-between xl:gap-4 ${active ? "gap-1" : "gap-2"}`}
      >
        <div className="min-w-0 flex-1">
          {active ? (
            <>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 sm:gap-x-3">
                <ProcurementBackLink href={PROCUREMENT_HOME_HREF} dense>
                  Procurement home
                </ProcurementBackLink>
                <h1 className="text-lg font-bold tracking-tight text-[var(--text-primary)] sm:text-xl">
                  New purchase request
                </h1>
              </div>
              {multiType ? (
                <ProcurementFlowSteps
                  compact
                  denseStrip
                  step={flowStep}
                  labels={["Pick type", "Fill & send"]}
                />
              ) : types.length === 1 ? (
                <p className="mt-0.5 text-xs font-medium text-[var(--text-muted)]">
                  Type: <span className="text-[var(--text-primary)]">{singleTypeLabel}</span>
                  <span className="ml-1.5 rounded-md bg-[var(--bg-elev)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--text-secondary)] sm:text-xs">
                    {types[0]!.code}
                  </span>
                </p>
              ) : null}
              <p className="mt-0.5 max-w-xl text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
                On a wide screen, lines and purchasing details sit side by side. Search fields support typing to find a
                row, then choose it from the list.{" "}
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
                  labels={["Pick what you're buying", "Fill in and send"]}
                />
              ) : types.length === 1 ? (
                <p className="mb-3 text-xs font-medium text-[var(--text-muted)]">
                  Type: <span className="text-[var(--text-primary)]">{singleTypeLabel}</span>
                  <span className="ml-2 rounded-md bg-[var(--bg-elev)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text-secondary)]">
                    {types[0]!.code}
                  </span>
                </p>
              ) : null}
              <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)] sm:text-3xl">
                New purchase request
              </h1>
              <p className="mt-1 max-w-3xl text-xs leading-snug text-[var(--text-secondary)]">
                Pick a request type in the header to open the form. Unsaved work is kept in this browser session until
                you send or leave. Lists with only one valid choice fill automatically.{" "}
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
            aria-label="Request category"
          >
            {types.map((d) => (
              <button
                key={d.code}
                type="button"
                onClick={() => pickDocumentType(d.code)}
                className={`rounded-full border px-3 py-1.5 text-left font-semibold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-green)] ${
                  active
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
        ) : null}
      </header>

      {schemaErr ? (
        <div className="shrink-0">
          <ErrorBanner onRetry={loadSchema}>{schemaErr}</ErrorBanner>
        </div>
      ) : null}
      {err ? (
        <div className="shrink-0">
          <ErrorBanner>{err}</ErrorBanner>
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

      {schema && multiType && !documentType ? (
        <div className="shrink-0 rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 text-sm text-[var(--text-secondary)] shadow-sm ring-1 ring-black/[0.02]">
          <p className="font-semibold text-[var(--text-primary)]">Select a request type</p>
          <p className="mt-1.5 text-xs leading-relaxed">
            Choose a category in the header to continue. Each workflow keeps its own draft if you switch before you
            send.
          </p>
        </div>
      ) : null}

      {active ? (
        <form
          id="proc-pr-new-form"
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
            {active.block_fields?.length ? (
              <ProcurementBlocksForm
                kind="PR"
                documentType={documentType!}
                active={{
                  code: active.code,
                  header_fields: active.header_fields,
                  block_fields: active.block_fields,
                  allocation_fields: active.allocation_fields ?? [],
                }}
                form={form}
                setForm={setForm}
                clientIssues={clientIssues}
                density="compact"
                autoFillSingleOptionRefs
              />
            ) : (
              <ErrorBanner>
                This workflow has no block-based form in the schema. Fix server configuration or contact support.
              </ErrorBanner>
            )}

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
                PDF, JPG, PNG, XLS, or XLSX — up to 10 files, 25 MB each. Sent to SAP after the PR is created.
              </p>
              <ProcurementFilePicker
                id="pr-new-file-upload"
                files={files}
                onChange={setFiles}
                disabled={busy}
                uploadLabel="Attach files to this request"
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
              Send request
            </ProcurementButtonSubmit>
          </ProcurementFormDock>
        </form>
      ) : null}
    </div>
  );
}
