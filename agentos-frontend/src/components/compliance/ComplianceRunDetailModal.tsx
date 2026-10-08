"use client";

import { useCallback, useId, useLayoutEffect, useRef, useState } from "react";
import {
  complianceHasRubricSummary,
  complianceScoreCardTone,
  formatCompositePctDisplay,
} from "@/lib/complianceGradeVisual";

export type ComplianceRunDetailData = {
  id: string;
  status: string;
  created_at: string;
  updated_at: string;
  input_data: Record<string, unknown>;
  output_data: Record<string, unknown>;
  error_message: string | null;
};

type TabId = "overview" | "transcript" | "rubric" | "technical";

const TAB_IDS: readonly TabId[] = ["overview", "transcript", "rubric", "technical"] as const;

function labelForInputKey(k: string): string {
  return k.replace(/_/g, " ");
}

function readStringOrNumber(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  return "";
}

/** Turn rubric snake_case ids into a short reading title (MTC, MEN2, BMI, “history” for hx). */
function rubricItemTitle(itemId: string | undefined): string {
  const id = (itemId ?? "").trim();
  if (!id) return "Criterion";
  const spaced = id.replace(/_/g, " ");
  const titled = spaced.replace(/\w\S*/g, (txt) => txt.charAt(0).toUpperCase() + txt.slice(1).toLowerCase());
  return titled
    .replace(/\bMtc\b/g, "MTC")
    .replace(/\bMen2\b/g, "MEN2")
    .replace(/\bBmi\b/g, "BMI")
    .replace(/\bHx\b/g, "history")
    .replace(/\bSu\b/g, "SU")
    .replace(/\bNa\b/g, "NA");
}

/** High-contrast rubric status pills (light + dark). */
function rubricStatusBadgeClass(status: string | undefined): string {
  const u = String(status ?? "")
    .trim()
    .toUpperCase();
  if (u === "YES" || u === "PASS" || u === "MET") {
    return "shrink-0 rounded-md bg-emerald-600 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white shadow-sm ring-1 ring-emerald-700/30 dark:bg-emerald-600 dark:text-white";
  }
  if (u === "NO" || u === "FAIL") {
    return "shrink-0 rounded-md bg-rose-600 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white shadow-sm ring-1 ring-rose-800/25 dark:bg-rose-600 dark:text-white";
  }
  if (u === "NA" || u === "N/A" || u === "UNKNOWN" || u === "UNCLEAR") {
    return "shrink-0 rounded-md bg-slate-500 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-white shadow-sm ring-1 ring-slate-600/30 dark:bg-slate-600 dark:text-white";
  }
  return "shrink-0 rounded-md bg-amber-500 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-950 shadow-sm ring-1 ring-amber-700/25 dark:bg-amber-500 dark:text-amber-950";
}

type Props = {
  open: boolean;
  onClose: () => void;
  detail: ComplianceRunDetailData | null;
  loading: boolean;
  error: string | null;
};

