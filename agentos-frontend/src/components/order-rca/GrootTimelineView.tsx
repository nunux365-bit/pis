"use client";

import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import {
  buildGlobalStatusSegments,
  buildGrootInsights,
  filterGrootMilestones,
  formatEventRangeLabel,
  GROOT_STATUS_PILL_CLASS,
  GROOT_STATUS_TONE_CLASS,
  failureSignalsCardTone,
  groupGrootAttempts,
  grootStatusTone,
  grootToneToInsightCard,
  outcomeToneToInsightCard,
  isMissingDisplay,
  normalizeGrootEvents,
  segmentWidthPcts,
  sortGrootEventsChronological,
  summarizeGrootTimeline,
  type GrootEvent,
  type GrootFailureInsight,
  type GrootStatusAggregate,
  type GrootTimelineSegment,
} from "@/lib/grootTimelineUtils";
import { displayVal } from "@/lib/orderRca";
import {
  GROOT_ACCENT_BORDER,
  GROOT_BODY,
  GROOT_CALLOUT,
  GROOT_CALLOUT_DISCLAIMER,
  GROOT_CHIP,
  GROOT_EMPTY,
  GROOT_HEADLINE,
  GROOT_HEADLINE_COMPACT,
  GROOT_META,
  GROOT_PANEL,
  GROOT_SECTION_BLOCK,
  GROOT_SECTION_HINT,
  GROOT_SECTION_TITLE,
  GROOT_STAT_GRID,
  GROOT_STAT_LABEL,
  GROOT_STAT_SUB,
  GROOT_STAT_VALUE,
  GROOT_STAT_VALUE_HERO,
  GROOT_TAB_ACTIVE,
  GROOT_TAB_IDLE,
} from "@/components/order-rca/grootTimelineStyles";

type ViewMode = "overview" | "attempts" | "list";

function StatusStrip({
  segments,
  rangeStart,
  rangeEnd,
  heightClass = "h-3",
  title,
}: {
  segments: GrootTimelineSegment[];
  rangeStart: number;
  rangeEnd: number;
  heightClass?: string;
  title?: string;
}) {
  const widths = useMemo(
    () => segmentWidthPcts(segments, rangeStart, rangeEnd),
    [segments, rangeStart, rangeEnd],
  );

  if (!segments.length) {
    return (
      <p className={GROOT_META}>
        No plottable status bands (events may lack parseable IST timestamps — use All events).
      </p>
    );
  }

  return (
    <div className="space-y-1">
      {title ? <p className={GROOT_META}>{title}</p> : null}
      <div
        className={cn("flex w-full overflow-hidden rounded-md border border-slate-200 bg-slate-100", heightClass)}
        role="img"
        aria-label={title ?? "Groot status over time"}
      >
        {segments.map((seg, i) => {
          const tone = grootStatusTone(seg.status);
          const pct = widths[i] ?? 100 / segments.length;
          const durationMin = Math.max(1, Math.round((seg.endMs - seg.startMs) / 60_000));
          const rangeNote = seg.timeUnknown
            ? "Time unknown"
            : `${new Date(seg.startMs).toISOString().slice(11, 16)}–${new Date(seg.endMs).toISOString().slice(11, 16)}`;
          return (
            <div
              key={`${seg.startMs}-${seg.status}-${i}`}
              className={cn(
                "min-w-[1px] shrink-0",
                GROOT_STATUS_TONE_CLASS[tone],
                seg.timeUnknown && "opacity-50",
              )}
              style={{ width: `${pct}%` }}
              title={`${seg.label}\n${rangeNote}\n~${durationMin} min`}
            />
          );
        })}
      </div>
    </div>
  );
}

type InsightCardTone = "slate" | "amber" | "rose" | "emerald" | "sky";

