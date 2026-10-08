"use client";

import { useEffect, useState } from "react";
import { getReceivableDashboard, type ReceivableDashboardSnapshot } from "@/lib/api";
import { ReceivableDashboardPanel } from "@/components/dashboard/ReceivableDashboardPanel";
import { getReceivableDashboardFullMock } from "@/lib/receivableDashboardMock";

export default function EmailAgentReceivablesPage() {
  const [recv, setRecv] = useState<ReceivableDashboardSnapshot | null>(null);
  const [recvError, setRecvError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const useFullMock =
      process.env.NODE_ENV === "development" &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("receivableMock") === "full";

    if (useFullMock) {
      setRecv(getReceivableDashboardFullMock());
      setIsLoading(false);
      return;
    }

    let c = false;
    getReceivableDashboard()
      .then((r) => {
        if (!c) { setRecv(r); setIsLoading(false); }
      })
      .catch((e) => {
        if (!c) {
          setRecv(null);
          setRecvError(
            e instanceof Error ? e.message : "Could not load receivables snapshot."
          );
          setIsLoading(false);
        }
      });
    return () => {
      c = true;
    };
  }, []);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 sm:gap-4 sm:p-4">
      <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)]/40 p-3 sm:p-4">
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
            <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
            </svg>
            Loading receivables…
          </div>
        ) : (
          <ReceivableDashboardPanel data={recv} error={recvError} />
        )}
      </section>
    </div>
  );
}
