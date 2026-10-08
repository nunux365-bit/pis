"use client";

import { useEffect, useState } from "react";
import { listAuditEntries, type AuditEntryDto } from "@/lib/api";

export default function AdminAuditPage() {
  const [entries, setEntries] = useState<AuditEntryDto[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listAuditEntries()
      .then((r) => setEntries(r.entries || []))
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed"));
  }, []);

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Audit trail</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Scoped by role: you see your actions; dept heads see their department;
        system admins see all.
      </p>
      {err && <p className="text-sm text-[var(--accent-red)] mb-4">{err}</p>}
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden">
        <div className="max-h-[70vh] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-[var(--bg-card)] border-b border-[var(--border)]">
              <tr className="text-left text-[var(--text-muted)] text-xs uppercase">
                <th className="px-4 py-3 font-medium">Time</th>
                <th className="px-4 py-3 font-medium">Action</th>
                <th className="px-4 py-3 font-medium">Resource</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((a) => (
                <tr
                  key={a.id}
                  className="border-b border-[var(--border)] last:border-0"
                >
                  <td className="px-4 py-2 text-xs text-[var(--text-muted)] whitespace-nowrap">
                    {new Date(a.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{a.action}</td>
                  <td className="px-4 py-2 text-xs">
                    {a.resource_type}
                    {a.resource_id ? ` / ${a.resource_id}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {entries.length === 0 && !err && (
          <p className="p-6 text-sm text-[var(--text-muted)]">No entries.</p>
        )}
      </div>
    </div>
  );
}
