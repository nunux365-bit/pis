"use client";

/**
 * ActionablesCard — Prosight's ranked daily actions for the selected BU/day,
 * composed from the componentized columns of
 * shared.data_systems.prosight_actionables (see docs/ui_actionables_render_prompt.md;
 * the daily Flock card is the visual reference).
 *
 * Row anatomy, top to bottom: title line (rank · ▼ · segment_label · lens pill ·
 * impact right-aligned) → fact line → L2 line (always) → news line (when present)
 * → footnote (why · lever · news tie-in). The legacy `action` string is never
 * rendered.
 *
 * Feedback ("Actionable? Yes/No" + "analysis days saved") is shared team-wide,
 * last write wins, and lives in a right rail beside each title line.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ProsightActionable,
  ProsightActionablesStatus,
  prosightListActionables,
  prosightSaveActionableFeedback,
} from "@/lib/prosightApi";
import { runSaveFeedback, type FeedbackPatch } from "./actionablesFeedback";

const COLLAPSED_KEY = "prosight.actionables.collapsed.v1";

interface ActionablesCardProps {
  /** Effective BU (e.g. "pharmacy", "labs") — from the summary toolbar. */
  bu: string;
  /** Day to show actionables for (YYYY-MM-DD). */
  date: string;
}

function formatDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function attribution(row: ProsightActionable): string {
  if (!row.feedback_updated_by && !row.feedback_updated_at) return "";
  const when = row.feedback_updated_at
    ? new Date(row.feedback_updated_at).toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
      })
    : "";
  return `by ${row.feedback_updated_by || "unknown"}${when ? ` · ${when}` : ""}`;
}

/**
 * news_url arrives from the Databricks sync of prosight_actionables — an
 * upstream pipeline we don't control — so it is untrusted input. Only absolute
 * http(s) URLs are allowed to become an anchor; anything else (javascript:,
 * data:, relative, garbage) returns null and is rendered as plain text.
 * rel="noopener noreferrer" is no defence against a javascript: href.
 */
function safeHttpUrl(raw: string | null): string | null {
  if (!raw) return null;
  let parsed: URL;
  try {
    parsed = new URL(raw); // throws unless absolute with a scheme
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  // Canonical form — drops leading control chars/whitespace the browser would
  // otherwise strip itself when resolving the href.
  return parsed.href;
}

/** Lens chip tint — text label always shown, color is never the only signal. */
function lensChipClass(lens: string): string {
  switch (lens.toUpperCase()) {
    case "BOTH LENSES": // strongest signal — both detection methods agree
      return "bg-emerald-50 text-emerald-800 ring-emerald-600/20";
    case "FORECAST ONLY":
      return "bg-blue-50 text-blue-800 ring-blue-600/20";
    case "STATIC ONLY":
      return "bg-violet-50 text-violet-800 ring-violet-600/20";
    default:
      return "bg-ink-50 text-ink-600 ring-ink-300/40";
  }
}

function BoltIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="currentColor"
      className="w-3.5 h-3.5"
      aria-hidden="true"
    >
      <path d="M9.5 1 3.8 9h3.4L6.5 15l5.7-8H8.8L9.5 1z" />
    </svg>
  );
}

function CardHeader({
  bu,
  date,
  right,
}: {
  bu: string;
  date: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="px-4 py-2.5 flex items-center gap-2.5 flex-wrap bg-gradient-to-r from-amber-50/70 via-white to-white border-b border-ink-100">
      <span className="inline-flex items-center justify-center w-6 h-6 rounded-md bg-amber-100 text-amber-700 ring-1 ring-amber-600/20">
        <BoltIcon />
      </span>
      <h2 className="text-[12px] font-semibold text-ink-800 tracking-wide uppercase">
        Actions
      </h2>
      <span className="text-[10px] text-ink-400 capitalize">
        {bu} · {formatDay(date)}
      </span>
      {right}
    </div>
  );
}

