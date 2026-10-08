"use client";

import { useCallback, useState } from "react";
import type { ProcurementTicket } from "@/lib/procurementApi";
import {
  downloadProcurementTicketAttachment,
  humanizeProcurementUnknownError,
  procurementAttachmentRef,
} from "@/lib/procurementApi";

function formatBytes(n: number | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 102.4) / 10} KB`;
  return `${Math.round(n / (102.4 * 1024)) / 10} MB`;
}

type Props = {
  prId: string;
  attachments: ProcurementTicket["attachments"];
  /** Tighter padding when nested (e.g. under form). */
  dense?: boolean;
};

/**
 * Read-only list of files on the linked purchase request — PO does not inherit binary attachments from the PR.
 */
export function LinkedPrRequestFiles({ prId, attachments, dense = false }: Props) {
  const [downloadErr, setDownloadErr] = useState<string | null>(null);
  const rows = (attachments || []).filter((a) => a && procurementAttachmentRef(a) !== "");

  const onDownload = useCallback(
    async (attachmentId: string, name: string) => {
      setDownloadErr(null);
      try {
        await downloadProcurementTicketAttachment(prId, attachmentId, name || "download");
      } catch (e) {
        setDownloadErr(humanizeProcurementUnknownError(e, "Could not download the file."));
      }
    },
    [prId]
  );

  if (rows.length === 0) {
    return (
      <div
        className={`rounded-lg border border-dashed border-[var(--border)] bg-[var(--bg-secondary)]/40 text-[11px] text-[var(--text-muted)] sm:text-xs ${dense ? "px-2.5 py-2" : "px-3 py-2.5"}`}
      >
        No files on the linked request.
      </div>
    );
  }

  return (
    <div
      className={`rounded-lg border border-[var(--border)] bg-[var(--bg-card)] ${dense ? "px-2.5 py-2" : "px-3 py-2.5"}`}
    >
      <p className="text-[11px] font-semibold text-[var(--text-primary)] sm:text-xs">Files on the linked request</p>
      <p className="mt-0.5 text-[10px] leading-snug text-[var(--text-muted)] sm:text-[11px]">
        Reference only — not copied to this order. Attach files below if the PO needs them.
      </p>
      {downloadErr ? (
        <p className="mt-1.5 text-[11px] font-medium text-[var(--accent-red)]" role="status">
          {downloadErr}
        </p>
      ) : null}
      <ul className={`mt-2 space-y-1.5 ${dense ? "" : "sm:space-y-2"}`}>
        {rows.map((a, idx) => {
          const id = procurementAttachmentRef(a);
          const name = (a.name || "").trim() || "Attachment";
          const sz = formatBytes(a.size);
          return (
            <li
              key={id || `att-${idx}`}
              className="flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 border-b border-[var(--border)]/70 pb-1.5 text-[11px] last:border-b-0 last:pb-0 sm:text-xs"
            >
              <span className="min-w-0 flex-1 truncate font-medium text-[var(--text-primary)]" title={name}>
                {name}
                {sz ? <span className="ml-1.5 font-normal text-[var(--text-muted)]">({sz})</span> : null}
              </span>
              <button
                type="button"
                className="shrink-0 font-semibold text-[var(--accent-blue)] underline-offset-2 hover:underline"
                onClick={() => void onDownload(id, name)}
              >
                Download
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