export function ComplianceRunDetailModal({ open, onClose, detail, loading, error }: Props) {
  const headingId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  const previouslyFocused = useRef<Element | null>(null);
  const [tab, setTab] = useState<TabId>("overview");

  useLayoutEffect(() => {
    if (!open) return;
    const d = dialogRef.current;
    if (!d) return;
    previouslyFocused.current = document.activeElement;
    if (!d.open) {
      d.showModal();
    }
    queueMicrotask(() => {
      closeRef.current?.focus();
    });
    return () => {
      if (d.open) {
        d.close();
      }
      const prev = previouslyFocused.current as HTMLElement | null;
      if (prev && typeof prev.focus === "function") {
        prev.focus();
      }
      previouslyFocused.current = null;
    };
  }, [open]);

  const focusTab = useCallback((id: TabId) => {
    setTab(id);
    queueMicrotask(() => {
      tabRefs.current[id]?.focus();
    });
  }, []);

  const onTabListKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const i = TAB_IDS.indexOf(tab);
      if (i < 0) return;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = TAB_IDS[(i + 1) % TAB_IDS.length];
        focusTab(next);
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = TAB_IDS[(i - 1 + TAB_IDS.length) % TAB_IDS.length];
        focusTab(next);
      } else if (e.key === "Home") {
        e.preventDefault();
        focusTab(TAB_IDS[0]);
      } else if (e.key === "End") {
        e.preventDefault();
        focusTab(TAB_IDS[TAB_IDS.length - 1]);
      }
    },
    [tab, focusTab]
  );

  if (!open) return null;

  const inp = detail?.input_data ?? {};
  const out = detail?.output_data ?? {};
  const normTranscript = String((out as { normalized_transcript_text?: string }).normalized_transcript_text || "");
  const rawTranscript = String((out as { transcript_text?: string }).transcript_text || "");
  const shownTranscript = normTranscript || rawTranscript || "—";
  const ev = (out.eval as Record<string, unknown> | undefined) ?? undefined;
  const scored = Array.isArray(ev?.scored_items) ? (ev.scored_items as Record<string, unknown>[]) : [];
  const hasRubricSummary = complianceHasRubricSummary(ev);
  const secondOpinionConversationId =
    readStringOrNumber((inp as { mysql_second_opinion_conversation_id?: unknown }).mysql_second_opinion_conversation_id) ||
    readStringOrNumber((inp as { second_opinion_conversation_id?: unknown }).second_opinion_conversation_id);

  return (
    <dialog
      ref={dialogRef}
      aria-modal="true"
      aria-labelledby={headingId}
      className="fixed inset-0 z-50 m-0 flex max-h-none w-full max-w-none items-end justify-center border-0 bg-transparent p-0 sm:items-center sm:p-4 [&::backdrop]:bg-black/55"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div
        className="relative z-10 flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-t-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex flex-shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
          <div>
            <h2 id={headingId} className="text-base font-semibold tracking-tight text-[var(--text-primary)]">
              Run detail
            </h2>
            <p className="mt-0.5 text-xs text-[var(--text-muted)]">
              Review metadata, transcript, and rubric. Use tabs (arrow keys) or press Esc to close.
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--bg-elev)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
            onClick={onClose}
          >
            Close
          </button>
        </header>

        <div className="border-b border-[var(--border)] px-4 pt-2">
          <div
            role="tablist"
            aria-label="Run detail sections"
            className="flex flex-wrap gap-1"
            onKeyDown={onTabListKeyDown}
          >
            {(
              [
                ["overview", "Overview"],
                ["transcript", "Transcript"],
                ["rubric", "Rubric"],
                ["technical", "Technical"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                ref={(el) => {
                  tabRefs.current[id] = el;
                }}
                type="button"
                role="tab"
                aria-selected={tab === id}
                id={`tab-${id}`}
                aria-controls={`panel-${id}`}
                tabIndex={tab === id ? 0 : -1}
                className={`rounded-t-md px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50 ${
                  tab === id
                    ? "bg-[var(--bg-primary)] text-[var(--text-primary)] ring-1 ring-b-0 ring-[var(--border)]"
                    : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                }`}
                onClick={() => focusTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {loading ? (
            <p className="text-sm text-[var(--text-muted)]">Loading…</p>
          ) : error ? (
            <div
              className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200"
              role="alert"
            >
              {error}
            </div>
          ) : !detail ? (
            <p className="text-sm text-[var(--text-muted)]">No data.</p>
          ) : (
            <>
              <div
                id="panel-overview"
                role="tabpanel"
                aria-labelledby="tab-overview"
                hidden={tab !== "overview"}
                className={tab === "overview" ? "space-y-4" : undefined}
              >
                {hasRubricSummary ? (
                  <div
                    className={`rounded-xl border px-4 py-4 ${complianceScoreCardTone(
                      ev?.grade as string | undefined,
                      ev?.composite_pct
                    )}`}
                  >
                    <p className="text-[11px] font-semibold uppercase tracking-wide opacity-80">Score</p>
                    <p className="mt-1 text-3xl font-semibold tabular-nums">
                      {formatCompositePctDisplay(ev?.composite_pct)}
                      <span className="ml-3 text-xl font-semibold">Grade {String(ev?.grade ?? "—")}</span>
                    </p>
                    {ev?.grade_label ? (
                      <p className="mt-1 text-sm opacity-90">{String(ev.grade_label)}</p>
                    ) : null}
                    {ev?.rubric_authority_version ? (
                      <p className="mt-2 text-xs opacity-75">Rubric v{String(ev.rubric_authority_version)}</p>
                    ) : null}
                  </div>
                ) : (
                  <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-3 text-sm text-[var(--text-secondary)]">
                    <p className="font-medium text-[var(--text-primary)]">No rubric summary on this run</p>
                    <p className="mt-1 text-xs text-[var(--text-muted)]">
                      Open <strong className="text-[var(--text-secondary)]">Rubric</strong> or{" "}
                      <strong className="text-[var(--text-secondary)]">Technical</strong> for raw eval data.
                    </p>
                  </div>
                )}
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                    File & source
                  </h3>
                  {secondOpinionConversationId ? (
                    <div className="mt-2 rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-2">
                      <div className="text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
                        Second opinion conversation id
                      </div>
                      <div className="mt-1 break-all font-mono text-xs text-[var(--text-primary)]">
                        {secondOpinionConversationId}
                      </div>
                    </div>
                  ) : null}
                  <dl className="mt-2 grid gap-2 sm:grid-cols-2">
                    {Object.entries(inp)
                      .filter(([k]) => !k.startsWith("_"))
                      .map(([k, v]) => (
                        <div
                          key={k}
                          className="rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-2"
                        >
                          <dt className="text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
                            {labelForInputKey(k)}
                          </dt>
                          <dd className="mt-1 break-all font-mono text-xs text-[var(--text-primary)]">
                            {typeof v === "object" ? JSON.stringify(v) : String(v ?? "—")}
                          </dd>
                        </div>
                      ))}
                  </dl>
                </div>
                <div className="flex flex-wrap gap-2 text-xs text-[var(--text-muted)]">
                  <span>
                    Status:{" "}
                    <strong className="text-[var(--text-primary)]">{detail.status}</strong>
                  </span>
                  <span aria-hidden>·</span>
                  <span>Created {new Date(detail.created_at).toLocaleString()}</span>
                </div>
              </div>

              <div
                id="panel-transcript"
                role="tabpanel"
                aria-labelledby="tab-transcript"
                hidden={tab !== "transcript"}
                className={tab === "transcript" ? "space-y-2" : undefined}
              >
                <p className="text-xs text-[var(--text-muted)]">
                  Grading source:{" "}
                  <span className="font-medium text-[var(--text-secondary)]">
                    {String((out as { transcript_grading_source?: string }).transcript_grading_source || "canonical")}
                  </span>
                </p>
                <div className="max-h-[min(60vh,520px)] overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] p-4 text-sm leading-relaxed text-[var(--text-primary)]">
                  {shownTranscript}
                </div>
                {normTranscript ? (
                  <details className="rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] p-3">
                    <summary className="cursor-pointer text-xs font-medium text-[var(--text-muted)]">
                      View original ASR transcript
                    </summary>
                    <pre className="mt-2 max-h-[28vh] overflow-auto whitespace-pre-wrap text-[12px] leading-relaxed text-[var(--text-secondary)]">
                      {rawTranscript || "—"}
                    </pre>
                  </details>
                ) : null}
                {(() => {
                  const dg = (out as { deepgram_summary?: Record<string, unknown> }).deepgram_summary;
                  const aq = dg?.asr_word_quality;
                  if (!aq || typeof aq !== "object") return null;
                  const m = aq as Record<string, unknown>;
                  return (
                    <p className="text-xs text-[var(--text-muted)]">
                      ASR words: mean confidence {String(m.mean_word_confidence ?? "—")} · low-confidence words{" "}
                      {String(m.pct_words_below_0_5 ?? "—")}%
                    </p>
                  );
                })()}
              </div>

              <div
                id="panel-rubric"
                role="tabpanel"
                aria-labelledby="tab-rubric"
                hidden={tab !== "rubric"}
                className={tab === "rubric" ? "space-y-3" : undefined}
              >
                {!ev ? (
                  <p className="text-sm text-[var(--text-muted)]">No eval payload.</p>
                ) : scored.length === 0 ? (
                  <p className="text-sm text-[var(--text-muted)]">No scored items.</p>
                ) : (
                  <div className="space-y-2">
                    <p className="text-[12px] leading-snug text-[var(--text-muted)]">
                      Each card is one rubric item. The line in monospace is the internal criterion id (also in Technical
                      JSON).
                    </p>
                    <ul className="m-0 list-none space-y-2 p-0">
                      {scored.map((row, idx) => {
                        const rec = row as {
                          item_id?: string;
                          status?: string;
                          evidence?: string;
                          rationale?: string;
                        };
                        const st = String(rec.status ?? "");
                        const idRaw = String(rec.item_id ?? "");
                        const body = String(rec.evidence ?? rec.rationale ?? "");
                        return (
                          <li
                            key={idRaw || `rubric-${idx}`}
                            className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2.5 shadow-sm"
                          >
                            <div className="flex flex-wrap items-start justify-between gap-2 gap-y-1">
                              <div className="min-w-0 flex-1">
                                <h3 className="text-[15px] font-semibold leading-snug tracking-tight text-[var(--text-primary)]">
                                  {rubricItemTitle(rec.item_id)}
                                </h3>
                                <p className="mt-0.5 font-mono text-[11px] leading-normal text-[var(--text-muted)]">
                                  {idRaw || "—"}
                                </p>
                              </div>
                              <span className={rubricStatusBadgeClass(st)}>{st || "—"}</span>
                            </div>
                            {body ? (
                              <p className="mt-2 border-t border-[var(--border)]/70 pt-2 text-[13px] leading-relaxed text-[var(--text-primary)]">
                                {body}
                              </p>
                            ) : null}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                )}
              </div>

              <div
                id="panel-technical"
                role="tabpanel"
                aria-labelledby="tab-technical"
                hidden={tab !== "technical"}
                className={tab === "technical" ? "space-y-3" : undefined}
              >
                {Array.isArray(ev?.openai_response_ids) && (ev.openai_response_ids as unknown[]).length > 0 ? (
                  <p className="break-all font-mono text-[11px] text-[var(--text-muted)]">
                    OpenAI responses: {(ev.openai_response_ids as unknown[]).map(String).join(", ")}
                  </p>
                ) : null}
                {detail.error_message ? (
                  <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
                    {detail.error_message}
                  </p>
                ) : null}
                <details className="rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] p-3">
                  <summary className="cursor-pointer text-sm font-medium text-[var(--text-secondary)]">
                    Raw input JSON
                  </summary>
                  <pre className="mt-2 max-h-[35vh] overflow-auto text-[11px] leading-relaxed">
                    {JSON.stringify(inp, null, 2)}
                  </pre>
                </details>
                <details className="rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] p-3">
                  <summary className="cursor-pointer text-sm font-medium text-[var(--text-secondary)]">
                    Raw eval JSON
                  </summary>
                  <pre className="mt-2 max-h-[35vh] overflow-auto text-[11px] leading-relaxed">
                    {JSON.stringify(ev ?? {}, null, 2)}
                  </pre>
                </details>
              </div>
            </>
          )}
        </div>
      </div>
    </dialog>
  );
}
