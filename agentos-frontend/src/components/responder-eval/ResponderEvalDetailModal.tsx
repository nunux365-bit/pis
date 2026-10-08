"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import type { ResponderEvalDetail } from "@/lib/responderEval";
import {
  isResponderEvalGraded,
  responderGradeChartFill,
  responderGradeLabel,
  responderScoreCardTone,
} from "@/lib/responderGradeVisual";
import {
  buildRubricMetricItems,
  chatRoleLabel,
  chatMessagePlainText,
  compositeMetric,
  evalStatusLabel,
  guardrailRuleLabel,
  hardGateReasonLabel,
  letterGradeMeaning,
  lifecycleStageLabel,
  notGradedReasonLabel,
  resolutionMetric,
  segmentEvalTitle,
  structuralFlagLabel,
  type RubricMetricItem,
  type SegmentEval,
} from "@/lib/responderEvalLabels";
import { ResponderEvalMetricCard } from "@/components/responder-eval/ResponderEvalMetricCard";
import { ResponderEvalGroundTruthPanel } from "@/components/responder-eval/ResponderEvalGroundTruthPanel";
import { formatLocalDateTime } from "@/lib/time";

type Tab = "overview" | "chat" | "rubric" | "ground_truth";

type Props = {
  open: boolean;
  onClose: () => void;
  detail: ResponderEvalDetail | null;
  loading: boolean;
  error: string | null;
};

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "chat", label: "Chat" },
  { id: "rubric", label: "Rubric" },
  { id: "ground_truth", label: "Ground truth" },
];

function asTurnSet(value: unknown): Set<number> {
  if (!Array.isArray(value)) return new Set();
  return new Set(value.map((t) => Number(t)).filter((n) => !Number.isNaN(n)));
}

function SegmentScoreCard({ title, seg }: { title: string; seg: SegmentEval }) {
  const grade = String(seg.letter_grade ?? "-");
  const composite = compositeMetric(
    seg.graded && seg.composite_score != null ? seg.composite_score : null,
    grade
  );
  return (
    <div className={`rounded-xl border p-4 ${responderScoreCardTone(grade)}`}>
      <p className="text-[10px] font-semibold uppercase tracking-wide opacity-80">{title}</p>
      <div className="mt-2 flex flex-wrap items-end gap-3">
        <span
          className="inline-flex h-11 w-11 flex-col items-center justify-center rounded-lg text-white shadow-sm"
          style={{ background: responderGradeChartFill(grade) }}
        >
          <span className="text-lg font-bold leading-none">{responderGradeLabel(grade)}</span>
        </span>
        <div>
          <p className="text-2xl font-bold tabular-nums">{composite.value}</p>
          <p className="text-xs opacity-90">
            Resolution: {resolutionMetric(seg.resolution).value}
          </p>
        </div>
      </div>
    </div>
  );
}

function RubricMetricsGrid({ items }: { items: RubricMetricItem[] }) {
  return (
    <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
      {items.map((m) => (
        <ResponderEvalMetricCard
          key={m.label}
          label={m.label}
          metric={m.metric}
          hint={m.hint}
        />
      ))}
    </div>
  );
}

function RubricMetricsSection({
  title,
  subtitle,
  items,
  toneClass,
}: {
  title: string;
  subtitle?: string;
  items: RubricMetricItem[];
  toneClass?: string;
}) {
  return (
    <section className={`rounded-xl border p-4 ${toneClass ?? "border-[var(--border)] bg-white/40 "}`}>
      <div className="mb-3">
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
          {title}
        </h3>
        {subtitle ? <p className="mt-0.5 text-[10px] text-[var(--text-muted)]">{subtitle}</p> : null}
      </div>
      <RubricMetricsGrid items={items} />
    </section>
  );
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 border-b border-[var(--border)]/50 py-2 last:border-0 sm:flex-row sm:gap-4">
      <dt className="shrink-0 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)] sm:w-36">
        {label}
      </dt>
      <dd className="min-w-0 flex-1 text-sm text-[var(--text-primary)]">{children}</dd>
    </div>
  );
}

function TurnIndexList({ turns }: { turns: unknown }) {
  if (!Array.isArray(turns) || turns.length === 0) {
    return <span className="text-[var(--text-muted)]">None</span>;
  }
  return (
    <span className="font-mono text-xs">
      {turns.map((t, i) => (
        <span key={String(t)}>
          {i > 0 ? ", " : null}
          <span className="rounded bg-amber-100 px-1 py-0.5 text-amber-900 ">
            {String(t)}
          </span>
        </span>
      ))}
    </span>
  );
}

