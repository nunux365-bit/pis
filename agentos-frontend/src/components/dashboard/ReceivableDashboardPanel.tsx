"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReceivableDashboardSnapshot } from "@/lib/api";
import { ReceivableCollectionGrids } from "./receivable/ReceivableCollectionGrids";
import { ReceivableSummarySection } from "./receivable/ReceivableSummarySection";
import { ReceivableTopPartiesTable } from "./receivable/ReceivableTopPartiesTable";
import {
  buildDueAgeingRows,
  isCollectionNotDueLabel,
  parseLakhField,
} from "./receivable/receivableDashboardUtils";

const receivableHeaderTitleClass =
  "text-base font-bold leading-snug !text-[var(--accent-blue)]";

export function ReceivableDashboardPanel(props: {
  data: ReceivableDashboardSnapshot | null;
  error: string | null;
}) {
  const { data, error } = props;
  const [scope, setScope] = useState<string>("All");
  const [gridFilterMin, setGridFilterMin] = useState<string>("10");
  const [gridFilterMax, setGridFilterMax] = useState<string>("");
  const bus = data?.payload?.business_units ?? ["All"];
  const kpi = data?.payload?.kpi_lakh;
  const top = data?.payload?.top_parties;
  const grid = data?.payload?.collection_grids;
  const unb = data?.payload?.unbilled_lakh;
  const payloadMeta = data?.payload?.meta;
  const reminderSendsMeta = payloadMeta?.reminder_sends;
  const staleParties = payloadMeta?.data_quality?.stale_bucket_parties ?? [];
  const tdsClipParties = payloadMeta?.data_quality?.tds_clip_parties ?? [];

  const kpiForScope = useMemo(() => {
    if (!kpi) return null;
    if (scope === "All" || !kpi.by_business_unit) return kpi.all ?? null;
    return kpi.by_business_unit[scope] ?? null;
  }, [kpi, scope]);

  const topForScope = useMemo(() => {
    if (!top) return null;
    if (scope === "All" || !top.by_business_unit) return top.all ?? null;
    return top.by_business_unit[scope] ?? null;
  }, [top, scope]);

  const unbLakh = useMemo(() => {
    if (unb == null) return null;
    if (scope === "All" || !unb.by_business_unit) return unb.all;
    return unb.by_business_unit[scope] ?? null;
  }, [unb, scope]);

  const gridForScope = useMemo(() => {
    if (!grid) return null;
    return scope === "All" || !grid.by_business_unit
      ? grid.all ?? null
      : grid.by_business_unit[scope] ?? null;
  }, [grid, scope]);

  const gridMinNum = useMemo(() => {
    if (gridFilterMin.trim() === "") return 0;
    const n = parseLakhField(gridFilterMin);
    if (!Number.isFinite(n)) return 0;
    return n;
  }, [gridFilterMin]);
  const gridMaxNum = useMemo((): number | null => {
    const t = gridFilterMax.trim();
    if (t === "") return null;
    const n = parseLakhField(gridFilterMax);
    return Number.isFinite(n) ? n : null;
  }, [gridFilterMax]);

  const {
    filterMinEffective,
    filterMaxEffective,
    filterRangeReversed,
  } = useMemo(() => {
    if (gridMaxNum == null || !Number.isFinite(gridMaxNum)) {
      return {
        filterMinEffective: gridMinNum,
        filterMaxEffective: gridMaxNum,
        filterRangeReversed: false,
      };
    }
    if (gridMinNum > gridMaxNum) {
      return {
        filterMinEffective: gridMaxNum,
        filterMaxEffective: gridMinNum,
        filterRangeReversed: true,
      };
    }
    return {
      filterMinEffective: gridMinNum,
      filterMaxEffective: gridMaxNum,
      filterRangeReversed: false,
    };
  }, [gridMinNum, gridMaxNum]);

  const businessUnits = data?.payload?.business_units;
  const buListKey = businessUnits?.join("|") ?? "";
  useEffect(() => {
    if (!businessUnits?.length) return;
    if (!businessUnits.includes(scope)) {
      setScope("All");
    }
  }, [data?.id, buListKey, businessUnits, scope]);

  const collectionVisibleRows = useMemo(() => {
    if (!gridForScope?.values_lakh) return [];
    return gridForScope.values_lakh
      .map((r, ri) => ({ r, ri }))
      .filter(({ r, ri }) => {
        const lab = String(gridForScope.ageing_buckets?.[ri] ?? "");
        if (isCollectionNotDueLabel(lab)) {
          return false;
        }
        if (lab === "—" && r.every((c) => (c ?? 0) <= 0)) {
          return false;
        }
        return true;
      });
  }, [gridForScope]);

  const hasKpi = Boolean(
    kpiForScope && Object.keys(kpiForScope).length > 0
  );
  const dueAgeingRows = useMemo(
    () => buildDueAgeingRows(kpiForScope),
    [kpiForScope]
  );

  if (error) {
    return (
      <div
        className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3 text-sm text-[var(--text-primary)]"
        role="alert"
      >
        {error}
      </div>
    );
  }
  if (!data) {
    return (
      <p className="text-sm text-[var(--text-secondary)]">
        No receivables snapshot yet. It appears after a weekly
        <code className="mx-1">receivables*.xlsx</code> message is processed.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between sm:gap-3">
        <div className="min-w-0 flex-1">
          <p className="mb-0.5 text-base font-bold leading-snug text-black">
            Accounts receivable
          </p>
          <p className="mt-1 text-xs font-normal leading-snug text-slate-600">
            Receivables (INR lakh).
          </p>
          <h2 className={`m-0 mt-1 ${receivableHeaderTitleClass}`}>Portfolio overview</h2>
        </div>
        <label
          className="flex w-full min-w-0 flex-shrink-0 flex-row items-center justify-between gap-3 sm:w-auto sm:gap-2"
          htmlFor="receivables-bu"
        >
          <span className={`whitespace-nowrap ${receivableHeaderTitleClass}`}>
            Business unit
          </span>
          <select
            id="receivables-bu"
            className="min-w-0 max-w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm text-slate-900 sm:min-w-[10rem] sm:max-w-xs"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
          >
            {bus.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 lg:items-start">
        <ReceivableSummarySection
          kpiForScope={kpiForScope}
          unbLakh={unbLakh ?? null}
        />
        <div className="min-w-0">
          <ReceivableTopPartiesTable rows={topForScope} />
        </div>
      </div>

      {gridForScope && (
        <ReceivableCollectionGrids
          scope={scope}
          gridForScope={gridForScope}
          collectionVisibleRows={collectionVisibleRows}
          gridFilterMin={gridFilterMin}
          setGridFilterMin={setGridFilterMin}
          gridFilterMax={gridFilterMax}
          setGridFilterMax={setGridFilterMax}
          filterRangeReversed={filterRangeReversed}
          filterMinEffective={filterMinEffective}
          filterMaxEffective={filterMaxEffective}
          reminderSendsMeta={reminderSendsMeta}
          staleParties={staleParties}
          tdsClipParties={tdsClipParties}
        />
      )}

      {!hasKpi &&
        dueAgeingRows.length === 0 &&
        !topForScope?.length &&
        !gridForScope && (
        <p className="text-sm text-slate-500">No receivables block data for this filter.</p>
      )}
    </div>
  );
}
