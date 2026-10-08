"use client";

import Link from "next/link";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

export type NotificationItem = {
  id: string;
  category: string;
  title: string;
  read: boolean;
  time?: string;
};

type Props = {
  open: boolean;
  onClose: () => void;
  items: NotificationItem[];
  onMarkAllRead?: () => void;
};

export function NotificationPanel({
  open,
  onClose,
  items,
  onMarkAllRead,
}: Props) {
  if (!open) return null;

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} aria-hidden />
      <div
        className="fixed top-14 right-4 w-[360px] max-h-[min(480px,70vh)] z-50 flex flex-col bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-2xl overflow-hidden"
        role="dialog"
        aria-label="Notifications"
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
          <span className="text-sm font-semibold">Notifications</span>
          <button
            type="button"
            className="text-xs text-[var(--accent-blue)] hover:underline"
            onClick={onMarkAllRead}
          >
            Mark all read
          </button>
        </div>
        <div className="overflow-y-auto flex-1">
          {items.length === 0 ? (
            <p className="p-6 text-sm text-[var(--text-muted)] text-center">
              You&apos;re all caught up
            </p>
          ) : (
            items.map((n) => (
              <div
                key={n.id}
                className={`px-4 py-3 border-b border-[var(--border)] text-sm ${
                  !n.read ? "bg-[rgba(59,130,246,0.08)]" : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  {!n.read && (
                    <span className="w-2 h-2 rounded-full bg-[var(--accent-blue)] mt-1.5 shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] mb-0.5">
                      {n.category}
                    </div>
                    <div className="text-[var(--text-primary)] leading-snug">
                      {n.title}
                    </div>
                    {n.time && (
                      <div className="text-[11px] text-[var(--text-muted)] mt-1">
                        {n.time}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
        <div className="p-3 border-t border-[var(--border)] bg-[var(--bg-secondary)]/30">
          <Link
            href={WORKFLOW_DEFAULT_PATH}
            className="text-xs text-[var(--accent-blue)] font-medium"
            onClick={onClose}
          >
            Open review queue →
          </Link>
        </div>
      </div>
    </>
  );
}
