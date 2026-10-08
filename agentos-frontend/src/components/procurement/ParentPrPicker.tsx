"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ProcurementTicket } from "@/lib/procurementApi";
import {
  downloadProcurementTicketAttachment,
  humanizeProcurementUnknownError,
  procurementAttachmentRef,
} from "@/lib/procurementApi";
import { FieldClearButton, inputPadForClear } from "@/components/procurement/ReferencePickers";

const inputFocus =
  "rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-sm outline-none transition " +
  "focus:border-[var(--border-active)] focus:ring-2 focus:ring-[var(--accent-green)]/35";

function formatUpdated(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function ticketSearchBlob(t: ProcurementTicket): string {
  const parts = [t.sap_id, t.document_type, t.kind, t.id, t.form?.header ? JSON.stringify(t.form.header) : ""];
  return parts.join(" ").toLowerCase();
}

function rankParentMatch(t: ProcurementTicket, q: string): number {
  const sap = (t.sap_id ?? "").toLowerCase();
  const blob = ticketSearchBlob(t);
  if (sap.startsWith(q)) return 0;
  if (sap.includes(q)) return 1;
  if (blob.includes(q)) return 2;
  return 3;
}

function SapIdHighlight({ sapId, query }: { sapId: string; query: string }) {
  const q = query.trim().toLowerCase();
  if (!q) {
    return <span className="font-mono text-xs font-semibold text-[var(--text-primary)]">{sapId}</span>;
  }
  const lower = sapId.toLowerCase();
  const idx = lower.indexOf(q);
  if (idx < 0) {
    return <span className="font-mono text-xs font-semibold text-[var(--text-primary)]">{sapId}</span>;
  }
  const before = sapId.slice(0, idx);
  const mid = sapId.slice(idx, idx + q.length);
  const after = sapId.slice(idx + q.length);
  return (
    <span className="font-mono text-xs font-semibold text-[var(--text-primary)]">
      {before}
      <mark className="rounded bg-[rgba(21,163,115,0.22)] px-0.5 text-[var(--text-primary)]">{mid}</mark>
      {after}
    </span>
  );
}

type Props = {
  parents: ProcurementTicket[];
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
  /** Parent list is loading from the API (input stays disabled). */
  parentsLoading?: boolean;
  /** Tighter vertical rhythm (e.g. inside a collapsed ``<details>`` panel). */
  dense?: boolean;
  /** Announced when selection changes (screen readers). */
  announceId?: string;
};

/**
 * Searchable parent PR chooser — scales better than a long native ``<select>`` and supports keyboard use.
 */
export function ParentPrPicker({
  parents,
  value,
  onChange,
  disabled = false,
  parentsLoading = false,
  dense = false,
  announceId = "parent-pr-announce",
}: Props) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(-1);
  const [downloadErr, setDownloadErr] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const blurCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return parents;
    const rows = parents.filter((t) => ticketSearchBlob(t).includes(q));
    rows.sort((a, b) => rankParentMatch(a, q) - rankParentMatch(b, q));
    return rows;
  }, [parents, query]);

  const safeHighlight = useMemo(
    () => (filtered.length === 0 || hi < 0 ? -1 : Math.min(hi, filtered.length - 1)),
    [filtered.length, hi]
  );

  const selected = useMemo(() => parents.find((p) => p.id === value) ?? null, [parents, value]);

  const selectAt = useCallback(
    (idx: number) => {
      const t = filtered[idx];
      if (!t) return;
      onChange(t.id);
      setHi(idx);
      setQuery(t.sap_id ?? "");
      setOpen(false);
      searchRef.current?.blur();
    },
    [filtered, onChange]
  );

  const cancelBlurClose = useCallback(() => {
    if (blurCloseTimer.current) {
      clearTimeout(blurCloseTimer.current);
      blurCloseTimer.current = null;
    }
  }, []);

  const scheduleClose = useCallback(() => {
    cancelBlurClose();
    blurCloseTimer.current = setTimeout(() => setOpen(false), 120);
  }, [cancelBlurClose]);

  useEffect(() => () => cancelBlurClose(), [cancelBlurClose]);

  const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      if (filtered.length === 0) return;
      setHi((h) => (h < 0 ? 0 : Math.min(filtered.length - 1, h + 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHi((h) => (h <= 0 ? 0 : h - 1));
    } else if (e.key === "Enter" && safeHighlight >= 0 && safeHighlight < filtered.length) {
      e.preventDefault();
      selectAt(safeHighlight);
    } else if (e.key === "Home") {
      e.preventDefault();
      if (filtered.length) setHi(0);
    } else if (e.key === "End") {
      e.preventDefault();
      if (filtered.length) setHi(filtered.length - 1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setHi(-1);
      setOpen(false);
    }
  };

  const panelVisible = open && !disabled;

  const activeDescendantId =
    panelVisible && safeHighlight >= 0 && safeHighlight < filtered.length
      ? `parent-pr-opt-${filtered[safeHighlight]!.id}`
      : undefined;

  useEffect(() => {
    if (!activeDescendantId || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[id="${activeDescendantId}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [activeDescendantId]);

  useEffect(() => {
    if (!value || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[id="parent-pr-opt-${value}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [value]);

  useEffect(() => {
    setDownloadErr(null);
  }, [value]);

  const qLower = query.trim().toLowerCase();

  return (
    <div className={dense ? "space-y-1.5" : "space-y-2.5"}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0 flex-1">
          <label htmlFor="parent-pr-search" className="block text-sm font-bold text-[var(--text-primary)]">
            Link a request (SAP number)
          </label>
          <p className="mt-0.5 text-[11px] leading-snug text-[var(--text-secondary)] sm:text-xs">
            Autocomplete: type a <strong className="text-[var(--text-primary)]">SAP document number</strong>.
          </p>
          {parentsLoading ? (
            <p className="mt-1 text-xs text-[var(--text-muted)]" aria-live="polite">
              Loading your SAP-linked requests…
            </p>
          ) : null}
          <div className="relative mt-2 max-w-xl">
            <input
              ref={searchRef}
              id="parent-pr-search"
              type="search"
              autoComplete="off"
              placeholder="Type SAP number — suggestions appear as you type…"
              value={query}
              disabled={disabled}
              onChange={(e) => {
                setQuery(e.target.value);
                setHi(-1);
                setOpen(true);
              }}
              onFocus={() => {
                cancelBlurClose();
                setOpen(true);
              }}
              onBlur={scheduleClose}
              onKeyDown={onSearchKeyDown}
              className={`w-full ${inputFocus} ${inputPadForClear(true)} [appearance:textfield] [&::-webkit-search-cancel-button]:appearance-none`}
              role="combobox"
              aria-expanded={panelVisible}
              aria-controls="parent-pr-listbox"
              aria-activedescendant={activeDescendantId}
              aria-autocomplete="list"
            />
            {query.trim() && !disabled ? (
              <FieldClearButton
                label="SAP number search"
                onClick={() => {
                  setQuery("");
                  setHi(-1);
                  setOpen(true);
                  searchRef.current?.focus();
                }}
              />
            ) : null}
            {panelVisible ? (
              <div
                id="parent-pr-listbox"
                ref={listRef}
                role="listbox"
                aria-label="Purchase requests with a SAP number"
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={cancelBlurClose}
                className="absolute left-0 right-0 top-full z-30 mt-1 max-h-64 overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] py-0.5 shadow-lg ring-1 ring-black/[0.06] sm:max-h-72"
              >
                {filtered.length === 0 ? (
                  <p className="px-3 py-3 text-left text-sm leading-snug text-[var(--text-secondary)]">
                    {parents.length === 0 ? (
                      <>
                        <span className="font-medium text-[var(--text-primary)]">Nothing to pick yet.</span> Create a PR
                        and wait until SAP assigns a number, then try again.
                      </>
                    ) : (
                      <>
                        <span className="font-medium text-[var(--text-primary)]">No matches.</span> Adjust the SAP
                        number or clear the field.
                      </>
                    )}
                  </p>
                ) : (
                  filtered.map((t, idx) => {
                    const active = safeHighlight === idx;
                    const isValue = t.id === value;
                    const sap = t.sap_id ?? "";
                    return (
                      <div
                        key={t.id}
                        id={`parent-pr-opt-${t.id}`}
                        role="option"
                        aria-selected={isValue}
                        data-idx={idx}
                        tabIndex={-1}
                        onMouseEnter={() => setHi(idx)}
                        onClick={() => {
                          if (!disabled) selectAt(idx);
                        }}
                        className={`flex w-full cursor-pointer flex-col gap-0.5 border-b border-[var(--border)]/80 px-3 py-2.5 text-left text-sm transition last:border-b-0 ${
                          isValue
                            ? "bg-[rgba(21,163,115,0.12)] ring-1 ring-inset ring-[var(--accent-green)]/25"
                            : active
                              ? "bg-[var(--bg-elev)]"
                              : "hover:bg-[var(--bg-elev)]/80"
                        } ${disabled ? "pointer-events-none opacity-50" : ""}`}
                      >
                        <SapIdHighlight sapId={sap} query={qLower} />
                        <span className="text-xs text-[var(--text-secondary)]">
                          {t.document_type} · {formatUpdated(t.updated_at)}
                        </span>
                        <span
                          className="font-mono text-[10px] text-[var(--text-muted)]"
                          title="AgentOS ticket id (for support)"
                        >
                          {t.id}
                        </span>
                      </div>
                    );
                  })
                )}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {selected ? (
        <div
          className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-4 py-3 text-sm shadow-sm"
          aria-live="polite"
          id={announceId}
        >
          <p className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
            Order will be linked to this request
          </p>
          <p className="mt-1 text-[var(--text-primary)]">
            <span className="font-mono font-semibold">{selected.sap_id}</span>
            <span className="text-[var(--text-muted)]"> · </span>
            {selected.document_type}
          </p>
          <p className="mt-1 text-xs text-[var(--text-secondary)]">
            Lines below are copied from this request — confirm quantities and vendor before you send the order.
          </p>
          {(() => {
            const files = (selected.attachments || []).filter(
              (a) => a && procurementAttachmentRef(a) !== ""
            );
            if (files.length === 0) {
              return (
                <p className="mt-2 border-t border-[var(--border)] pt-2 text-[11px] text-[var(--text-muted)]">
                  No files on this request.
                </p>
              );
            }
            return (
              <div className="mt-2 border-t border-[var(--border)] pt-2">
                <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                  Request files
                </p>
                {downloadErr ? (
                  <p className="mt-1 text-[11px] font-medium text-[var(--accent-red)]" role="status">
                    {downloadErr}
                  </p>
                ) : null}
                <ul className="mt-1 space-y-1">
                  {files.map((a, idx) => {
                    const fid = procurementAttachmentRef(a);
                    const nm = (a.name || "").trim() || "Attachment";
                    return (
                      <li key={fid || `f-${idx}`} className="flex min-w-0 items-center justify-between gap-2 text-xs">
                        <span className="min-w-0 truncate text-[var(--text-secondary)]" title={nm}>
                          {nm}
                        </span>
                        <button
                          type="button"
                          className="shrink-0 font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline"
                          onClick={() => {
                            setDownloadErr(null);
                            void downloadProcurementTicketAttachment(selected.id, fid, nm).catch((e) =>
                              setDownloadErr(humanizeProcurementUnknownError(e, "Could not download the file."))
                            );
                          }}
                        >
                          Download
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })()}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={disabled || !value}
          className="text-xs font-medium text-[var(--accent-blue)] hover:underline disabled:pointer-events-none disabled:opacity-40"
          onClick={() => {
            onChange(null);
            setQuery("");
            setHi(-1);
            setOpen(false);
          }}
        >
          Clear selection
        </button>
        <span className="text-xs text-[var(--text-secondary)]">
          <strong className="text-[var(--text-primary)]">Keyboard:</strong> arrows to move, Enter to select.
        </span>
      </div>
    </div>
  );
}
