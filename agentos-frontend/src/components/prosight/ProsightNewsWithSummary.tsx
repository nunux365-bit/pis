"use client";

/**
 * ProsightNewsWithSummary - Wrapper component with tab toggle between Summary and Detail views
 */

import { useEffect, useState } from "react";
import NewsPage from "./NewsPage";
import ProsightSummaryView from "./ProsightSummaryView";
import type { BaselineMode } from "./types";
import { BASELINE_MODES } from "./adapters";

interface DetailSelection {
  full_id: string;
  label: string;
  startDate: string;
  endDate: string;
  detailStartDate?: string;
  detailEndDate?: string;
  clickedDate?: string;
  summaryStartDate?: string;
  summaryEndDate?: string;
  nonce?: string;
}

interface SeriesPayload {
  full_id?: string;
  fullId?: string;
  id?: string;
  label?: string;
  name?: string;
  startDate?: string;
  endDate?: string;
  detailStartDate?: string;
  detailEndDate?: string;
  summaryStartDate?: string;
  summaryEndDate?: string;
  clickedDate?: string;
}

function normalizeDetailSelection(
  payload: SeriesPayload | null
): DetailSelection | null {
  if (!payload) return null;

  const start =
    payload.detailStartDate ||
    payload.summaryStartDate ||
    payload.startDate ||
    "";

  const end =
    payload.detailEndDate ||
    payload.summaryEndDate ||
    payload.endDate ||
    start;

  const fullId = payload.full_id || payload.fullId || payload.id || "";

  return {
    full_id: fullId,
    label: payload.label || payload.name || fullId,
    startDate: start,
    endDate: end,
    detailStartDate: start,
    detailEndDate: end,
    clickedDate: payload.clickedDate || "",
    summaryStartDate:
      payload.summaryStartDate || payload.detailStartDate || payload.startDate || "",
    summaryEndDate:
      payload.summaryEndDate || payload.detailEndDate || payload.endDate || "",
    nonce: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
  };
}

const DETAIL_SELECTION_KEY = "prosight.detailSelection.v1";
const BASELINE_MODE_KEY = "prosight.baselineMode.v1";

export default function ProsightNewsWithSummary() {
  const [view, setView] = useState<"summary" | "detail">("summary");
  const [detailRequest, setDetailRequest] = useState<DetailSelection | null>(null);

  // Part B: lifted here so the comparison choice survives Summary↔Detail switches
  // and reloads. Defaults to forecast; hydrated from localStorage on mount.
  const [baselineMode, setBaselineMode] = useState<BaselineMode>("forecast");
  useEffect(() => {
    try {
      const saved = localStorage.getItem(BASELINE_MODE_KEY);
      if (saved && (BASELINE_MODES as string[]).includes(saved)) {
        // Hydrate from a browser-only API after mount so SSR and the first client
        // render agree ("forecast"), avoiding a hydration mismatch.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setBaselineMode(saved as BaselineMode);
      }
    } catch {}
  }, []);
  const changeBaselineMode = (mode: BaselineMode) => {
    setBaselineMode(mode);
    try {
      localStorage.setItem(BASELINE_MODE_KEY, mode);
    } catch {}
  };

  const openDetailForSeries = (series: SeriesPayload) => {
    const fullId = series?.full_id || series?.fullId || series?.id;
    if (!fullId) return;

    const request = normalizeDetailSelection({
      full_id: fullId,
      label: series.label || series.name || fullId,
      detailStartDate:
        series.detailStartDate || series.summaryStartDate || series.startDate || "",
      detailEndDate:
        series.detailEndDate || series.summaryEndDate || series.endDate || "",
      startDate:
        series.detailStartDate || series.summaryStartDate || series.startDate || "",
      endDate:
        series.detailEndDate || series.summaryEndDate || series.endDate || "",
      clickedDate: series.clickedDate || "",
      summaryStartDate:
        series.summaryStartDate || series.detailStartDate || series.startDate || "",
      summaryEndDate:
        series.summaryEndDate || series.detailEndDate || series.endDate || "",
    });

    try {
      sessionStorage.setItem(DETAIL_SELECTION_KEY, JSON.stringify(request));
    } catch {}

    setDetailRequest(request);
    setView("detail");
  };

  const openSummary = () => setView("summary");
  const openDetail = () => setView("detail");

  return (
    <div className="h-full flex flex-col bg-[var(--bg-primary)]">
      {/* Header with tabs */}
      <div className="border-b border-[var(--border)] bg-[var(--bg-card)] px-4 py-2.5 flex items-center gap-3 flex-shrink-0">
        <div>
          <h1 className="text-sm font-medium text-[var(--text-primary)]">
            Prosight
          </h1>
        </div>

        {detailRequest && view === "detail" ? (
          <div className="hidden md:block text-[11px] text-[var(--text-muted)] truncate max-w-lg">
            Opened from summary:{" "}
            <span className="text-[var(--text-secondary)] font-medium">
              {detailRequest.label}
            </span>
          </div>
        ) : null}

        <div className="ml-auto inline-flex rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-0.5">
          <button
            type="button"
            onClick={openSummary}
            className={`px-3 py-1.5 rounded text-[11px] font-medium transition-colors ${
              view === "summary"
                ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm border border-[var(--border)]"
                : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
            }`}
          >
            Summary
          </button>
          <button
            type="button"
            onClick={openDetail}
            className={`px-3 py-1.5 rounded text-[11px] font-medium transition-colors ${
              view === "detail"
                ? "bg-[var(--bg-card)] text-[var(--text-primary)] shadow-sm border border-[var(--border)]"
                : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"
            }`}
          >
            Detailed graph + cards
          </button>
        </div>
      </div>

      {/* Content - keep both tabs mounted for state persistence */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <div className={view === "summary" ? "h-full" : "hidden h-full"}>
          <ProsightSummaryView
            onOpenDetail={openDetailForSeries}
            baselineMode={baselineMode}
            onBaselineModeChange={changeBaselineMode}
          />
        </div>

        <div className={view === "detail" ? "h-full" : "hidden h-full"}>
          <NewsPage
            initialSelection={detailRequest}
            detailSelectionStorageKey={DETAIL_SELECTION_KEY}
            disableAiExplain={true}
          />
        </div>
      </div>
    </div>
  );
}
