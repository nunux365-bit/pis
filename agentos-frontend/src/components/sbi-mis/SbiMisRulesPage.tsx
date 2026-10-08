"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  sbiGetPreview,
  sbiGetRules,
  sbiImportRules,
  SbiPreviewColumn,
  SbiPreviewGrid,
  SbiRule,
} from "@/lib/api";
import { SbiMisRuleEditor } from "./SbiMisRuleEditor";
import { SbiMisPivotPanel } from "./SbiMisPivotPanel";
import { notifySbiRerun } from "./SbiMisLeftNav";

type EditTarget = { sheet: string; col: string; header: string };

// Sheets whose rules are edited via the full pivot panel
const PIVOT_SHEETS = new Set(["Order level ", "Order level - non permissible"]);

/**
 * Convert raw config_json into a readable one-liner per rule type.
 * `col` is the rule's own column_letter — used as fallback when source is null.
 */
function formatConfig(
  ruleType: string,
  cfg: Record<string, unknown> | null | undefined,
  col?: string,
): string {
  if (!cfg) return "—";

  // On staging config_json can arrive as a serialised JSON string instead of a
  // parsed object (e.g. after a rules import stored the value as text rather than JSONB).
  // Normalise to an object before switching.
  let c: Record<string, unknown>;
  try {
    c = typeof cfg === "string"
      ? (JSON.parse(cfg as unknown as string) as Record<string, unknown>)
      : cfg;
  } catch {
    return String(cfg).slice(0, 60);
  }

  if (Object.keys(c).length === 0) return "—";

  switch (ruleType) {
    case "formula":
      return String(c.formula ?? "").replace(/\\"/g, '"') || "—";
    case "pivot_agg": {
      const agg = String(c.agg ?? "sum").toUpperCase();
      const src = String(c.agg_col ?? c.source ?? col ?? "?");
      return `${agg}(${src})`;
    }
    case "pivot_key":
    case "pivot_group_key": {
      const src = String(c.source ?? col ?? "?");
      // Production uses "key"; editor may have saved "key_index" — handle both
      const keyVal = c.key ?? c.key_index;
      const ki  = keyVal != null ? ` · key ${keyVal}` : "";
      return `group by ${src}${ki}`;
    }
    case "pivot_first":
      return `first of ${c.source ?? col ?? "?"}`;
    case "lookup":
      return `lookup: ${c.lookup_id ?? c.source ?? JSON.stringify(c)}`;
    case "counter":
      return "counter";
    case "blank":
      return "blank";
    case "raw":
      return "raw";
    default:
      return Object.entries(c)
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join(", ");
  }
}

export function SbiMisRulesPage() {
  const [rules, setRules] = useState<SbiRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [editTarget, setEditTarget] = useState<EditTarget | null>(null);
  const [filter, setFilter] = useState("");
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Preview columns for pivot sheets — loaded on demand when a pivot rule is opened
  const [pivotCols, setPivotCols] = useState<SbiPreviewColumn[] | null>(null);
  const [pivotColsSheet, setPivotColsSheet] = useState<string | null>(null);

  // When editTarget changes to a pivot sheet, fetch its preview columns
  useEffect(() => {
    if (!editTarget || !PIVOT_SHEETS.has(editTarget.sheet)) {
      setPivotCols(null);
      setPivotColsSheet(null);
      return;
    }
    if (pivotColsSheet === editTarget.sheet) return; // already loaded
    sbiGetPreview(editTarget.sheet, 0, 0)
      .then((p) => {
        setPivotCols((p as SbiPreviewGrid).columns ?? []);
        setPivotColsSheet(editTarget.sheet);
      })
      .catch(() => {
        setPivotCols([]);
        setPivotColsSheet(editTarget.sheet);
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editTarget?.sheet]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setRules(await sbiGetRules());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  // ── Export: fetch all rules → download as { "rules": [...] } .json ────────
  const handleExport = async () => {
    setExporting(true);
    try {
      const data = await sbiGetRules();
      // Wrap in production-compatible format: { "rules": [...] }
      const blob = new Blob([JSON.stringify({ rules: data }, null, 2)], { type: "application/json" });
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `sbi-rules-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  // ── Import: read file → POST to /rules/import → reload ───────────────────
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    // reset so same file can be re-selected if needed
    e.target.value = "";
    setImporting(true);
    setImportMsg(null);
    try {
      const text = await file.text();
      const parsed: unknown = JSON.parse(text);
      // Accept both production format { "rules": [...] } and bare array [...]
      let rulesArray: SbiRule[];
      if (Array.isArray(parsed)) {
        rulesArray = parsed as SbiRule[];
      } else if (
        parsed !== null &&
        typeof parsed === "object" &&
        Array.isArray((parsed as Record<string, unknown>).rules)
      ) {
        rulesArray = (parsed as { rules: SbiRule[] }).rules;
      } else {
        throw new Error("File must contain a JSON array or { \"rules\": [...] } object.");
      }
      const result = await sbiImportRules(rulesArray);
      setImportMsg({ kind: "ok", text: `${result.imported} rule${result.imported !== 1 ? "s" : ""} imported.${result.rerun_job_id ? " Pipeline rerun triggered." : ""}` });
      await reload();
    } catch (err) {
      setImportMsg({ kind: "err", text: String(err) });
    } finally {
      setImporting(false);
    }
  };

  // Group rules by sheet
  const bySheet: Record<string, SbiRule[]> = {};
  for (const r of rules) {
    if (
      filter &&
      !r.sheet.toLowerCase().includes(filter.toLowerCase()) &&
      !r.column_letter.toLowerCase().includes(filter.toLowerCase()) &&
      !r.rule_type.toLowerCase().includes(filter.toLowerCase())
    ) continue;
    (bySheet[r.sheet] = bySheet[r.sheet] || []).push(r);
  }

  return (
    <div className="p-6 space-y-4 max-w-5xl">
      {/* Header row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-base font-bold text-[var(--text-primary)]">Rules</h1>
        <div className="flex items-center gap-2 ml-auto">
          <input
            className="border border-[var(--border)] rounded px-3 py-1.5 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] w-40"
            placeholder="Filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {/* hidden file picker for import */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={handleFileChange}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--border)] text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] disabled:opacity-50 transition-colors"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M8 10V3M5 6l3-3 3 3" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 12h12" strokeLinecap="round"/>
            </svg>
            {importing ? "Importing…" : "Import .json"}
          </button>
          <button
            onClick={handleExport}
            disabled={exporting || loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--border)] text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] disabled:opacity-50 transition-colors"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M8 3v7M5 7l3 3 3-3" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 12h12" strokeLinecap="round"/>
            </svg>
            {exporting ? "Exporting…" : "Export .json"}
          </button>
        </div>
      </div>

      {/* Import result banner */}
      {importMsg && (
        <div className={[
          "rounded-lg px-4 py-2.5 text-sm flex items-center justify-between gap-3",
          importMsg.kind === "ok"
            ? "bg-emerald-50 border border-emerald-200 text-emerald-800"
            : "bg-red-50 border border-red-200 text-red-700",
        ].join(" ")}>
          <span>{importMsg.text}</span>
          <button onClick={() => setImportMsg(null)} className="text-base opacity-60 hover:opacity-100">×</button>
        </div>
      )}

      {loading && <p className="text-sm text-[var(--text-muted)]">Loading…</p>}

      {Object.entries(bySheet).map(([sheet, sheetRules]) => (
        <div key={sheet} className="rounded-xl border border-[var(--border)] overflow-hidden">
          <div className="px-4 py-2 bg-[var(--bg-elev)] border-b border-[var(--border)]">
            <p className="text-xs font-semibold text-[var(--text-primary)] uppercase tracking-wider">
              {sheet}
            </p>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--border)]">
                {["Col", "Type", "Config", "Number Format", "Status", ""].map((h) => (
                  <th key={h} className="px-4 py-2 text-left text-xs font-medium text-[var(--text-muted)]">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sheetRules.map((r) => (
                <tr key={r.column_letter} className="border-t border-[var(--border)]/40 hover:bg-[var(--bg-elev)]/50">
                  <td className="px-4 py-2 font-mono text-[var(--text-primary)]">{r.column_letter}</td>
                  <td className="px-4 py-2 text-[var(--text-secondary)]">{r.rule_type}</td>
                  <td className="px-4 py-2 text-xs text-[var(--text-secondary)] max-w-xs truncate" title={JSON.stringify(r.config_json)}>
                    {formatConfig(r.rule_type, r.config_json as Record<string, unknown>, r.column_letter)}
                  </td>
                  <td className="px-4 py-2 text-xs text-[var(--text-secondary)]">
                    {r.number_format ?? "—"}
                  </td>
                  <td className="px-4 py-2">
                    <span className={[
                      "inline-flex px-2 py-0.5 rounded text-xs font-medium",
                      r.status === "active"
                        ? "bg-green-100 text-green-700"
                        : "bg-slate-100 text-slate-600",
                    ].join(" ")}>
                      {r.status}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={() => {
                        // Reset pivot cols cache if switching sheets
                        if (pivotColsSheet !== r.sheet) setPivotCols(null);
                        setEditTarget({ sheet: r.sheet, col: r.column_letter, header: r.column_letter });
                      }}
                      className="text-xs text-[var(--accent-green)] hover:underline"
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {/* Rule editor — right-side drawer */}
      {editTarget && (() => {
        const isPivot = PIVOT_SHEETS.has(editTarget.sheet);
        // Build a SbiPreviewColumn for the highlighted column from what we know
        const matchRule = rules.find(
          (r) => r.sheet === editTarget.sheet && r.column_letter === editTarget.col,
        );
        const highlightCol: SbiPreviewColumn = {
          col: editTarget.col,
          header: editTarget.header,
          is_derived: true,
          rule_type: matchRule?.rule_type ?? null,
          // number_format comes from sbiGetRules() which merges sbi_column_formats
          number_format: matchRule?.number_format ?? null,
        };
        return (
          <div className="fixed inset-0 z-40 flex justify-end">
            <div className="absolute inset-0 bg-black/30" onClick={() => setEditTarget(null)} />
            <div className={[
              "relative z-50 bg-[var(--bg-card)] shadow-2xl flex flex-col h-full border-l border-[var(--border)]",
              isPivot ? "w-[480px]" : "w-[400px]",
              "max-w-full",
            ].join(" ")}>
              {isPivot ? (
                pivotCols === null ? (
                  <div className="flex-1 flex items-center justify-center text-sm text-[var(--text-muted)]">
                    Loading…
                  </div>
                ) : (
                  <SbiMisPivotPanel
                    sheet={editTarget.sheet}
                    highlightCol={highlightCol}
                    previewColumns={pivotCols}
                    onClose={() => setEditTarget(null)}
                    onSaved={(needsRerun) => { if (needsRerun) notifySbiRerun(); setEditTarget(null); reload(); }}
                  />
                )
              ) : (
                <SbiMisRuleEditor
                  sheet={editTarget.sheet}
                  col={editTarget.col}
                  header={editTarget.header}
                  initialNumberFormat={matchRule?.number_format ?? null}
                  onClose={() => setEditTarget(null)}
                  onSaved={(needsRerun) => { if (needsRerun) notifySbiRerun(); setEditTarget(null); reload(); }}
                />
              )}
            </div>
          </div>
        );
      })()}
    </div>
  );
}
