import { useState } from "react";
import type { ReceivableDashboardSnapshot } from "@/lib/api";
import {
  FILTER_MUTED,
  HEAT_EMPTY,
  HEAT_TEXT_HEX,
} from "@/lib/receivableDashboardConstants";
import { HEADER_BG, HEADER_TEXT } from "./receivableDashboardConstants";
import {
  collectionGridCellBackground,
  displayPartyLine,
  displayReminderBandLabel,
  formatLakh,
  isLakhRangeFilterActive,
  resolveCollectionCellForFilter,
} from "./receivableDashboardUtils";
import { HorizontalTableScrollArea } from "./HorizontalTableScrollArea";
import { ScrollAreaForwardWheelToPageAtEdge } from "./ScrollAreaForwardWheelToPageAtEdge";

const GRID_TABLE =
  "w-full min-w-[340px] border-collapse text-[13px] leading-[1.45] tabular-nums [font-feature-settings:'tnum'] antialiased sm:text-sm sm:leading-[1.4]";
const GRID_TH_CORNER = `min-w-[4rem] border border-white/20 px-1.5 py-1.5 ${HEADER_BG}`;
const GRID_TH_REM = `border border-white/20 px-1.5 py-1.5 text-center text-xs font-bold text-white/95 sm:px-2 sm:py-1.5 ${HEADER_BG}`;
const GRID_TH_SUB = `min-w-[5.5rem] border border-white/20 px-1.5 py-1.5 text-left text-xs font-bold text-white/95 sm:px-2 sm:py-1.5 ${HEADER_BG}`;
const GRID_TH_NUM = `border border-white/20 px-1 py-1.5 text-center text-xs font-bold text-white/95 min-w-[2.5rem] sm:min-w-[2.75rem] sm:px-1.5 ${HEADER_BG}`;
const GRID_TH_REMINDERS_LABEL = `text-[10px] font-bold uppercase tracking-wider sm:text-[11px]`;
const GRID_AGE = `max-w-[6.5rem] truncate border border-slate-200/90 px-1.5 py-1.5 text-xs font-semibold text-white sm:max-w-[7rem] ${HEADER_BG} ${HEADER_TEXT}`;
const GRID_NUM =
  "border border-white/50 px-1.5 py-1 text-right text-[13px] font-medium tabular-nums leading-[1.45] sm:text-sm";
const GRID_TEXT = "align-top border border-white/50 px-1.5 py-1 text-left text-xs leading-[1.45] sm:text-[13px] sm:leading-[1.45]";
/** Party lines: smaller type + tight leading so more names fit; scroll in tall cells. */
const GRID_TEXT_PARTIES =
  "min-w-[12rem] max-w-[20rem] align-top border border-white/50 px-1 py-0.5 text-left text-[10px] font-normal leading-tight sm:min-w-[14rem] sm:max-w-[22rem] sm:px-1.5 sm:py-0.5 sm:text-[11px] sm:leading-snug [font-feature-settings:'tnum']";

type GridScope = NonNullable<
  NonNullable<
    ReceivableDashboardSnapshot["payload"]["collection_grids"]
  >["all"]
>;

type ReminderMeta = NonNullable<
  ReceivableDashboardSnapshot["payload"]["meta"]
>["reminder_sends"];

type StaleParty = NonNullable<
  NonNullable<ReceivableDashboardSnapshot["payload"]["meta"]>["data_quality"]
>["stale_bucket_parties"][number];

type TdsClipParty = NonNullable<
  NonNullable<
    NonNullable<ReceivableDashboardSnapshot["payload"]["meta"]>["data_quality"]
  >["tds_clip_parties"]
>[number];

