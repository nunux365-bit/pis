"use client";

import { useEffect, useState } from "react";
import {
  type EmailAutomationMetricsResponse,
  type EmailAutomationMetricsWindow,
  getEmailAutomationMetrics,
} from "@/lib/api";
import { EmailAutomationMetricsPanel } from "@/components/dashboard/EmailAutomationMetricsPanel";

export default function EmailAgentOperationalPage() {
  const [eaWindow, setEaWindow] = useState<EmailAutomationMetricsWindow>("24h");
  const [eaMetrics, setEaMetrics] = useState<EmailAutomationMetricsResponse | null>(null);
  const [eaError, setEaError] = useState<string | null>(null);

  useEffect(() => {
    let c = false;
    getEmailAutomationMetrics(eaWindow)
      .then((data) => {
        if (!c) {
          setEaError(null);
          setEaMetrics(data);
        }
      })
      .catch((e) => {
        if (!c) {
          setEaMetrics(null);
          setEaError(
            e instanceof Error ? e.message : "Could not load email automation metrics."
          );
        }
      });
    return () => {
      c = true;
    };
  }, [eaWindow]);

  return (
    <div className="flex min-w-0 flex-col gap-3 p-3 sm:gap-4 sm:p-4">
      {eaError && !eaMetrics && (
        <div
          className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3 text-sm text-[var(--text-primary)]"
          role="alert"
        >
          {eaError}
        </div>
      )}
      <EmailAutomationMetricsPanel
        window={eaWindow}
        onWindowChange={setEaWindow}
        metrics={eaMetrics}
        error={eaError}
      />
    </div>
  );
}
