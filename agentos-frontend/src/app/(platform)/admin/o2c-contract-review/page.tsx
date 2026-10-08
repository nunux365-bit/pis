"use client";

import { useCallback, useMemo, useState } from "react";

import { ClientContractsTab } from "@/components/contract-health/ClientContractsTab";
import { IngestTab } from "@/components/contract-health/IngestTab";
import { OpsSummaryTab } from "@/components/contract-health/OpsSummaryTab";
import { PeriodFields } from "@/components/contract-health/PeriodFields";
import { priorBillingMonthPeriod } from "@/lib/api";

type TabId = "ops" | "client" | "ingest";

export default function ContractHealthPage() {
  const defaultPeriod = useMemo(() => priorBillingMonthPeriod(), []);
  const [tab, setTab] = useState<TabId>("ops");
  const [periodStart, setPeriodStart] = useState(defaultPeriod.period_start);
  const [periodEnd, setPeriodEnd] = useState(defaultPeriod.period_end);
  const [clientNav, setClientNav] = useState<{ clientId?: string; ctvId?: string }>({});

  const onPeriodChange = useCallback((start: string, end: string) => {
    if (start && end && start > end) return;
    setPeriodStart(start);
    setPeriodEnd(end);
  }, []);

  const openClient = useCallback((billingClientId: string, ctvId?: string) => {
    setClientNav({ clientId: billingClientId, ctvId });
    setTab("client");
  }, []);

  const tabs: { id: TabId; label: string }[] = [
    { id: "ops", label: "Ops summary" },
    { id: "client", label: "Client & contracts" },
    { id: "ingest", label: "Ingest & issues" },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-bold mb-1">Contract Health</h1>
        <p className="text-[var(--text-secondary)] text-sm">
          Unblock MIS for the billing month: fix overlaps, then run OHC MIS. Approval happens only on MIS Save
          &amp; Approve.
        </p>
      </div>

      <PeriodFields periodStart={periodStart} periodEnd={periodEnd} onChange={onPeriodChange} />

      <div className="flex gap-2 flex-wrap border-b border-[var(--border)] pb-2">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium ${
              tab === t.id
                ? "bg-[var(--accent-green)] text-white"
                : "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "ops" && (
        <OpsSummaryTab periodStart={periodStart} periodEnd={periodEnd} onOpenClient={openClient} />
      )}
      {tab === "client" && (
        <ClientContractsTab
          periodStart={periodStart}
          periodEnd={periodEnd}
          initialClientId={clientNav.clientId}
          initialCtvId={clientNav.ctvId}
        />
      )}
      {tab === "ingest" && <IngestTab />}
    </div>
  );
}
