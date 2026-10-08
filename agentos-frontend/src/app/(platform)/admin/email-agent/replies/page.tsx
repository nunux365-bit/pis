"use client";

/** @deprecated Tab slated for removal; use Receivables executive summary instead. */
import { useState } from "react";
import { type EmailIntelligenceWindow } from "@/lib/api";
import { CollectionsReplyIntelligencePanel } from "@/components/dashboard/CollectionsReplyIntelligencePanel";

export default function EmailAgentRepliesPage() {
  const [intelWindow, setIntelWindow] = useState<EmailIntelligenceWindow>("30d");

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 sm:gap-4 sm:p-4">
      <section className="rounded-2xl border border-[var(--border)]/90 bg-[var(--bg-secondary)]/50 p-3 shadow-sm sm:p-4 md:p-5">
        <CollectionsReplyIntelligencePanel
          timeWindow={intelWindow}
          onTimeWindowChange={setIntelWindow}
        />
      </section>
    </div>
  );
}
