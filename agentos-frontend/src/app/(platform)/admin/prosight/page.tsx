"use client";

import { useState, useEffect, useCallback } from "react";
import {
  prosightGetStatus,
  prosightListSnapshots,
  prosightTriggerSync,
  prosightSyncNow,
  type ProsightStatus,
  type ProsightSnapshot,
} from "@/lib/prosightApi";

function cn(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

// ─────────────────────────────────────────────────────────────────────────────
// Data Management (Sync & Snapshots)
// ─────────────────────────────────────────────────────────────────────────────

function DataManagementTab() {
  const [status, setStatus] = useState<ProsightStatus | null>(null);
  const [snapshots, setSnapshots] = useState<ProsightSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  // Announced while a sync is in flight. Separate from `success`/`error`, which
  // only ever describe a *finished* sync — see the announcer below.
  const [syncNotice, setSyncNotice] = useState("");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [statusData, snapshotsData] = await Promise.all([
        prosightGetStatus(),
        prosightListSnapshots(10),
      ]);
      setStatus(statusData);
      setSnapshots(snapshotsData.snapshots || []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleTriggerSync = async () => {
    setSyncing(true);
    setSyncNotice("Starting background sync.");
    setError(null);
    setSuccess(null);
    try {
      const result = await prosightTriggerSync();
      setSuccess(result.message || "Sync triggered in background");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to trigger sync");
    } finally {
      setSyncing(false);
      setSyncNotice("");
    }
  };

  const handleSyncNow = async () => {
    setSyncing(true);
    // This one blocks for minutes, which is the gap being closed: the button
    // disables on click and drops focus, so nothing was announced between the
    // click and the result banner.
    setSyncNotice("Sync started. This may take a few minutes.");
    setError(null);
    setSuccess(null);
    try {
      const result = await prosightSyncNow();
      if (result.status === "success") {
        setSuccess(`Sync completed. Snapshot: ${result.snapshot_date}`);
        await loadData();
      } else {
        setError(result.error || "Sync failed");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to sync");
    } finally {
      setSyncing(false);
      setSyncNotice("");
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-sm text-[var(--text-muted)]">
        Loading data...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Sync-progress announcer.

          Both sync buttons set `disabled` on click, which removes them from the
          focus order and drops focus to <body>, so the label flip to
          "Syncing..." is never announced — a screen-reader user got silence from
          the click until the result banner, which for "Sync Now" is minutes.

          Always mounted rather than conditionally rendered: a live region
          inserted at the same instant as its text is announced unreliably. It is
          the *first* child, so `space-y-6`'s `> * + *` margin never applies to
          it, and `sr-only` is absolutely positioned — so it costs no layout,
          which is what ruled out a pre-mounted container previously. */}
      <div role="status" aria-live="polite" className="sr-only">
        {syncNotice}
      </div>

      {/* Status Card */}
      <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-5">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Service Status</h3>

        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-xs text-[var(--text-muted)] w-36">Status:</span>
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-xs font-medium",
                status?.status === "active"
                  ? "bg-green-100 text-green-700"
                  : "bg-yellow-100 text-yellow-700"
              )}
            >
              {status?.status === "active" ? "Active" : "No Data"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-xs text-[var(--text-muted)] w-36">Databricks Configured:</span>
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-xs font-medium",
                status?.databricks_configured
                  ? "bg-green-100 text-green-700"
                  : "bg-red-100 text-red-700"
              )}
            >
              {status?.databricks_configured ? "Yes" : "No"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-xs text-[var(--text-muted)] w-36">Latest Snapshot:</span>
            <span className="text-xs text-[var(--text-secondary)]">
              {status?.latest_snapshot || "None"}
            </span>
          </div>
        </div>
      </div>

      {/* Sync Controls */}
      <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-5">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">
          Databricks Sync
        </h3>

        {!status?.databricks_configured ? (
          <div className="rounded-lg border border-yellow-300 bg-yellow-50 p-4">
            <p className="text-sm text-yellow-800 font-medium">Databricks Not Configured</p>
            <p className="text-xs text-yellow-700 mt-1">
              Set the following environment variables to enable sync:
            </p>
            <ul className="text-xs text-yellow-700 mt-2 space-y-1 font-mono">
              <li>PROSIGHT_DATABRICKS_HOST</li>
              <li>PROSIGHT_DATABRICKS_TOKEN</li>
              <li>PROSIGHT_DATABRICKS_WAREHOUSE_ID</li>
              <li>PROSIGHT_DATABRICKS_TABLE</li>
            </ul>
          </div>
        ) : (
          <div className="flex items-center gap-3">
            <button
              onClick={handleTriggerSync}
              disabled={syncing}
              className={cn(
                "rounded-lg border border-[var(--border)] px-4 py-2 text-sm font-medium transition-colors",
                syncing
                  ? "opacity-50 cursor-not-allowed"
                  : "hover:bg-[var(--bg-elev)] text-[var(--text-primary)]"
              )}
            >
              {syncing ? "Syncing..." : "Trigger Background Sync"}
            </button>
            <button
              onClick={handleSyncNow}
              disabled={syncing}
              className={cn(
                "rounded-lg px-4 py-2 text-sm font-medium transition-colors",
                syncing
                  ? "opacity-50 cursor-not-allowed bg-gray-200 text-gray-500"
                  : "bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green-hover)]"
              )}
            >
              {syncing ? "Syncing..." : "Sync Now (Wait)"}
            </button>
            <span className="text-xs text-[var(--text-muted)]">
              Daily auto-sync runs at {status?.sync_hour ?? 11}:00 AM IST
            </span>
          </div>
        )}
      </div>

      {/* Messages — a sync finishes with no visual focus change, so the result
          is announced rather than left for the user to notice. */}
      {success && (
        <div
          role="status"
          aria-live="polite"
          className="rounded-lg border border-green-300 bg-green-50 p-3 text-sm text-green-700"
        >
          {success}
        </div>
      )}
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700"
        >
          {error}
        </div>
      )}

      {/* Snapshots Table */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">
            Recent Snapshots ({snapshots.length})
          </h3>
          <button
            onClick={loadData}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
          >
            Refresh
          </button>
        </div>

        {snapshots.length === 0 ? (
          <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-8 text-center text-sm text-[var(--text-muted)]">
            No snapshots yet. Sync from Databricks or upload data manually.
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-[var(--border)]">
            <table className="w-full">
              <thead className="bg-[var(--bg-secondary)]">
                <tr className="text-left text-xs font-medium text-[var(--text-muted)]">
                  <th className="px-4 py-3">Snapshot Date</th>
                  <th className="px-4 py-3">Model Version</th>
                  <th className="px-4 py-3">Total Series</th>
                  <th className="px-4 py-3">Qualified Flagged</th>
                  <th className="px-4 py-3">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border)]">
                {snapshots.map((snap) => (
                  <tr
                    key={snap.id}
                    className="bg-[var(--bg-card)] transition-colors hover:bg-[var(--bg-elev)]"
                  >
                    <td className="px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
                      {snap.snapshot_date}
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {snap.model_version || "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {snap.total_series?.toLocaleString() || "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {snap.qualified_flagged?.toLocaleString() || "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                      {snap.created_at ? new Date(snap.created_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────────────

export default function ProsightAdminPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-xl font-bold text-[var(--text-primary)]">Prosight Administration</h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Databricks data sync and snapshots — Prosight is open to all 1mg users
        </p>
      </div>

      <DataManagementTab />
    </div>
  );
}
