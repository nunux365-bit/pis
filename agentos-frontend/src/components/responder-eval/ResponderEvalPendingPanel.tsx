"use client";

import Link from "next/link";
import type { ResponderEvalPendingDump } from "@/lib/responderEval";

type Props = {
  items: ResponderEvalPendingDump[];
  total: number;
  loading: boolean;
  error: string | null;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onRetry?: (chatId: string) => void;
  retryingId?: string | null;
};

function statusLabel(status: string): string {
  if (status === "waiting_chat") return "Waiting for chat close";
  if (status === "failed") return "Failed (retry)";
  if (status === "abandoned") return "Abandoned";
  if (status === "processing") return "Processing";
  if (status === "pending") return "Pending";
  return status.replaceAll("_", " ");
}

export function ResponderEvalPendingPanel({
  items,
  total,
  loading,
  error,
  page,
  pageSize,
  onPageChange,
  onRetry,
  retryingId,
}: Props) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const fromIdx = total === 0 ? 0 : page * pageSize + 1;
  const toIdx = Math.min(total, page * pageSize + items.length);
  const canRetry = (status: string) =>
    status === "failed" || status === "abandoned" || status === "waiting_chat";

  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-3 py-3 shadow-sm sm:px-4">
      <div className="mb-3">
        <h2 className="text-[13px] font-semibold text-[var(--text-primary)]">Eval queue</h2>
        <p className="text-[11px] text-[var(--text-muted)]">
          Chats in eval queue ({total} total, all time)
        </p>
      </div>
      {error ? (
        <p className="mb-2 text-sm text-red-700 " role="alert">
          {error}
        </p>
      ) : null}
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]/80">
        <table className="w-full min-w-[720px] border-collapse text-left text-[12px]">
          <caption className="sr-only">Responder eval pending queue</caption>
          <thead>
            <tr className="border-b border-[var(--border)] bg-[var(--bg-primary)]/60 text-[10px] uppercase tracking-wide text-[var(--text-muted)]">
              {["Chat", "Order", "Status", "Retries", "Last error", "Updated", ""].map((h) => (
                <th key={h || "actions"} className="px-3 py-2 font-medium" scope="col">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-sm text-[var(--text-muted)]">
                  Loading queue…
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-sm text-[var(--text-muted)]">
                  Queue is empty
                </td>
              </tr>
            ) : (
              items.map((row) => (
                <tr key={row.chat_id} className="border-b border-[var(--border)]/50 last:border-0">
                  <td className="px-3 py-2 font-mono text-[11px]">{row.chat_id}</td>
                  <td className="px-3 py-2">
                    {row.order_id ? (
                      <Link
                        href={`/order-rca?po=${encodeURIComponent(row.order_id)}`}
                        className="text-violet-700 underline-offset-2 hover:underline "
                        onClick={(e) => e.stopPropagation()}
                      >
                        {row.order_id}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-3 py-2">{statusLabel(row.eval_status)}</td>
                  <td className="px-3 py-2 tabular-nums">{row.retry_count}</td>
                  <td className="max-w-[14rem] truncate px-3 py-2 text-[var(--text-secondary)]">
                    {row.last_error ?? "—"}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 text-[var(--text-secondary)]">
                    {new Date(row.updated_at).toLocaleString(undefined, {
                      dateStyle: "short",
                      timeStyle: "short",
                    })}
                  </td>
                  <td className="px-3 py-2">
                    {onRetry && canRetry(row.eval_status) ? (
                      <button
                        type="button"
                        className="rounded-md border border-violet-500/30 px-2 py-1 text-[11px] font-medium text-violet-900 disabled:opacity-50 "
                        disabled={retryingId === row.chat_id}
                        onClick={() => onRetry(row.chat_id)}
                      >
                        {retryingId === row.chat_id ? "Retrying…" : "Retry"}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {total > pageSize ? (
        <div className="mt-3 flex items-center justify-between text-xs text-[var(--text-muted)]">
          <span>
            {total === 0 ? "0 items" : `Showing ${fromIdx}–${toIdx} of ${total}`}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md border border-[var(--border)] px-2 py-1 disabled:opacity-40"
              disabled={page <= 0}
              onClick={() => onPageChange(page - 1)}
            >
              Previous
            </button>
            <span>
              Page {page + 1} of {pageCount}
            </span>
            <button
              type="button"
              className="rounded-md border border-[var(--border)] px-2 py-1 disabled:opacity-40"
              disabled={page + 1 >= pageCount}
              onClick={() => onPageChange(page + 1)}
            >
              Next
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
