"use client";

import { useCallback, useEffect, useState } from "react";

import { ingestO2CContracts, listO2CContractReviewFailures, reingestFailedParsing } from "@/lib/api";

export function IngestTab() {
  const [failures, setFailures] = useState<Record<string, unknown>[]>([]);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const loadFailures = useCallback(async () => {
    try {
      const out = await listO2CContractReviewFailures(50, true);
      setFailures(out.items);
    } catch {
      setFailures([]);
    }
  }, []);

  useEffect(() => {
    void loadFailures();
  }, [loadFailures]);

  async function onIngest() {
    setBusy("ingest");
    setError("");
    setMessage("");
    try {
      const res = await ingestO2CContracts({});
      setMessage(`Ingest finished: ${JSON.stringify(res).slice(0, 200)}…`);
      await loadFailures();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ingest failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 space-y-3">
        <h3 className="text-sm font-semibold">Contract ingest (no MIS)</h3>
        <p className="text-xs text-[var(--text-muted)]">
          Runs PDF folder ingest only. MIS generation is separate on the OHC MIS page.
        </p>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void onIngest()}
          className="px-4 py-2 rounded-lg bg-[var(--accent-blue)] text-white text-sm font-semibold disabled:opacity-50"
        >
          {busy === "ingest" ? "Running…" : "Run contract ingest"}
        </button>
        {message && <p className="text-xs text-[var(--accent-green)]">{message}</p>}
        {error && <p className="text-xs text-red-500">{error}</p>}
      </div>

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4">
        <h3 className="text-sm font-semibold mb-2">Open parse failures</h3>
        {failures.length === 0 ? (
          <p className="text-xs text-[var(--text-muted)]">None</p>
        ) : (
          <ul className="space-y-2 max-h-[480px] overflow-auto">
            {failures.map((f, idx) => (
              <li key={String(f.id ?? idx)} className="text-xs border border-[var(--border)] rounded p-2">
                <div className="font-medium truncate">{String(f.original_filename ?? "-")}</div>
                <div className="text-[var(--text-muted)] truncate">{String(f.last_error ?? "-")}</div>
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={async () => {
                    const fid = String(f.id ?? "");
                    if (!fid) return;
                    setBusy(fid);
                    try {
                      await reingestFailedParsing(fid);
                      setMessage("Re-ingest triggered");
                      await loadFailures();
                    } catch (e) {
                      setError(e instanceof Error ? e.message : "Re-ingest failed");
                    } finally {
                      setBusy("");
                    }
                  }}
                  className="mt-1 text-[var(--accent-blue)] hover:underline disabled:opacity-50"
                >
                  Re-ingest
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
