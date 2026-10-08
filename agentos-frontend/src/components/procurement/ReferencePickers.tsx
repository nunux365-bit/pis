"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReferenceValueRow } from "@/lib/procurementApi";
import { searchReferenceValues } from "@/lib/procurementApi";
import { shouldShowValidOnBlur } from "@/lib/fieldBlurValidity";

export type RefRow = { code: string; label: string; description?: string; extra?: unknown };

/** One readable line for dropdowns: ``CODE — Name`` when both differ; avoids duplicate-looking labels without losing the code. */
export function refRowDisplayLine(r: RefRow): string {
  const c = String(r.code ?? "").trim();
  const l = String(r.label ?? "").trim();
  if (!c && !l) return "";
  if (!l) return c;
  if (!c) return l;
  if (l === c) return c;
  if (l.includes("—") && l.toLowerCase().startsWith(c.toLowerCase())) return l;
  const ll = l.toLowerCase();
  const cl = c.toLowerCase();
  if (ll === cl || ll.startsWith(`${cl} `) || ll.startsWith(`${cl}—`) || ll.startsWith(`${cl} -`)) return l;
  return `${c} — ${l}`;
}

/** Optional catalogue fields when picking from search (label, vendor payt, etc.). */
export type ReferencePickMeta = { label: string; description?: string; payt?: string };

export function fieldErrorRing(active: boolean): string {
  return active ? "!border-[var(--accent-red)] ring-1 ring-[var(--accent-red)]/30" : "";
}

const CLEAR_BTN =
  "flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[var(--text-muted)] transition hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]";

/** Inline clear control for optional fields — ``tabIndex={-1}`` keeps keyboard tab order lean. */
export function FieldClearButton({
  label,
  onClick,
  className,
  inline = false,
}: {
  label: string;
  onClick: () => void;
  className?: string;
  /** When true, omit absolute positioning (e.g. inside a combobox trigger). */
  inline?: boolean;
}) {
  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={`Clear ${label}`}
      onMouseDown={(e) => e.preventDefault()}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={
        inline
          ? `${CLEAR_BTN} ${className ?? ""}`
          : `absolute right-2 top-1/2 z-10 -translate-y-1/2 ${CLEAR_BTN} ${className ?? ""}`
      }
    >
      <span aria-hidden className="text-base leading-none">
        ×
      </span>
    </button>
  );
}

export function inputPadForClear(clearable: boolean): string {
  return clearable ? "pr-10" : "";
}

/** Room for inline ✓ and/or clear affordances (single tray on the right). */
export function inputPadForAffordances(clearable: boolean, valid: boolean): string {
  if (clearable && valid) return "pr-14";
  if (clearable || valid) return "pr-10";
  return "";
}

/** ✓ and × in one flex row — avoids overlap from separate absolute positions. */
export function FieldAffordanceTray({
  showValid,
  showClear,
  clearLabel = "",
  onClear,
  trayClassName = "",
}: {
  showValid: boolean;
  showClear: boolean;
  clearLabel?: string;
  onClear?: () => void;
  /** Extra classes on the absolute tray (e.g. ``right-7`` beside a combobox chevron). */
  trayClassName?: string;
}) {
  if (!showValid && !showClear) return null;

  return (
    <div
      className={`absolute right-2 top-1/2 z-10 flex -translate-y-1/2 items-center gap-1 ${trayClassName}`.trim()}
    >
      {showValid ? (
        <span
          className="pointer-events-none select-none text-sm font-bold leading-none text-[var(--accent-green)]"
          title="Looks valid"
        >
          ✓
        </span>
      ) : null}
      {showClear ? (
        <FieldClearButton label={clearLabel || "selection"} onClick={onClear ?? (() => {})} inline />
      ) : null}
    </div>
  );
}

function CatalogPickFooter({ code }: { code: string }) {
  const cd = code.trim();
  if (!cd) return null;
  return (
    <p className="mt-1 text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs" aria-live="polite">
      Code: <span className="font-mono text-[var(--text-secondary)]">{cd}</span>
    </p>
  );
}

/** Human-readable catalogue line for the closed combobox (omit bare code-only labels). */
export function catalogClosedDisplayName(rawName: string, code: string): string {
  const nm = rawName.trim();
  const cd = code.trim();
  if (!nm) return cd;
  if (!cd) return nm;
  if (nm === cd || nm.toLowerCase() === cd.toLowerCase()) return cd;
  return nm;
}