export default function ActionablesCard({ bu, date }: ActionablesCardProps) {
  const [rows, setRows] = useState<ProsightActionable[]>([]);
  const [status, setStatus] = useState<ProsightActionablesStatus | null>(null);
  const [daySummary, setDaySummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Set when the server's row cap cut the read short — the rows below are then
  // a partial set, and so is every roll-up computed from them.
  const [truncated, setTruncated] = useState(false);
  const [rowLimit, setRowLimit] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  // Hashes with a save in flight — rows save independently, so this is a set
  // rather than a single hash (one row's save must not re-enable another's).
  const [savingHashes, setSavingHashes] = useState<Set<string>>(new Set());
  // Local draft for the days input, keyed by hash (committed on blur/Enter)
  const [daysDraft, setDaysDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    try {
      // Hydrate browser-only state after mount so SSR and the first client
      // render agree, avoiding a hydration mismatch. Reading localStorage in a
      // lazy initializer would run on the server and mismatch, so the mount
      // effect is the pattern here rather than an oversight.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (localStorage.getItem(COLLAPSED_KEY) === "1") setCollapsed(true);
    } catch {}
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      } catch {}
      return !c;
    });
  };

  const load = useCallback(async () => {
    if (!bu || !date) return;
    setError(null);
    try {
      const res = await prosightListActionables(bu, date);
      setRows(res.actionables);
      setStatus(res.status);
      setDaySummary(res.day_summary);
      setTruncated(res.truncated);
      setRowLimit(res.limit);
      setDaysDraft({});
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load actionables");
    }
  }, [bu, date]);

  useEffect(() => {
    // Fetch on mount / on bu+date change: the resulting setState is the point
    // of the effect, not a cascade to design out.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  /**
   * Thin binding of React state onto the save flow. The flow itself lives in
   * ./actionablesFeedback so it can be exercised without a DOM — keep this
   * wrapper free of logic so the tested path is the one that runs here.
   */
  // Per-row save queues. A ref, not state: it must survive re-renders without
  // causing them, and two saves on one row have to see the same map.
  const saveChains = useRef(new Map<string, Promise<ProsightActionable | null>>());

  const saveFeedback = (row: ProsightActionable, patch: FeedbackPatch) =>
    runSaveFeedback(row, patch, {
      save: prosightSaveActionableFeedback,
      setRows,
      setSaving: setSavingHashes,
      setNotice,
      reload: () => void load(),
      chains: saveChains.current,
    });

  const commitDays = (row: ProsightActionable) => {
    const draft = daysDraft[row.action_hash];
    if (draft === undefined) return;
    setDaysDraft((d) => {
      const rest = { ...d };
      delete rest[row.action_hash];
      return rest;
    });
    const trimmed = draft.trim();
    if (trimmed === "") {
      if (row.days_saved !== null) void saveFeedback(row, { days_saved: null });
      return;
    }
    const value = Number(trimmed);
    if (!isFinite(value) || value < 0 || value > 365) return; // revert to last saved
    if (value === row.days_saved) return;
    void saveFeedback(row, { days_saved: value });
  };

  // Roll-ups (scoped to the selected BU/day)
  const markedCount = rows.filter((r) => r.is_actionable === true).length;
  const daysSavedTotal = rows.reduce((sum, r) => sum + (r.days_saved ?? 0), 0);
  const daysSavedLabel =
    daysSavedTotal % 1 === 0 ? String(daysSavedTotal) : daysSavedTotal.toFixed(1);
  // Rows come back newest day first, then by rank, so a capped read drops the
  // oldest days and the lowest-ranked actions.
  const truncationDetail = `The server capped this read at ${rowLimit ?? "the row limit"} rows, so this isn't the complete set — the oldest days and lowest-ranked actions are missing.`;
  const rollupCaveat = "Counts cover the rows shown, not the full set.";

  if (!bu || !date) return null;

  if (error) {
    return (
      <div
        role="alert"
        className="rounded-md border border-ink-200 bg-white px-3 py-2 flex items-center gap-2 text-[12px] text-ink-400"
      >
        <span>Couldn&apos;t load actionables.</span>
        <button
          type="button"
          onClick={() => void load()}
          className="text-blue-700 hover:underline"
        >
          Retry
        </button>
      </div>
    );
  }

  // First fetch still in flight
  if (status === null) return null;

  if (status === "not_processed") {
    return (
      <div className="rounded-md border border-ink-200 bg-white shadow-sm">
        <CardHeader
          bu={bu}
          date={date}
          right={
            <span className="ml-auto text-[11px] text-ink-400 italic">
              Actionables not processed
            </span>
          }
        />
      </div>
    );
  }

  if (status === "no_actionables" || rows.length === 0) {
    return (
      <div className="rounded-md border border-ink-200 bg-white shadow-sm">
        <CardHeader bu={bu} date={date} />
        <div className="px-4 py-3">
          {daySummary && (
            <p className="text-[12px] text-ink-600 leading-relaxed mb-1.5">
              {daySummary}
            </p>
          )}
          <p className="text-[12px] text-emerald-700">No actionables today.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-ink-200 bg-white shadow-sm overflow-hidden">
      <CardHeader
        bu={bu}
        date={date}
        right={
          <div className="ml-auto flex items-center gap-2 flex-wrap">
            {/* Only ever set when a save didn't land, so it's an alert: the
                click gave no other feedback that the answer wasn't recorded. */}
            {notice && (
              <span
                role="alert"
                className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-0.5"
              >
                {notice}
              </span>
            )}
            {truncated && (
              <span
                role="status"
                aria-live="polite"
                title={truncationDetail}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-900"
              >
                <span aria-hidden="true">⚠</span> Results truncated
              </span>
            )}
            <span
              title={truncated ? rollupCaveat : undefined}
              className="inline-flex items-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-[11px] text-emerald-800"
            >
              <span className="font-semibold">{markedCount}</span>/{rows.length}{" "}
              marked actionable{truncated ? " (shown)" : ""}
            </span>
            <span
              title={truncated ? rollupCaveat : undefined}
              className="inline-flex items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-[11px] text-blue-800"
            >
              <span className="font-semibold">{daysSavedLabel}</span> analysis days
              saved{truncated ? " (shown)" : ""}
            </span>
            <button
              type="button"
              onClick={toggleCollapsed}
              // Disclosure state, matching UnifiedDimensionBreakdown's drill-down
              // rows. `collapsed` is the inverse of what the attribute reports.
              aria-expanded={!collapsed}
              aria-label={collapsed ? "Expand actionables" : "Collapse actionables"}
              className="w-6 h-6 inline-flex items-center justify-center rounded text-ink-500 hover:bg-ink-100 hover:text-ink-700 transition-colors"
            >
              <svg
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                strokeLinejoin="round"
                className={`w-3.5 h-3.5 transition-transform ${collapsed ? "" : "rotate-180"}`}
                aria-hidden="true"
              >
                <path d="M3.5 6 8 10.5 12.5 6" />
              </svg>
            </button>
          </div>
        }
      />

      {!collapsed && (
        <>
          {truncated && (
            <p className="px-4 py-2 text-[11px] text-amber-900 bg-amber-50 border-b border-amber-200 leading-relaxed">
              Showing the first{" "}
              <span className="font-semibold">{rowLimit ?? rows.length}</span> rows —
              this is a partial set. The oldest days and lowest-ranked actions were
              cut, and the counts above cover only what&apos;s listed here.
            </p>
          )}

          {daySummary && (
            <p className="px-4 py-2.5 text-[12px] text-ink-600 leading-relaxed border-b border-ink-100">
              {daySummary}
            </p>
          )}

          <ol className="divide-y divide-ink-100">
            {rows.map((row) => {
              const saving = savingHashes.has(row.action_hash);
              const attr = attribution(row);
              const newsHref = safeHttpUrl(row.news_url);
              const footnote = [
                row.why,
                row.lever,
                row.news_relation
                  ? `News tie-in: ${row.news_relation.replace(/\.?\s*$/, ".")}`
                  : null,
              ]
                .filter(Boolean)
                .join(" ");

              return (
                <li key={row.action_hash} className="px-4 py-3 flex gap-4">
                  {/* Main content */}
                  <div className="flex-1 min-w-0">
                    {/* 1. Title line */}
                    <div className="flex items-baseline gap-2 flex-wrap">
                      <span className="text-[12px] text-ink-400 tabular-nums">
                        {row.rank ?? "—"}.
                      </span>
                      <span className="text-red-600 text-[11px]" aria-hidden="true">
                        ▼
                      </span>
                      <span
                        className="text-[13px] font-bold text-ink-900"
                        title={row.dimension ? `${row.dimension} · ${row.segment}` : row.segment}
                      >
                        {row.segment_label || row.segment}
                      </span>
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[9px] font-semibold tracking-wide ring-1 ${lensChipClass(row.lens)}`}
                      >
                        {row.lens}
                      </span>
                      {row.run_days != null && row.run_days >= 3 && (
                        <span
                          className="inline-flex items-center rounded-full bg-red-50 text-red-700 ring-1 ring-red-600/20 px-1.5 py-0.5 text-[9px] font-semibold"
                          title={`${row.run_days} consecutive anomaly days`}
                        >
                          {row.run_days}d
                        </span>
                      )}
                    </div>

                    {/* 2. Fact line */}
                    {row.fact && (
                      <p className="mt-1 pl-6 text-[12px] text-ink-600">{row.fact}</p>
                    )}

                    {/* 3a. L2 line — always rendered; absence is stated */}
                    <div className="mt-1.5 pl-6 flex items-baseline gap-1.5">
                      <span className="inline-flex items-center rounded bg-violet-50 text-violet-700 ring-1 ring-violet-600/20 px-1.5 py-0.5 text-[9px] font-semibold">
                        L2
                      </span>
                      {row.l2_pocket ? (
                        <span className="text-[11px] font-medium text-ink-700">
                          {row.l2_pocket}
                        </span>
                      ) : (
                        <span className="text-[11px] italic text-ink-400">
                          no L2 support for this drop
                        </span>
                      )}
                    </div>

                    {/* 3b. News line — only when a web-verified event exists */}
                    {row.news_summary && (
                      <div className="mt-1.5 pl-6 flex items-baseline gap-1.5">
                        <span className="inline-flex items-center rounded bg-amber-50 text-amber-800 ring-1 ring-amber-600/25 px-1.5 py-0.5 text-[9px] font-semibold">
                          NEWS
                        </span>
                        <span className="text-[11px] italic text-ink-700">
                          {newsHref ? (
                            <a
                              href={newsHref}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="hover:underline decoration-ink-300"
                            >
                              {row.news_summary}
                              {row.news_source ? ` — ${row.news_source}` : ""}
                            </a>
                          ) : (
                            <>
                              {row.news_summary}
                              {row.news_source ? ` — ${row.news_source}` : ""}
                            </>
                          )}
                        </span>
                      </div>
                    )}

                    {/* 4. Footnote */}
                    {footnote && (
                      <p className="mt-1.5 pl-6 text-[11px] italic text-ink-400 leading-relaxed">
                        {footnote}
                      </p>
                    )}
                  </div>

                  {/* Right rail: impact + accept/reject */}
                  <div
                    className="flex-none flex flex-col items-end gap-1.5"
                    title={attr}
                  >
                    {row.impact_display && (
                      <div className="text-[12px] font-bold text-ink-900 whitespace-nowrap">
                        {row.impact_display}
                      </div>
                    )}
                    <div className="inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5">
                      {([
                        ["Yes", true],
                        ["No", false],
                      ] as const).map(([label, value]) => {
                        const active = row.is_actionable === value;
                        return (
                          <button
                            key={label}
                            type="button"
                            // A successful save has no status text to announce —
                            // the only signal is which option became active, so
                            // that state has to be exposed, not just coloured.
                            aria-pressed={active}
                            disabled={saving}
                            onClick={() =>
                              void saveFeedback(row, {
                                // Clicking the active option clears the answer
                                is_actionable: active ? null : value,
                              })
                            }
                            className={`px-2.5 py-1 rounded text-[10px] font-medium transition-colors disabled:opacity-50 ${
                              active
                                ? value
                                  ? "bg-emerald-600 text-white shadow-sm"
                                  : "bg-rose-600 text-white shadow-sm"
                                : "text-ink-500 hover:bg-white hover:shadow-sm"
                            }`}
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                    <label className="inline-flex items-center gap-1 text-[10px] text-ink-400">
                      <input
                        type="number"
                        min={0}
                        max={365}
                        step={0.5}
                        disabled={saving}
                        value={daysDraft[row.action_hash] ?? (row.days_saved ?? "")}
                        onChange={(e) =>
                          setDaysDraft((d) => ({
                            ...d,
                            [row.action_hash]: e.target.value,
                          }))
                        }
                        onBlur={() => commitDays(row)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") e.currentTarget.blur();
                        }}
                        placeholder="—"
                        className="w-14 rounded-md border border-ink-200 bg-white px-1.5 py-0.5 text-[11px] text-ink-800 text-center focus:border-blue-400 focus:ring-1 focus:ring-blue-300 focus:outline-none disabled:opacity-50"
                      />
                      days saved
                    </label>
                  </div>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </div>
  );
}
