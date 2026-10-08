"use client";

import { useEffect, useMemo, useState } from "react";
import { format as numFormat } from "numfmt";
import {
  sbiAddSheetColumn,
  sbiDeleteSheetColumn,
  sbiGetPreview,
  sbiGetRule,
  sbiGetRules,
  sbiUpdateColumnFormat,
  sbiUpdateRule,
  SbiPreviewColumn,
  SbiPreviewGrid,
  SbiRule,
} from "@/lib/api";

const PIVOT_RULE_TYPES = [
  { value: "pivot_group_key", label: "Pivot group key — composite group-by key" },
  { value: "pivot_key",       label: "Pivot key — group-by column" },
  { value: "pivot_first",     label: "Pivot first — first value in group" },
  { value: "pivot_agg",       label: "Pivot agg — sum/count aggregation" },
  { value: "counter",         label: "Counter — row number" },
  { value: "formula",         label: "Formula — custom expression" },
  { value: "static",          label: "Static — constant value" },
  { value: "blank",           label: "Blank — empty column" },
] as const;

const NUMBER_FORMATS = [
  { value: "",           label: "Default" },
  { value: "0",          label: "Integer (0)" },
  { value: "0.00",       label: "2 decimal places" },
  { value: "#,##0",      label: "Thousands (1,000)" },
  { value: "#,##0.00",   label: "Thousands + 2dp" },
  { value: "0%",         label: "Percentage" },
  { value: "0.00%",      label: "Percentage (2dp)" },
  { value: "₹#,##0",    label: "₹ INR (no decimal)" },
  { value: "₹#,##0.00", label: "₹ INR (2dp)" },
];

const STATUS_COLORS: Record<string, string> = {
  draft:    "bg-amber-100 text-amber-700",
  approved: "bg-emerald-100 text-emerald-700",
  disabled: "bg-gray-100 text-gray-500",
};

// ─── Small col-letter chip ────────────────────────────────────────────────────
function ColChip({ col, highlight }: { col: string; highlight: boolean }) {
  return (
    <span
      className={[
        "w-5 h-5 flex items-center justify-center rounded font-mono text-[10px] font-semibold shrink-0",
        highlight ? "bg-violet-200 text-violet-800" : "bg-transparent text-[var(--text-muted)]",
      ].join(" ")}
    >
      {col}
    </span>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[10px] font-bold uppercase tracking-widest text-violet-600 mb-1.5">
      {children}
    </p>
  );
}

