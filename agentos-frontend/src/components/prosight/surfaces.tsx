"use client";

/**
 * Shared presentational surfaces for the Prosight cards.
 *
 * Extracted from `FlockDayCard.tsx` — see the note in `verdict.ts`.
 *
 * `SurfaceSection` and `DirectionPill` are the two consumed across the feature
 * (by RangeSummaryCard, CounterfactualImpact, UnifiedDimensionBreakdown and
 * PairDrilldown). `MiniStat` moved with them: same kind of thing, and used by
 * the day card.
 *
 * `VerdictBlock` did NOT move — it was exported from FlockDayCard and called
 * from nowhere at all, so it was deleted rather than carried across.
 */
import { useState } from "react";

import type { DirectionType, L0Verdict, Softness } from "./types";
import { directionToneSet } from "./verdict";

export function DirectionPill({
  direction,
  label,
  size = "xs",
}: {
  direction: DirectionType;
  label?: string;
  size?: "xs" | "sm";
}) {
  if (!direction)
    return <span className="text-ink-400 text-[10px]">—</span>;
  const tone = directionToneSet(direction);
  const text = label || direction;
  const sizeClass =
    size === "sm" ? "text-[10px] px-2 py-0.5" : "text-[9px] px-1.5 py-[1px]";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border font-semibold uppercase tracking-wide ${sizeClass} ${tone.pillBg} ${tone.pillText} ${tone.pillBorder}`}
    >
      <span aria-hidden="true">{tone.arrow}</span>
      {text}
    </span>
  );
}

export function SurfaceSection({
  title,
  action,
  tone = "neutral",
  children,
  className = "",
  collapsible = false,
}: {
  title: React.ReactNode;
  action?: React.ReactNode;
  tone?: "neutral" | "drop" | "spike" | "mixed";
  children: React.ReactNode;
  className?: string;
  collapsible?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const toneSet =
    tone === "drop" || tone === "spike" || tone === "mixed"
      ? directionToneSet(tone)
      : null;
  const headerBg = toneSet ? toneSet.surface : "bg-ink-100";
  const headerText = toneSet ? toneSet.text : "text-ink-700";
  const leftRule = toneSet ? `border-l-2 ${toneSet.border}` : "";
  return (
    <section
      className={`rounded-md border border-ink-200 bg-white overflow-hidden ${leftRule} ${className}`}
    >
      <div
        className={`flex items-center gap-2 px-3 py-1.5 ${collapsed ? "" : "border-b border-ink-200"} ${headerBg}`}
      >
        <h3
          className={`text-[11px] font-semibold uppercase tracking-[1.5px] ${headerText} flex-1`}
        >
          {title}
        </h3>
        {action && !collapsed && (
          <div className="flex items-center gap-2 text-[10px]">{action}</div>
        )}
        {collapsible && (
          <button
            type="button"
            onClick={() => setCollapsed((c) => !c)}
            className="text-[10px] text-ink-500 hover:text-ink-800 px-1.5 py-0.5 rounded border border-ink-200 bg-white hover:bg-ink-50 leading-none"
            title={collapsed ? "Expand section" : "Collapse section"}
            aria-label={collapsed ? "Expand section" : "Collapse section"}
            // Note the inversion: the state variable is `collapsed`. Finding 8
            // fixed this on ActionablesCard's own toggle; this one is the shared
            // kit, so it was missing on every card that uses SurfaceSection.
            aria-expanded={!collapsed}
          >
            {collapsed ? "▾" : "▴"}
          </button>
        )}
      </div>
      {!collapsed && <div className="px-4 py-3">{children}</div>}
    </section>
  );
}

export function MiniStat({
  label,
  value,
  hint,
  tone = "",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "" | "drop" | "spike";
}) {
  const valueColor =
    tone === "drop"
      ? "text-red-700"
      : tone === "spike"
        ? "text-amber-700"
        : "text-ink-800";
  const arrow = tone === "drop" ? "\u25BC" : tone === "spike" ? "\u25B2" : "";
  const arrowColor =
    tone === "drop"
      ? "text-red-600"
      : tone === "spike"
        ? "text-amber-600"
        : "text-ink-400";
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wide text-ink-500 font-medium">
        {label}
      </span>
      <span
        className={`text-[14px] font-mono font-semibold ${valueColor} flex items-baseline gap-1`}
      >
        {arrow && (
          <span className={`text-[10px] ${arrowColor}`} aria-hidden="true">
            {arrow}
          </span>
        )}
        {value}
      </span>
      {hint && (
        <span className="text-[10px] text-ink-400 font-mono">{hint}</span>
      )}
    </div>
  );
}
