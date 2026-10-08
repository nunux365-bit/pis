"use client";

import { useEffect, useState } from "react";
import { listIntegrations, type IntegrationDto } from "@/lib/api";

export default function IntegrationsPage() {
  const [rows, setRows] = useState<IntegrationDto[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listIntegrations()
      .then((r) => setRows(r.integrations || []))
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed"));
  }, []);

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Integration health</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Live status from PostgreSQL (updated by ops / scheduler). Qdrant vector
        index for policies.
      </p>
      {err && (
        <p className="text-sm text-[var(--accent-red)] mb-4">{err}</p>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {rows.map((int) => (
          <div
            key={int.name}
            className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5"
          >
            <div className="flex justify-between items-start mb-3">
              <div>
                <div className="font-semibold mb-1">{int.name}</div>
                <div className="text-xs text-[var(--text-muted)]">{int.type}</div>
              </div>
              <span className="text-[10px] font-semibold px-2 py-0.5 rounded bg-[rgba(16,185,129,0.15)] text-[var(--accent-green)]">
                {int.status}
              </span>
            </div>
            <div className="text-[11px] text-[var(--text-secondary)] mb-2">
              <span className="text-[var(--text-muted)]">Modules: </span>
              {int.modules || "—"}
            </div>
            <div className="text-[11px] text-[var(--text-muted)] mb-2">
              Last sync: {int.last_sync ? new Date(int.last_sync).toLocaleString() : "—"}
            </div>
            {int.last_error && (
              <div className="text-[11px] text-[var(--accent-red)] mb-2">
                {int.last_error}
              </div>
            )}
            <div className="h-1 bg-[var(--bg-secondary)] rounded overflow-hidden">
              <div
                className="h-full bg-[var(--accent-green)] rounded"
                style={{ width: `${int.health}%` }}
              />
            </div>
            <div className="text-[11px] text-[var(--text-muted)] mt-1">
              Health: {int.health}%
            </div>
          </div>
        ))}
        {rows.length === 0 && !err && (
          <p className="text-sm text-[var(--text-muted)]">Loading…</p>
        )}
      </div>
    </div>
  );
}
