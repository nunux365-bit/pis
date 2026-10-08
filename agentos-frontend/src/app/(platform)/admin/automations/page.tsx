"use client";

import { useCallback, useEffect, useState } from "react";
import {
  createAutomationRule,
  enableAutomationSuggestion,
  listAutomationRules,
  listAutomationSuggestions,
  toggleAutomationRule,
  type AutomationRuleDto,
  type AutomationSuggestionDto,
} from "@/lib/api";

export default function AdminAutomationsPage() {
  const [rules, setRules] = useState<AutomationRuleDto[]>([]);
  const [suggestions, setSuggestions] = useState<AutomationSuggestionDto[]>(
    []
  );
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setErr(null);
    Promise.all([listAutomationRules(), listAutomationSuggestions()])
      .then(([r, s]) => {
        setRules(r || []);
        setSuggestions(s || []);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Failed"));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onToggle = async (id: string) => {
    try {
      await toggleAutomationRule(id);
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Toggle failed");
    }
  };

  const onEnableSuggestion = async (id: string) => {
    try {
      await enableAutomationSuggestion(id);
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Enable failed");
    }
  };

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    const t = trigger.trim();
    if (!n || !t) return;
    setSaving(true);
    setErr(null);
    try {
      await createAutomationRule({
        name: n,
        trigger_description: t,
        confidence_threshold: 90,
      });
      setName("");
      setTrigger("");
      load();
    } catch (er) {
      setErr(er instanceof Error ? er.message : "Create failed");
    } finally {
      setSaving(false);
    }
  };

  const suggested = suggestions.filter((x) => x.status === "suggested");

  return (
    <div>
      <h1 className="text-xl font-bold mb-1">Automations</h1>
      <p className="text-[var(--text-secondary)] text-sm mb-6">
        Your rules and pattern-learning suggestions (isolated per user). Toggle
        rules on/off or promote AI suggestions into enabled playbooks.
      </p>
      {err && <p className="text-sm text-[var(--accent-red)] mb-4">{err}</p>}

      <form
        onSubmit={onCreate}
        className="mb-8 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5"
      >
        <h3 className="font-semibold text-sm mb-3">New automation rule</h3>
        <div className="flex flex-col gap-3 max-w-xl">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Rule name"
            className="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
          />
          <textarea
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
            placeholder="When should this run? (natural language)"
            rows={3}
            className="bg-[var(--bg-secondary)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm resize-y min-h-[80px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
          />
          <button
            type="submit"
            disabled={saving || !name.trim() || !trigger.trim()}
            className="self-start px-4 py-2 rounded-lg text-sm font-medium bg-[var(--accent-blue)] text-white border-none disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
          >
            {saving ? "Saving…" : "Create rule"}
          </button>
        </div>
      </form>

      <div className="mb-8">
        <h3 className="font-semibold mb-4 text-sm">Your rules</h3>
        {rules.length === 0 ? (
          <p className="text-sm text-[var(--text-muted)]">
            No rules yet — create one above or enable a suggestion.
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {rules.map((r) => (
              <div
                key={r.id}
                className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 flex flex-wrap items-center gap-4"
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <span
                    className={`w-2 h-2 rounded-full shrink-0 ${
                      r.enabled ?
                        "bg-[var(--accent-green)]"
                      : "bg-[var(--text-muted)]"
                    }`}
                  />
                  <div>
                    <div className="font-medium text-sm">{r.name}</div>
                    <div className="text-[11px] text-[var(--text-muted)]">
                      {r.trigger_description}
                    </div>
                  </div>
                </div>
                <div className="text-xs text-[var(--text-secondary)]">
                  Confidence{" "}
                  <span className="font-semibold">{r.confidence_threshold}%</span>
                </div>
                <div className="text-xs text-[var(--text-muted)]">
                  {r.execution_count} runs
                </div>
                <button
                  type="button"
                  onClick={() => onToggle(r.id)}
                  className="text-xs font-medium px-3 py-1.5 rounded-lg border border-[var(--border)] hover:border-[var(--border-active)]/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)]"
                >
                  {r.enabled ? "Disable" : "Enable"}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-2xl p-5 border border-[var(--border)] bg-gradient-to-br from-[rgba(139,92,246,0.12)] to-[rgba(59,130,246,0.08)]">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full bg-[rgba(139,92,246,0.25)] text-[var(--accent-purple)]">
            AI recommended
          </span>
        </div>
        <h3 className="font-semibold text-sm mb-2">Suggested by pattern learning</h3>
        {suggested.length === 0 ? (
          <p className="text-sm text-[var(--text-secondary)]">
            No open suggestions. The platform surfaces these after consistent
            approval patterns.
          </p>
        ) : (
          <ul className="space-y-4">
            {suggested.map((s) => (
              <li
                key={s.id}
                className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border)]/60 pb-4 last:border-0 last:pb-0"
              >
                <div>
                  <p className="text-sm text-[var(--text-secondary)] leading-relaxed">
                    <span className="font-medium text-[var(--text-primary)]">
                      {s.title}
                    </span>
                    : {s.pattern_summary}
                  </p>
                  {s.est_savings && (
                    <span className="text-xs font-semibold text-[var(--accent-green)]">
                      {s.est_savings}
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => onEnableSuggestion(s.id)}
                  className="shrink-0 text-xs font-semibold px-3 py-1.5 rounded-lg bg-[var(--accent-purple)]/20 text-[var(--accent-purple)] hover:bg-[var(--accent-purple)]/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-purple)]"
                >
                  Enable
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
