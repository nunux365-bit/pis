"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ErrorBanner } from "@/components/ErrorBanner";
import {
  FieldAffordanceTray,
  FieldClearButton,
  SearchApiReferenceField,
  StaticReferenceCombobox,
  inputPadForAffordances,
  inputPadForClear,
  refRowDisplayLine,
  type RefRow,
} from "@/components/procurement/ReferencePickers";
import type { CostCenterFacetsResponse, FieldSpec, ProcurementTicket } from "@/lib/procurementApi";
import { getCostCenterFacetOptions, getReferenceValues } from "@/lib/procurementApi";
import { shouldShowValidOnBlur } from "@/lib/fieldBlurValidity";
import { applyProcurementBlockCascade, applyProcurementHeaderCascade } from "@/lib/procurementFieldCascade";
import {
  catalogGroupField,
  catalogLineSearchBlocked,
  catalogLineSearchPlaceholder,
  lineCatalogGroup,
  syncHeaderCatalogGroup,
} from "@/lib/procurementCatalogGroup";
import { syncHeaderTaxCode } from "@/lib/procurementLineTax";
import type { YserPoGroupTaxHint } from "@/lib/procurementYserPoGroupTax";
import {
  yserPoGroupTaxHintByFirstLine,
} from "@/lib/procurementYserPoGroupTax";
import { YserPoGroupTaxBanner } from "@/components/procurement/YserPoGroupTaxBanner";
import { defaultProcurementOrderUnit } from "@/lib/procurementEditorShared";
import { filterPlantsForOrg } from "@/lib/procurementPlantOrg";
import { isPoFieldLockedFromPr } from "@/lib/procurementPoLinkedLocks";
import { procurementFieldDomId } from "@/lib/procurementFieldId";
import {
  resolveProcurementAllocationFields,
  type ClientFormIssue,
} from "@/lib/procurementFormValidation";

const CTL =
  "w-full rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2 text-[13px] leading-snug text-[var(--text-primary)] shadow-sm outline-none transition " +
  "focus:border-[var(--border-active)] focus:ring-2 focus:ring-[var(--accent-green)]/25";

type SchemaSlice = {
  code: string;
  header_fields: FieldSpec[];
  block_fields: FieldSpec[];
  allocation_fields: FieldSpec[];
};

type Props = {
  kind: "PR" | "PO";
  documentType: string;
  active: SchemaSlice;
  form: ProcurementTicket["form"];
  setForm: Dispatch<SetStateAction<ProcurementTicket["form"]>>;
  clientIssues: ClientFormIssue[];
  density?: "compact" | "default";
  /** When a catalog has exactly one choice, fill it automatically (fewer clicks). */
  autoFillSingleOptionRefs?: boolean;
  /** Put “what you’re ordering” before org/plant — best for fast purchase requests. */
  prioritizeBlocksFirst?: boolean;
  /**
   * ``split`` (default): on wide screens, header and lines sit in two columns like the classic PR/PO editor — less vertical scroll.
   * ``stacked``: single column everywhere.
   */
  layout?: "stacked" | "split";
  /**
   * PO linked to a PR: org/plant/storage/group, material/service, and account assignment
   * are read-only (must match the request). Vendor, price, qty, tax, and notes stay editable.
   */
  linkedToPr?: boolean;
  /** @deprecated Use ``linkedToPr`` — kept for compatibility. */
  lockAccountAssignment?: boolean;
  /** When set, the whole form is read-only (e.g. SAP document has no active lines). */
  formLocked?: boolean;
};

function reqFor(spec: FieldSpec, ticketKind: "PR" | "PO"): boolean {
  return ticketKind === "PR" ? spec.required_pr : spec.required_po;
}

/** Strict rule: clear icon only on optional, unlocked fields. */
function fieldClearable(spec: FieldSpec, ticketKind: "PR" | "PO", locked: boolean): boolean {
  if (spec.key === "valuation_price") return false;
  return !reqFor(spec, ticketKind) && !locked;
}

function isComputedValuationField(spec: FieldSpec): boolean {
  return spec.key === "valuation_price";
}

/** Required in schema and user-editable (valuation is computed read-only). */
function showFieldRequired(spec: FieldSpec, ticketKind: "PR" | "PO"): boolean {
  if (isComputedValuationField(spec)) return false;
  return reqFor(spec, ticketKind);
}

function clampToMaxLength(value: string, maxLength?: number | null): string {
  if (maxLength == null || maxLength < 0) return value;
  return value.length > maxLength ? value.slice(0, maxLength) : value;
}

function issueFor(
  issues: ClientFormIssue[],
  scope: ClientFormIssue["scope"],
  bi: number,
  ai: number | null,
  key: string
): string | undefined {
  for (const x of issues) {
    if (scope === "header" && x.scope === "header" && x.key === key) return x.message;
    if (scope === "block" && x.scope === "block" && x.blockIndex === bi && x.key === key) return x.message;
    if (
      scope === "allocation" &&
      x.scope === "allocation" &&
      x.blockIndex === bi &&
      ai != null &&
      x.allocationIndex === ai &&
      x.key === key
    )
      return x.message;
  }
  return undefined;
}

function HelpIcon({ text }: { text?: string | null }) {
  const t = text?.trim();
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (ev: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(ev.target as Node)) setOpen(false);
    };
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (!t) return null;
  return (
    <span ref={rootRef} className="relative inline-flex align-middle">
      <button
        type="button"
        className="ml-1 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-[var(--border)] text-[10px] font-bold text-[var(--text-muted)] outline-none hover:bg-[var(--bg-elev)] focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30"
        aria-label={`Help: ${t}`}
        aria-expanded={open}
        aria-controls={open ? bodyId : undefined}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <span className="sr-only">Field help</span>
        <span aria-hidden>i</span>
      </button>
      {open ? (
        <span
          id={bodyId}
          role="note"
          className="absolute left-0 top-[calc(100%+4px)] z-30 w-max max-w-[min(22rem,calc(100vw-2rem))] rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-2 text-left text-[11px] font-normal leading-snug text-[var(--text-secondary)] shadow-md ring-1 ring-black/5"
        >
          {t}
        </span>
      ) : null}
    </span>
  );
}

/**
 * Phase 1: purchasing header + item blocks + cost-centre splits.
 * Uses the same searchable list pattern as enterprise procurement tools (type-ahead + dropdown).
 */
