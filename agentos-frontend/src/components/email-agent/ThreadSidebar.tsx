"use client";

import { useEffect, useRef, useState } from "react";
import { getEmailThread, type EmailThreadMessage } from "@/lib/api";

type Props = {
  threadId: string | null;
  partyName: string;
  onClose: () => void;
  fetchFn?: (threadId: string) => Promise<{ messages: EmailThreadMessage[] }>;
};

function formatDate(dateMs: number): string {
  if (!dateMs) return "";
  return new Date(dateMs).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function senderInitials(fromAddr: string): string {
  // "Alice Smith <alice@example.com>" → "AS"
  // "alice@example.com" → "A"
  const nameMatch = fromAddr.match(/^([^<]+)</);
  const name = nameMatch ? nameMatch[1].trim() : fromAddr.split("@")[0];
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");
}

function senderDisplay(fromAddr: string): string {
  const nameMatch = fromAddr.match(/^([^<]+)</);
  return nameMatch ? nameMatch[1].trim() : fromAddr;
}

function MessageCard({ msg, defaultExpanded = false }: { msg: EmailThreadMessage; defaultExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const initials = senderInitials(msg.from_addr);
  const sender = senderDisplay(msg.from_addr);
  const isOurs = msg.from_addr.toLowerCase().includes("tata1mg") ||
    msg.from_addr.toLowerCase().includes("1mg");

  return (
    <div
      className={`rounded-xl border ${isOurs
        ? "border-[#e8604c]/30 bg-[#fff5f3]"
        : "border-[var(--border)] bg-[var(--bg-card)]"
        } shadow-sm ring-1 ring-black/[0.02]`}
    >
      {/* Header row */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-start gap-3 px-4 py-3 text-left"
        aria-expanded={expanded}
      >
        {/* Avatar */}
        <span
          className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${isOurs
            ? "bg-[#e8604c]/15 text-[#e8604c]"
            : "bg-[var(--bg-primary)] text-[var(--text-secondary)]"
            } ring-1 ring-black/[0.06]`}
          aria-hidden="true"
        >
          {initials || "?"}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[13px] font-semibold text-[var(--text-primary)]">
              {sender}
            </span>
            <span className="shrink-0 text-[11px] tabular-nums text-[var(--text-muted)]">
              {formatDate(msg.date_ms)}
            </span>
          </div>
          {!expanded && (
            <p className="mt-0.5 truncate text-[12px] text-[var(--text-secondary)]">
              {msg.body.slice(0, 100)}
            </p>
          )}
        </div>

        <span className="ml-1 mt-1 shrink-0 text-[var(--text-muted)] transition-transform duration-150"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }}>
          ▾
        </span>
      </button>

      {/* Body */}
      {expanded && (
        <div className="border-t border-[var(--border)]/60 px-4 py-3 space-y-2">
          {/* From / To / CC */}
          <div className="text-[11.5px] text-[var(--text-muted)] space-y-0.5">
            <p><span className="font-medium text-[var(--text-secondary)]">From:</span> {msg.from_addr}</p>
            <p><span className="font-medium text-[var(--text-secondary)]">To:</span> {msg.to_addr}</p>
            {msg.cc_addr && (
              <p><span className="font-medium text-[var(--text-secondary)]">Cc:</span> {msg.cc_addr}</p>
            )}
          </div>
          <div className="border-t border-[var(--border)]/40 pt-2">
            <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-[var(--text-secondary)]">
              {msg.body || <span className="italic text-[var(--text-muted)]">No body content.</span>}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

export function ThreadSidebar({ threadId, partyName, onClose, fetchFn }: Props) {
  const [messages, setMessages] = useState<EmailThreadMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Keep a stable ref to onClose so the Escape listener never needs to
  // re-register just because the parent re-rendered with a new function identity.
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  // Fetch thread messages whenever threadId changes.
  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    setMessages([]);

    const loader = fetchFn ? fetchFn(threadId) : getEmailThread(threadId);
    loader
      .then((data) => {
        if (!cancelled) {
          setMessages(data.messages);
          setLoading(false);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Could not load thread.");
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [threadId, fetchFn]);

  // Close on Escape key — registered once, reads latest onClose via ref.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCloseRef.current();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  if (!threadId) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/20 backdrop-blur-[1px]"
        aria-hidden="true"
        onClick={onClose}
      />

      {/* Panel */}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Email thread for ${partyName}`}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[560px] flex-col border-l border-[var(--border)] bg-[var(--bg-card)] shadow-2xl"
      >
        {/* Sidebar header */}
        <div className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-4 py-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
              Email Thread
            </p>
            <p className="mt-0.5 truncate text-[14px] font-semibold text-[var(--text-primary)]">
              {partyName}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close thread sidebar"
            className="ml-3 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--text-muted)] transition hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)]"
          >
            ✕
          </button>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {loading && (
            <div className="flex flex-col gap-3">
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-24 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
                />
              ))}
            </div>
          )}

          {!loading && error && (
            <div className="rounded-lg border border-red-200/90 bg-red-50/80 px-4 py-3 text-[13px] text-red-950 ring-1 ring-red-900/10">
              {error}
            </div>
          )}

          {!loading && !error && messages.length === 0 && (
            <p className="py-8 text-center text-[13px] text-[var(--text-muted)]">
              No messages found in this thread.
            </p>
          )}

          {!loading && !error && messages.length > 0 && (
            <div className="flex flex-col gap-3">
              <p className="text-[11px] tabular-nums text-[var(--text-muted)]">
                {messages.length} message{messages.length === 1 ? "" : "s"}
              </p>
              {messages.map((msg, idx) => (
                <MessageCard key={msg.message_id} msg={msg} defaultExpanded={idx === 0} />
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="shrink-0 border-t border-[var(--border)] px-4 py-3">
          <a
            href={`https://mail.google.com/mail/u/1/#all/${encodeURIComponent(threadId)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[12px] font-semibold text-[var(--accent-green)] hover:underline"
          >
            Open in Gmail ↗
          </a>
        </div>
      </div>
    </>
  );
}