function InsightStatCard({
  label,
  value,
  sub,
  tone = "slate",
  variant = "default",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: InsightCardTone;
  /** hero = full tint + large value; accent = white card + colored left bar; default = tinted or neutral */
  variant?: "hero" | "accent" | "default";
}) {
  const fill: Record<InsightCardTone, string> = {
    slate: "bg-slate-50 border-slate-200",
    amber: "bg-amber-50 border-amber-200",
    rose: "bg-rose-50 border-rose-200",
    emerald: "bg-emerald-50 border-emerald-200",
    sky: "bg-sky-50 border-sky-200",
  };
  const accentBar = GROOT_ACCENT_BORDER[tone] ?? GROOT_ACCENT_BORDER.slate;

  if (variant === "accent") {
    return (
      <div
        className={cn(
          "min-w-0 rounded-lg border border-slate-200 border-l-4 bg-white px-3 py-2",
          accentBar,
        )}
      >
        <p className={GROOT_STAT_LABEL}>{label}</p>
        <p className={cn("mt-1", GROOT_STAT_VALUE)}>{value}</p>
        {sub ? <p className={GROOT_STAT_SUB}>{sub}</p> : null}
      </div>
    );
  }

  if (variant === "hero") {
    return (
      <div className={cn("min-w-0 rounded-lg border px-3 py-2.5", fill[tone])}>
        <p className={GROOT_STAT_LABEL}>{label}</p>
        <p className={cn("mt-1", GROOT_STAT_VALUE_HERO)}>{value}</p>
        {sub ? <p className={GROOT_STAT_SUB}>{sub}</p> : null}
      </div>
    );
  }

  return (
    <div className={cn("min-w-0 rounded-lg border px-3 py-2", fill[tone])}>
      <p className={GROOT_STAT_LABEL}>{label}</p>
      <p className={cn("mt-1", GROOT_STAT_VALUE)}>{value}</p>
      {sub ? <p className={GROOT_STAT_SUB}>{sub}</p> : null}
    </div>
  );
}

