"use client";

import { useCallback, useEffect, useState } from "react";
import { format as numfmt } from "numfmt";
import {
  sbiGetPreview,
  sbiGetStatus,
  SbiPreviewColumn,
  SbiPreviewGrid,
  SbiPreviewSummary,
  SbiStatusResponse,
} from "@/lib/api";
import { SbiMisRuleEditor } from "./SbiMisRuleEditor";
import { SbiMisPivotPanel } from "./SbiMisPivotPanel";
import { notifySbiRerun } from "./SbiMisLeftNav";

/** Apply a number format string to a value, falling back to plain String() on error. */
function applyNumFmt(value: unknown, fmt: string | null | undefined): string {
  if (value == null) return "";
  if (fmt && typeof value === "number") {
    try { return numfmt(fmt, value); } catch { /* fall through */ }
  }
  return String(value);
}

// Sheets whose columns are defined by pivot (GROUP BY + agg + first-of)
const PIVOT_SHEETS = new Set<string>(["Order level ", "Order level - non permissible"]);

// Canonical sheet list matching SHEET_ORDER in format_spec
const SHEETS = [
  "Dump",
  "Order level ",
  "Order level - non permissible",
  "Summary",
  "PF Summary",
  "AHC",
] as const;

type SheetName = (typeof SHEETS)[number];

const PREVIEW_LIMIT = 100;

// Rule-type badge colours
const RULE_TYPE_COLORS: Record<string, string> = {
  raw: "bg-slate-100 text-slate-600",
  counter: "bg-violet-100 text-violet-700",
  pivot_group_key: "bg-amber-100 text-amber-700",
  pivot_key: "bg-amber-100 text-amber-700",
  pivot_first: "bg-sky-100 text-sky-700",
  pivot_agg: "bg-emerald-100 text-emerald-700",
  formula: "bg-orange-100 text-orange-700",
  direct: "bg-blue-100 text-blue-700",
  lookup: "bg-pink-100 text-pink-700",
  static: "bg-gray-100 text-gray-600",
  blank: "bg-gray-100 text-gray-400",
  filter: "bg-red-100 text-red-600",
  override: "bg-fuchsia-100 text-fuchsia-700",
  group_any: "bg-teal-100 text-teal-700",
};

function RuleTypeBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const cls = RULE_TYPE_COLORS[type] ?? "bg-gray-100 text-gray-500";
  return (
    <span className={`ml-1 px-1 py-0 rounded text-[9px] font-mono font-medium uppercase ${cls}`}>
      {type}
    </span>
  );
}

// Humanized short label for sheet section
function sheetLabel(sheet: SheetName): { kind: "INPUT" | "OUTPUT"; label: string } {
  if (sheet === "Dump") return { kind: "INPUT", label: "Dump" };
  return { kind: "OUTPUT", label: sheet.trim() };
}

// "2026-03" → "MAR 2026"
function fmtMonth(ym: string): string {
  const [y, m] = ym.split("-");
  const d = new Date(Number(y), Number(m) - 1, 1);
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" }).toUpperCase();
}

// Order level summary stat columns (J-N in format_spec)
const OL_STAT_COLS: Array<{ col: string; label: string }> = [
  { col: "J", label: "GMV_MRP" },
  { col: "K", label: "DISCOUNT" },
  { col: "L", label: "GMV_LIST" },
  { col: "M", label: "CO_PAY_DISCOUNT" },
  { col: "N", label: "USER_PAID" },
];

function StatChip({ label, value }: { label: string; value: number | null | undefined }) {
  const display =
    value == null
      ? "—"
      : value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return (
    <div className="flex flex-col items-start gap-0.5 px-3 py-2 bg-[var(--bg-elev)] rounded-lg border border-[var(--border)]">
      <span className="text-[10px] font-medium text-[var(--text-muted)] uppercase tracking-wide">
        {label}
      </span>
      <span className="text-sm font-semibold tabular-nums text-[var(--text-primary)]">
        {display}
      </span>
    </div>
  );
}