function CitationList({ items }: { items: unknown }) {
  if (!Array.isArray(items) || items.length === 0) {
    return <span className="text-[var(--text-muted)]">None cited</span>;
  }
  return (
    <ul className="space-y-1 text-xs">
      {items.map((item, i) => (
        <li key={i} className="font-mono text-[var(--text-secondary)]">
          {typeof item === "string"
            ? item
            : typeof item === "object" &&
                item !== null &&
                "turn" in item &&
                "field" in item
              ? `Turn ${Number((item as { turn: unknown }).turn) + 1}: ${String((item as { field: unknown }).field)}`
              : JSON.stringify(item)}
        </li>
      ))}
    </ul>
  );
}

function RubricPanel({
  ev,
  graded,
  guardViolations,
  policyViolations,
  traceMode,
  traceScore,
  segmentEvals,
}: {
  ev: Record<string, unknown>;
  graded: boolean;
  guardViolations: Array<{ rule?: string; turns?: number[] }>;
  policyViolations: Array<{ rule?: string; turns?: number[] }>;
  traceMode: unknown;
  traceScore: unknown;
  segmentEvals?: Record<string, SegmentEval>;
}) {
  const structuralFlags = Array.isArray(ev.flags) ? (ev.flags as string[]) : [];
  const segmentEntries = segmentEvals ? Object.entries(segmentEvals) : [];
  const overallMetrics = buildRubricMetricItems(ev, graded, { traceMode });

  return (
    <div className="space-y-5">
      <HardGateBanner ev={ev} />
      {!graded ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-50/50 px-3 py-2 text-sm ">
          <span className="font-medium">Not fully graded.</span>
          {ev.not_graded_reason ? (
            <span className="ml-1 text-[var(--text-secondary)]">
              {notGradedReasonLabel(ev.not_graded_reason)}
            </span>
          ) : null}
        </div>
      ) : null}

      <RubricMetricsSection
        title="Overall rubric"
        subtitle="End-to-end chat quality (composite grade)"
        items={overallMetrics}
        toneClass="border-violet-500/25 bg-violet-50/40 "
      />

      {segmentEntries.map(([key, seg]) => (
        <RubricMetricsSection
          key={key}
          title={`${segmentEvalTitle(key)} rubric`}
          subtitle={
            Array.isArray(seg.turn_indexes) && seg.turn_indexes.length > 0
              ? `Turns: ${seg.turn_indexes.join(", ")}`
              : undefined
          }
          items={buildRubricMetricItems(seg as Record<string, unknown>, Boolean(seg.graded ?? graded), {
            isSegment: true,
          })}
          toneClass={
            key === "bot"
              ? "border-sky-500/25 bg-sky-50/40 "
              : "border-emerald-500/25 bg-emerald-50/40 "
          }
        />
      ))}

      {segmentEntries.length === 0 ? null : (
        <p className="text-[10px] text-[var(--text-muted)]">
          Overall rubric uses the full transcript; bot and human agent rubrics score only that
          speaker&apos;s turns.
        </p>
      )}

      <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-2">
        <h3 className="mb-1 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
          Traceability detail
        </h3>
        <dl>
          <DetailRow label="Final score">
            {traceScore != null ? String(traceScore) : "—"}
          </DetailRow>
          <DetailRow label="Mode">{traceMode ? String(traceMode) : "—"}</DetailRow>
          <DetailRow label="Deterministic">
            {ev.det_traceability_score != null ? String(ev.det_traceability_score) : "—"}
          </DetailRow>
          <DetailRow label="LLM judge">
            {ev.llm_traceability_score != null ? String(ev.llm_traceability_score) : "—"}
          </DetailRow>
          <DetailRow label="Untraceable turns">
            <TurnIndexList turns={ev.untraceable_turns} />
          </DetailRow>
        </dl>
      </section>

      <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-2">
        <h3 className="mb-1 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
          Flagged turns
        </h3>
        <dl>
          <DetailRow label="Hallucination">
            <TurnIndexList turns={ev.hallucination_turns} />
            {ev.hallucination_facts_missed != null ? (
              <p className="mt-1 text-xs text-[var(--text-muted)]">
                {String(ev.hallucination_facts_missed)} fact(s) missed
              </p>
            ) : null}
          </DetailRow>
          <DetailRow label="Tonality">
            <TurnIndexList turns={ev.tonality_turns} />
          </DetailRow>
          <DetailRow label="Sentiment">
            <TurnIndexList turns={ev.sentiment_turns} />
          </DetailRow>
        </dl>
      </section>

      <ViolationList title="Guardrail violations" items={guardViolations} />
      <ViolationList title="Policy violations (judge)" items={policyViolations} />

      <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-2">
        <h3 className="mb-1 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
          Ground-truth citations
        </h3>
        <CitationList items={ev.ground_truth_citations} />
      </section>

      <section className="rounded-xl border border-[var(--border)] bg-[var(--bg-primary)] px-4 py-2">
        <h3 className="mb-1 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
          Transcript stats
        </h3>
        <dl>
          <DetailRow label="Turns">{ev.turn_count != null ? String(ev.turn_count) : "—"}</DetailRow>
          <DetailRow label="AI Bot turns">{ev.bot_turns != null ? String(ev.bot_turns) : "—"}</DetailRow>
          <DetailRow label="Human agent turns">
            {ev.human_agent_turns != null ? String(ev.human_agent_turns) : "—"}
          </DetailRow>
          <DetailRow label="User turns">{ev.user_turns != null ? String(ev.user_turns) : "—"}</DetailRow>
          <DetailRow label="Structural flags">
            {structuralFlags.length === 0 ? (
              <span className="text-[var(--text-muted)]">None (advisory only)</span>
            ) : (
              <ul className="list-inside list-disc text-xs">
                {structuralFlags.map((f) => (
                  <li key={f}>{structuralFlagLabel(f)}</li>
                ))}
              </ul>
            )}
          </DetailRow>
        </dl>
        <p className="pb-2 text-[10px] text-[var(--text-muted)]">
          Structural flags are advisory and do not change the composite score.
        </p>
      </section>
    </div>
  );
}