export function ProcurementBlocksForm({
  kind,
  documentType,
  active,
  form,
  setForm,
  clientIssues,
  density = "compact",
  autoFillSingleOptionRefs = false,
  prioritizeBlocksFirst = false,
  layout = "split",
  linkedToPr = false,
  lockAccountAssignment = false,
  formLocked = false,
}: Props) {
  const lockFromPr = Boolean(linkedToPr || (lockAccountAssignment && kind === "PO"));
  const editorLocked = lockFromPr || formLocked;
  const split = layout === "split";
  const sectionPad = density === "compact" ? "p-2.5 sm:p-3" : "p-3 sm:p-4";
  /** Split rail: single column + generous vertical gap so combobox helper lines (“Code: …”) never crowd the next label. */
  const headerFieldsGrid = split
    ? "flex min-w-0 flex-col gap-6"
    : "grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3";
  /** Fewer columns on wide screens = wider inputs (unit price, dates) and less horizontal squeeze. */
  const blockFieldsGrid = split
    ? "grid min-w-0 grid-cols-1 gap-x-3 gap-y-4 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3"
    : "grid gap-2 sm:grid-cols-2 lg:grid-cols-3";

  const header = (form.header || {}) as Record<string, unknown>;
  const lines = (Array.isArray(form.lines) ? form.lines : []) as Record<string, unknown>[];
  const cocd = String(header.purchasing_org || header.company_code || "").trim();
  const purchasingOrgCode = String(header.purchasing_org ?? "").trim();
  const allocationFields = useMemo(
    () => resolveProcurementAllocationFields(documentType, active.allocation_fields),
    [documentType, active.allocation_fields]
  );

  /**
   * Stable React keys per material/service line. Kept in **state** (not a ref) so that
   * insert / remove mutations are batched atomically with ``setForm`` — avoiding the
   * desync where a rejected ``setForm`` updater (``return prev``) would leave the keys
   * list pointing at a row that no longer exists.
   */
  const [lineKeys, setLineKeys] = useState<string[]>(() =>
    Array.from({ length: lines.length }, () => crypto.randomUUID())
  );
  // When the workflow document type / kind changes, the backend schema changes too —
  // treat it as a fresh set of lines (full key rotation). Length-only drift (parent
  // replaced ``form.lines`` without going through us, e.g. prefill) is reconciled by
  // padding / trimming the tail so already-rendered rows keep their identity.
  useEffect(() => {
    setLineKeys(() => Array.from({ length: lines.length }, () => crypto.randomUUID()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, documentType]);
  useEffect(() => {
    setLineKeys((prev) => {
      if (prev.length === lines.length) return prev;
      if (prev.length < lines.length) {
        return [
          ...prev,
          ...Array.from({ length: lines.length - prev.length }, () => crypto.randomUUID()),
        ];
      }
      return prev.slice(0, lines.length);
    });
  }, [lines.length]);

  const selectRefDomains = useMemo(() => {
    const s = new Set<string>();
    for (const f of active.header_fields) {
      if (f.widget === "select" && f.reference_domain) s.add(f.reference_domain);
    }
    for (const f of active.block_fields) {
      if (f.widget === "select" && f.reference_domain) s.add(f.reference_domain);
    }
    return [...s];
  }, [active.header_fields, active.block_fields]);

  const [refOptions, setRefOptions] = useState<Record<string, RefRow[]>>({});
  const [refLoading, setRefLoading] = useState<Record<string, boolean>>({});
  const [domainLoadErrors, setDomainLoadErrors] = useState<Record<string, string>>({});
  const [blurValid, setBlurValid] = useState<Record<string, boolean>>({});

  const markBlurValid = useCallback((key: string, valid: boolean) => {
    setBlurValid((m) => ({ ...m, [key]: valid }));
  }, []);
  const clearBlurValid = useCallback((key: string) => {
    setBlurValid((m) => ({ ...m, [key]: false }));
  }, []);

  const headerPlant = String(header.plant ?? "").trim();

  const loadRefDomain = useCallback(
    async (domain: string, plantForSloc?: string) => {
      setRefLoading((m) => ({ ...m, [domain]: true }));
      try {
        const rows = await getReferenceValues(domain, documentType, {
          ticketKind: kind,
          purchasingOrg: domain === "plant" && purchasingOrgCode ? purchasingOrgCode : undefined,
          plant:
            domain === "storage_location"
              ? (plantForSloc ?? headerPlant) || undefined
              : undefined,
        });
        let mapped = rows.map((r) => ({ code: r.code, label: r.label }));
        if (domain === "plant" && purchasingOrgCode) {
          mapped = filterPlantsForOrg(mapped, purchasingOrgCode);
        }
        setRefOptions((prev) => ({
          ...prev,
          [domain]: mapped,
        }));
        setDomainLoadErrors((m) => {
          if (!(domain in m)) return m;
          const next = { ...m };
          delete next[domain];
          return next;
        });
      } catch {
        setRefOptions((prev) => ({ ...prev, [domain]: [] }));
        setDomainLoadErrors((m) => ({
          ...m,
          [domain]: "Could not load dropdown values. Check your connection and try again.",
        }));
      } finally {
        setRefLoading((m) => ({ ...m, [domain]: false }));
      }
    },
    [documentType, kind, purchasingOrgCode, headerPlant]
  );

  const retryFailedReferenceDomains = useCallback(() => {
    setDomainLoadErrors((m) => {
      for (const d of Object.keys(m)) void loadRefDomain(d);
      return m;
    });
  }, [loadRefDomain]);

  useEffect(() => {
    setRefOptions({});
    setRefLoading({});
    setDomainLoadErrors({});
  }, [documentType, kind]);

  useEffect(() => {
    for (const d of selectRefDomains) void loadRefDomain(d);
  }, [selectRefDomains, loadRefDomain]);

  useEffect(() => {
    if (selectRefDomains.includes("plant")) void loadRefDomain("plant");
  }, [purchasingOrgCode, selectRefDomains, loadRefDomain]);

  useEffect(() => {
    if (selectRefDomains.includes("storage_location") && headerPlant) {
      void loadRefDomain("storage_location", headerPlant);
    }
  }, [headerPlant, selectRefDomains, loadRefDomain]);

  useEffect(() => {
    if (!autoFillSingleOptionRefs) return;
    setForm((prev) => {
      const headerPrev = { ...(prev.header || {}) } as Record<string, unknown>;
      const linesPrev: Record<string, unknown>[] =
        prev.lines?.length > 0
          ? prev.lines.map((row) => ({ ...(typeof row === "object" && row ? row : {}) }))
          : [];
      if (linesPrev.length === 0) return prev;

      let changed = false;
      for (const f of active.header_fields) {
        if (f.widget !== "select" || !f.reference_domain) continue;
        const opts = refOptions[f.reference_domain];
        if (!opts || opts.length !== 1) continue;
        if (!reqFor(f, kind)) continue;
        if (String(headerPrev[f.key] ?? "").trim()) continue;
        headerPrev[f.key] = opts[0]!.code;
        changed = true;
      }

      const nextLines = linesPrev.map((row) => {
        const r = { ...row };
        for (const f of active.block_fields) {
          if (f.widget !== "select" || !f.reference_domain) continue;
          const opts = refOptions[f.reference_domain];
          if (!opts || opts.length !== 1) continue;
          if (!reqFor(f, kind)) continue;
          if (String(r[f.key] ?? "").trim()) continue;
          r[f.key] = opts[0]!.code;
          changed = true;
        }
        return r;
      });

      if (!changed) return prev;
      return { ...prev, header: headerPrev, lines: nextLines };
    });
  }, [autoFillSingleOptionRefs, refOptions, active.header_fields, active.block_fields, kind, setForm]);

  const setHeader = (key: string, v: string) => {
    setForm((prev) => {
      const hdr = { ...(prev.header || {}) } as Record<string, unknown>;
      const prevVal = String(hdr[key] ?? "");
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const lineCopies = prevLines.map((row) => ({ ...(typeof row === "object" && row ? row : {}) }));
      const cascaded = applyProcurementHeaderCascade(documentType, key, prevVal, v, { ...hdr }, lineCopies);
      if (cascaded) {
        return { ...prev, header: cascaded.header, lines: cascaded.lines };
      }
      return { ...prev, header: { ...hdr, [key]: v } };
    });
  };

  const setBlock = (bi: number, patch: Record<string, unknown>) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const next = prevLines.map((row, i) =>
        i === bi ? { ...(typeof row === "object" && row ? row : {}), ...patch } : row
      );
      return { ...prev, lines: next };
    });
  };

  const setAlloc = (bi: number, ai: number, patch: Record<string, unknown>) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const row = prevLines[bi];
      if (!row) return prev;
      const baseRow = { ...(typeof row === "object" && row ? row : {}) };
      const allocs = Array.isArray(baseRow.allocations) ? [...(baseRow.allocations as object[])] : [];
      const cur = (allocs[ai] && typeof allocs[ai] === "object" ? allocs[ai] : {}) as Record<string, unknown>;
      allocs[ai] = { ...cur, ...patch };
      const nextRow: Record<string, unknown> = { ...baseRow, allocations: allocs };
      if (documentType.toUpperCase() === "YAST" && "asset" in patch) {
        const first = allocs.find((a) => {
          const o = a && typeof a === "object" ? (a as Record<string, unknown>) : null;
          return o && String(o.asset ?? "").trim();
        }) as Record<string, unknown> | undefined;
        nextRow.asset = first ? String(first.asset ?? "").trim() : "";
      }
      const next = prevLines.map((r, i) => (i === bi ? nextRow : r));
      return { ...prev, lines: next };
    });
  };

  const setBlockField = (
    bi: number,
    key: string,
    v: string,
    opts?: { catalogDescription?: string }
  ) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const row = prevLines[bi];
      if (!row) return prev;
      const base = { ...(typeof row === "object" && row ? row : {}) };
      const patch: Record<string, unknown> = { [key]: v };
      const dt = documentType.toUpperCase();
      if (key === "material" && dt !== "YSER") {
        const prevMat = String(base.material ?? "");
        if (opts?.catalogDescription !== undefined) {
          patch.short_text = opts.catalogDescription;
        } else if (prevMat !== v) {
          patch.short_text = "";
        }
      }
      if (key === "service" && dt === "YSER") {
        const prevSvc = String(base.service ?? "");
        if (opts?.catalogDescription !== undefined) {
          patch.short_text = opts.catalogDescription;
        } else if (prevSvc !== v) {
          patch.short_text = "";
        }
      }
      if (key === "material_group" || key === "service_group") {
        const cascaded = applyProcurementBlockCascade(
          documentType,
          key,
          String(base[key] ?? ""),
          v,
          base
        );
        const nextRow = cascaded ? { ...base, ...cascaded } : { ...base, ...patch };
        const next = prevLines.map((r, i) => (i === bi ? nextRow : r));
        const hdr = { ...(prev.header || {}) } as Record<string, unknown>;
        syncHeaderCatalogGroup(hdr, next, documentType);
        return { ...prev, header: hdr, lines: next };
      }
      const next = prevLines.map((r, i) => (i === bi ? { ...base, ...patch } : r));
      if (key === "tax_code") {
        const hdr = { ...(prev.header || {}) } as Record<string, unknown>;
        syncHeaderTaxCode(hdr, next);
        return { ...prev, header: hdr, lines: next };
      }
      return { ...prev, lines: next };
    });
  };

  const addBlock = () => {
    const lk = active.code.toUpperCase() === "YSER" ? "service" : "material";
    // Push the key for the new row in the same event as the form update —
    // React batches both setStates so the next render sees a matched (lines, keys) pair
    // and never falls back to the unstable ``line-${bi}`` default.
    const newKey = crypto.randomUUID();
    setLineKeys((prev) => [...prev, newKey]);
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const firstDelivery = String(prevLines[0]?.delivery_date ?? "").trim();
      const firstTax = String(prevLines[0]?.tax_code ?? "").trim();
      const hdr =
        prev.header && typeof prev.header === "object"
          ? (prev.header as Record<string, unknown>)
          : {};
      const grpKey = lk === "service" ? "service_group" : "material_group";
      const firstLine =
        prevLines[0] && typeof prevLines[0] === "object"
          ? (prevLines[0] as Record<string, unknown>)
          : {};
      const grpVal = lineCatalogGroup(firstLine, hdr, active.code);
      return {
        ...prev,
        lines: [
          ...prevLines,
          {
            line_kind: lk,
            material_group: grpKey === "material_group" ? grpVal : "",
            service_group: grpKey === "service_group" ? grpVal : "",
            material: "",
            service: "",
            short_text: "",
            order_unit: defaultProcurementOrderUnit(active.code),
            unit_price: "",
            valuation_price: "",
            delivery_date: firstDelivery,
            tax_code: firstTax,
            asset: "",
            account_assignment_cat: "",
            item_category: "",
            net_price: "",
            gross_price: "",
            allocations:
              active.code.toUpperCase() === "YAST"
                ? [{ asset: "", qty: "" }]
                : [{ cost_center: "", qty: "" }],
          },
        ],
      };
    });
  };

  const removeBlock = (bi: number) => {
    if (lines.length <= 1) return;
    setLineKeys((prev) => (prev.length > bi ? prev.filter((_, i) => i !== bi) : prev));
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      if (prevLines.length <= 1) return prev;
      const nextLines = prevLines.filter((_, i) => i !== bi);
      const hdr = { ...(prev.header || {}) } as Record<string, unknown>;
      syncHeaderCatalogGroup(hdr, nextLines, documentType);
      syncHeaderTaxCode(hdr, nextLines);
      return { ...prev, header: hdr, lines: nextLines };
    });
  };

  const addAlloc = (bi: number) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const row = prevLines[bi];
      if (!row) return prev;
      const base = { ...(typeof row === "object" && row ? row : {}) };
      const allocs = Array.isArray(base.allocations) ? [...(base.allocations as object[])] : [];
      allocs.push(
        documentType.toUpperCase() === "YAST"
          ? { asset: "", qty: "" }
          : { cost_center: "", qty: "" }
      );
      const next = prevLines.map((r, i) => (i === bi ? { ...base, allocations: allocs } : r));
      return { ...prev, lines: next };
    });
  };

  const removeAlloc = (bi: number, ai: number) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const row = prevLines[bi];
      if (!row) return prev;
      const base = { ...(typeof row === "object" && row ? row : {}) };
      const allocs = Array.isArray(base.allocations) ? [...(base.allocations as object[])] : [];
      if (allocs.length <= 1) return prev;
      allocs.splice(ai, 1);
      const nextRow: Record<string, unknown> = { ...base, allocations: allocs };
      if (documentType.toUpperCase() === "YAST") {
        const first = allocs.find((a) => {
          const o = a && typeof a === "object" ? (a as Record<string, unknown>) : null;
          return o && String(o.asset ?? "").trim();
        }) as Record<string, unknown> | undefined;
        nextRow.asset = first ? String(first.asset ?? "").trim() : "";
      }
      const next = prevLines.map((r, i) => (i === bi ? nextRow : r));
      return { ...prev, lines: next };
    });
  };

  const suggestValuation = (bi: number) => {
    setForm((prev) => {
      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<string, unknown>[];
      const row = prevLines[bi];
      if (!row || typeof row !== "object") return prev;
      const rec = row as Record<string, unknown>;
      const up = Number.parseFloat(String(rec.unit_price || "").replace(",", "."));
      if (!Number.isFinite(up)) return prev;
      const allocs = Array.isArray(rec.allocations) ? rec.allocations : [];
      let sum = 0;
      for (const a of allocs) {
        if (!a || typeof a !== "object") continue;
        const q = Number.parseFloat(String((a as Record<string, unknown>).qty || "").replace(",", "."));
        if (Number.isFinite(q) && q > 0) sum += q * up;
      }
      if (sum <= 0) {
        const curVp = String(rec.valuation_price ?? "").trim();
        if (!curVp) return prev;
        const next = prevLines.map((r, i) =>
          i === bi ? { ...(typeof r === "object" && r ? r : {}), valuation_price: "" } : r
        );
        return { ...prev, lines: next };
      }
      const vp = String(Math.round(sum * 100) / 100);
      const next = prevLines.map((r, i) =>
        i === bi ? { ...(typeof r === "object" && r ? r : {}), valuation_price: vp } : r
      );
      return { ...prev, lines: next };
    });
  };

  const firstSearchKey = useMemo(
    () =>
      active.block_fields.find((x) => x.widget === "search" && x.reference_domain && x.ui_visible !== false)?.key ??
      null,
    [active.block_fields]
  );

  const firstHeaderSearchKey = useMemo(
    () =>
      active.header_fields.find((x) => x.widget === "search" && x.reference_domain && x.ui_visible !== false)?.key ??
      null,
    [active.header_fields]
  );

  const itemKindLabel = active.code.toUpperCase() === "YSER" ? "service" : "material";

  const showYserPoGroupTaxHints = kind === "PO" && active.code.toUpperCase() === "YSER";

  const yserPoTaxHintByFirstLine = useMemo(() => {
    if (!showYserPoGroupTaxHints) return new Map<number, YserPoGroupTaxHint>();
    return yserPoGroupTaxHintByFirstLine(lines, header);
  }, [showYserPoGroupTaxHints, lines, header]);

  const renderHeaderFields = () => (
    <div className={headerFieldsGrid}>
      {active.header_fields.map((spec) => {
        const fid = procurementFieldDomId(kind, documentType, `h-${spec.key}`);
        const val = String(header[spec.key] ?? "");
        const err = issueFor(clientIssues, "header", 0, null, spec.key);
        const ring = err ? "ring-1 ring-[var(--accent-red)]/40 border-[var(--accent-red)]" : "";
        const headerLocked = isPoFieldLockedFromPr(lockFromPr, kind, "header", spec.key) || formLocked;
        const label = (
          <label
            htmlFor={fid}
            className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] font-semibold leading-snug text-[var(--text-primary)]"
          >
            <span className="min-w-0">{spec.label}</span>
            {reqFor(spec, kind) ? (
              <span className="shrink-0 text-[10px] font-bold uppercase text-[var(--accent-red)]">Required</span>
            ) : null}
            <HelpIcon text={spec.help_text} />
          </label>
        );
        if (spec.widget === "textarea") {
          const clearable = fieldClearable(spec, kind, headerLocked);
          const maxLen = spec.max_length ?? undefined;
          return (
            <div key={spec.key} className={`min-w-0 ${split ? "w-full" : "sm:col-span-2 lg:col-span-3"}`}>
              {label}
              <div className="relative mt-0.5">
                <textarea
                  id={fid}
                  rows={2}
                  value={val}
                  maxLength={maxLen}
                  readOnly={headerLocked}
                  disabled={headerLocked}
                  onChange={(e) => {
                    if (headerLocked) return;
                    setHeader(spec.key, clampToMaxLength(e.target.value, spec.max_length));
                  }}
                  className={`${CTL} ${inputPadForClear(clearable)} ${ring} ${headerLocked ? "cursor-not-allowed opacity-80" : ""}`}
                />
                {clearable && val.trim() ? (
                  <FieldClearButton
                    label={spec.label}
                    className="top-2 translate-y-0"
                    onClick={() => setHeader(spec.key, "")}
                  />
                ) : null}
              </div>
              {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
            </div>
          );
        }
        if (spec.widget === "search" && spec.reference_domain) {
          const clearable = fieldClearable(spec, kind, headerLocked);
          return (
            <div key={spec.key} className={`min-w-0 ${split ? "w-full" : "sm:col-span-2"}`}>
              <SearchApiReferenceField
                fid={fid}
                labelEl={label}
                domain={spec.reference_domain}
                documentType={documentType}
                kind={kind}
                facets={{ companyCode: spec.reference_domain === "vendor" ? cocd || undefined : undefined }}
                value={val}
                onChange={(v, meta) => {
                  if (spec.key === "vendor" && !v.trim()) {
                    setForm((prev) => ({
                      ...prev,
                      header: {
                        ...(typeof prev.header === "object" && prev.header ? prev.header : {}),
                        vendor: "",
                        payment_terms: "",
                      },
                    }));
                    return;
                  }
                  if (spec.key === "vendor" && meta?.payt) {
                    setForm((prev) => {
                      const hdr = { ...(prev.header || {}) } as Record<string, unknown>;
                      const prevVal = String(hdr.vendor ?? "");
                      const prevLines = (Array.isArray(prev.lines) ? prev.lines : []) as Record<
                        string,
                        unknown
                      >[];
                      const lineCopies = prevLines.map((row) => ({
                        ...(typeof row === "object" && row ? row : {}),
                      }));
                      const cascaded = applyProcurementHeaderCascade(
                        documentType,
                        "vendor",
                        prevVal,
                        v,
                        { ...hdr, vendor: v, payment_terms: meta.payt },
                        lineCopies
                      );
                      if (cascaded) {
                        return {
                          ...prev,
                          header: { ...cascaded.header, payment_terms: meta.payt },
                          lines: cascaded.lines,
                        };
                      }
                      return {
                        ...prev,
                        header: { ...hdr, vendor: v, payment_terms: meta.payt },
                      };
                    });
                    return;
                  }
                  setHeader(spec.key, v);
                }}
                required={reqFor(spec, kind)}
                controlClass={CTL}
                autoFocus={Boolean(!prioritizeBlocksFirst && firstHeaderSearchKey === spec.key)}
                errorText={err}
                disabled={headerLocked}
                clearable={clearable}
                clearLabel={spec.label}
              />
            </div>
          );
        }
        if (spec.widget === "select" && spec.reference_domain) {
          const rawOpts = refOptions[spec.reference_domain] ?? [];
          const opts =
            spec.reference_domain === "plant" && purchasingOrgCode
              ? filterPlantsForOrg(rawOpts, purchasingOrgCode)
              : rawOpts;
          const loading = Boolean(refLoading[spec.reference_domain] && opts.length === 0);
          const slocNeedsPlant =
            spec.reference_domain === "storage_location" && !headerPlant;
          const clearable = fieldClearable(spec, kind, headerLocked || slocNeedsPlant);
          return (
            <div
              key={spec.key}
              className={split ? "min-w-0 w-full" : spec.key === "purchasing_org" ? "min-w-0 sm:col-span-2" : "min-w-0"}
            >
              <StaticReferenceCombobox
                fid={fid}
                labelEl={label}
                options={opts}
                value={val}
                onChange={(v) => setHeader(spec.key, v)}
                required={reqFor(spec, kind)}
                controlClass={CTL}
                loading={loading}
                errorText={err}
                disabled={headerLocked || slocNeedsPlant}
                placeholder={
                  slocNeedsPlant ? "Select plant first" : undefined
                }
                clearable={clearable}
                clearLabel={spec.label}
              />
            </div>
          );
        }
        return (
          <div key={spec.key} className={`min-w-0 ${split ? "w-full" : ""}`}>
            {label}
            <input
              id={fid}
              value={val}
              maxLength={spec.max_length ?? undefined}
              readOnly={headerLocked}
              disabled={headerLocked}
              onChange={(e) => {
                if (headerLocked) return;
                setHeader(spec.key, clampToMaxLength(e.target.value, spec.max_length));
              }}
              className={`${CTL} mt-1 ${ring} ${headerLocked ? "cursor-not-allowed opacity-80" : ""}`}
            />
            {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
          </div>
        );
      })}
    </div>
  );

  const headerSection = (
    <section className={`isolate rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${sectionPad}`}>
      <h2 className="text-sm font-semibold text-[var(--text-primary)]">Who buys it and where it goes</h2>
      <p
        className={`mt-1 text-[11px] leading-relaxed text-[var(--text-secondary)] ${
          density === "compact" ? "mb-2.5" : "mb-4"
        }`}
      >
        {lockFromPr
          ? "Organisation, plant, and storage come from the linked purchase request and cannot be changed here."
          : "Organisation, plant, and storage apply to the whole document. Open each list and type a few letters to narrow choices."}
      </p>
      {renderHeaderFields()}
    </section>
  );

  const blocksSection = (
    <>
      {lines.map((row, bi) => {
        const groupTaxHint = yserPoTaxHintByFirstLine.get(bi);
        return (
        <section
          key={lineKeys[bi] ?? `line-${bi}`}
          className={`rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm ${sectionPad}`}
        >
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                Line {bi + 1} — {active.code.toUpperCase() === "YSER" ? "Service" : "Material"}
              </h2>
              <p className="text-[11px] text-[var(--text-muted)]">What you need, price, then how cost is split.</p>
            </div>
            <div className="flex gap-2">
              {!editorLocked ? (
                <button
                  type="button"
                  onClick={() => addAlloc(bi)}
                  className="text-[11px] font-semibold text-[var(--accent-blue)] hover:underline"
                >
                  {active.code.toUpperCase() === "YAST" ? "+ Another asset" : "+ Another cost centre"}
                </button>
              ) : null}
              {lines.length > 1 && !formLocked ? (
                <button
                  type="button"
                  onClick={() => removeBlock(bi)}
                  className="text-[11px] font-semibold text-[var(--accent-red)] hover:underline"
                >
                  Remove this line
                </button>
              ) : null}
            </div>
          </div>
          {groupTaxHint ? <YserPoGroupTaxBanner hint={groupTaxHint} /> : null}
          <div className={blockFieldsGrid}>
            {active.block_fields.map((spec) => {
              if (spec.ui_visible === false) return null;
              // Line-level asset is mirrored from the first allocation; only edit on splits.
              if (spec.key === "asset" && active.code.toUpperCase() === "YAST") return null;
              if (spec.key === "material" && active.code.toUpperCase() === "YSER") return null;
              if (spec.key === "service" && active.code.toUpperCase() !== "YSER") return null;
              const blockLocked = isPoFieldLockedFromPr(lockFromPr, kind, "block", spec.key) || formLocked;
              const fid = procurementFieldDomId(kind, documentType, `B${bi}-b-${spec.key}`);
              const grpKey = catalogGroupField(active.code);
              const val =
                spec.key === grpKey
                  ? lineCatalogGroup(row, (form.header || {}) as Record<string, unknown>, active.code)
                  : String(row[spec.key] ?? "");
              const err = issueFor(clientIssues, "block", bi, null, spec.key);
              const ring = err ? "ring-1 ring-[var(--accent-red)]/40 border-[var(--accent-red)]" : "";
              const label = (
                <label
                  htmlFor={fid}
                  className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] font-semibold leading-snug text-[var(--text-primary)]"
                >
                  <span className="min-w-0">{spec.label}</span>
                  {showFieldRequired(spec, kind) ? (
                    <span className="shrink-0 text-[10px] font-bold uppercase text-[var(--accent-red)]">Required</span>
                  ) : null}
                  <HelpIcon text={spec.help_text} />
                </label>
              );
              if (spec.widget === "textarea") {
                const readOnlyDesc = spec.key === "short_text" && blockLocked;
                return (
                  <div key={`${bi}-b-${spec.key}`} className="min-w-0 sm:col-span-2">
                    {label}
                    <textarea
                      id={fid}
                      rows={2}
                      value={val}
                      readOnly={readOnlyDesc}
                      disabled={readOnlyDesc}
                      onChange={(e) => {
                        if (readOnlyDesc) return;
                        setBlockField(bi, spec.key, e.target.value);
                      }}
                      className={`${CTL} mt-0.5 ${ring} ${readOnlyDesc ? "cursor-not-allowed opacity-80" : ""}`}
                    />
                    {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
                  </div>
                );
              }
              if (spec.widget === "search" && spec.reference_domain) {
                const clearable = fieldClearable(spec, kind, blockLocked);
                const isCatalogLine = spec.key === "material" || spec.key === "service";
                const catalogBlocked =
                  isCatalogLine &&
                  catalogLineSearchBlocked(spec.key, documentType, row, header);
                const catalogName = String(row.short_text ?? "").trim();
                const lineGroup = lineCatalogGroup(row, header, documentType);
                return (
                  <div key={`${bi}-b-${spec.key}`} className="min-w-0 sm:col-span-2">
                    <SearchApiReferenceField
                      fid={fid}
                      labelEl={label}
                      domain={spec.reference_domain}
                      documentType={documentType}
                      kind={kind}
                      facets={{
                        companyCode: cocd || undefined,
                        materialGroup:
                          spec.key === "material"
                            ? lineGroup || undefined
                            : undefined,
                        serviceGroup:
                          spec.key === "service" ? lineGroup || undefined : undefined,
                      }}
                      value={val}
                      onChange={(v, meta) =>
                        setBlockField(
                          bi,
                          spec.key,
                          v,
                          meta?.description !== undefined || meta?.label !== undefined
                            ? { catalogDescription: meta.description ?? meta.label }
                            : undefined
                        )
                      }
                      required={reqFor(spec, kind)}
                      controlClass={CTL}
                      autoFocus={Boolean(
                        prioritizeBlocksFirst && firstSearchKey === spec.key && bi === 0 && !catalogBlocked
                      )}
                      errorText={err}
                      disabled={blockLocked || catalogBlocked}
                      placeholder={catalogBlocked ? catalogLineSearchPlaceholder(spec.key) : undefined}
                      clearable={clearable && !catalogBlocked}
                      clearLabel={spec.label}
                      catalogFooter={
                        isCatalogLine && val.trim()
                          ? { name: catalogName, code: val }
                          : undefined
                      }
                    />
                  </div>
                );
              }
              if (spec.widget === "select" && spec.reference_domain) {
                const opts = refOptions[spec.reference_domain] ?? [];
                const loading = Boolean(refLoading[spec.reference_domain] && opts.length === 0);
                const clearable = fieldClearable(spec, kind, blockLocked);
                return (
                  <div key={`${bi}-b-${spec.key}`} className="min-w-0">
                    <StaticReferenceCombobox
                      fid={fid}
                      labelEl={label}
                      options={opts}
                      value={val}
                      onChange={(v) => setBlockField(bi, spec.key, v)}
                      required={reqFor(spec, kind)}
                      controlClass={CTL}
                      loading={loading}
                      errorText={err}
                      disabled={blockLocked}
                      clearable={clearable}
                      clearLabel={spec.label}
                    />
                  </div>
                );
              }
              const clearable = fieldClearable(spec, kind, blockLocked);
              const computedValuation = isComputedValuationField(spec);
              if (computedValuation) {
                const displayId = `${fid}-display`;
                return (
                  <div key={`${bi}-b-${spec.key}`} className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] font-semibold leading-snug text-[var(--text-primary)]">
                      <span className="min-w-0">{spec.label}</span>
                      {showFieldRequired(spec, kind) ? (
                        <span className="shrink-0 text-[10px] font-bold uppercase text-[var(--accent-red)]">Required</span>
                      ) : null}
                      <HelpIcon text={spec.help_text} />
                    </div>
                    <p
                      id={displayId}
                      className={`mt-1 text-[15px] font-semibold tabular-nums leading-snug text-[var(--text-primary)] ${ring}`}
                      aria-live="polite"
                    >
                      {val.trim() ? val : "—"}
                    </p>
                    <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                      Calculated from unit price × allocation quantities.
                    </p>
                    {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
                  </div>
                );
              }
              const inputLocked = blockLocked;
              const blurKey = `b-${bi}-${spec.key}`;
              const blurCheck =
                spec.widget === "date"
                  ? { kind: "date" as const, value: val, required: reqFor(spec, kind) }
                  : spec.key === "unit_price"
                    ? { kind: "positiveDecimal" as const, value: val, required: reqFor(spec, kind) }
                    : null;
              const showValid =
                !inputLocked &&
                !err &&
                blurValid[blurKey] &&
                blurCheck &&
                shouldShowValidOnBlur(blurCheck);
              const clearableBlock = clearable && val.trim();
              return (
                <div key={`${bi}-b-${spec.key}`} className="min-w-0">
                  {label}
                  <div className="relative mt-0.5">
                    <input
                      id={fid}
                      type={spec.widget === "date" ? "date" : spec.widget === "number" ? "text" : "text"}
                      inputMode={spec.widget === "number" ? "decimal" : undefined}
                      value={val}
                      readOnly={inputLocked}
                      disabled={inputLocked}
                      onChange={(e) => {
                        if (inputLocked) return;
                        clearBlurValid(blurKey);
                        setBlockField(bi, spec.key, e.target.value);
                        if (spec.key === "unit_price") suggestValuation(bi);
                      }}
                      onBlur={() => {
                        if (!blurCheck) return;
                        markBlurValid(blurKey, shouldShowValidOnBlur(blurCheck));
                      }}
                      className={`${CTL} w-full ${inputPadForAffordances(Boolean(clearableBlock), Boolean(showValid))} ${ring} ${inputLocked ? "cursor-not-allowed opacity-80" : ""}`}
                    />
                    {clearableBlock || showValid ? (
                      <FieldAffordanceTray
                        showValid={Boolean(showValid)}
                        showClear={Boolean(clearableBlock)}
                        clearLabel={spec.label}
                        onClear={() => {
                          clearBlurValid(blurKey);
                          setBlockField(bi, spec.key, "");
                          if (spec.key === "unit_price") suggestValuation(bi);
                        }}
                      />
                    ) : null}
                  </div>
                  {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
                </div>
              );
            })}
          </div>

          <div
            className={`border-t border-[var(--border)] ${
              density === "compact" ? "mt-3 space-y-2 pt-3" : "mt-4 space-y-3 pt-4"
            }`}
          >
            {lockFromPr ? (
              <p className="mb-2 text-[11px] leading-relaxed text-[var(--text-secondary)]">
                Material, organisation, plant, and{" "}
                {active.code.toUpperCase() === "YAST" ? "assets" : "cost centres"} come from the linked
                purchase request. You can still change supplier, quantities, price, tax, delivery date,
                and header note on the order.
              </p>
            ) : null}
            {(Array.isArray(row.allocations) ? row.allocations : []).map((alloc, ai) => {
              const a = (alloc && typeof alloc === "object" ? alloc : {}) as Record<string, unknown>;
              return (
                <div
                  key={`${bi}-${ai}`}
                  className="relative flex flex-col gap-3 rounded-lg border border-[var(--border)]/60 bg-[var(--bg-secondary)]/20 p-3"
                >
                  {allocationFields.map((spec) => {
                    const fid = procurementFieldDomId(kind, documentType, `B${bi}-a${ai}-${spec.key}`);
                    const val = String(a[spec.key] ?? "");
                    const err = issueFor(clientIssues, "allocation", bi, ai, spec.key);
                    const ring = err ? "ring-1 ring-[var(--accent-red)]/40 border-[var(--accent-red)]" : "";
                    const allocRequired = reqFor(spec, kind);
                    const label = (
                      <label
                        htmlFor={fid}
                        className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] font-semibold leading-snug text-[var(--text-primary)]"
                      >
                        <span className="min-w-0">{spec.label}</span>
                        {allocRequired ? (
                          <span className="shrink-0 text-[10px] font-bold uppercase text-[var(--accent-red)]">Required</span>
                        ) : null}
                        <HelpIcon text={spec.help_text} />
                      </label>
                    );
                    if (spec.widget === "search" && spec.reference_domain === "cost_center") {
                      if (editorLocked) {
                        return (
                          <div key={`${bi}-${ai}-${spec.key}`} className="min-w-0">
                            {label}
                            <input
                              id={fid}
                              type="text"
                              value={val}
                              readOnly
                              disabled
                              className={`${CTL} mt-0.5 cursor-not-allowed opacity-80`}
                            />
                          </div>
                        );
                      }
                      return (
                        <CostCenterField
                          key={`${bi}-${ai}-${spec.key}`}
                          fid={fid}
                          label={label}
                          documentType={documentType}
                          kind={kind}
                          purchasingOrgCode={String(header.purchasing_org ?? "").trim()}
                          plantCode={String(header.plant ?? "").trim()}
                          required={allocRequired}
                          value={val}
                          onChange={(v) => {
                            setAlloc(bi, ai, { [spec.key]: v });
                            suggestValuation(bi);
                          }}
                          errorText={err}
                        />
                      );
                    }
                    if (spec.widget === "search" && spec.reference_domain === "asset") {
                      if (editorLocked) {
                        return (
                          <div key={`${bi}-${ai}-${spec.key}`} className="min-w-0 sm:col-span-2">
                            {label}
                            <input
                              id={fid}
                              type="text"
                              value={val}
                              readOnly
                              disabled
                              className={`${CTL} mt-0.5 cursor-not-allowed opacity-80`}
                            />
                          </div>
                        );
                      }
                      return (
                        <div key={`${bi}-${ai}-${spec.key}`} className="min-w-0 sm:col-span-2">
                          <SearchApiReferenceField
                            fid={fid}
                            labelEl={label}
                            domain="asset"
                            documentType={documentType}
                            kind={kind}
                            facets={{ companyCode: cocd || undefined }}
                            value={val}
                            onChange={(v) => {
                              setAlloc(bi, ai, { [spec.key]: v });
                              suggestValuation(bi);
                            }}
                            required={allocRequired}
                            controlClass={CTL}
                            errorText={err}
                            clearable
                            clearLabel={spec.label}
                          />
                        </div>
                      );
                    }
                    const allocLocked = editorLocked;
                    const qtyField = spec.key === "qty";
                    const blurKey = `a-${bi}-${ai}-${spec.key}`;
                    const qtyCheck = qtyField
                      ? { kind: "positiveQty" as const, value: val, required: allocRequired }
                      : null;
                    const showQtyValid =
                      qtyCheck &&
                      !err &&
                      blurValid[blurKey] &&
                      shouldShowValidOnBlur(qtyCheck);
                    return (
                      <div key={`${bi}-${ai}-${spec.key}`} className={`min-w-0 w-full ${split ? "" : "max-w-[16rem]"}`}>
                        {label}
                        <div className="relative mt-0.5">
                          <input
                            id={fid}
                            type="text"
                            inputMode={qtyField ? "decimal" : undefined}
                            value={val}
                            readOnly={allocLocked}
                            disabled={allocLocked}
                            aria-required={allocRequired}
                            aria-invalid={Boolean(err)}
                            onChange={(e) => {
                              if (allocLocked) return;
                              clearBlurValid(blurKey);
                              setAlloc(bi, ai, { [spec.key]: e.target.value });
                              suggestValuation(bi);
                            }}
                            onBlur={() => {
                              if (!qtyCheck) return;
                              markBlurValid(blurKey, shouldShowValidOnBlur(qtyCheck));
                            }}
                            className={`${CTL} w-full ${inputPadForAffordances(false, Boolean(showQtyValid))} ${ring}`}
                          />
                          {showQtyValid ? <FieldAffordanceTray showValid showClear={false} /> : null}
                        </div>
                        {err ? <p className="mt-0.5 text-[11px] text-[var(--accent-red)]">{err}</p> : null}
                      </div>
                    );
                  })}
                  {!editorLocked && (row.allocations as unknown[]).length > 1 ? (
                    <div className="flex shrink-0 items-center pt-0.5">
                      <button
                        type="button"
                        onClick={() => removeAlloc(bi, ai)}
                        className="text-[11px] text-[var(--accent-red)] hover:underline"
                      >
                        Remove this split
                      </button>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </section>
        );
      })}

      {!editorLocked ? (
        <button type="button" onClick={addBlock} className="text-sm font-semibold text-[var(--accent-green)] hover:underline">
          + Add another {itemKindLabel} line
        </button>
      ) : null}
    </>
  );

  const headerRail = (
    // Deliberately **not** clipping overflow here: nested comboboxes / search dropdowns
    // render absolutely positioned children; ``overflow-y-auto`` on the sticky wrapper
    // would chop them off when they extend past the visible header. The page itself
    // scrolls when the header exceeds the viewport.
    <div className="xl:sticky xl:top-2 xl:z-[1] xl:pr-1">
      {headerSection}
    </div>
  );

  const blocksRail = <div className={`min-w-0 ${density === "compact" ? "space-y-2" : "space-y-3"}`}>{blocksSection}</div>;

  const failedRefDomains = Object.keys(domainLoadErrors);

  return (
    <div className={density === "compact" ? "space-y-2" : "space-y-6"}>
      {failedRefDomains.length > 0 ? (
        <ErrorBanner onRetry={retryFailedReferenceDomains} retryLabel="Retry lists">
          Some reference lists did not load ({failedRefDomains.join(", ")}). Dropdowns may stay empty until you
          retry.
        </ErrorBanner>
      ) : null}
      {split ? (
        <>
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-12 xl:items-start xl:gap-4">
            {prioritizeBlocksFirst ? (
              <>
                <div className="min-w-0 xl:col-span-7 xl:order-1">{blocksRail}</div>
                <div className="min-w-0 xl:col-span-5 xl:order-2">{headerRail}</div>
              </>
            ) : (
              <>
                <div className="min-w-0 xl:col-span-5 xl:order-1">{headerRail}</div>
                <div className="min-w-0 xl:col-span-7 xl:order-2">{blocksRail}</div>
              </>
            )}
          </div>
        </>
      ) : prioritizeBlocksFirst ? (
        <>
          {blocksSection}
          {headerSection}
        </>
      ) : (
        <>
          {headerSection}
          {blocksSection}
        </>
      )}
    </div>
  );
}

function facetOptsToRefRows(f: CostCenterFacetsResponse | null, key: "entity" | "profit_center" | "department"): RefRow[] {
  if (!f) return [];
  return f[key].map((o) => ({
    code: o.value,
    label: refRowDisplayLine({ code: o.value, label: o.label }),
  }));
}

function CostCenterField({
  fid,
  label,
  documentType,
  kind,
  purchasingOrgCode,
  plantCode = "",
  required,
  value,
  onChange,
  errorText,
}: {
  fid: string;
  label: React.ReactNode;
  documentType: string;
  kind: "PR" | "PO";
  /** Purchasing organisation from the ticket header — used as company/entity scope when it matches directory values. */
  purchasingOrgCode: string;
  /** Header plant — cost centres whose Business Area matches any storage location under this plant. */
  plantCode?: string;
  required: boolean;
  value: string;
  onChange: (v: string) => void;
  errorText?: string;
}) {
  const [facetCatalog, setFacetCatalog] = useState<CostCenterFacetsResponse | null>(null);
  const [advPc, setAdvPc] = useState("");
  const [advDept, setAdvDept] = useState("");
  /** ``<details>`` cannot use ``defaultOpen`` in React; control ``open`` so the panel starts collapsed. */
  const [facetFiltersOpen, setFacetFiltersOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getCostCenterFacetOptions(documentType, { ticketKind: kind, purchasingOrg: purchasingOrgCode })
      .then((r) => {
        if (!cancelled) setFacetCatalog(r);
      })
      .catch(() => {
        if (!cancelled) {
          setFacetCatalog({
            entity: [],
            profit_center: [],
            department: [],
            has_rich_extra: false,
            facet_scope: null,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [documentType, kind, purchasingOrgCode]);

  /** Company scope for search: server-resolved entity when using scoped facets; else legacy client match. */
  const ccEntityFromHeader = useMemo(() => {
    const sr = facetCatalog?.facet_scope?.resolved_entity?.trim();
    if (sr) return sr;
    if (facetCatalog?.facet_scope) return undefined;
    const po = purchasingOrgCode.trim();
    if (!po || !facetCatalog?.entity?.length) return undefined;
    const opts = facetCatalog.entity;
    const exact = opts.find((o) => o.value.trim() === po);
    if (exact) return exact.value.trim();
    const poLo = po.toLowerCase();
    const ci = opts.find((o) => o.value.trim().toLowerCase() === poLo);
    return ci?.value.trim() || undefined;
  }, [purchasingOrgCode, facetCatalog]);

  useEffect(() => {
    setAdvPc("");
    setAdvDept("");
  }, [purchasingOrgCode, facetCatalog?.facet_scope?.resolved_entity]);

  const plant = plantCode.trim() || undefined;

  const facets = useMemo(
    () => ({
      ccEntity: ccEntityFromHeader,
      ccProfitCenter: advPc.trim() || undefined,
      ccDepartment: advDept.trim() || undefined,
      plant,
    }),
    [ccEntityFromHeader, advPc, advDept, plant]
  );

  const pcRows = useMemo(() => facetOptsToRefRows(facetCatalog, "profit_center"), [facetCatalog]);
  const deptRows = useMemo(() => facetOptsToRefRows(facetCatalog, "department"), [facetCatalog]);

  const facetLabel = (text: string) => (
    <span className="flex items-center gap-1 text-[11px] font-semibold text-[var(--text-primary)]">{text}</span>
  );

  const scope = facetCatalog?.facet_scope;
  const needsPo = Boolean(scope?.needs_purchasing_organisation);
  const poNoEntityMatch = Boolean(scope?.no_directory_match_for_purchasing_org);

  return (
    <div className="relative z-[1] min-w-0 w-full space-y-2">
      <div className="space-y-1">{label}</div>
      <details
        className="text-[11px] text-[var(--text-muted)]"
        open={facetFiltersOpen}
        onToggle={(e) => setFacetFiltersOpen(e.currentTarget.open)}
      >
        <summary className="cursor-pointer font-medium text-[var(--accent-blue)]">
          Optional: narrow by profit centre or department
        </summary>
        <p className="mb-2 mt-1 text-[11px] leading-relaxed text-[var(--text-secondary)]">
          Profit centre and department lists only show values for your header{" "}
          <span className="font-medium text-[var(--text-primary)]">Purchasing organisation</span> when it matches a
          company in the directory. Cost centre search is also narrowed by your header{" "}
          <span className="font-medium text-[var(--text-primary)]">Plant</span> (any storage location under that plant)
          when set. Then search below and pick a cost centre from the suggestions.
        </p>
        {facetCatalog && !facetCatalog.has_rich_extra ? (
          <p className="mb-2 rounded-md border border-[var(--border)] bg-[var(--bg-elev)] px-2 py-1.5 text-[11px] text-[var(--text-secondary)]">
            These optional filters are not loaded yet. You can still search for any cost centre below. If you expect
            filters here, ask your administrator to reload the cost centre reference data.
          </p>
        ) : null}
        {facetCatalog?.has_rich_extra && needsPo ? (
          <p className="mb-2 rounded-md border border-[var(--border)] bg-[var(--bg-elev)] px-2 py-1.5 text-[11px] text-[var(--text-secondary)]">
            Choose <span className="font-medium text-[var(--text-primary)]">Purchasing organisation</span> in the
            header above to load profit centre and department choices for your company.
          </p>
        ) : null}
        {facetCatalog?.has_rich_extra && poNoEntityMatch ? (
          <p className="mb-2 rounded-md border border-[var(--border)] bg-[var(--bg-elev)] px-2 py-1.5 text-[11px] text-[var(--text-secondary)]">
            This purchasing organisation is not listed as a company on your cost centres, so profit centre and
            department filters are hidden. You can still search below, or pick a different purchasing organisation.
          </p>
        ) : null}
        <div className="mt-1 grid gap-2 sm:grid-cols-1 lg:grid-cols-2">
          <div>
            <StaticReferenceCombobox
              fid={`${fid}-facet-pc`}
              labelEl={facetLabel("Profit centre")}
              options={pcRows}
              value={advPc}
              onChange={(v) => setAdvPc(v)}
              required={false}
              controlClass={CTL}
              loading={facetCatalog === null}
              errorText={undefined}
              clearable
              clearLabel="Profit centre"
            />
          </div>
          <div>
            <StaticReferenceCombobox
              fid={`${fid}-facet-dept`}
              labelEl={facetLabel("Department")}
              options={deptRows}
              value={advDept}
              onChange={(v) => setAdvDept(v)}
              required={false}
              controlClass={CTL}
              loading={facetCatalog === null}
              errorText={undefined}
              clearable
              clearLabel="Department"
            />
          </div>
        </div>
      </details>
      <SearchApiReferenceField
        fid={fid}
        domain="cost_center"
        documentType={documentType}
        kind={kind}
        facets={facets}
        value={value}
        onChange={onChange}
        required={required}
        controlClass={CTL}
        errorText={errorText}
        placeholder="Type to search, or open the list and choose a cost centre…"
        clearable={!required}
        clearLabel="Cost center"
      />
    </div>
  );
}
