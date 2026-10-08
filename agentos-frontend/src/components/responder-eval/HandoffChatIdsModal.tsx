"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { HandoffChatIds } from "@/lib/responderEval";

type Props = {
  open: boolean;
  onClose: () => void;
  label: string | null;
  data: HandoffChatIds | null;
  loading: boolean;
  error: string | null;
};

export function HandoffChatIdsModal({ open, onClose, label, data, loading, error }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<Element | null>(null);
  const [copied, setCopied] = useState(false);
  const [prevData, setPrevData] = useState(data);
  if (data !== prevData) {
    setPrevData(data);
    setCopied(false);
  }

  useLayoutEffect(() => {
    if (!open) return;
    const d = dialogRef.current;
    if (!d) return;
    previouslyFocused.current = document.activeElement;
    if (!d.open) d.showModal();
    queueMicrotask(() => closeRef.current?.focus());
    return () => {
      if (d.open) d.close();
      const prev = previouslyFocused.current as HTMLElement | null;
      prev?.focus?.();
      previouslyFocused.current = null;
    };
  }, [open]);

  if (!open) return null;

  async function copyAll() {
    if (!data?.chat_ids.length) return;
    try {
      await navigator.clipboard.writeText(data.chat_ids.join("\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable — ignore, list is still selectable/copyable by hand
    }
  }

  return (
    <dialog
      ref={dialogRef}
      aria-modal="true"
      aria-label={`Chat IDs — ${label ?? ""}`}
      className="fixed inset-0 z-50 m-0 flex max-h-none w-full max-w-none items-end justify-center overflow-hidden border-0 bg-transparent p-0 sm:items-center sm:p-4 [&::backdrop]:bg-black/55"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div
        className="flex h-[min(80dvh,80vh)] max-h-[min(80dvh,80vh)] w-full max-w-md flex-col overflow-hidden rounded-t-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl sm:h-auto sm:max-h-[min(80vh,640px)] sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 border-b border-[var(--border)] px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                Chat IDs
              </p>
              <h2 className="truncate text-[14px] font-semibold text-[var(--text-primary)]">{label}</h2>
              {data ? (
                <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                  {data.total.toLocaleString()} chat{data.total === 1 ? "" : "s"} ·{" "}
                  {data.since === data.until ? data.since : `${data.since} to ${data.until}`}
                </p>
              ) : null}
            </div>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--bg-secondary)]"
            >
              Close
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {loading ? (
            <p className="py-8 text-center text-[13px] text-[var(--text-secondary)]">Loading…</p>
          ) : error ? (
            <p className="py-8 text-center text-[13px] text-[var(--accent-red)]">{error}</p>
          ) : !data || data.chat_ids.length === 0 ? (
            <p className="py-8 text-center text-[13px] text-[var(--text-secondary)]">
              No hand-off chats in this window.
            </p>
          ) : (
            <ul className="space-y-1">
              {data.chat_ids.map((id) => (
                <li
                  key={id}
                  className="rounded-md border border-[var(--border)]/70 bg-[var(--bg-elev)] px-2.5 py-1.5 font-mono text-[12px] text-[var(--text-primary)]"
                >
                  {id}
                </li>
              ))}
            </ul>
          )}
        </div>
        {data && data.chat_ids.length > 0 ? (
          <div className="shrink-0 border-t border-[var(--border)] px-4 py-2.5">
            <button
              type="button"
              onClick={() => void copyAll()}
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--bg-secondary)]"
            >
              {copied ? "Copied!" : "Copy all"}
            </button>
          </div>
        ) : null}
      </div>
    </dialog>
  );
}
