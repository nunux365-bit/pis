"use client";

import { useRef, useState } from "react";
import { sbiBatchRerun, sbiUploadFileChunked } from "@/lib/api";

type Kind = "raw" | "ahc" | "wallet_checker";

// ── Per-slot state ───────────────────────────────────────────────────────────

interface SlotState {
  file: File | null;
  uploading: boolean;
  progress: number;
  stage: string;
  done: boolean;
  error: string | null;
}

const emptySlot = (): SlotState => ({
  file: null, uploading: false, progress: 0,
  stage: "", done: false, error: null,
});

// ── Single upload slot ───────────────────────────────────────────────────────

function UploadSlot({
  kind,
  label,
  badge,
  description,
  accept,
  month,
  state,
  onChange,
  onUpload,
}: {
  kind: Kind;
  label: string;
  badge: "required" | "optional";
  description: string;
  accept: string;
  month: string;
  state: SlotState;
  onChange: (file: File | null) => void;
  onUpload: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) onChange(f);
  };

  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] p-5 space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2">
        <p className="text-sm font-semibold text-[var(--text-primary)]">{label}</p>
        <span
          className={[
            "text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full",
            badge === "required"
              ? "bg-amber-100 text-amber-700"
              : "bg-[var(--bg-elev)] text-[var(--text-muted)]",
          ].join(" ")}
        >
          {badge}
        </span>
      </div>
      <p className="text-xs text-[var(--text-muted)]">{description}</p>

      {/* Drop zone */}
      {!state.done && (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => !state.uploading && inputRef.current?.click()}
          className={[
            "rounded-lg border-2 border-dashed px-4 py-6 text-center cursor-pointer transition-colors select-none",
            dragging
              ? "border-[var(--accent-green)] bg-[var(--accent-green)]/5"
              : state.file
              ? "border-[var(--accent-green)]/50 bg-[var(--accent-green)]/5"
              : "border-[var(--border)] hover:border-[var(--accent-green)]/40 hover:bg-[var(--bg-elev)]",
            state.uploading ? "pointer-events-none opacity-60" : "",
          ].join(" ")}
        >
          {state.file ? (
            <p className="text-xs text-[var(--text-primary)] font-medium truncate px-2">
              {state.file.name}
            </p>
          ) : (
            <p className="text-xs text-[var(--text-muted)]">
              Drop xlsx here or{" "}
              <span className="text-[var(--accent-green)] underline underline-offset-2">browse</span>
            </p>
          )}
          <input
            ref={inputRef}
            type="file"
            accept={accept}
            className="hidden"
            onChange={(e) => onChange(e.target.files?.[0] ?? null)}
          />
        </div>
      )}

      {/* Success state */}
      {state.done && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 flex items-center gap-2">
          <span className="text-emerald-600 text-base">✓</span>
          <p className="text-xs text-emerald-700 font-medium">
            Uploaded successfully — pipeline running
          </p>
        </div>
      )}

      {/* Progress */}
      {state.uploading && (
        <div className="space-y-1">
          <div className="h-1.5 bg-[var(--bg-elev)] rounded-full overflow-hidden">
            <div
              className="h-full bg-[var(--accent-green)] transition-all duration-200"
              style={{ width: `${Math.round(state.progress * 100)}%` }}
            />
          </div>
          <p className="text-xs text-[var(--text-muted)]">
            {state.stage || "Uploading…"} — {Math.round(state.progress * 100)}%
          </p>
        </div>
      )}

      {/* Error */}
      {state.error && (
        <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">
          {state.error}
        </div>
      )}

      {/* Upload button */}
      {!state.done && (
        <div className="flex justify-end">
          <button
            onClick={onUpload}
            disabled={!state.file || !month || state.uploading}
            className="px-4 py-1.5 rounded bg-[var(--accent-green)] text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90 transition-opacity"
          >
            {state.uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── Modal ────────────────────────────────────────────────────────────────────

interface ModalProps {
  onDone: () => void;
  onCancel: () => void;
  /** Pre-fill the month field with the currently active run month so that
   *  supplementary uploads (AHC, PF Summary) always land on the right month. */
  initialMonth?: string | null;
}

export function SbiMisUploadModal({ onDone, onCancel, initialMonth }: ModalProps) {
  const [month, setMonth] = useState(() => {
    if (initialMonth) return initialMonth;
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  });

  const [raw, setRaw] = useState<SlotState>(emptySlot);
  const [ahc, setAhc] = useState<SlotState>(emptySlot);
  const [wc, setWc]   = useState<SlotState>(emptySlot);
  const [batchUploading, setBatchUploading] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);

  const uploadSlot = async (
    kind: Kind,
    state: SlotState,
    setState: React.Dispatch<React.SetStateAction<SlotState>>,
    skipRerun = false,
  ) => {
    if (!state.file || !month) return;
    setState((s) => ({ ...s, uploading: true, error: null, progress: 0 }));
    try {
      await sbiUploadFileChunked(
        state.file,
        month,
        kind,
        (p) => setState((s) => ({ ...s, progress: p })),
        skipRerun,
      );
      setState((s) => ({ ...s, uploading: false, done: true }));
      if (!skipRerun) onDone();
    } catch (e) {
      setState((s) => ({ ...s, uploading: false, error: String(e) }));
      throw e;
    }
  };

  const handleUploadAll = async () => {
    const slots: Array<{ kind: Kind; state: SlotState; setState: React.Dispatch<React.SetStateAction<SlotState>> }> = [];
    if (raw.file && !raw.done) slots.push({ kind: "raw", state: raw, setState: setRaw });
    if (ahc.file && !ahc.done) slots.push({ kind: "ahc", state: ahc, setState: setAhc });
    if (wc.file  && !wc.done)  slots.push({ kind: "wallet_checker", state: wc, setState: setWc });
    if (slots.length === 0) return;

    setBatchUploading(true);
    setBatchError(null);
    try {
      // Upload all files in parallel with pipeline rerun suppressed.
      // A single combined rerun is triggered after all uploads complete.
      await Promise.all(
        slots.map(({ kind, state, setState }) =>
          uploadSlot(kind, state, setState, /* skipRerun */ true)
        )
      );
      await sbiBatchRerun(month, slots.map((s) => s.kind));
      onDone();
    } catch (e) {
      setBatchError(String(e));
    } finally {
      setBatchUploading(false);
    }
  };

  const anyUploading = raw.uploading || ahc.uploading || wc.uploading || batchUploading;
  const anyPendingFile = (raw.file && !raw.done) || (ahc.file && !ahc.done) || (wc.file && !wc.done);
  const multipleFiles = [raw.file && !raw.done, ahc.file && !ahc.done, wc.file && !wc.done].filter(Boolean).length >= 2;

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-[var(--bg-card)] rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-4 border-b border-[var(--border)] shrink-0">
          <div>
            <h2 className="font-semibold text-[var(--text-primary)]">
              Upload files for {month
                ? new Date(month + "-01").toLocaleDateString("en-US", { month: "long", year: "numeric" })
                : "…"}
            </h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">
              Raw dump is required. AHC and Wallet Checker are optional — upload anytime.
              If the raw dump contains a PF Summary sheet, historicals are seeded automatically.
            </p>
          </div>
          <button
            onClick={onCancel}
            disabled={anyUploading}
            className="ml-4 shrink-0 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-50 px-3 py-1.5 rounded hover:bg-[var(--bg-elev)] transition-colors"
          >
            Cancel
          </button>
        </div>

        {/* Scrollable body */}
        <div className="overflow-y-auto flex-1 px-6 py-4 space-y-4">

          {/* Month */}
          <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-5 py-4">
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-2">
              Month
            </label>
            <input
              type="month"
              value={month}
              onChange={(e) => setMonth(e.target.value)}
              disabled={anyUploading}
              className="border border-[var(--border)] rounded px-3 py-1.5 text-sm bg-[var(--bg-card)] text-[var(--text-primary)] disabled:opacity-50"
            />
          </div>

          <UploadSlot
            kind="raw"
            label="Raw data dump"
            badge="required"
            description="Metabase export / query result (.xlsx or .csv). Required — drives the pipeline."
            accept=".xlsx,.xlsm,.csv,.tsv"
            month={month}
            state={raw}
            onChange={(f) => setRaw((s) => ({ ...s, file: f, error: null }))}
            onUpload={() => uploadSlot("raw", raw, setRaw)}
          />

          <UploadSlot
            kind="ahc"
            label="AHC"
            badge="optional"
            description="AHC data file — used to calculate AHC amounts per PF (.xlsx or .csv)."
            accept=".xlsx,.xlsm,.csv,.tsv"
            month={month}
            state={ahc}
            onChange={(f) => setAhc((s) => ({ ...s, file: f, error: null }))}
            onUpload={() => uploadSlot("ahc", ahc, setAhc)}
          />

          <UploadSlot
            kind="wallet_checker"
            label="Wallet Checker"
            badge="optional"
            description="Metabase wallet checker export — provides service limit per PF for PF Summary Wallet Balance column (.xlsx or .csv)."
            accept=".xlsx,.xlsm,.csv,.tsv"
            month={month}
            state={wc}
            onChange={(f) => setWc((s) => ({ ...s, file: f, error: null }))}
            onUpload={() => uploadSlot("wallet_checker", wc, setWc)}
          />

          {/* Batch error */}
          {batchError && (
            <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">
              {batchError}
            </div>
          )}

        </div>

        {/* Footer — Upload All */}
        {anyPendingFile && (
          <div className="px-6 py-4 border-t border-[var(--border)] shrink-0 flex items-center justify-between gap-3">
            <p className="text-xs text-[var(--text-muted)]">
              {multipleFiles
                ? "Upload all selected files with a single pipeline run."
                : "Or use the Upload button above to upload individually."}
            </p>
            <button
              onClick={handleUploadAll}
              disabled={anyUploading}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded bg-[var(--accent-green)] text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90 transition-opacity whitespace-nowrap"
            >
              {batchUploading ? (
                <>
                  <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  Uploading…
                </>
              ) : (
                "Upload All"
              )}
            </button>
          </div>
        )}

      </div>
    </div>
  );
}