function SummaryView({
  cells,
  sheet,
  onOpenRule,
}: {
  cells: SbiPreviewSummary["cells"];
  sheet: string;
  onOpenRule: (sheet: string, col: SbiPreviewColumn) => void;
}) {
  // Parse "B4" → { colLetter:"B", rowNum:4, ...cell }
  const parsed = cells.map((c) => {
    const m = c.cell.match(/^([A-Z]+)(\d+)$/);
    return { ...c, colLetter: m?.[1] ?? "?", rowNum: m ? parseInt(m[2], 10) : 0 };
  });

  // Sorted unique columns and rows
  const colLetters = [...new Set(parsed.map((c) => c.colLetter))].sort();
  const rowNums = [...new Set(parsed.map((c) => c.rowNum))].sort((a, b) => a - b);
  const cellMap = new Map(parsed.map((c) => [`${c.colLetter}${c.rowNum}`, c]));

  const handleClick = (c: (typeof parsed)[0]) => {
    onOpenRule(sheet, {
      col: c.cell,
      header: c.cell,
      is_derived: c.rule_type !== "static",
      rule_type: c.rule_type,
      number_format: c.number_format,
    });
  };

  // Right-align if the value is numeric
  const isNumeric = (v: unknown) => typeof v === "number" || (typeof v === "string" && v !== "" && !isNaN(Number(v)));

  return (
    <div className="flex-1 overflow-auto">
      <table className="border-collapse text-xs w-full">
        <thead>
          <tr className="bg-[var(--bg-elev)] sticky top-0 z-10">
            {/* row-number gutter */}
            <th className="w-8 border-b border-r border-[var(--border)] text-[var(--text-muted)] font-normal px-2 py-1.5 text-right select-none" />
            {colLetters.map((col) => (
              <th
                key={col}
                className="border-b border-r border-[var(--border)] px-3 py-1.5 text-center font-semibold text-[var(--text-secondary)] tracking-wide min-w-[120px]"
              >
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rowNums.map((rowNum, ri) => (
            <tr
              key={rowNum}
              className={ri % 2 === 0 ? "bg-[var(--bg-card)]" : "bg-[var(--bg-primary)]"}
            >
              {/* row number */}
              <td className="border-b border-r border-[var(--border)] px-2 py-2 text-right text-[var(--text-muted)] select-none font-mono">
                {rowNum}
              </td>
              {colLetters.map((col) => {
                const c = cellMap.get(`${col}${rowNum}`);
                if (!c) {
                  return (
                    <td
                      key={col}
                      className="border-b border-r border-[var(--border)] px-3 py-2"
                    />
                  );
                }
                const numeric = isNumeric(c.value);
                return (
                  <td
                    key={col}
                    onClick={() => handleClick(c)}
                    className={[
                      "border-b border-r border-[var(--border)] px-3 py-2 cursor-pointer",
                      "transition-colors hover:bg-[var(--accent-green)]/5 active:bg-[var(--accent-green)]/10",
                      numeric ? "text-right tabular-nums" : "text-left",
                    ].join(" ")}
                  >
                    <span className="text-[var(--text-primary)] font-medium">
                      {c.value == null
                        ? "—"
                        : c.number_format
                          ? applyNumFmt(numeric ? Number(c.value) : c.value, c.number_format)
                          : numeric
                            ? Number(c.value).toLocaleString("en-IN")
                            : String(c.value)}
                    </span>
                    <RuleTypeBadge type={c.rule_type} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Panel target — a column rule or the sheet-level filter
type PanelTarget =
  | { kind: "col"; sheet: string; col: SbiPreviewColumn }
  | { kind: "filter"; sheet: string };

function GridView({
  data,
  activeCol,
  onOpenRule,
}: {
  data: SbiPreviewGrid;
  activeCol: string | null; // currently open panel's col letter
  onOpenRule: (sheet: string, col: SbiPreviewColumn) => void;
}) {
  return (
    <div className="overflow-auto flex-1 min-h-0">
      <table className="text-xs w-full border-collapse">
        <thead className="sticky top-0 bg-[var(--bg-elev)] z-10">
          <tr>
            {data.columns.map((c) => {
              const isActive = c.col === activeCol;
              const isClickable = c.is_derived;
              return (
                <th
                  key={c.col}
                  onClick={() => isClickable ? onOpenRule(data.sheet, c) : undefined}
                  className={[
                    "px-3 py-2 text-left font-medium text-[var(--text-muted)] border-b border-[var(--border)] whitespace-nowrap max-w-[160px] group select-none",
                    isClickable ? "cursor-pointer" : "",
                    isActive ? "bg-[var(--accent-green)]/5 border-b-2 border-b-[var(--accent-green)]" : "",
                    isClickable && !isActive ? "hover:bg-[var(--bg-card)]" : "",
                  ].join(" ")}
                >
                  <div className="flex items-center gap-0.5 flex-wrap">
                    <span className="font-mono text-[10px] text-[var(--text-muted)]">{c.col}</span>
                    <span
                      className={[
                        "ml-0.5 transition-colors",
                        isActive ? "text-[var(--accent-green)]" : "",
                        isClickable && !isActive ? "group-hover:text-[var(--accent-green)]" : "",
                      ].join(" ")}
                    >
                      {c.header}
                    </span>
                    <RuleTypeBadge type={c.rule_type} />
                  </div>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row, ri) => (
            <tr
              key={ri}
              className="hover:bg-[var(--bg-elev)]/50 border-b border-[var(--border)]/40"
            >
              {row.map((cell, ci) => {
                const col = data.columns[ci];
                const display = applyNumFmt(cell, col?.number_format);
                const raw = cell == null ? "" : String(cell);
                return (
                  <td
                    key={ci}
                    className="px-3 py-1.5 text-[var(--text-secondary)] max-w-[220px]"
                    title={raw.length > 40 ? raw : undefined}
                  >
                    <div className="truncate tabular-nums">{display}</div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="p-3 text-xs text-[var(--text-muted)]">
        Showing {data.rows.length.toLocaleString("en-IN")} of{" "}
        {data.total.toLocaleString("en-IN")} rows
      </p>
    </div>
  );
}

export function SbiMisWorkingSheet() {
  const [status, setStatus] = useState<SbiStatusResponse | null>(null);
  const [activeSheet, setActiveSheet] = useState<SheetName>(SHEETS[1]);
  const [preview, setPreview] = useState<SbiPreviewGrid | SbiPreviewSummary | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [panel, setPanel] = useState<PanelTarget | null>(null);
  const [dumpBannerDismissed, setDumpBannerDismissed] = useState(false);
  const [rerunning, setRerunning] = useState(false);

  useEffect(() => {
    sbiGetStatus().then(setStatus).catch(console.error);
  }, []);

  const loadPreview = useCallback(async (sheet: string) => {
    setLoading(true);
    setPreview(null);
    setPreviewError(null);
    try {
      const data = await sbiGetPreview(sheet, 0, PREVIEW_LIMIT);
      setPreview(data);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setPreviewError(msg);
      console.error("Preview error for", sheet, msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (status?.has_upload) loadPreview(activeSheet);
  }, [activeSheet, status?.has_upload, loadPreview]);

  // Close panel and clear errors when switching sheets
  useEffect(() => {
    setPanel(null);
    setPreviewError(null);
  }, [activeSheet]);

  // Must be declared before any conditional return (Rules of Hooks).
  const savedPanel = useCallback((needsRerun: boolean) => {
    setPanel(null);
    if (!needsRerun) {
      // Format-only change: just reload the preview — no pipeline rerun was triggered.
      loadPreview(activeSheet);
      return;
    }
    notifySbiRerun(); // signal the nav spinner to activate
    setRerunning(true);
    const id = setInterval(async () => {
      try {
        const s = await sbiGetStatus();
        if (!s.computing) {
          clearInterval(id);
          setRerunning(false);
          if (s.has_upload) loadPreview(activeSheet);
        }
      } catch {
        clearInterval(id);
        setRerunning(false);
      }
    }, 1500);
  }, [activeSheet, loadPreview]);

  if (!status?.has_upload) {
    return (
      <div className="p-6 text-sm text-[var(--text-muted)]">
        No data loaded. Upload a Metabase Dump on the Dashboard page first.
      </div>
    );
  }

  const meta = sheetLabel(activeSheet);
  const isOutputSheet =
    activeSheet !== "Dump" && activeSheet !== "Summary" && activeSheet !== "AHC";
  const isOrderLevel =
    activeSheet === "Order level " || activeSheet === "Order level - non permissible";

  const grandTotals =
    preview?.kind === "grid" ? (preview as SbiPreviewGrid).grand_totals : null;

  // Derive the currently active panel column letter (for header highlight)
  const activePanelCol =
    panel?.kind === "col" ? panel.col.col : panel?.kind === "filter" ? "__filter__" : null;

  // Panel props
  const panelSheet = panel ? (panel.kind === "col" ? panel.sheet : panel.sheet) : "";
  const panelCol = panel ? (panel.kind === "col" ? panel.col.col : "__filter__") : "";
  const panelHeader = panel
    ? panel.kind === "col"
      ? panel.col.header
      : `Row filter — ${panel.sheet.trim()}`
    : "";
  const panelNumFmt = panel?.kind === "col" ? panel.col.number_format : null;

  const closePanel = () => setPanel(null);

  return (
    <div className="flex flex-col h-full min-h-0 relative">
      {/* ── Section header ─────────────────────────── */}
      <div className="shrink-0 px-5 pt-4 pb-3 border-b border-[var(--border)] bg-[var(--bg-primary)]">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-muted)]">
              {meta.kind} SHEET
            </p>
            <h2 className="text-sm font-semibold text-[var(--text-primary)] mt-0.5">
              {meta.label}
              {activeSheet === "PF Summary" && status?.active_month && (
                <span className="ml-2 text-xs font-semibold text-[var(--accent-green)]">
                  {fmtMonth(status.active_month)}
                </span>
              )}
              {preview?.kind === "grid" && (
                <span className="ml-2 text-xs font-normal text-[var(--text-muted)]">
                  {(preview as SbiPreviewGrid).total.toLocaleString("en-IN")} rows · showing first{" "}
                  {PREVIEW_LIMIT}
                </span>
              )}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {isOutputSheet && (
              <button
                onClick={() => setPanel({ kind: "filter", sheet: activeSheet })}
                className={[
                  "px-3 py-1.5 rounded border text-xs transition-colors",
                  panel?.kind === "filter"
                    ? "border-[var(--accent-green)] text-[var(--accent-green)] bg-[var(--accent-green)]/5"
                    : "border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]",
                ].join(" ")}
              >
                Edit row filter
              </button>
            )}
          </div>
        </div>

        {/* Order level summary stat chips */}
        {isOrderLevel && grandTotals && (
          <div className="mt-3 flex flex-wrap gap-2">
            {OL_STAT_COLS.map(({ col, label }) => (
              <StatChip key={col} label={label} value={grandTotals[col] as number | null} />
            ))}
          </div>
        )}
      </div>

      {/* ── Main body: table (slides left when panel open) ── */}
      <div className={["flex flex-1 min-h-0 overflow-hidden", panel ? "mr-[380px]" : ""].join(" ")}>
        {/* Table content */}
        <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
          {/* Dump info banner */}
          {activeSheet === "Dump" && !dumpBannerDismissed && (
            <div className="shrink-0 mx-5 mt-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 flex items-start justify-between gap-3">
              <p className="text-xs text-amber-800 leading-relaxed">
                This is the <strong>Dump</strong> sheet from the uploaded file.
                Columns A–AS come straight from the Metabase export.
                AT–AY are enrichment columns produced by the rules here.
              </p>
              <button
                onClick={() => setDumpBannerDismissed(true)}
                className="shrink-0 text-amber-600 hover:text-amber-800 text-sm"
                aria-label="Dismiss"
              >
                ✕
              </button>
            </div>
          )}

          {/* Loading */}
          {loading && (
            <div className="p-6 text-sm text-[var(--text-muted)]">Loading…</div>
          )}

          {/* Rerunning banner — sticky so it stays visible while scrolling */}
          {rerunning && (
            <div className="sticky top-0 z-20 shrink-0 border-b border-blue-300 bg-blue-600 px-5 py-3 text-sm text-white flex items-center gap-3 shadow-md">
              <svg className="w-6 h-6 animate-spin shrink-0" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-30" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3"/>
                <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
              </svg>
              <span className="font-semibold">Pipeline recalculating…</span>
              <span className="opacity-80 text-xs">Results will refresh automatically.</span>
            </div>
          )}

          {/* AHC: backend doesn't have this sheet yet */}
          {!loading && activeSheet === "AHC" && previewError && (
            <div className="p-6 space-y-1">
              <p className="text-sm font-semibold text-[var(--text-primary)]">AHC data not found</p>
              <p className="text-xs text-[var(--text-muted)]">
                The AHC sheet was not found in the pipeline output. Make sure your uploaded
                Excel file contains the AHC tab — it is read from the same file as the Dump.
                Re-upload the file via <strong>Upload Input</strong> if needed.
              </p>
              <p className="text-[10px] text-[var(--text-muted)] font-mono pt-1">{previewError}</p>
            </div>
          )}

          {/* Preview fetch error (non-AHC sheets) */}
          {!loading && previewError && activeSheet !== "AHC" && (
            <div className="mx-5 mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700 space-y-1">
              <p className="font-semibold">Could not load {activeSheet.trim()} preview</p>
              <p className="font-mono break-all">{previewError}</p>
            </div>
          )}

          {/* Summary view */}
          {!loading && preview?.kind === "summary" && (
            <SummaryView
              cells={preview.cells}
              sheet={activeSheet}
              onOpenRule={(s, col) => setPanel({ kind: "col", sheet: s, col })}
            />
          )}

          {/* Grid view */}
          {!loading && preview?.kind === "grid" && (
            <GridView
              data={preview as SbiPreviewGrid}
              activeCol={activePanelCol}
              onOpenRule={(sheet, col) => setPanel({ kind: "col", sheet, col })}
            />
          )}
        </div>
      </div>

      {/* ── Sheet tabs — bottom bar (like spreadsheet sheet tabs) ── */}
      <div className="flex border-t border-[var(--border)] bg-[var(--bg-card)] px-4 gap-1 pb-1 overflow-x-auto shrink-0">
        {SHEETS.map((s) => (
          <button
            key={s}
            onClick={() => setActiveSheet(s)}
            className={[
              "px-3 py-1.5 text-sm rounded-b font-medium whitespace-nowrap border-t-2 -mt-px transition-colors",
              activeSheet === s
                ? "border-[var(--accent-green)] text-[var(--accent-green)] bg-[var(--bg-primary)]"
                : "border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-elev)]",
            ].join(" ")}
          >
            {s.trim()}
          </button>
        ))}
      </div>

      {/* ── Right panel: full-height, positioned above the bottom tab bar ── */}
      {panel && (
        <div className="absolute top-0 right-0 bottom-10 w-[380px] border-l border-[var(--border)] bg-[var(--bg-card)] flex flex-col z-10">
          {panel.kind === "col" && PIVOT_SHEETS.has(panel.sheet) ? (
            // Pivot output sheet — show full sheet structure + inline column editor
            <SbiMisPivotPanel
              sheet={panel.sheet}
              highlightCol={panel.col}
              previewColumns={
                preview?.kind === "grid"
                  ? (preview as SbiPreviewGrid).columns
                  : []
              }
              onClose={closePanel}
              onSaved={savedPanel}
            />
          ) : (
            // Dump / Summary / PF Summary / filter — show per-column rule editor
            <SbiMisRuleEditor
              sheet={panelSheet}
              col={panelCol}
              header={panelHeader}
              initialNumberFormat={panelNumFmt}
              onClose={closePanel}
              onSaved={savedPanel}
            />
          )}
        </div>
      )}

    </div>
  );
}
