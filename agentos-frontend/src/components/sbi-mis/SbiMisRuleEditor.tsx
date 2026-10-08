"use client";

import { useEffect, useMemo, useState } from "react";
import { format as numFormat } from "numfmt";
import {
  sbiGetRule,
  sbiNlToFormula,
  sbiUpdateColumnFormat,
  sbiUpdateRule,
  SbiRule,
} from "@/lib/api";
import { validateFormula } from "@/lib/sbiFormulaUtils";

const RULE_TYPES = [
  { value: "direct",           label: "Direct — copy source column" },
  { value: "formula",          label: "Formula — custom Excel-like" },
  { value: "filter",           label: "Filter — row filter expression" },
  { value: "pivot_group_key",  label: "Pivot group key — composite group-by key" },
  { value: "pivot_key",        label: "Pivot key — group-by column" },
  { value: "pivot_first",      label: "Pivot first — first value in group" },
  { value: "pivot_agg",        label: "Pivot agg — sum/count aggregation" },
  { value: "counter",          label: "Counter — auto row number" },
  { value: "lookup",           label: "Lookup — join to lookup table" },
  { value: "static",           label: "Static — constant value" },
  { value: "blank",            label: "Blank — empty column" },
  { value: "override",         label: "Override — rewrite raw Dump column" },
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

/** Sample value for numfmt previews. Percentage formats expect 0–1. */
function previewSample(fmt: string): number {
  return fmt.includes("%") ? 0.1234 : 1234567.89;
}

/** Render a live number-format preview using numfmt. Returns null on error. */
function NumFmtPreview({ fmt }: { fmt: string }) {
  if (!fmt) return null;
  try {
    const preview = numFormat(fmt, previewSample(fmt));
    return (
      <p className="text-[11px] text-[var(--text-muted)] mt-1">
        Preview:{" "}
        <span className="font-mono text-[var(--text-secondary)]">{preview}</span>
      </p>
    );
  } catch {
    return null;
  }
}

interface Props {
  sheet: string;
  col: string;
  header: string;
  initialNumberFormat?: string | null;
  onClose: () => void;
  /** Called after a successful save. `needsRerun` is false when only number_format changed. */
  onSaved: (needsRerun: boolean) => void;
}

export function SbiMisRuleEditor({
  sheet,
  col,
  header,
  initialNumberFormat,
  onClose,
  onSaved,
}: Props) {
  const [rule, setRule]             = useState<SbiRule | null>(null);
  const [loadingRule, setLoadingRule] = useState(true);
  const [ruleType, setRuleType]     = useState("blank");
  const [config, setConfig]         = useState<Record<string, unknown>>({});
  const [numFmt, setNumFmt]         = useState(initialNumberFormat ?? "");
  const [saving, setSaving]         = useState(false);
  const [formulaTab, setFormulaTab] = useState<"formula" | "guided">("formula");
  const [nlText, setNlText]         = useState("");
  const [nlLoading, setNlLoading]   = useState(false);
  const [error, setError]           = useState<string | null>(null);

  useEffect(() => {
    setLoadingRule(true);
    setError(null);
    setNlText("");
    setFormulaTab("formula");
    setNumFmt(initialNumberFormat ?? "");
    sbiGetRule(sheet, col)
      .then((r) => {
        setRule(r);
        setRuleType(r.rule_type);
        // config_json can arrive as a JSON string on staging — normalise to object
        const raw = r.config_json;
        const parsed: Record<string, unknown> =
          typeof raw === "string"
            ? (() => { try { return JSON.parse(raw); } catch { return {}; } })()
            : (raw as Record<string, unknown>) ?? {};
        setConfig(parsed);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoadingRule(false));
  }, [sheet, col, initialNumberFormat]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  // Live formula validation — runs on every keystroke, zero latency
  const formulaVal = useMemo(
    () => validateFormula(String(config.formula ?? "")),
    [config.formula],
  );

  // Normalise config_json before stringifying — on staging it arrives as a JSON string,
  // not a parsed object. Comparing raw vs parsed would always differ and trigger a spurious
  // rule update (and pipeline rerun) even when only the number format changed.
  const origConfigStr  = useMemo(() => {
    if (!rule) return "";
    const raw = rule.config_json;
    const parsed: Record<string, unknown> =
      typeof raw === "string"
        ? (() => { try { return JSON.parse(raw); } catch { return {}; } })()
        : (raw as Record<string, unknown>) ?? {};
    return JSON.stringify(parsed);
  }, [rule]);
  const hasRuleChanges = !!rule && (ruleType !== rule.rule_type || JSON.stringify(config) !== origConfigStr);
  const hasFmtChanges  = numFmt !== (initialNumberFormat ?? "");
  const hasChanges     = hasRuleChanges || hasFmtChanges;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Only send a rule update (and trigger rerun) when the rule logic changed.
      // Pure number_format changes are presentational — no rerun needed.
      if (hasRuleChanges) {
        await sbiUpdateRule(sheet, col, { rule_type: ruleType, config });
      }
      if (hasFmtChanges && col !== "__filter__") {
        await sbiUpdateColumnFormat(sheet, col, numFmt || null);
      }
      onSaved(hasRuleChanges);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const generateFormula = async () => {
    if (!nlText.trim()) return;
    setNlLoading(true);
    try {
      const res = await sbiNlToFormula({ text: nlText, sheet, column: col, header });
      setConfig((c) => ({ ...c, formula: res.formula }));
      setFormulaTab("formula");
    } catch (e) {
      setError(String(e));
    } finally {
      setNlLoading(false);
    }
  };

  const isFilterCol  = col === "__filter__";
  const statusColor  = STATUS_COLORS[rule?.status ?? "draft"] ?? STATUS_COLORS.draft;
  const savedFormula = rule?.config_json?.formula as string | undefined;

  return (
    <div className="flex flex-col h-full">
      {/* ── Header ──────────────────────────────────────────── */}
      <div className="px-4 pt-4 pb-3 border-b border-[var(--border)] shrink-0">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[10px] font-bold uppercase tracking-widest text-[var(--text-muted)] leading-none">
              {sheet.trim().toUpperCase()} · {col.toUpperCase()}
            </p>
            <div className="flex items-center gap-2 mt-1.5 flex-wrap">
              <h2 className="text-sm font-semibold text-[var(--text-primary)] leading-tight">
                {header}
              </h2>
              {rule && (
                <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${statusColor}`}>
                  {rule.status}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 text-[var(--text-muted)] hover:text-[var(--text-primary)] text-base leading-none mt-0.5 transition-colors"
            aria-label="Close panel"
          >
            ✕
          </button>
        </div>
      </div>

      {/* ── Body ────────────────────────────────────────────── */}
      {loadingRule ? (
        <div className="flex-1 flex items-center justify-center text-sm text-[var(--text-muted)]">
          Loading…
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <div className="p-4 space-y-4">
            {error && (
              <div className="rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">
                {error}
              </div>
            )}

            {/* Saved formula hint */}
            {savedFormula && ruleType === "formula" && (
              <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 font-mono text-xs text-amber-900 break-all leading-relaxed">
                {savedFormula}
              </div>
            )}

            {/* Rule type */}
            <div>
              <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                Rule type
              </label>
              <select
                value={ruleType}
                onChange={(e) => { setRuleType(e.target.value); setConfig({}); }}
                disabled={isFilterCol}
                className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] disabled:opacity-60"
              >
                {RULE_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </div>

            {/* ── Config fields ────────────────────────────── */}

            {ruleType === "direct" && (
              <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                  Source column letter
                </label>
                <input
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                  value={String(config.source ?? "")}
                  onChange={(e) => setConfig({ source: e.target.value.toUpperCase() })}
                  placeholder="e.g. AF"
                />
              </div>
            )}

            {ruleType === "formula" && (
              <div className="space-y-3">
                {/* Guided / Formula tabs */}
                <div className="flex gap-0.5 p-0.5 bg-[var(--bg-elev)] rounded-lg w-fit border border-[var(--border)]">
                  {(["Guided", "Formula"] as const).map((tab) => {
                    const v = tab.toLowerCase() as "guided" | "formula";
                    return (
                      <button
                        key={tab}
                        onClick={() => setFormulaTab(v)}
                        className={[
                          "px-3 py-1 rounded text-xs font-medium transition-colors",
                          formulaTab === v
                            ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm"
                            : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]",
                        ].join(" ")}
                      >
                        {tab}
                      </button>
                    );
                  })}
                </div>

                {formulaTab === "guided" ? (
                  <div className="space-y-2">
                    <label className="block text-xs font-medium text-[var(--text-secondary)]">
                      Describe in plain English
                    </label>
                    <textarea
                      className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                      rows={3}
                      value={nlText}
                      onChange={(e) => setNlText(e.target.value)}
                      placeholder="e.g. Sum of upfront discount and coupon discount, divided by GMV MRP"
                    />
                    <button
                      onClick={generateFormula}
                      disabled={nlLoading || !nlText.trim()}
                      className="w-full px-3 py-2 rounded bg-[var(--accent-green)] text-white text-xs font-medium disabled:opacity-50 transition-opacity"
                    >
                      {nlLoading ? "Generating…" : "✨ Generate formula"}
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <label className="text-xs font-medium text-[var(--text-secondary)]">
                        Excel formula
                      </label>
                      <button
                        onClick={() => setFormulaTab("guided")}
                        className="text-[10px] text-[var(--accent-green)] hover:underline"
                      >
                        ✨ Plain English
                      </button>
                    </div>

                    {/* Formula textarea — red border on syntax error */}
                    <textarea
                      className={[
                        "w-full border rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono transition-colors",
                        formulaVal.error
                          ? "border-red-400 focus:outline-none focus:ring-1 focus:ring-red-400"
                          : "border-[var(--border)]",
                      ].join(" ")}
                      rows={4}
                      value={String(config.formula ?? "")}
                      onChange={(e) => setConfig((c) => ({ ...c, formula: e.target.value }))}
                      placeholder="=(AA+AC)/Z"
                      spellCheck={false}
                    />

                    {/* Syntax error */}
                    {formulaVal.error && (
                      <p className="text-[11px] text-red-500 flex items-center gap-1">
                        <span>⚠</span> {formulaVal.error}
                      </p>
                    )}

                    {/* Referenced column chips */}
                    {formulaVal.refs.length > 0 && (
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-[10px] text-[var(--text-muted)]">reads:</span>
                        {formulaVal.refs.map((ref) => (
                          <span
                            key={ref}
                            className="px-1.5 py-0.5 rounded bg-[var(--bg-elev)] border border-[var(--border)] font-mono text-[10px] text-[var(--text-secondary)]"
                          >
                            {ref}
                          </span>
                        ))}
                      </div>
                    )}

                    <p className="text-[11px] text-[var(--text-muted)] leading-relaxed">
                      Column refs: plain letters (AH, Z) = current row.{" "}
                      Cross-sheet:{" "}
                      <code className="font-mono">Dump.AF</code>,{" "}
                      <code className="font-mono">&apos;PF Summary&apos;.K</code>.{" "}
                      Supported: IF, AND, OR, NOT, SUM, SUMIF, SUMIFS, MAX, MIN,
                      ABS, ROUND, ROW, arithmetic, comparisons
                    </p>
                  </div>
                )}
              </div>
            )}

            {(ruleType === "filter" || isFilterCol) && (
              <div className="space-y-2">
                <label className="block text-xs font-medium text-[var(--text-secondary)]">
                  {isFilterCol ? "Row filter expression" : "Filter expression"}
                </label>
                <textarea
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                  rows={4}
                  value={String(config.expr ?? "")}
                  onChange={(e) => setConfig((c) => ({ ...c, expr: e.target.value }))}
                  placeholder="checker == 'permissible' and Next Step != 'non-permissible'"
                  spellCheck={false}
                />
                <p className="text-[11px] text-[var(--text-muted)]">
                  DSL:{" "}
                  <code className="font-mono">{"<col> == 'val'"}</code>,{" "}
                  <code className="font-mono">{"<col> IN (a, b)"}</code>, combined with{" "}
                  <code className="font-mono">and</code> /{" "}
                  <code className="font-mono">or</code>
                </p>
              </div>
            )}

            {ruleType === "lookup" && (
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                    Lookup table name
                  </label>
                  <input
                    className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                    value={String(config.table ?? "")}
                    onChange={(e) => setConfig((c) => ({ ...c, table: e.target.value }))}
                    placeholder="e.g. pf_wallet_limits"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                    Key column letter
                  </label>
                  <input
                    className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                    value={String(config.key_col ?? "")}
                    onChange={(e) => setConfig((c) => ({ ...c, key_col: e.target.value }))}
                    placeholder="e.g. AH"
                  />
                </div>
              </div>
            )}

            {ruleType === "static" && (
              <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                  Constant value
                </label>
                <input
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                  value={String(config.value ?? "")}
                  onChange={(e) => setConfig({ value: e.target.value })}
                />
              </div>
            )}

            {(ruleType === "pivot_group_key" || ruleType === "pivot_key") && (
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                    Key index (0 = first part, 1 = second part…)
                  </label>
                  <input
                    type="number"
                    min={0}
                    className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                    value={String(config.key ?? config.key_index ?? 0)}
                    onChange={(e) =>
                      setConfig((c) => ({ ...c, key: parseInt(e.target.value, 10) || 0, key_index: undefined }))
                    }
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                    Source column (letter in Dump)
                  </label>
                  <input
                    className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                    value={String(config.source ?? "")}
                    onChange={(e) => setConfig((c) => ({ ...c, source: e.target.value.toUpperCase() }))}
                    placeholder="e.g. D"
                  />
                </div>
              </div>
            )}

            {ruleType === "pivot_first" && (
              <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                  Source column (letter in Dump)
                </label>
                <input
                  className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                  value={String(config.source ?? "")}
                  onChange={(e) => setConfig((c) => ({ ...c, source: e.target.value.toUpperCase() }))}
                  placeholder="e.g. A"
                />
              </div>
            )}

            {ruleType === "pivot_agg" && (
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                    Source column (Dump letter to aggregate)
                  </label>
                  <input
                    className="w-36 border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] font-mono"
                    value={String(config.agg_col ?? config.source ?? "")}
                    onChange={(e) =>
                      setConfig((c) => ({ ...c, agg_col: e.target.value.toUpperCase(), source: undefined }))
                    }
                    placeholder="e.g. Z"
                  />
                </div>
                <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                  Aggregation function
                </label>
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
              </div>
            )}

            {ruleType === "counter" && (
              <p className="text-sm text-[var(--text-muted)]">
                Auto-assigned row number (1-based). No configuration needed.
              </p>
            )}

            {/* Number format + live numfmt preview */}
            {!isFilterCol && (
              <div>
                <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">
                  Number format
                </label>
                <select
                  value={numFmt}
                  onChange={(e) => setNumFmt(e.target.value)}
                  className="w-full border border-[var(--border)] rounded px-3 py-2 text-sm bg-[var(--bg-primary)] text-[var(--text-primary)]"
                >
                  {NUMBER_FORMATS.map((f) => (
                    <option key={f.value} value={f.value}>{f.label}</option>
                  ))}
                </select>
                <NumFmtPreview fmt={numFmt} />
                {!numFmt && (
                  <p className="text-[11px] text-[var(--text-muted)] mt-1">
                    Applies to this column in the downloaded .xlsx.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Footer ──────────────────────────────────────────── */}
      <div className="shrink-0 border-t border-[var(--border)] px-4 py-3 bg-[var(--bg-card)]">
        <p className="text-[11px] text-[var(--text-muted)] mb-2 leading-relaxed">
          {hasRuleChanges
            ? "Rule changes pending — saving will trigger a pipeline rerun."
            : hasFmtChanges
              ? "Format change pending — display only, no rerun needed."
              : "No changes yet. Edit the rule or format above, then approve & save."}
        </p>
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={saving}
            className="px-3 py-1.5 rounded text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] disabled:opacity-50 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving || !hasChanges || loadingRule}
            className="px-3 py-1.5 rounded bg-[var(--accent-green)] text-white text-xs font-medium disabled:opacity-40 transition-opacity"
          >
            {saving ? "Saving…" : "Approve & save"}
          </button>
        </div>
      </div>
    </div>
  );
}
