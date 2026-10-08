"use client";

import { useMemo } from "react";

import { GROOT_CALLOUT, GROOT_HEADLINE, GROOT_PANEL } from "@/components/order-rca/grootTimelineStyles";
import { cn } from "@/lib/cn";
import {
  buildClickpostInsights,
  normalizeClickpostEvents,
  sortClickpostEventsChronological,
  summarizeClickpostTimeline,
  type ClickpostEvent,
} from "@/lib/clickpostTimelineUtils";

function ClickpostEventRow({ event }: { event: ClickpostEvent }) {
  return (
    <div className="flex min-w-0 gap-3 border-b border-slate-100 py-2 last:border-b-0">
      <div className="w-[148px] shrink-0 tabular-nums text-[11px] text-slate-600">{event.at}</div>
      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-semibold text-slate-900">{event.status}</p>
        {event.location ? (
          <p className="text-[11px] leading-relaxed text-slate-600">{event.location}</p>
        ) : null}
      </div>
    </div>
  );
}

const INSIGHT_TONE: Record<string, string> = {
  ok: "border-l-emerald-500 bg-emerald-50/60",
  warn: "border-l-amber-500 bg-amber-50/60",
  slate: "border-l-slate-400 bg-slate-50/80",
};

export function ClickpostTimelineView({
  events,
}: {
  events?: Array<Record<string, unknown>> | null;
}) {
  const normalized = useMemo(() => normalizeClickpostEvents(events), [events]);
  const summary = useMemo(() => summarizeClickpostTimeline(normalized), [normalized]);
  const insights = useMemo(() => buildClickpostInsights(normalized), [normalized]);
  const chron = useMemo(() => sortClickpostEventsChronological(normalized), [normalized]);
  const newestFirst = useMemo(() => [...chron].reverse(), [chron]);

  if (!normalized.length) {
    return (
      <p className={cn(GROOT_CALLOUT, "border-slate-200 bg-slate-50 text-slate-700")}>
        No ClickPost courier scan events (partner may be unsupported or tracking not yet available).
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded-full bg-slate-200/80 px-2 py-0.5 font-medium text-slate-700">
          {summary.eventCount} scan buckets
        </span>
        {summary.rangeLabel ? (
          <span className="rounded-full bg-slate-200/80 px-2 py-0.5 tabular-nums">{summary.rangeLabel}</span>
        ) : null}
        {summary.longestGapLabel ? (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-900">
            Longest gap: {summary.longestGapLabel}
          </span>
        ) : null}
      </div>
      {insights.length ? (
        <div className="grid gap-2 sm:grid-cols-2">
          {insights.map((insight) => (
            <div
              key={insight.headline}
              className={cn(
                GROOT_PANEL,
                "border-l-4",
                INSIGHT_TONE[insight.tone ?? "slate"] ?? INSIGHT_TONE.slate,
              )}
            >
              <p className="text-[12px] font-semibold text-slate-900">{insight.headline}</p>
              <p className={GROOT_HEADLINE}>{insight.detail}</p>
            </div>
          ))}
        </div>
      ) : null}
      <div className="rounded-md border border-slate-200 bg-white px-3">
        {newestFirst.map((e, i) => (
          <ClickpostEventRow key={`${e.at}-${e.status}-${i}`} event={e} />
        ))}
      </div>
    </div>
  );
}