export function ReceivableCollectionGrids(props: {
  scope: string;
  gridForScope: GridScope;
  collectionVisibleRows: { r: (number | unknown)[]; ri: number }[];
  gridFilterMin: string;
  setGridFilterMin: (v: string) => void;
  gridFilterMax: string;
  setGridFilterMax: (v: string) => void;
  filterRangeReversed: boolean;
  filterMinEffective: number;
  filterMaxEffective: number | null;
  reminderSendsMeta: ReminderMeta | undefined;
  staleParties: StaleParty[];
  tdsClipParties: TdsClipParty[];
}) {
  const {
    scope,
    gridForScope,
    collectionVisibleRows,
    gridFilterMin,
    setGridFilterMin,
    gridFilterMax,
    setGridFilterMax,
    filterRangeReversed,
    filterMinEffective,
    filterMaxEffective,
    reminderSendsMeta,
    staleParties,
    tdsClipParties,
  } = props;
  const [staleOpen, setStaleOpen] = useState(false);
  const [tdsClipOpen, setTdsClipOpen] = useState(false);

  return (
    <div className="w-full min-w-0">
      <p
        className={`mb-0 rounded-t-md px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-wider sm:text-xs ${HEADER_BG} ${HEADER_TEXT}`}
      >
        Collection action grid · {scope}
      </p>
      <div className="rounded-b-md border border-slate-200 border-t-0 text-[13px] leading-[1.45] sm:text-sm sm:leading-[1.4]">
        {staleParties.length > 0 && (
          <div className="border-b border-amber-200 bg-amber-50 px-3 py-2">
            <button
              type="button"
              onClick={() => setStaleOpen((o) => !o)}
              className="flex w-full items-center gap-2 text-left"
            >
              <span className="text-base leading-none text-amber-500">⚠</span>
              <span className="flex-1 text-xs font-medium text-amber-800">
                <span className="font-bold">{staleParties.length}</span> {staleParties.length === 1 ? "party has " : "parties have "} stale ageing data — bucket sums don&apos;t reconcile with Net Due
              </span>
              <span className="shrink-0 text-[11px] font-semibold text-amber-600">
                {staleOpen ? "Hide ▲" : "Show ▼"}
              </span>
            </button>
            {staleOpen && (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[32rem] border-collapse text-[11px] sm:text-xs">
                  <thead>
                    <tr className="border-b border-amber-200 text-left text-amber-700">
                      <th className="py-1 pr-3 font-semibold">Party</th>
                      <th className="py-1 pr-3 font-semibold">BU</th>
                      <th className="py-1 pr-3 text-right font-semibold">Buckets (L)</th>
                      <th className="py-1 pr-3 text-right font-semibold">Net Due (L)</th>
                      <th className="py-1 text-right font-semibold">Gap (L)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {staleParties.map((p) => (
                      <tr key={p.code} className="border-b border-amber-100 last:border-0">
                        <td className="py-1 pr-3 text-amber-900">
                          <span className="font-medium">{p.name}</span>
                          <span className="ml-1 text-[10px] text-amber-600 opacity-70">{p.code}</span>
                        </td>
                        <td className="py-1 pr-3 text-amber-700">{p.bu || "—"}</td>
                        <td className="py-1 pr-3 text-right tabular-nums text-amber-900">{p.bucket_sum_lakh.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-right tabular-nums text-amber-900">{p.net_due_lakh.toFixed(2)}</td>
                        <td className="py-1 text-right tabular-nums font-semibold text-amber-800">{p.gap_lakh.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
        {tdsClipParties.length > 0 && (
          <div className="border-b border-red-200 bg-red-50 px-3 py-2">
            <button
              type="button"
              onClick={() => setTdsClipOpen((o) => !o)}
              className="flex w-full items-center gap-2 text-left"
            >
              <span className="text-base leading-none text-red-500">⚠</span>
              <span className="flex-1 text-xs font-medium text-red-800">
                <span className="font-bold">{tdsClipParties.length}</span>{" "}
                {tdsClipParties.length === 1 ? "bucket was" : "buckets were"} clamped to ₹0 — TDS exceeded the receivable amount.
              </span>
              <span className="shrink-0 text-[11px] font-semibold text-red-600">
                {tdsClipOpen ? "Hide ▲" : "Show ▼"}
              </span>
            </button>
            {tdsClipOpen && (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[32rem] border-collapse text-[11px] sm:text-xs">
                  <thead>
                    <tr className="border-b border-red-200 text-left text-red-700">
                      <th className="py-1 pr-3 font-semibold">Party</th>
                      <th className="py-1 pr-3 font-semibold">BU</th>
                      <th className="py-1 pr-3 font-semibold">Bucket</th>
                      <th className="py-1 pr-3 text-right font-semibold">Rec (L)</th>
                      <th className="py-1 text-right font-semibold">TDS (L)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tdsClipParties.map((p, i) => (
                      <tr key={i} className="border-b border-red-100 last:border-0">
                        <td className="py-1 pr-3 text-red-900">
                          <span className="font-medium">{p.name}</span>
                          <span className="ml-1 text-[10px] text-red-500 opacity-70">{p.code}</span>
                        </td>
                        <td className="py-1 pr-3 text-red-700">{p.bu || "—"}</td>
                        <td className="py-1 pr-3 text-red-700">{p.bucket}</td>
                        <td className="py-1 pr-3 text-right tabular-nums text-red-900">{p.rec_lakh.toFixed(2)}</td>
                        <td className="py-1 text-right tabular-nums font-semibold text-red-800">{p.tds_lakh.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
        <div className="flex flex-col gap-2 border-b border-slate-200 bg-slate-50 px-2.5 py-2 sm:flex-row sm:flex-wrap sm:items-end sm:px-3">
          <span className="text-xs font-medium text-slate-800">
            Filter both grids (₹ Lakh):
          </span>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <span className="text-slate-500">Min</span>
            <input
              type="number"
              step="0.1"
              className="h-8 w-[4.5rem] rounded border border-slate-200 bg-white px-1.5 text-xs text-slate-900"
              value={gridFilterMin}
              onChange={(e) => setGridFilterMin(e.target.value)}
              aria-label="Minimum lakh filter"
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <span className="text-slate-500">Max</span>
            <input
              type="number"
              step="0.1"
              className="h-8 w-[4.5rem] rounded border border-slate-200 bg-white px-1.5 text-xs text-slate-900"
              value={gridFilterMax}
              onChange={(e) => setGridFilterMax(e.target.value)}
              placeholder="—"
              aria-label="Maximum lakh filter, blank for no limit"
            />
            <span className="whitespace-nowrap text-[12px] text-slate-500">
              (blank = no limit)
            </span>
          </label>
          {filterRangeReversed && (
            <span className="text-xs text-amber-800" role="status">
              Min was greater than max — using {filterMinEffective}–{filterMaxEffective} lakh.
            </span>
          )}
        </div>

        <div className="flex flex-col gap-4 p-2.5 sm:p-3">
          <div className="min-w-0">
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-600 sm:text-sm sm:normal-case sm:tracking-normal sm:text-slate-800">
              <span className="block">Total due (₹ Lakh)</span>
            </p>
            <HorizontalTableScrollArea>
              <table
                className={GRID_TABLE}
                aria-label="Collection grid amounts by ageing and reminder band"
              >
                <thead>
                  <tr>
                    <th colSpan={1} className={GRID_TH_CORNER} scope="col" aria-hidden />
                    <th
                      colSpan={gridForScope.reminder_bands.length}
                      className={GRID_TH_REM}
                      scope="colgroup"
                    >
                      <span className={GRID_TH_REMINDERS_LABEL}>Reminders</span>
                    </th>
                  </tr>
                  <tr>
                    <th className={GRID_TH_SUB} scope="col">Ageing</th>
                    {gridForScope.reminder_bands.map((b) => (
                      <th
                        key={b}
                        className={GRID_TH_NUM}
                        scope="col"
                      >
                        {displayReminderBandLabel(b)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {collectionVisibleRows.map(({ r, ri }) => (
                    <tr key={ri}>
                      <th
                        className={GRID_AGE}
                        scope="row"
                        title={String(
                          gridForScope.ageing_buckets[ri] ?? ""
                        )}
                      >
                        {String(
                          gridForScope.ageing_buckets[ri] ?? "—"
                        )}
                      </th>
                      {r.map((c, ci) => {
                        const names =
                          gridForScope.client_names?.[ri]?.[ci] ?? [];
                        const resolved = resolveCollectionCellForFilter(
                          c,
                          names,
                          filterMinEffective,
                          filterMaxEffective
                        );
                        const bg = collectionGridCellBackground(
                          ri,
                          ci,
                          resolved,
                          filterMinEffective,
                          filterMaxEffective
                        );
                        const textMuted =
                          isLakhRangeFilterActive(
                            filterMinEffective,
                            filterMaxEffective
                          ) && !resolved.inRange;
                        return (
                          <td
                            key={ci}
                            className={GRID_NUM}
                            style={{
                              backgroundColor: bg,
                              color: textMuted
                                ? FILTER_MUTED
                                : HEAT_TEXT_HEX,
                            }}
                          >
                            {resolved.inRange && resolved.displayAmount > 0
                              ? formatLakh(resolved.displayAmount)
                              : "—"}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </HorizontalTableScrollArea>
          </div>

          <div className="min-w-0">
            <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-600 sm:text-sm sm:normal-case sm:tracking-normal sm:text-slate-800">
              Parties
            </p>
            <HorizontalTableScrollArea>
              <table
                className={GRID_TABLE}
                aria-label="Collection grid party names by cell"
              >
                <thead>
                  <tr>
                    <th colSpan={1} className={GRID_TH_CORNER} scope="col" aria-hidden />
                    <th
                      colSpan={gridForScope.reminder_bands.length}
                      className={GRID_TH_REM}
                      scope="colgroup"
                    >
                      <span className={GRID_TH_REMINDERS_LABEL}>Reminders</span>
                    </th>
                  </tr>
                  <tr>
                    <th className={GRID_TH_SUB} scope="col">Ageing</th>
                    {gridForScope.reminder_bands.map((b) => (
                      <th
                        key={`p-${b}`}
                        className={GRID_TH_NUM}
                        scope="col"
                      >
                        {displayReminderBandLabel(b)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {collectionVisibleRows.map(({ r, ri }) => (
                    <tr key={`p-${ri}`}>
                      <th
                        className={GRID_AGE}
                        scope="row"
                        title={String(
                          gridForScope.ageing_buckets[ri] ?? ""
                        )}
                      >
                        {String(
                          gridForScope.ageing_buckets[ri] ?? "—"
                        )}
                      </th>
                      {r.map((c, ci) => {
                        const names =
                          gridForScope.client_names?.[ri]?.[ci] ?? [];
                        const resolved = resolveCollectionCellForFilter(
                          c,
                          names,
                          filterMinEffective,
                          filterMaxEffective
                        );
                        const bg = collectionGridCellBackground(
                          ri,
                          ci,
                          resolved,
                          filterMinEffective,
                          filterMaxEffective
                        );
                        const textMuted =
                          isLakhRangeFilterActive(
                            filterMinEffective,
                            filterMaxEffective
                          ) && !resolved.inRange;
                        const vis = resolved.visibleNames;
                        return (
                          <td
                            key={ci}
                            className={GRID_TEXT_PARTIES}
                            style={{
                              backgroundColor: bg,
                              color: textMuted
                                ? FILTER_MUTED
                                : HEAT_TEXT_HEX,
                            }}
                          >
                            {resolved.inRange && resolved.displayAmount > 0 ? (
                              vis.length > 0 ? (
                                <div className="flex w-full min-w-0 max-w-full flex-col gap-0.5 bg-inherit [color:inherit]">
                                  {vis.length > 1 && (
                                    <p className="mb-0.5 border-b border-slate-400/25 pb-0.5 text-[9px] font-medium uppercase leading-none tracking-wide text-slate-800/80 [color:inherit] sm:text-[10px]">
                                      {vis.length} parties — scroll
                                    </p>
                                  )}
                                  <ScrollAreaForwardWheelToPageAtEdge
                                    className="flex max-h-[min(20rem,52vh)] w-full min-w-0 max-w-full flex-col gap-1 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable] sm:max-h-[min(44rem,82vh)]"
                                    role="list"
                                    aria-label="Parties in this cell"
                                  >
                                    {vis.map((n, ni) => (
                                      <div
                                        key={`${ri}-${ci}-${ni}`}
                                        role="listitem"
                                        className="flex gap-1.5 border-b border-slate-900/15 py-1 first:pt-0.5 [color:inherit] last:border-b-0 last:pb-0.5 [overflow-wrap:break-word] sm:gap-2"
                                      >
                                        <span
                                          className="w-6 shrink-0 select-none pr-0.5 text-right text-[8px] font-bold tabular-nums leading-none [color:inherit] opacity-60 sm:w-7 sm:text-[9px]"
                                          aria-hidden
                                        >
                                          {ni + 1}.
                                        </span>
                                        <div className="min-w-0 flex-1 break-words leading-snug [color:inherit] sm:leading-[1.35]">
                                          {displayPartyLine(
                                            n,
                                            resolved.displayAmount,
                                            vis.length
                                          )}
                                        </div>
                                      </div>
                                    ))}
                                    {vis.length > 1 &&
                                      resolved.displayAmount > 0 &&
                                      !vis.some((s) => s.includes("₹")) && (
                                        <div className="mt-1 border-t border-slate-300/50 pt-1 text-[10px] font-bold tabular-nums text-slate-900 sm:text-xs">
                                          Cell total ₹{formatLakh(resolved.displayAmount)} L
                                        </div>
                                      )}
                                  </ScrollAreaForwardWheelToPageAtEdge>
                                </div>
                              ) : (
                                "—"
                              )
                            ) : (
                              "—"
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </HorizontalTableScrollArea>
          </div>
        </div>
        {reminderSendsMeta && (
          <p className="border-t border-slate-200/80 bg-slate-50/50 px-3 py-1.5 text-[11px] leading-relaxed text-slate-500 sm:text-xs">
            Reminder column bands:{" "}
            <span className="font-medium text-slate-600">
              {reminderSendsMeta.distinct_business_keys}
            </span>{" "}
            parties with a non-test{" "}
            <code className="rounded bg-slate-100 px-0.5 text-[10px]">
              {reminderSendsMeta.filters?.workflow_type ?? "PAYMENT_REMINDER_WEEKLY"}
            </code>{" "}
            row (status{" "}
            {reminderSendsMeta.filters?.status_in?.length
              ? reminderSendsMeta.filters.status_in.join("/")
              : "approved/rendered/sent"}
            ); <span className="font-medium text-slate-600">
              {reminderSendsMeta.distinct_period_keys}
            </span>{" "}
            distinct week(s) in the DB; {reminderSendsMeta.row_count} row(s)
            matched the filter.
          </p>
        )}
      </div>
    </div>
  );
}
