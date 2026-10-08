"use client";

import React from "react";

interface PayrollSummaryCardsProps {
  totalEntries: number;
  totalAmount: number;
  averagePayout: number;
}

export default function PayrollSummaryCards({
  totalEntries,
  totalAmount,
  averagePayout,
}: PayrollSummaryCardsProps) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm flex flex-col">
        <span className="text-[10px] uppercase font-bold tracking-wider text-[var(--text-muted)]">
          Total Entries
        </span>
        <p className="text-xl font-bold mt-1 text-[var(--text-primary)]">
          {totalEntries.toLocaleString()}
        </p>
      </div>

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm flex flex-col">
        <span className="text-[10px] uppercase font-bold tracking-wider text-[var(--text-muted)]">
          Total Amount
        </span>
        <p className="text-xl font-bold mt-1 text-[var(--accent-blue)]">
          ₹ {totalAmount.toLocaleString()}
        </p>
      </div>

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm flex flex-col">
        <span className="text-[10px] uppercase font-bold tracking-wider text-[var(--text-muted)]">
          Average Payout
        </span>
        <p className="text-xl font-bold mt-1 text-[var(--accent-green)]">
          ₹ {averagePayout.toLocaleString()}
        </p>
      </div>
    </div>
  );
}