// ─── Guided / Formula tab pill ────────────────────────────────────────────────
function TabPills({
  active,
  onChange,
}: {
  active: "guided" | "formula";
  onChange: (t: "guided" | "formula") => void;
}) {
  return (
    <div className="flex gap-0.5 p-0.5 bg-[var(--bg-elev)] rounded-lg w-fit border border-[var(--border)] mb-4">
      {(["Guided", "Formula"] as const).map((label) => {
        const v = label.toLowerCase() as "guided" | "formula";
        return (
          <button
            key={label}
            onClick={() => onChange(v)}
            className={[
              "px-4 py-1 rounded text-xs font-medium transition-colors",
              active === v
                ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm"
                : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]",
            ].join(" ")}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

// ─── Inline field helpers ─────────────────────────────────────────────────────
function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-sm font-semibold text-[var(--text-primary)] mb-1">
      {children}
    </label>
  );
}

function FieldHint({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs text-[var(--accent-green)] mt-1 leading-relaxed">{children}</p>
  );
}

// ─── Add-column modal ─────────────────────────────────────────────────────────
function AddColumnModal({
  sheet,
  onDone,
  onCancel,
}: {
  sheet: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [header, setHeader]   = useState("");
  const [ruleType, setRuleType] = useState("pivot_first");
  const [saving, setSaving]   = useState(false);
  const [error, setError]     = useState<string | null>(null);

  const submit = async () => {
    if (!header.trim()) return;
    setSaving(true);
    try {
      await sbiAddSheetColumn(sheet, { header: header.trim(), rule_type: ruleType });
      onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
      <div className="bg-[var(--bg-card)] rounded-xl shadow-xl w-full max-w-sm">
        <div className="p-4 border-b border-[var(--border)]">
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">Add column</h3>
        </div>
        <div className="p-4 space-y-3">
          {error && (
            <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
              {error}
            </div>
          )}
          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
              Column name
            </label>
            <input
              autoFocus
              className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
              value={header}
              onChange={(e) => setHeader(e.target.value)}
              placeholder="e.g. net_discount"
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
              Initial rule type
            </label>
            <select
              value={ruleType}
              onChange={(e) => setRuleType(e.target.value)}
              className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
            >
              {PIVOT_RULE_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="p-4 border-t border-[var(--border)] flex justify-end gap-2">
          <button onClick={onCancel} className="px-3 py-1.5 rounded text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={saving || !header.trim()}
            className="px-3 py-1.5 rounded bg-[var(--accent-green)] text-white text-xs font-medium disabled:opacity-50"
          >
            {saving ? "Adding…" : "Add"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────
interface Props {
  sheet: string;
  highlightCol: SbiPreviewColumn;
  previewColumns: SbiPreviewColumn[];
  onClose: () => void;
  /** Called after a successful save. `needsRerun` is false when only number_format changed — no pipeline rerun is required in that case. */
  onSaved: (needsRerun: boolean) => void;
}

export function SbiMisPivotPanel({
  sheet,
  highlightCol,
  previewColumns,
  onClose,
  onSaved,
}: Props) {
  const [allRules, setAllRules]     = useState<SbiRule[]>([]);
  const [filterRule, setFilterRule] = useState<SbiRule | null>(null);
  const [loading, setLoading]       = useState(true);
  // letter → header, e.g. { D: "group_id" }  (for editor hints)
  const [letterToHeader, setLetterToHeader] = useState<Record<string, string>>({});
  // header → letter, e.g. { group_id: "D" }  (to handle local rules saved with names)
  const [headerToLetter, setHeaderToLetter] = useState<Record<string, string>>({});

  // Rule editor state
  const [origRule, setOrigRule]     = useState<SbiRule | null>(null);
  const [ruleType, setRuleType]     = useState(highlightCol.rule_type ?? "blank");
  const [config, setConfig]         = useState<Record<string, unknown>>({});
  const [numFmt, setNumFmt]         = useState(highlightCol.number_format ?? "");
  const [editorTab, setEditorTab]   = useState<"guided" | "formula">("guided");

  const [saving, setSaving]         = useState(false);
  const [deleting, setDeleting]     = useState<string | null>(null);
  const [showAdd, setShowAdd]       = useState(false);
  const [error, setError]           = useState<string | null>(null);

  const loadRules = async () => {
    setLoading(true);
    try {
      const [all, hlRule, filt, dumpPreview] = await Promise.all([
        sbiGetRules().then((rs) => rs.filter((r) => r.sheet === sheet)),
        sbiGetRule(sheet, highlightCol.col).catch(() => null),
        sbiGetRule(sheet, "__filter__").catch(() => null),
        // Fetch Dump columns with limit=0 — we only need the column metadata, not rows
        sbiGetPreview("Dump", 0, 0).catch(() => null),
      ]);
      setAllRules(all);
      setFilterRule(filt);
      if (hlRule) {
        setOrigRule(hlRule);
        setRuleType(hlRule.rule_type);
        // Normalise string config_json → dict (staging stores it as a JSON string)
        const rawCfg = hlRule.config_json;
        const parsedCfg: Record<string, unknown> =
          typeof rawCfg === "string"
            ? (() => { try { return JSON.parse(rawCfg); } catch { return {}; } })()
            : (rawCfg as Record<string, unknown>) ?? {};
        setConfig(parsedCfg);
      }
      if (dumpPreview && (dumpPreview as SbiPreviewGrid).columns) {
        const l2h: Record<string, string> = {};
        const h2l: Record<string, string> = {};
        for (const c of (dumpPreview as SbiPreviewGrid).columns) {
          l2h[c.col.toUpperCase()] = c.header;
          h2l[c.header.toLowerCase()] = c.col.toUpperCase();
        }
        setLetterToHeader(l2h);
        setHeaderToLetter(h2l);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setError(null);
    setOrigRule(null);
    setNumFmt(highlightCol.number_format ?? "");
    setEditorTab("guided");
    loadRules();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sheet, highlightCol.col]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const ruleMap = useMemo(() => {
    const m: Record<string, SbiRule> = {};
    for (const r of allRules) m[r.column_letter] = r;
    return m;
  }, [allRules]);

  const groups = useMemo(() => {
    const derived = previewColumns.filter((c) => c.is_derived);
    const byType = (type: string | string[]) =>
      derived
        .filter((c) => Array.isArray(type) ? type.includes(c.rule_type ?? "") : c.rule_type === type)
        .sort((a, b) => a.col.localeCompare(b.col));
    return {
      keys:     byType(["pivot_key", "pivot_group_key"]),
      aggs:     byType("pivot_agg"),
      firsts:   byType("pivot_first"),
      counters: byType("counter"),
      formulas: byType("formula"),
    };
  }, [previewColumns]);

  // Build help text for pivot_group_key — shows column letters, e.g. "groups by (D, E) — 2 parts"
  const groupKeyHint = useMemo(() => {
    const keyParts = groups.keys
      .map((c) => {
        const r = ruleMap[c.col];
        const cfg = r?.config_json as Record<string, unknown> | undefined;
        const idx = Number(cfg?.key ?? cfg?.key_index ?? 0);
        const src = cfg?.source ? String(cfg.source) : null;
        // If stored as a header name (local), convert back to letter; otherwise use as-is
        const letter = src
          ? (headerToLetter[src.toLowerCase()] ?? src.toUpperCase())
          : c.col;
        return { idx, letter };
      })
      .sort((a, b) => a.idx - b.idx)
      .map((x) => x.letter);
    const n = keyParts.length;
    if (n === 0) return "No group keys defined yet.";
    const parts = n === 1 ? "one part" : `${n} parts`;
    return `Order level groups by (${keyParts.join(", ")}) — ${parts}.`;
  }, [groups.keys, ruleMap, headerToLetter]);

  /**
   * Given a stored source value (either a Dump column letter like "D" or a header
   * name like "group_id" stored on local), return the canonical column letter.
   * Falls back to `fallback` (usually c.col) when nothing is stored.
   */
  const toLetter = (src: unknown, fallback: string): string => {
    if (!src) return fallback;
    const s = String(src);
    // If stored as header name (local anomaly), reverse-look up to the letter
    const viaReverse = headerToLetter[s.toLowerCase()];
    if (viaReverse) return viaReverse;
    // Otherwise treat it as already a letter
    return s.toUpperCase();
  };

  const describe = (c: SbiPreviewColumn, keyIdx?: number): string => {
    const rule = ruleMap[c.col];
    if (!rule) return c.header;
    // config_json can arrive as a JSON string on staging — normalise to object
    const raw = rule.config_json;
    const cfg: Record<string, unknown> =
      typeof raw === "string"
        ? (() => { try { return JSON.parse(raw); } catch { return {}; } })()
        : (raw as Record<string, unknown>) ?? {};
    switch (rule.rule_type) {
      case "pivot_key":
      case "pivot_group_key": {
        // Production uses "key"; editor may have saved "key_index" — handle both
        const kiVal = cfg.key ?? cfg.key_index;
        const ki = kiVal != null ? Number(kiVal) : (keyIdx ?? 0);
        return `${toLetter(cfg.source, c.col)} (key ${ki})`;
      }
      case "pivot_agg":
        return `${String(cfg.agg ?? "sum").toUpperCase()}(${toLetter(cfg.agg_col ?? cfg.source, c.col)})`;
      case "pivot_first":
        // Fall back to c.col (output column letter) instead of "?" when source is unset
        return `first of ${toLetter(cfg.source, c.col)}`;
      case "counter":
        return "row number";
      default:
        return c.header;
    }
  };

  // Normalise config_json before stringifying — on staging it arrives as a JSON string,
  // not a parsed object. Same fix as SbiMisRuleEditor.tsx.
  const origConfigStr = origRule
    ? (() => {
        const raw = origRule.config_json;
        const parsed: Record<string, unknown> =
          typeof raw === "string"
            ? (() => { try { return JSON.parse(raw); } catch { return {}; } })()
            : (raw as Record<string, unknown>) ?? {};
        return JSON.stringify(parsed);
      })()
    : "";
  const origNumFmt       = highlightCol.number_format ?? "";
  const hasRuleChanges   = !!origRule && (ruleType !== origRule.rule_type || JSON.stringify(config) !== origConfigStr);
  const hasFormatChanges = numFmt !== origNumFmt;
  const hasChanges       = hasRuleChanges || hasFormatChanges;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Only trigger a pipeline rerun when the rule logic actually changed.
      // Changing only number_format is purely presentational and must NOT rerun.
      if (hasRuleChanges) {
        await sbiUpdateRule(sheet, highlightCol.col, { rule_type: ruleType, config });
      }
      if (hasFormatChanges) {
        await sbiUpdateColumnFormat(sheet, highlightCol.col, numFmt || null);
      }
      onSaved(hasRuleChanges);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const deleteCol = async (col: string) => {
    if (!confirm(`Remove column ${col} from ${sheet.trim()}? This will trigger a pipeline rerun.`)) return;
    setDeleting(col);
    try {
      await sbiDeleteSheetColumn(sheet, col);
      await loadRules();
      onSaved(true); // column deletion always requires a rerun
    } catch (e) {
      setError(String(e));
    } finally {
      setDeleting(null);
    }
  };

  const filterExpr  = filterRule?.config_json?.expr as string | undefined;
  const statusColor = STATUS_COLORS[origRule?.status ?? "draft"] ?? STATUS_COLORS.draft;

  // Inner row in the structure card
  const StructureRow = ({ c, label, deletable = false }: { c: SbiPreviewColumn; label: string; deletable?: boolean }) => {
    const isHL = c.col === highlightCol.col;
    return (
      <div className={["flex items-center gap-2 px-2 py-1.5 rounded-lg group", isHL ? "bg-violet-50" : "hover:bg-[var(--bg-elev)]/60"].join(" ")}>
        <ColChip col={c.col} highlight={isHL} />
        <span className={["flex-1 text-xs", isHL ? "font-medium text-[var(--text-primary)]" : "text-[var(--text-secondary)]"].join(" ")}>
          {label}
        </span>
        {deletable && (
          <button
            onClick={() => deleteCol(c.col)}
            disabled={!!deleting}
            className="opacity-0 group-hover:opacity-100 text-[var(--text-muted)] hover:text-red-500 text-xs transition-opacity disabled:opacity-30"
          >
            {deleting === c.col ? "…" : "×"}
          </button>
        )}
      </div>
    );
  };

  return (
    <>
      <div className="flex flex-col h-full">
        {/* ── Header ─────────────────────────────────────── */}
        <div className="px-4 pt-4 pb-3 border-b border-[var(--border)] shrink-0">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-muted)] leading-none">
                {sheet.trim().toUpperCase()} · {highlightCol.col.toUpperCase()}
              </p>
              <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                  {highlightCol.header}
                </h2>
                {origRule && (
                  <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${statusColor}`}>
                    {origRule.status}
                  </span>
                )}
              </div>
            </div>
            <button
              onClick={onClose}
              className="shrink-0 text-[var(--text-muted)] hover:text-[var(--text-primary)] text-base mt-0.5 transition-colors"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </div>

        {/* ── Body ───────────────────────────────────────── */}
        {loading ? (
          <div className="flex-1 flex items-center justify-center text-sm text-[var(--text-muted)]">Loading…</div>
        ) : (
          <div className="flex-1 overflow-y-auto">
            <div className="p-4 space-y-4">
              {error && (
                <div className="rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">{error}</div>
              )}

              {/* ── Pivot structure card ─────────────────── */}
              <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 space-y-3">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-widest text-violet-600">Pivot Structure</span>
                  <span className="text-xs text-[var(--text-muted)]">{sheet.trim()}</span>
                </div>

                {/* Filter */}
                <div>
                  <SectionLabel>Filter (which dump rows flow in)</SectionLabel>
                  {filterExpr ? (
                    <div className="px-3 py-2 bg-[var(--bg-elev)] rounded-lg border border-[var(--border)]">
                      <code className="text-xs font-mono text-[var(--text-secondary)] break-all">{filterExpr}</code>
                    </div>
                  ) : (
                    <p className="text-xs text-[var(--text-muted)] italic">No filter — all Dump rows included.</p>
                  )}
                </div>

                {groups.keys.length > 0 && (
                  <div>
                    <SectionLabel>Group By</SectionLabel>
                    {groups.keys.map((c, idx) => <StructureRow key={c.col} c={c} label={describe(c, idx)} deletable />)}
                  </div>
                )}

                {groups.aggs.length > 0 && (
                  <div>
                    <SectionLabel>Values (Aggregations)</SectionLabel>
                    {groups.aggs.map((c) => <StructureRow key={c.col} c={c} label={describe(c)} deletable />)}
                  </div>
                )}

                {groups.firsts.length > 0 && (
                  <div>
                    <SectionLabel>First-of (Categoricals)</SectionLabel>
                    {groups.firsts.map((c) => <StructureRow key={c.col} c={c} label={describe(c)} deletable />)}
                  </div>
                )}

                {groups.counters.length > 0 && (
                  <div>
                    <SectionLabel>Counter</SectionLabel>
                    {groups.counters.map((c) => <StructureRow key={c.col} c={c} label={describe(c)} />)}
                  </div>
                )}

                {groups.formulas.length > 0 && (
                  <div>
                    <SectionLabel>Formulas</SectionLabel>
                    {groups.formulas.map((c) => <StructureRow key={c.col} c={c} label={c.header} deletable />)}
                  </div>
                )}

                <button
                  onClick={() => setShowAdd(true)}
                  className="w-full mt-1 py-2 rounded-lg border border-dashed border-[var(--border)] text-xs text-[var(--text-muted)] hover:border-violet-300 hover:text-violet-600 transition-colors"
                >
                  + Add column
                </button>
              </div>

              {/* ── Rule editor ──────────────────────────── */}

              {/* Rule type */}
              <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">Rule type</label>
                <select
                  value={ruleType}
                  onChange={(e) => { setRuleType(e.target.value); setConfig({}); }}
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                >
                  {PIVOT_RULE_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
              </div>

              {/* Guided / Formula tabs */}
              <TabPills active={editorTab} onChange={setEditorTab} />

              {editorTab === "guided" ? (
                /* ── Guided mode ─────────────────────────── */
                <div className="space-y-4">
                  {/* pivot_group_key */}
                  {(ruleType === "pivot_group_key" || ruleType === "pivot_key") && (
                    <>
                      <div>
                        <FieldLabel>Part of composite group key</FieldLabel>
                        <input
                          type="number"
                          min={0}
                          className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                          value={String(config.key ?? config.key_index ?? 0)}
                          onChange={(e) =>
                            // Store as "key" to match the production / engine format
                            setConfig((c) => ({ ...c, key: parseInt(e.target.value, 10) || 0, key_index: undefined }))
                          }
                        />
                        <FieldHint>
                          0 = first part, 1 = second part. {groupKeyHint}
                        </FieldHint>
                      </div>
                      <div>
                        <FieldLabel>Source column (letter in Dump)</FieldLabel>
                        <input
                          className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                          value={String(config.source ?? "")}
                          onChange={(e) =>
                            setConfig((c) => ({ ...c, source: e.target.value.toUpperCase() }))
                          }
                          placeholder="e.g. D"
                        />
                        {!!config.source && (
                          <FieldHint>
                            {letterToHeader[String(config.source).toUpperCase()]
                              ? `→ ${letterToHeader[String(config.source).toUpperCase()]}`
                              : "Column letter not found in Dump sheet"}
                          </FieldHint>
                        )}
                        {!config.source && (
                          <FieldHint>
                            {groups.keys.length > 0
                              ? groups.keys
                                  .map((k) => {
                                    const src = String((ruleMap[k.col]?.config_json as Record<string,unknown>)?.source ?? "?");
                                    const letter = headerToLetter[src.toLowerCase()] ?? src.toUpperCase();
                                    const name = letterToHeader[letter] ?? src;
                                    return `${letter} (${name})`;
                                  })
                                  .join(", ")
                              : "e.g. D (group_id), E (order_id)"}
                          </FieldHint>
                        )}
                      </div>
                    </>
                  )}

                  {/* pivot_agg */}
                  {ruleType === "pivot_agg" && (
                    <>
                      <div>
                        <FieldLabel>Source column (Dump letter)</FieldLabel>
                        <input
                          className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                          value={String(config.agg_col ?? config.source ?? "")}
                          onChange={(e) =>
                            setConfig((c) => ({ ...c, agg_col: e.target.value.toUpperCase(), source: undefined }))
                          }
                          placeholder="e.g. Z"
                        />
                        <FieldHint>
                          {(() => {
                            const v = String(config.agg_col ?? config.source ?? "").toUpperCase();
                            const name = v ? letterToHeader[v] : null;
                            return name ? `→ ${name} — values will be aggregated per group.` : "Column whose values will be aggregated per group.";
                          })()}
                        </FieldHint>
                      </div>
                      <div>
                        <FieldLabel>Aggregation function</FieldLabel>
                        <select
                          value={String(config.agg ?? "sum")}
                          onChange={(e) => setConfig((c) => ({ ...c, agg: e.target.value }))}
                          className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                        >
                          <option value="sum">Sum</option>
                          <option value="count">Count</option>
                          <option value="mean">Mean</option>
                        </select>
                      </div>
                    </>
                  )}

                  {/* pivot_first */}
                  {ruleType === "pivot_first" && (
                    <div>
                      <FieldLabel>Source column (Dump letter)</FieldLabel>
                      <input
                        className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                        value={String(config.source ?? "")}
                        onChange={(e) =>
                          setConfig((c) => ({ ...c, source: e.target.value.toUpperCase() }))
                        }
                        placeholder="e.g. A"
                      />
                      <FieldHint>
                        {(() => {
                          const v = String(config.source ?? "").toUpperCase();
                          const name = v ? letterToHeader[v] : null;
                          return name ? `→ ${name} — first value per group.` : "First value seen for this Dump column within each group.";
                        })()}
                      </FieldHint>
                    </div>
                  )}

                  {/* counter — no config */}
                  {ruleType === "counter" && (
                    <p className="text-sm text-[var(--text-muted)]">
                      Auto-assigned row number (1-based). No configuration needed.
                    </p>
                  )}

                  {/* blank — no config */}
                  {ruleType === "blank" && (
                    <p className="text-sm text-[var(--text-muted)]">
                      Column will be empty in output. No configuration needed.
                    </p>
                  )}

                  {/* formula */}
                  {ruleType === "formula" && (
                    <div>
                      <FieldLabel>Excel formula</FieldLabel>
                      <textarea
                        className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                        rows={3}
                        value={String(config.formula ?? "")}
                        onChange={(e) => setConfig((c) => ({ ...c, formula: e.target.value }))}
                        placeholder="=(B+C)/D"
                        spellCheck={false}
                      />
                    </div>
                  )}

                  {/* static */}
                  {ruleType === "static" && (
                    <div>
                      <FieldLabel>Constant value</FieldLabel>
                      <input
                        className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                        value={String(config.value ?? "")}
                        onChange={(e) => setConfig({ value: e.target.value })}
                      />
                    </div>
                  )}
                </div>
              ) : (
                /* ── Formula mode: raw JSON config ───────── */
                <div>
                  <FieldLabel>Raw config (JSON)</FieldLabel>
                  <textarea
                    className="w-full border border-[var(--border)] rounded px-3 py-2 text-xs bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                    rows={6}
                    value={JSON.stringify(config, null, 2)}
                    onChange={(e) => {
                      try {
                        setConfig(JSON.parse(e.target.value));
                        setError(null);
                      } catch {
                        // let user keep typing
                      }
                    }}
                    spellCheck={false}
                  />
                  <p className="text-[11px] text-[var(--text-muted)] mt-1">
                    Edit the raw config JSON for advanced use cases.
                  </p>
                </div>
              )}

              {/* ── Number format ─────────────────────────── */}
              <div className="rounded-xl border border-[var(--border)] p-4 space-y-2">
                <FieldLabel>Number format</FieldLabel>
                <select
                  value={numFmt}
                  onChange={(e) => setNumFmt(e.target.value)}
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                >
                  {NUMBER_FORMATS.map((f) => (
                    <option key={f.value} value={f.value}>{f.label}</option>
                  ))}
                </select>
                {/* Live numfmt preview */}
                {numFmt ? (() => {
                  try {
                    const sample = numFmt.includes("%") ? 0.1234 : 1234567.89;
                    const preview = numFormat(numFmt, sample);
                    return (
                      <p className="text-xs text-[var(--text-muted)]">
                        Preview:{" "}
                        <span className="font-mono text-[var(--text-secondary)]">{preview}</span>
                      </p>
                    );
                  } catch { return null; }
                })() : (
                  <p className="text-xs text-[var(--text-muted)]">
                    Applies to this column in the downloaded .xlsx.
                  </p>
                )}
              </div>

              {/* ── Change status box ─────────────────────── */}
              <div className="rounded-xl border border-[var(--border)] px-4 py-3">
                <p className="text-sm text-[var(--text-muted)] leading-relaxed">
                  {hasRuleChanges
                    ? "Rule changes pending — saving will trigger a pipeline rerun."
                    : hasFormatChanges
                      ? "Format change pending — saving will update display only, no rerun needed."
                      : "No changes yet. Edit the rule or format above, then approve & save."}
                </p>
              </div>
            </div>
          </div>
        )}

        {/* ── Footer ─────────────────────────────────────── */}
        <div className="shrink-0 border-t border-[var(--border)] px-4 py-3 bg-[var(--bg-card)] flex justify-between items-center">
          <button
            onClick={onClose}
            disabled={saving}
            className="px-5 py-2 rounded border border-[var(--border)] text-sm text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] disabled:opacity-50 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving || !hasChanges || loading}
            className="px-5 py-2 rounded bg-[var(--bg-elev)] text-sm font-medium text-[var(--text-muted)] disabled:opacity-40 enabled:bg-slate-700 enabled:text-white transition-all"
          >
            {saving ? "Saving…" : "Approve & save"}
          </button>
        </div>
      </div>

      {showAdd && (
        <AddColumnModal
          sheet={sheet}
          onDone={() => { setShowAdd(false); loadRules(); onSaved(false); }}
          onCancel={() => setShowAdd(false)}
        />
      )}
    </>
  );
}