function ViolationList({
  title,
  items,
}: {
  title: string;
  items: Array<{ rule?: string; turns?: number[] }>;
}) {
  if (!items.length) return null;
  return (
    <div className="mt-4 rounded-lg border border-black/5 bg-white/30 p-3 ">
      <p className="text-[11px] font-semibold uppercase tracking-wide opacity-70">{title}</p>
      <ul className="mt-2 space-y-1.5 text-xs">
        {items.map((v, i) => (
          <li key={`${v.rule}-${i}`} className="flex flex-wrap gap-x-2 gap-y-0.5">
            <span className="font-medium">{guardrailRuleLabel(v.rule ?? "violation")}</span>
            {Array.isArray(v.turns) && v.turns.length > 0 ? (
              <span className="text-[var(--text-muted)]">turns {v.turns.join(", ")}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function HardGateBanner({ ev }: { ev: Record<string, unknown> }) {
  if (ev.hard_gate !== true) return null;
  const reasons = Array.isArray(ev.hard_gate_reasons) ? ev.hard_gate_reasons : [];
  return (
    <div className="rounded-lg border border-red-500/40 bg-red-50/60 px-3 py-2 text-sm ">
      <p className="font-medium text-red-800 ">Hard gate — Grade E</p>
      {reasons.length > 0 ? (
        <ul className="mt-1 list-inside list-disc text-red-700 ">
          {reasons.map((r) => (
            <li key={String(r)}>{hardGateReasonLabel(r)}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function ResponderEvalDetailModal({ open, onClose, detail, loading, error }: Props) {
  const headingId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<Element | null>(null);
  const [tab, setTab] = useState<Tab>("overview");

  useLayoutEffect(() => {
    if (!open) return;
    const d = dialogRef.current;
    if (!d) return;
    previouslyFocused.current = document.activeElement;
    if (!d.open) d.showModal();
    queueMicrotask(() => closeRef.current?.focus());
    return () => {
      if (d.open) d.close();
      const prev = previouslyFocused.current as HTMLElement | null;
      prev?.focus?.();
      previouslyFocused.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (open) setTab("overview");
  }, [open, detail?.id]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [tab, detail?.id]);

  const ev = (detail?.eval ?? {}) as Record<string, unknown>;
  const graded = detail ? isResponderEvalGraded(detail) : false;
  const messages = ((detail?.chat as { messages?: unknown[] })?.messages ?? []) as Array<{
    role?: string;
    content?: string;
    created_at?: string;
  }>;

  const flaggedTurns = useMemo(() => {
    const hal = asTurnSet(ev.hallucination_turns);
    const untrace = asTurnSet(ev.untraceable_turns);
    const tonality = asTurnSet(ev.tonality_turns);
    const sentiment = asTurnSet(ev.sentiment_turns);
    return { hal, untrace, tonality, sentiment };
  }, [ev]);

  const guardViolations = (Array.isArray(ev.violations) ? ev.violations : []) as Array<{
    rule?: string;
    turns?: number[];
  }>;
  const policyViolations = (Array.isArray(ev.policy_violations) ? ev.policy_violations : []) as Array<{
    rule?: string;
    turns?: number[];
  }>;

  const traceScore = ev.traceability_score;
  const traceMode = ev.traceability_mode;
  const composite = compositeMetric(
    graded && detail?.composite_score != null ? detail.composite_score : null,
    detail?.letter_grade ?? "-"
  );
  const segmentEvals = ev.segment_evals as Record<string, SegmentEval> | undefined;
  const segmentEntries = segmentEvals
    ? (Object.entries(segmentEvals) as [string, SegmentEval][])
    : [];
  const overallRubricMetrics = buildRubricMetricItems(ev, graded, { traceMode });

  const isTurnHighlighted = useCallback(
    (i: number) =>
      flaggedTurns.hal.has(i) ||
      flaggedTurns.untrace.has(i) ||
      flaggedTurns.tonality.has(i) ||
      flaggedTurns.sentiment.has(i),
    [flaggedTurns]
  );

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      aria-modal="true"
      aria-labelledby={headingId}
      className="fixed inset-0 z-50 m-0 flex max-h-none w-full max-w-none items-end justify-center overflow-hidden border-0 bg-transparent p-0 sm:items-center sm:p-4 [&::backdrop]:bg-black/55"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div
        className="flex h-[min(92dvh,92vh)] max-h-[min(92dvh,92vh)] w-full max-w-4xl flex-col overflow-hidden rounded-t-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl sm:h-[min(88vh,920px)] sm:max-h-[min(88vh,920px)] sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 border-b border-[var(--border)] bg-[var(--bg-card)] px-4 py-4 sm:px-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-violet-600 ">
                Eval detail
              </p>
              <h2 id={headingId} className="text-lg font-semibold text-[var(--text-primary)]">
                Chat quality review
              </h2>
              {detail ? (
                <p className="mt-1 truncate font-mono text-xs text-[var(--text-muted)]">
                  {detail.chat_id}
                  {detail.order_id ? (
                    <>
                      {" · "}
                      <Link
                        href={`/order-rca?po=${encodeURIComponent(detail.order_id)}`}
                        className="text-violet-700 underline-offset-2 hover:underline "
                      >
                        {detail.order_id}
                      </Link>
                    </>
                  ) : null}
                </p>
              ) : null}
            </div>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--bg-secondary)]"
            >
              Close
            </button>
          </div>
        </div>

        {loading ? (
          <p className="shrink-0 px-5 py-8 text-sm text-[var(--text-muted)]">Loading detail…</p>
        ) : null}
        {error ? (
          <p className="mx-5 my-4 shrink-0 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 ">
            {error}
          </p>
        ) : null}

        {detail ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div
              className="flex shrink-0 gap-1 overflow-x-auto border-b border-[var(--border)] bg-[var(--bg-card)] px-4 py-2 sm:px-5"
              role="tablist"
              aria-label="Eval detail sections"
            >
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  aria-selected={tab === t.id}
                  className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                    tab === t.id
                      ? "bg-violet-600 text-white shadow-sm "
                      : "text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)]"
                  }`}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            <div
              ref={bodyRef}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-5"
            >
              {tab === "overview" ? (
                <div className={`rounded-xl border p-5 ${responderScoreCardTone(detail.letter_grade)}`}>
                  <p className="text-[10px] font-semibold uppercase tracking-wide opacity-80">
                    {segmentEntries.length > 0 ? "Overall" : "Composite"}
                  </p>
                  <div className="mt-1 flex flex-wrap items-end gap-3">
                    <span
                      className="inline-flex h-14 w-14 flex-col items-center justify-center rounded-xl text-white shadow-md"
                      style={{ background: responderGradeChartFill(detail.letter_grade) }}
                    >
                      <span className="text-2xl font-bold leading-none">
                        {responderGradeLabel(detail.letter_grade)}
                      </span>
                      <span className="mt-0.5 text-[9px] font-medium uppercase leading-none opacity-90">
                        {letterGradeMeaning(detail.letter_grade)}
                      </span>
                    </span>
                    <div>
                      <p className="text-3xl font-bold tabular-nums">{composite.value}</p>
                      <p className="text-sm font-medium opacity-90">{composite.meaning}</p>
                      <p className="text-xs opacity-70">
                        {graded
                          ? segmentEntries.length > 0
                            ? "Overall composite (0–100)"
                            : "Composite score (0–100)"
                          : "Not graded"}
                      </p>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-xs">
                    <span className="rounded-md bg-black/5 px-2 py-1 ">
                      Status: {evalStatusLabel(detail.eval_status)}
                    </span>
                    {detail.rca_run_id ? (
                      <span className="rounded-md bg-black/5 px-2 py-1 font-mono ">
                        RCA run: {detail.rca_run_id}
                      </span>
                    ) : null}
                  </div>
                  {!graded && ev.not_graded_reason ? (
                    <p className="mt-3 text-sm opacity-80">
                      Reason: {notGradedReasonLabel(ev.not_graded_reason)}
                    </p>
                  ) : null}
                  {segmentEntries.length > 0 ? (
                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                      {segmentEntries.map(([key, seg]) => (
                        <SegmentScoreCard key={key} title={segmentEvalTitle(key)} seg={seg} />
                      ))}
                    </div>
                  ) : null}
                  <HardGateBanner ev={ev} />
                  {ev.policy_lifecycle_stage ? (
                    <p className="mt-2 text-xs opacity-75">
                      Policy lifecycle: {lifecycleStageLabel(ev.policy_lifecycle_stage)}
                    </p>
                  ) : null}
                  {ev.hallucination_severity ? (
                    <p className="mt-1 text-xs opacity-75">
                      Hallucination severity: {String(ev.hallucination_severity)}
                    </p>
                  ) : null}
                  <p className="mt-3 text-xs opacity-75">Rubric version {detail.eval_version}</p>

                  {segmentEntries.length > 0 ? (
                    <div className="mt-5 space-y-4">
                      <RubricMetricsSection
                        title="Overall rubric"
                        subtitle="Full conversation"
                        items={overallRubricMetrics}
                        toneClass="border-black/10 bg-white/30 "
                      />
                      {segmentEntries.map(([key, seg]) => (
                        <RubricMetricsSection
                          key={key}
                          title={`${segmentEvalTitle(key)} rubric`}
                          subtitle={`Scores for ${segmentEvalTitle(key).toLowerCase()} turns only`}
                          items={buildRubricMetricItems(
                            seg as Record<string, unknown>,
                            Boolean(seg.graded ?? graded),
                            { isSegment: true }
                          )}
                          toneClass="border-black/10 bg-white/20 "
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="mt-5">
                      <RubricMetricsGrid items={overallRubricMetrics} />
                    </div>
                  )}
                  <ViolationList title="Guardrail violations" items={guardViolations} />
                  <ViolationList title="Policy violations" items={policyViolations} />
                </div>
              ) : null}

              {tab === "chat" ? (
                <div className="space-y-3">
                  <p className="text-[11px] text-[var(--text-muted)]">
                    Transcript may be redacted.{" "}
                    <span className="font-mono">[customer]</span> /{" "}
                    <span className="font-mono">[agent]</span> are privacy placeholders in text,
                    not speaker labels. Highlighted turns were flagged in the rubric.
                  </p>
                  {messages.length === 0 ? (
                    <p className="text-sm text-[var(--text-muted)]">No messages in this chat.</p>
                  ) : (
                    messages.map((m, i) => {
                      const isUser = String(m.role).toLowerCase() === "user";
                      const highlighted = isTurnHighlighted(i);
                      return (
                        <div
                          key={`${m.created_at ?? "t"}-${i}`}
                          className={`rounded-xl border p-3 text-sm ${
                            highlighted
                              ? "border-amber-500/40 bg-amber-50/60 "
                              : isUser
                                ? "border-sky-500/20 bg-sky-50/50 "
                                : "border-[var(--border)] bg-[var(--bg-primary)]"
                          }`}
                        >
                          <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                            <span>{chatRoleLabel(m.role)}</span>
                            <span>· turn {i}</span>
                            {highlighted ? <span className="text-amber-700 ">· flagged</span> : null}
                            {m.created_at ? (
                              <span>· {formatLocalDateTime(m.created_at)}</span>
                            ) : null}
                          </div>
                          <div className="whitespace-pre-wrap leading-relaxed text-[var(--text-primary)]">
                            {chatMessagePlainText(m.content)}
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              ) : null}

              {tab === "rubric" ? (
                <RubricPanel
                  ev={ev}
                  graded={graded}
                  guardViolations={guardViolations}
                  policyViolations={policyViolations}
                  traceMode={traceMode}
                  traceScore={traceScore}
                  segmentEvals={segmentEvals}
                />
              ) : null}

              {tab === "ground_truth" ? (
                <ResponderEvalGroundTruthPanel
                  artifact={(detail.eval_ground_truth ?? {}) as Record<string, unknown>}
                  lifecycleStage={ev.policy_lifecycle_stage}
                />
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </dialog>
  );
}