function DwellBarChart({ stages }: { stages: GrootStatusAggregate[] }) {
  const top = stages.filter((s) => s.dwellMin > 0 || s.eventCount > 0).slice(0, 8);
  if (!top.length) {
    return <p className={GROOT_META}>Not enough timed stages to rank dwell.</p>;
  }
  const max = Math.max(...top.map((s) => s.dwellMin), 1);
  return (
    <div className="space-y-2.5" role="img" aria-label="Time spent by Groot status">
      {top.map((s) => (
        <div key={s.status}>
          <div className="mb-0.5 flex items-baseline justify-between gap-2">
            <span className={cn(GROOT_BODY, "font-medium")}>{s.label}</span>
            <span className={cn("shrink-0 tabular-nums", GROOT_META)}>
              {s.dwellDisplay}
              {s.pctOfJourney > 0 ? ` · ${s.pctOfJourney}%` : ""}
              {s.eventCount > 1 ? ` · ×${s.eventCount} events` : ""}
            </span>
          </div>
          <div className="h-3 overflow-hidden rounded-full bg-slate-200/80">
            <div
              className={cn("h-full rounded-full transition-[width]", GROOT_STATUS_TONE_CLASS[s.tone])}
              style={{ width: `${Math.max(4, (s.dwellMin / max) * 100)}%` }}
              title={`${s.label}: ${s.dwellDisplay}, ${s.eventCount} events`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function RepeatedStagesRow({ stages }: { stages: GrootStatusAggregate[] }) {
  if (!stages.length) {
    return <p className={GROOT_EMPTY}>No stage repeated more than once (by event count).</p>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {stages.slice(0, 12).map((s) => (
        <span
          key={s.status}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
            GROOT_STATUS_PILL_CLASS[s.tone],
          )}
          title={`${s.dwellDisplay} total dwell · ${s.segmentVisits} visits`}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", GROOT_STATUS_TONE_CLASS[s.tone])} />
          {s.label}
          <span className="tabular-nums text-slate-500">×{s.eventCount}</span>
        </span>
      ))}
      {stages.length > 12 ? (
        <span className="text-[10px] text-slate-500">+{stages.length - 12} more</span>
      ) : null}
    </div>
  );
}

function RiderChurnPanel({
  churn,
}: {
  churn: ReturnType<typeof buildGrootInsights>["riderChurn"];
}) {
  if (!churn.assignCount && !churn.unassignCount && !churn.distinctRiders.length) {
    return <p className={GROOT_EMPTY}>No rider assign / unassign events in Groot comments.</p>;
  }
  return (
    <div className="space-y-2 rounded-md border border-sky-200 bg-sky-50/80 px-3 py-2">
      <p className={GROOT_BODY}>
        <span className="font-semibold">{churn.assignCount}</span> assigns ·{" "}
        <span className="font-semibold">{churn.unassignCount}</span> unassigns ·{" "}
        <span className="font-semibold">{churn.distinctRiders.length}</span> riders
      </p>
      {churn.distinctRiders.length ? (
        <p className={GROOT_META}>Riders: {churn.distinctRiders.join(", ")}</p>
      ) : null}
      {churn.perAttemptRiders.length > 1 ? (
        <div className="space-y-1">
          <p className={cn(GROOT_META, "font-semibold")}>Per attempt (newest first)</p>
          {churn.perAttemptRiders.slice(0, 5).map((a) => (
            <p key={a.attemptId} className={GROOT_META}>
              Attempt {a.attemptId}: {a.riders.length ? a.riders.join(", ") : "—"}
            </p>
          ))}
          {churn.perAttemptRiders.length > 5 ? (
            <p className={GROOT_META}>+{churn.perAttemptRiders.length - 5} more attempts</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function FulfillmentLegsRow({
  legs,
}: {
  legs: ReturnType<typeof buildGrootInsights>["fulfillmentLegs"];
}) {
  if (!legs.length) return <p className={GROOT_EMPTY}>No distinct pickup or forward-delivery leg in Groot statuses.</p>;
  return (
    <div className="flex flex-wrap gap-2">
      {legs.map((leg) => (
        <div
          key={leg.leg}
          className={cn(
            "min-w-[140px] flex-1 rounded-lg border px-3 py-2",
            leg.leg === "pickup" ? "border-amber-200 bg-amber-50" : "border-sky-200 bg-sky-50",
          )}
        >
          <p className={GROOT_STAT_LABEL}>{leg.label}</p>
          <p className={cn("mt-1", GROOT_STAT_VALUE)}>{leg.dwellDisplay}</p>
          <p className={GROOT_META}>
            {leg.eventCount} events · tracked wait between Groot pings
          </p>
        </div>
      ))}
    </div>
  );
}

function FailureInsightCards({ items }: { items: GrootFailureInsight[] }) {
  if (!items.length) {
    return (
      <p className={cn(GROOT_CALLOUT, "border-emerald-200 bg-emerald-50 text-emerald-900")}>
        No failure patterns detected in Groot comments (cancel / reschedule / unassign / hub).
      </p>
    );
  }
  const toneClass: Record<GrootFailureInsight["tone"], string> = {
    danger: "border-rose-200 bg-rose-50 text-rose-950",
    warning: "border-amber-200 bg-amber-50 text-amber-950",
    info: "border-sky-200 bg-sky-50 text-sky-950",
  };
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {items.map((f) => (
        <div key={f.id} className={cn("rounded-lg border px-3 py-2", toneClass[f.tone])}>
          <div className="flex items-start justify-between gap-2">
            <p className={cn(GROOT_BODY, "font-semibold")}>{f.title}</p>
            <span
              className={cn(
                "shrink-0 rounded-full bg-white/80 px-1.5 py-0.5 font-bold tabular-nums",
                GROOT_META,
              )}
            >
              {f.count}
            </span>
          </div>
          <p className={cn("mt-1 leading-relaxed", GROOT_META)}>{f.detail}</p>
        </div>
      ))}
    </div>
  );
}

function StatusLegend() {
  const items: Array<{ tone: keyof typeof GROOT_STATUS_TONE_CLASS; label: string }> = [
    { tone: "neutral", label: "Created / parked / placed" },
    { tone: "progress", label: "Assigned / picked" },
    { tone: "delivery", label: "Out for delivery or pickup / on the way" },
    { tone: "success", label: "Completed" },
    { tone: "warning", label: "Rescheduled" },
    { tone: "danger", label: "Cancelled" },
  ];
  return (
    <div className="space-y-1">
      <div className={cn("flex flex-wrap gap-x-3 gap-y-1", GROOT_META)}>
        {items.map((it) => (
          <span key={it.tone} className="inline-flex items-center gap-1">
            <span className={cn("inline-block h-2 w-3 rounded-sm", GROOT_STATUS_TONE_CLASS[it.tone])} />
            {it.label}
          </span>
        ))}
      </div>
      <p className={GROOT_META}>Bar, dwell, repeat pills, and event dots use the same colors.</p>
    </div>
  );
}

function GrootEventRow({ event }: { event: GrootEvent }) {
  const tone = grootStatusTone(event.status);
  const showSub = event.sub_status && !isMissingDisplay(event.sub_status);
  return (
    <div className="relative flex gap-2 py-1">
      <span
        className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", GROOT_STATUS_TONE_CLASS[tone])}
        aria-hidden
      />
      <div className={cn("min-w-0 flex-1 rounded border border-slate-200 bg-white px-2 py-1", GROOT_BODY)}>
        <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
          <span className="font-medium tabular-nums text-slate-800">{displayVal(event.at)}</span>
          <span className="text-slate-600">
            {displayVal(event.status)}
            {showSub ? ` / ${displayVal(event.sub_status)}` : ""}
          </span>
        </div>
        {event.comments ? (
          <p className="mt-0.5 leading-snug text-slate-600">{displayVal(event.comments, "")}</p>
        ) : null}
        {event.performed_by && !isMissingDisplay(event.performed_by) ? (
          <p className="mt-0.5 text-[10px] text-slate-500">{displayVal(event.performed_by)}</p>
        ) : null}
      </div>
    </div>
  );
}

function AttemptCard({
  attempt,
  defaultOpen,
}: {
  attempt: ReturnType<typeof groupGrootAttempts>[number];
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const rangeStart = attempt.startMs;
  const rangeEnd = Math.max(attempt.endMs, attempt.startMs + 60_000);
  const cancelCount = attempt.events.filter((e) =>
    String(e.comments ?? "").toLowerCase().includes("cancel"),
  ).length;

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="rounded-lg border border-slate-200 bg-slate-50/80"
    >
      <summary className="cursor-pointer list-none px-3 py-2 [&::-webkit-details-marker]:hidden">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className={GROOT_SECTION_TITLE}>
              Attempt {attempt.id}
              <span className="ml-1.5 font-normal text-slate-600">· {attempt.label}</span>
            </p>
            <p className={cn("mt-0.5 tabular-nums", GROOT_META)}>
              {formatEventRangeLabel(attempt.events)} · {attempt.events.length} events
              {cancelCount > 0 ? ` · ${cancelCount} cancel-related` : ""}
            </p>
          </div>
          <span className={cn(GROOT_META, "font-medium")}>{open ? "Hide" : "Show"} events</span>
        </div>
        <div className="mt-2">
          <StatusStrip segments={attempt.segments} rangeStart={rangeStart} rangeEnd={rangeEnd} />
        </div>
      </summary>
      <div className="border-t border-slate-200/80 px-2 pb-2 pt-1">
        {attempt.events.length ? (
          attempt.events.map((e, i) => <GrootEventRow key={`${e.at ?? "na"}-${e.status}-${i}`} event={e} />)
        ) : (
          <p className="py-2 text-[11px] text-slate-500">No events in this attempt.</p>
        )}
      </div>
    </details>
  );
}

export function GrootTimelineView({ events }: { events?: Array<Record<string, unknown>> | null }) {
  const normalized = useMemo(() => normalizeGrootEvents(events), [events]);
  const summary = useMemo(() => summarizeGrootTimeline(normalized), [normalized]);
  const attempts = useMemo(() => groupGrootAttempts(normalized), [normalized]);
  const globalSegments = useMemo(() => buildGlobalStatusSegments(normalized), [normalized]);
  const insights = useMemo(() => buildGrootInsights(normalized), [normalized]);
  const chron = useMemo(() => sortGrootEventsChronological(normalized), [normalized]);

  const [mode, setMode] = useState<ViewMode>("overview");
  const [milestonesOnly, setMilestonesOnly] = useState(false);

  const filteredList = useMemo(() => {
    const list = [...chron].reverse();
    return milestonesOnly ? filterGrootMilestones(normalized) : list;
  }, [chron, milestonesOnly, normalized]);

  if (!normalized.length) {
    return (
      <p className={cn(GROOT_CALLOUT, "border-slate-200 bg-slate-50 text-slate-700")}>
        No hyperlocal Groot events (typical for warehouse / 3PL path).
      </p>
    );
  }

  const tabs: Array<{ id: ViewMode; label: string }> = [
    { id: "overview", label: "Overview" },
    { id: "attempts", label: `Attempts (${summary.attemptCount})` },
    { id: "list", label: "All events" },
  ];

  return (
    <div className="space-y-3">
      {summary.unparsedCount > 0 ? (
        <p className={cn(GROOT_CALLOUT, "border-amber-200 bg-amber-50 text-amber-950")}>
          {summary.unparsedCount} of {summary.eventCount} events have no parseable IST timestamp — they still
          appear under All events; timeline bands may be approximate for those rows.
        </p>
      ) : null}

      <div className={cn("flex flex-wrap items-center gap-2", GROOT_CHIP)}>
        <span className="rounded-full bg-slate-200/80 px-2 py-0.5 font-medium text-slate-700">
          {summary.eventCount} events
        </span>
        {summary.rangeLabel ? (
          <span className="rounded-full bg-slate-200/80 px-2 py-0.5 tabular-nums">{summary.rangeLabel}</span>
        ) : null}
        {summary.attemptCount > 1 ? (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-900">
            {summary.attemptCount} fulfillment attempts
          </span>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-1">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setMode(t.id)}
            className={mode === t.id ? GROOT_TAB_ACTIVE : GROOT_TAB_IDLE}
            aria-selected={mode === t.id}
            role="tab"
          >
            {t.label}
          </button>
        ))}
        {mode === "list" ? (
          <label className={cn("ml-auto flex cursor-pointer items-center gap-1.5", GROOT_CHIP)}>
            <input
              type="checkbox"
              className="rounded border-slate-300"
              checked={milestonesOnly}
              onChange={(e) => setMilestonesOnly(e.target.checked)}
            />
            Milestones only
          </label>
        ) : null}
      </div>

      {mode === "overview" ? (
        <div className={GROOT_PANEL}>
          <div className="mb-4">
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Journey at a glance</p>
            <StatusStrip
              title="Status over time (IST)"
              segments={globalSegments}
              rangeStart={summary.rangeStart}
              rangeEnd={summary.rangeEnd}
              heightClass="h-4"
            />
            <div className="mt-2">
              <StatusLegend />
            </div>
          </div>

          {insights.showDwellDisclaimer ? (
            <p className={cn("mb-4", GROOT_CALLOUT_DISCLAIMER)}>
              <span className="font-semibold text-slate-800">Tracked wait only.</span> Dwell % and bars measure time
              between Groot events (IST), not customer promise SLA or silent calendar gaps. Use Order status trace for
              delivered-on-promise proof.
            </p>
          ) : null}

          {insights.headlines.length ? (
            insights.headlines.length <= 2 ? (
              <p className={cn("mb-4", GROOT_CALLOUT, "border-slate-200 bg-slate-50")}>
                <span className={GROOT_HEADLINE_COMPACT}>{insights.headlines.join(" · ")}</span>
              </p>
            ) : (
              <ul className={cn("mb-4 space-y-1", GROOT_CALLOUT, "border-slate-200 bg-slate-50")}>
                {insights.headlines.map((h) => (
                  <li key={h} className={cn("flex gap-1.5", GROOT_HEADLINE)}>
                    <span className="text-slate-400" aria-hidden>
                      ▸
                    </span>
                    <span>{h}</span>
                  </li>
                ))}
              </ul>
            )
          ) : null}

          <div className={cn("mb-2", GROOT_STAT_GRID)}>
            <InsightStatCard
              label="Outcome (latest)"
              value={insights.outcome.outcomeTitle}
              sub={insights.outcome.outcomeDetail}
              tone={outcomeToneToInsightCard(insights.outcome.outcomeTone)}
              variant="hero"
            />
            <InsightStatCard
              label="Longest blocker"
              value={insights.longestBlocker?.label ?? "—"}
              sub={
                insights.longestBlocker
                  ? `${insights.longestBlocker.dwellDisplay} · ${insights.longestBlocker.pctOfJourney}% tracked wait`
                  : undefined
              }
              tone={insights.longestBlocker ? "amber" : "slate"}
            />
            <InsightStatCard
              label="Failure signals"
              value={String(insights.failures.length)}
              sub={
                insights.failures[0]
                  ? `${insights.failures[0].title} ×${insights.failures[0].count}`
                  : "None detected"
              }
              tone={failureSignalsCardTone(insights.failures)}
            />
          </div>
          <div className={cn("mb-4", GROOT_STAT_GRID)}>
            <InsightStatCard
              label="Last Groot status"
              value={insights.lastStatus.label}
              sub={insights.lastStatus.at}
              tone={grootToneToInsightCard(insights.lastStatus.tone)}
              variant="accent"
            />
            <InsightStatCard
              label="Repeated stages"
              value={String(insights.stagesRepeated.length)}
              sub={
                insights.stagesRepeated[0]
                  ? `Top: ${insights.stagesRepeated[0].label} ×${insights.stagesRepeated[0].eventCount}`
                  : "None"
              }
              tone="slate"
            />
          </div>

          {insights.hubSequence.length > 0 ? (
            <div className={GROOT_SECTION_BLOCK}>
              <p className={cn("mb-1", GROOT_SECTION_TITLE)}>Hub path</p>
              <p className={GROOT_BODY}>{insights.hubSequence.join(" → ")}</p>
            </div>
          ) : null}

          <div className={GROOT_SECTION_BLOCK}>
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Pickup vs delivery leg</p>
            <FulfillmentLegsRow legs={insights.fulfillmentLegs} />
          </div>

          <div className={GROOT_SECTION_BLOCK}>
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Rider churn</p>
            <RiderChurnPanel churn={insights.riderChurn} />
          </div>

          <div className={GROOT_SECTION_BLOCK}>
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Time spent by stage (longest first)</p>
            <p className={cn("mb-2", GROOT_SECTION_HINT)}>
              Bar width ≈ dwell between consecutive Groot events. % is share of that tracked wait, not calendar days.
            </p>
            <DwellBarChart stages={insights.stagesByDwell} />
          </div>

          <div className={GROOT_SECTION_BLOCK}>
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Stages that repeated</p>
            <RepeatedStagesRow stages={insights.stagesRepeated} />
          </div>

          <div className={GROOT_SECTION_BLOCK}>
            <p className={cn("mb-2", GROOT_SECTION_TITLE)}>Failures & friction</p>
            <FailureInsightCards items={insights.failures} />
          </div>
        </div>
      ) : null}

      {mode === "attempts" ? (
        <div className="space-y-2">
          <p className={GROOT_SECTION_HINT}>
            Attempts split on inhouse re-create or a new fulfillment-status create (not the paired line right after
            inhouse). Newest first.
            {summary.attemptCount === 1 ? " Single bucket — no separate retry anchor detected." : ""}
          </p>
          {attempts.length ? (
            attempts.map((a, i) => <AttemptCard key={a.id} attempt={a} defaultOpen={i === 0} />)
          ) : (
            <p className="text-[11px] text-slate-500">No attempts to display.</p>
          )}
        </div>
      ) : null}

      {mode === "list" ? (
        <div className="max-h-[420px] space-y-0 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50/50 p-2">
          {filteredList.length ? (
            filteredList.map((e, i) => <GrootEventRow key={`${e.at ?? "na"}-${e.status}-${i}`} event={e} />)
          ) : (
            <p className="px-2 py-3 text-[11px] text-slate-500">
              No milestones matched. Turn off “Milestones only” to see all {summary.eventCount} events.
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
