"use client";

import { useCallback, useEffect, useState } from "react";
import { CommandBar } from "@/components/command-bar/CommandBar";
import {
  NotificationPanel,
  type NotificationItem,
} from "@/components/notification-center/NotificationPanel";
import { listNotifications, markAllNotificationsRead } from "@/lib/api";
import { formatRelativeTime } from "@/lib/time";

export function TopBar() {
  const [cmdOpen, setCmdOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);

  const loadNotifs = useCallback(async () => {
    try {
      const { notifications: rows } = await listNotifications(false);
      setNotifications(
        rows.map((n) => ({
          id: n.id,
          category: n.category,
          title: n.title,
          read: n.read,
          time: formatRelativeTime(n.created_at),
        }))
      );
    } catch {
      setNotifications([]);
    }
  }, []);

  useEffect(() => {
    if (notifOpen) loadNotifs();
  }, [notifOpen, loadNotifs]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCmdOpen((o) => !o);
        setNotifOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const unreadCount = notifications.filter((n) => !n.read).length;

  const markAllRead = useCallback(async () => {
    try {
      await markAllNotificationsRead();
      await loadNotifs();
    } catch {
      setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
    }
  }, [loadNotifs]);

  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-6">
        <button
          type="button"
          onClick={() => {
            setCmdOpen(true);
            setNotifOpen(false);
          }}
          className="flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3.5 py-2 text-left text-sm text-[var(--text-muted)] transition-colors hover:border-[var(--border-active)]/70 min-w-[240px]"
        >
          <svg
            width="18"
            height="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            viewBox="0 0 24 24"
            aria-hidden
          >
            <path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <span>Search or jump to...</span>
          <span className="ml-auto text-[11px] bg-[var(--bg-secondary)] px-1.5 py-0.5 rounded font-mono">
            ⌘K
          </span>
        </button>

        <div className="flex items-center gap-3">
          <button
            type="button"
            className="relative w-9 h-9 rounded-lg bg-[var(--bg-card)] border border-[var(--border)] flex items-center justify-center text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:border-[var(--border-active)]/70 transition-colors"
            onClick={() => {
              setNotifOpen((o) => !o);
              setCmdOpen(false);
            }}
            aria-label="Notifications"
          >
            <svg
              width="18"
              height="18"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              viewBox="0 0 24 24"
            >
              <path d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
            </svg>
            {unreadCount > 0 && (
              <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-[var(--accent-red)] border-2 border-[var(--bg-card)]" />
            )}
          </button>
        </div>
      </header>

      <CommandBar open={cmdOpen} onClose={() => setCmdOpen(false)} />
      <NotificationPanel
        open={notifOpen}
        onClose={() => setNotifOpen(false)}
        items={notifications}
        onMarkAllRead={markAllRead}
      />
    </>
  );
}
