"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { sbiGetStatus } from "@/lib/api";

const NAV_ITEMS = [
  { id: "dashboard", label: "Dashboard",     href: "/o2c/pharma-mis/sbi/dashboard" },
  { id: "working",   label: "Working sheet", href: "/o2c/pharma-mis/sbi/working" },
  { id: "rules",     label: "Rules",         href: "/o2c/pharma-mis/sbi/rules" },
  { id: "recon",     label: "Recon",         href: "/o2c/pharma-mis/sbi/recon" },
] as const;

/**
 * Custom DOM event dispatched whenever any component triggers a pipeline rerun.
 * The nav listens for this to start showing the spinner and polling — without
 * having to poll unconditionally in the background.
 */
const SBI_RERUN_EVENT = "sbi:rerun-triggered" as const;

/**
 * Call this from any component that triggers a pipeline rerun (rule save, upload,
 * run activation). The top nav will immediately show the computing spinner and
 * start polling until the pipeline finishes.
 */
export function notifySbiRerun(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(SBI_RERUN_EVENT));
  }
}

export function SbiMisTopNav() {
  const pathname = usePathname();
  const [computing, setComputing] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /** Start a 2s poll loop that stops itself once computing flips to false. */
  const startPolling = useCallback(() => {
    if (intervalRef.current !== null) return; // already running

    // Optimistically show the spinner before the first poll confirms it.
    setComputing(true);

    intervalRef.current = setInterval(async () => {
      try {
        const s = await sbiGetStatus();
        setComputing(s.computing ?? false);
        if (!s.computing) {
          clearInterval(intervalRef.current!);
          intervalRef.current = null;
        }
      } catch {
        setComputing(false);
        clearInterval(intervalRef.current!);
        intervalRef.current = null;
      }
    }, 2000);
  }, []);

  useEffect(() => {
    // Single status check on mount — if the pipeline is already running when the
    // page loads (e.g. after a refresh mid-run), start polling immediately.
    sbiGetStatus()
      .then((s) => { if (s.computing) startPolling(); })
      .catch(() => {});

    // Listen for explicit "rerun triggered" signals from other components.
    window.addEventListener(SBI_RERUN_EVENT, startPolling);

    return () => {
      window.removeEventListener(SBI_RERUN_EVENT, startPolling);
      if (intervalRef.current !== null) clearInterval(intervalRef.current);
    };
  }, [startPolling]);

  return (
    <nav className="flex items-center gap-0 px-4 border-b border-[var(--border)] bg-[var(--bg-card)] shrink-0">
      {/* Module label */}
      <span className="flex items-center gap-1.5 text-xs font-bold text-[var(--text-primary)] tracking-wide pr-4 mr-2 border-r border-[var(--border)] py-2.5 shrink-0">
        SBI MIS
        {computing && (
          <svg className="w-3 h-3 animate-spin text-[var(--accent-green)]" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3"/>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
        )}
      </span>

      {/* Tab links */}
      {NAV_ITEMS.map((item) => {
        const active = pathname.startsWith(item.href);
        return (
          <Link
            key={item.id}
            href={item.href}
            className={[
              "px-4 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap",
              active
                ? "border-[var(--accent-green)] text-[var(--accent-green)]"
                : "border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:border-[var(--border)]",
            ].join(" ")}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

// Backward-compat alias (layout.tsx still imports SbiMisLeftNav)
export { SbiMisTopNav as SbiMisLeftNav };
