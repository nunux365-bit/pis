"use client";

import { removeProcurementFileAtIndex } from "@/lib/procurementApi";

type Props = {
  files: File[];
  onChange: (files: File[]) => void;
  /** Shown above the list when at least one file is staged. */
  caption?: string;
  disabled?: boolean;
};

/**
 * Staged (not yet uploaded) PR/PO attachments — one row per file with remove control.
 */
export function PendingProcurementAttachments({ files, onChange, caption, disabled = false }: Props) {
  if (files.length === 0) return null;

  return (
    <div className="mt-2">
      {caption ? <p className="mb-2 text-xs text-[var(--text-secondary)]">{caption}</p> : null}
      <ul className="space-y-2 text-sm">
        {files.map((file, idx) => (
          <li
            key={`${file.name}-${file.size}-${file.lastModified}-${idx}`}
            className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-2"
          >
            <span className="min-w-0 truncate text-[var(--text-primary)]" title={file.name}>
              {file.name}
            </span>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onChange(removeProcurementFileAtIndex(files, idx))}
              className="shrink-0 rounded p-1 text-sm font-bold leading-none text-[var(--text-muted)] hover:bg-[var(--bg-card)] hover:text-[var(--accent-red)] disabled:opacity-40"
              aria-label={`Remove ${file.name}`}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