type SearchFacets = {
  companyCode?: string;
  materialGroup?: string;
  serviceGroup?: string;
  /** Cost centre directory: narrow by JSON ``extra`` (used with or without main ``q``). */
  ccEntity?: string;
  ccProfitCenter?: string;
  ccDepartment?: string;
  ccBusinessArea?: string;
  /** Cost centre: plant-union Business Area scope (any sloc under plant). */
  plant?: string;
};

function hasCostCenterFacets(f: SearchFacets | undefined): boolean {
  return Boolean(
    (f?.ccEntity || "").trim() ||
      (f?.ccProfitCenter || "").trim() ||
      (f?.ccDepartment || "").trim() ||
      (f?.ccBusinessArea || "").trim() ||
      (f?.plant || "").trim()
  );
}

/**
 * Server-backed search with dropdown results (vendor, material, service, etc.).
 * Shows human-readable label when closed; stores SAP code in the form.
 */
export function SearchApiReferenceField({
  fid,
  labelEl,
  domain,
  documentType,
  kind,
  facets,
  value,
  onChange,
  required,
  controlClass,
  autoFocus,
  errorText,
  placeholder,
  disabled = false,
  clearable = false,
  clearLabel,
  catalogFooter,
}: {
  fid: string;
  /** Omit when a visible ``<label htmlFor={fid}>`` is rendered above this control (e.g. cost centre + filters layout). */
  labelEl?: React.ReactNode;
  domain: string;
  documentType: string;
  kind: "PR" | "PO";
  facets?: SearchFacets;
  value: string;
  onChange: (v: string, meta?: ReferencePickMeta) => void;
  required: boolean;
  controlClass: string;
  autoFocus?: boolean;
  errorText?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Optional fields only — shows inline × when there is a committed value or active search text. */
  clearable?: boolean;
  clearLabel?: string;
  /** Catalogue name + code under material / service pickers. */
  catalogFooter?: { name: string; code: string };
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<RefRow[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [pickedLabel, setPickedLabel] = useState<string | null>(null);
  const [blurValid, setBlurValid] = useState(false);
  const invalid = Boolean(errorText) || Boolean(fetchError);
  const describeIds = [errorText ? `${fid}-err` : null, fetchError ? `${fid}-search-err` : null]
    .filter(Boolean)
    .join(" ");

  const companyCode =
    domain === "vendor" || domain === "asset" ? facets?.companyCode : undefined;
  const materialGroup = facets?.materialGroup;
  const serviceGroup = facets?.serviceGroup;
  const ccEntity = facets?.ccEntity?.trim() || undefined;
  const ccProfitCenter = facets?.ccProfitCenter?.trim() || undefined;
  const ccDepartment = facets?.ccDepartment?.trim() || undefined;
  const ccBusinessArea = facets?.ccBusinessArea?.trim() || undefined;
  const plant = facets?.plant?.trim() || undefined;

  const catalogFacetMissing =
    (domain === "material" && !materialGroup?.trim()) ||
    (domain === "service" && !serviceGroup?.trim());

  useEffect(() => {
    const qTrim = q.trim();
    if (domain === "asset" && !companyCode?.trim()) {
      setResults([]);
      setFetchError(null);
      setLoading(false);
      return;
    }
    if (catalogFacetMissing) {
      setResults([]);
      setFetchError(null);
      setLoading(false);
      return;
    }
    if (domain === "cost_center" && !qTrim && !hasCostCenterFacets(facets)) {
      setResults([]);
      setFetchError(null);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    const t = window.setTimeout(() => {
      void (async () => {
        setLoading(true);
        try {
          const res = await searchReferenceValues(domain, documentType, {
            ticketKind: kind,
            q: qTrim,
            companyCode: companyCode || undefined,
            materialGroup: materialGroup?.trim() || undefined,
            serviceGroup: serviceGroup?.trim() || undefined,
            ccEntity,
            ccProfitCenter,
            ccDepartment,
            ccBusinessArea,
            plant,
            limit: 25,
            offset: 0,
            signal: ac.signal,
          });
          if (ac.signal.aborted) return;
          setResults(
            res.items.map((r: ReferenceValueRow) => ({
              code: r.code,
              label: r.label,
              description: r.description,
              extra: r.extra,
            }))
          );
          setFetchError(null);
        } catch (e) {
          if (ac.signal.aborted) return;
          setResults([]);
          setFetchError("Could not reach the directory. Check your connection and try again.");
        } finally {
          if (!ac.signal.aborted) setLoading(false);
        }
      })();
    }, 200);
    return () => {
      window.clearTimeout(t);
      ac.abort();
    };
  }, [q, domain, documentType, kind, companyCode, materialGroup, serviceGroup, ccEntity, ccProfitCenter, ccDepartment, ccBusinessArea, plant, catalogFacetMissing]);

  /** Resolve label for display when ``value`` is a saved code — do not call ``onChange`` here (avoids parent updates during effects). */
  useEffect(() => {
    const code = value.trim();
    if (!code) {
      setPickedLabel(null);
      return;
    }
    if (catalogFacetMissing) {
      return;
    }
    const ac = new AbortController();
    const t = window.setTimeout(() => {
      void searchReferenceValues(domain, documentType, {
        ticketKind: kind,
        q: code,
        companyCode: companyCode || undefined,
        materialGroup: materialGroup?.trim() || undefined,
        serviceGroup: serviceGroup?.trim() || undefined,
        ccEntity,
        ccProfitCenter,
        ccDepartment,
        ccBusinessArea,
        plant,
        limit: 30,
        offset: 0,
        signal: ac.signal,
      })
        .then((res) => {
          if (ac.signal.aborted) return;
          const hit = res.items.find((r) => r.code === code);
          if (hit) setPickedLabel(hit.label);
        })
        .catch(() => {
          if (!ac.signal.aborted) setPickedLabel(null);
        });
    }, 120);
    return () => {
      window.clearTimeout(t);
      ac.abort();
    };
  }, [value, domain, documentType, kind, companyCode, materialGroup, serviceGroup, ccEntity, ccProfitCenter, ccDepartment, ccBusinessArea, plant, catalogFacetMissing]);

  const closedDisplay = catalogFooter
    ? catalogClosedDisplayName(
        (catalogFooter.name || pickedLabel || "").trim(),
        value.trim()
      )
    : pickedLabel && value.trim()
      ? pickedLabel
      : value;
  const clearName = clearLabel?.trim() || "entry";
  const showClear =
    clearable &&
    !disabled &&
    (open ? Boolean(q.trim()) : Boolean(value.trim()));
  const showValid =
    !disabled &&
    !invalid &&
    blurValid &&
    shouldShowValidOnBlur({
      kind: "picked",
      value: value.trim(),
      picked: Boolean(pickedLabel),
      required,
    });

  const handleBlur = () => {
    window.setTimeout(() => setOpen(false), 200);
    if (disabled) return;
    setBlurValid(
      shouldShowValidOnBlur({
        kind: "picked",
        value: value.trim(),
        picked: Boolean(pickedLabel),
        required,
      })
    );
  };

  const handleClear = () => {
    if (open) {
      setQ("");
      setPickedLabel(null);
    } else {
      onChange("");
      setPickedLabel(null);
      setQ("");
    }
    setBlurValid(false);
    inputRef.current?.focus();
  };
  const defaultPh =
    domain === "vendor"
      ? "Type to search vendors"
      : domain === "asset"
        ? companyCode?.trim()
          ? "Search asset number or description"
          : "Select purchasing organisation first"
        : domain === "cost_center"
          ? "Search by code or description — matching cost centres appear as you type."
          : domain === "material"
            ? materialGroup?.trim()
              ? "Search by material code or name…"
              : "Select material group in the header first"
            : domain === "service"
              ? serviceGroup?.trim()
                ? "Search by service code or name…"
                : "Select service group in the header first"
              : "Type a few letters — matching items appear below.";

  const footerCode = (catalogFooter?.code || value).trim();
  const showCatalogFooter = Boolean(
    catalogFooter &&
      footerCode &&
      !open &&
      closedDisplay.trim().toLowerCase() !== footerCode.toLowerCase()
  );

  if (disabled) {
    const disabledPh = !closedDisplay.trim() ? (placeholder ?? defaultPh) : undefined;
    return (
      <div className="relative space-y-2">
        {labelEl != null ? labelEl : null}
        <input
          id={fid}
          type="text"
          readOnly
          disabled
          value={closedDisplay}
          placeholder={disabledPh}
          className={`${controlClass} cursor-not-allowed opacity-80`}
        />
        {catalogFooter && value.trim() ? (
          <CatalogPickFooter code={footerCode || value} />
        ) : value.trim() ? (
          <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
            Code: <span className="font-mono">{value}</span>
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className={`relative space-y-2 ${open ? "z-40" : ""}`}>
      {labelEl != null ? labelEl : null}
      <div className="relative">
        <input
          ref={inputRef}
          id={fid}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          value={open ? q : closedDisplay}
          aria-required={required}
          aria-invalid={invalid}
          aria-describedby={describeIds || undefined}
          aria-busy={loading}
          aria-expanded={open}
          aria-controls={open ? `${fid}-suggest` : undefined}
          placeholder={placeholder ?? defaultPh}
          onFocus={() => {
            setOpen(true);
            setQ(value.trim() ? value : "");
          }}
          onBlur={handleBlur}
          onChange={(e) => {
            setOpen(true);
            setQ(e.target.value);
            setBlurValid(false);
            if (pickedLabel && e.target.value !== pickedLabel) setPickedLabel(null);
          }}
          className={`${controlClass} ${inputPadForAffordances(showClear, showValid)} ${fieldErrorRing(invalid)}`}
          autoFocus={autoFocus}
        />
        {showClear || showValid ? (
          <FieldAffordanceTray
            showValid={showValid}
            showClear={showClear}
            clearLabel={clearName}
            onClear={handleClear}
          />
        ) : null}
      </div>
      {showCatalogFooter ? <CatalogPickFooter code={footerCode} /> : value.trim() && !open ? (
        <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
          Saved code: <span className="font-mono">{value}</span>
        </p>
      ) : null}
      {errorText ? (
        <p id={`${fid}-err`} className="text-[11px] font-medium text-[var(--accent-red)]" role="status">
          {errorText}
        </p>
      ) : null}
      {fetchError ? (
        <p id={`${fid}-search-err`} className="text-[11px] font-medium text-[var(--accent-red)]" role="status">
          {fetchError}
        </p>
      ) : null}
      {open ? (
        <ul
          id={`${fid}-suggest`}
          className="absolute z-50 mt-1.5 max-h-56 w-full overflow-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] py-1.5 text-sm shadow-xl ring-1 ring-black/5"
          role="listbox"
          aria-label="Matching reference values"
        >
          {loading && results.length === 0 ? (
            <li className="px-3 py-2 text-xs text-[var(--text-muted)]">Searching…</li>
          ) : null}
          {results.map((r, ri) => (
            <li key={`${r.code}-${r.label}-${ri}`} role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={value === r.code}
                className="w-full px-3 py-2.5 text-left transition hover:bg-[var(--bg-elev)]"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  let meta: ReferencePickMeta | undefined;
                  if (domain === "material" || domain === "service") {
                    const desc = String(r.description ?? r.label ?? "").trim();
                    meta = { label: r.label, description: desc || undefined };
                  } else if (domain === "vendor") {
                    const ex =
                      r.extra && typeof r.extra === "object"
                        ? (r.extra as Record<string, unknown>)
                        : undefined;
                    const payt = ex?.payt != null ? String(ex.payt).trim() : "";
                    meta = { label: r.label, payt: payt || undefined };
                  }
                  onChange(r.code, meta);
                  setPickedLabel(r.label);
                  setQ("");
                  setOpen(false);
                  setBlurValid(true);
                }}
              >
                <span className="block text-[var(--text-primary)]">{refRowDisplayLine(r)}</span>
              </button>
            </li>
          ))}
          {!loading && results.length === 0 ? (
            <li className="px-3 py-2 text-xs text-[var(--text-muted)]">Nothing yet — keep typing to search.</li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

/** Filterable dropdown for static reference lists (same pattern as SAP GUI / Ariba “value help”). */
export function StaticReferenceCombobox({
  fid,
  labelEl,
  options,
  value,
  onChange,
  required,
  controlClass,
  loading,
  autoFocus,
  errorText,
  disabled = false,
  placeholder,
  clearable = false,
  clearLabel,
}: {
  fid: string;
  labelEl: React.ReactNode;
  options: RefRow[];
  value: string;
  onChange: (v: string) => void;
  required: boolean;
  controlClass: string;
  loading: boolean;
  autoFocus?: boolean;
  errorText?: string;
  disabled?: boolean;
  placeholder?: string;
  clearable?: boolean;
  clearLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [blurValid, setBlurValid] = useState(false);
  const filterRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const invalid = Boolean(errorText);

  const selected = useMemo(() => options.find((o) => o.code === value), [options, value]);
  const filtered = useMemo(() => {
    const t = q.trim().toLowerCase();
    if (!t) return options;
    return options.filter(
      (o) => o.label.toLowerCase().includes(t) || String(o.code).toLowerCase().includes(t)
    );
  }, [options, q]);

  const toggleOpen = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setQ("");
    setOpen(true);
    window.setTimeout(() => filterRef.current?.focus(), 30);
  };

  const onContainerBlur = (e: React.FocusEvent) => {
    const next = e.relatedTarget as Node | null;
    if (rootRef.current?.contains(next)) return;
    window.setTimeout(() => setOpen(false), 80);
    if (disabled) return;
    const codes = options.map((o) => String(o.code));
    setBlurValid(
      shouldShowValidOnBlur({
        kind: "staticOption",
        value: value.trim(),
        optionCodes: codes,
        required,
      })
    );
  };

  const wrapBtn = `${controlClass} flex min-h-[2.5rem] w-full items-center justify-between gap-2 text-left ${fieldErrorRing(invalid)}`;
  const clearName = clearLabel?.trim() || "selection";
  const showInlineClear = clearable && !disabled && Boolean(value.trim()) && !open;
  const optionCodes = useMemo(() => options.map((o) => String(o.code)), [options]);
  const showValid =
    !disabled &&
    !invalid &&
    blurValid &&
    shouldShowValidOnBlur({
      kind: "staticOption",
      value: value.trim(),
      optionCodes,
      required,
    });

  const showAffordances = showValid || showInlineClear;
  const comboboxTextPad =
    showValid && showInlineClear ? "pr-10" : showAffordances ? "pr-6" : "";

  if (disabled) {
    return (
      <div className="space-y-2">
        {labelEl}
        <input
          id={fid}
          type="text"
          readOnly
          disabled
          placeholder={placeholder}
          value={selected ? refRowDisplayLine(selected) : value}
          className={`${controlClass} cursor-not-allowed opacity-80`}
        />
        {value.trim() ? (
          <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
            Code: <span className="font-mono">{value}</span>
          </p>
        ) : null}
      </div>
    );
  }

  if (loading && options.length === 0) {
    return (
      <div className="space-y-2">
        {labelEl}
        <button type="button" disabled className={`${wrapBtn} cursor-wait opacity-70`} id={fid}>
          Loading list…
        </button>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="relative space-y-2" onBlur={onContainerBlur}>
      {labelEl}
      <div className="relative">
        <button
          type="button"
          id={fid}
          role="combobox"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? `${fid}-listbox` : undefined}
          aria-required={required}
          aria-invalid={invalid}
          aria-describedby={invalid ? `${fid}-err` : undefined}
          autoFocus={autoFocus}
          onClick={toggleOpen}
          className={wrapBtn}
        >
          <span
            className={`min-w-0 flex-1 whitespace-normal text-left leading-snug ${comboboxTextPad} ${
              selected ? "text-[var(--text-primary)]" : "text-[var(--text-muted)]"
            }`}
          >
            {selected ? refRowDisplayLine(selected) : "Choose from list…"}
          </span>
          <span aria-hidden className="shrink-0 pl-1 text-[var(--text-muted)]">
            ▾
          </span>
        </button>
        {showAffordances ? (
          <FieldAffordanceTray
            trayClassName="right-7"
            showValid={showValid}
            showClear={showInlineClear}
            clearLabel={clearName}
            onClear={() => {
              onChange("");
              setBlurValid(false);
            }}
          />
        ) : null}
      </div>
      {selected && !open ? (
        <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)] sm:text-xs">
          Code: <span className="font-mono">{String(selected.code).trim()}</span>
        </p>
      ) : null}
      {errorText ? (
        <p id={`${fid}-err`} className="text-[11px] font-medium text-[var(--accent-red)]" role="status">
          {errorText}
        </p>
      ) : null}
      {open ? (
        <div className="absolute left-0 right-0 top-full z-30 mt-1.5 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-2 shadow-xl ring-1 ring-black/5">
          <input
            ref={filterRef}
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Type to narrow the list…"
            className="mb-2 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-[var(--accent-green)]/25"
            aria-label="Filter list"
          />
          <ul
            id={`${fid}-listbox`}
            className="max-h-52 overflow-auto text-sm"
            role="listbox"
            aria-label="Reference values"
          >
            {!required ? (
              <li role="presentation">
                <button
                  type="button"
                  className="w-full px-2 py-2 text-left text-[var(--text-muted)] hover:bg-[var(--bg-elev)]"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onChange("");
                    setOpen(false);
                    setBlurValid(false);
                  }}
                >
                  (clear)
                </button>
              </li>
            ) : null}
            {filtered.map((o, oi) => (
              <li key={`${fid}-opt-${o.code}-${oi}`} role="presentation">
                <button
                  type="button"
                  role="option"
                  aria-selected={value === o.code}
                  className="w-full px-2 py-2 text-left hover:bg-[var(--bg-elev)]"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onChange(o.code);
                    setOpen(false);
                    setBlurValid(true);
                  }}
                >
                  <span className="block text-[var(--text-primary)]">{refRowDisplayLine(o)}</span>
                </button>
              </li>
            ))}
            {!filtered.length ? (
              <li className="px-2 py-3 text-center text-xs text-[var(--text-muted)]">No matches — try other words.</li>
            ) : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
