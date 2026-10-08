import {
  HEADER_BG,
  HEADER_TEXT,
  tableShell,
  th,
  thCenter,
  thRight,
} from "./receivableDashboardConstants";
import {
  buildDueAgeingRows,
  findKpi,
  findKpiNetDue,
  formatLakh,
  formatPct,
} from "./receivableDashboardUtils";

type Summary = {
  totalNotDue: number | null;
  totalDue: number | null;
  unbilled: number | null;
  totalReceivable: number | null;
};

export function ReceivableSummarySection(props: {
  kpiForScope: Record<string, number> | null;
  unbLakh: number | null;
}) {
  const { kpiForScope, unbLakh } = props;
  const hasKpi = kpiForScope && Object.keys(kpiForScope).length > 0;
  const dueAgeingRows = buildDueAgeingRows(kpiForScope);
  const dueAgeingSum = dueAgeingRows.reduce((s, r) => s + r.amount, 0);

  /**
   * Summary "Overdue" = **Net due** from the file only (workbook is already TDS-excluded; no
   * extra subtraction). If missing, fall back to ageing total. Total receivable = not + due + unb.
   */
  const summary: Summary | null = (() => {
    const m = kpiForScope;
    if (!m) return null;
    const notDue = findKpi(m, [/^not due$/i]);
    const unbilled = unbLakh;
    const fromNet = findKpiNetDue(m);
    const totalDue =
      fromNet != null && Number.isFinite(fromNet)
        ? fromNet
        : dueAgeingSum > 0
          ? dueAgeingSum
          : null;
    let totalReceivable: number | null = null;
    if (
      notDue != null &&
      totalDue != null &&
      Number.isFinite(totalDue) &&
      unbilled != null &&
      Number.isFinite(unbilled)
    ) {
      totalReceivable = notDue + totalDue + unbilled;
    } else {
      totalReceivable = findKpi(m, [/^receivables$/i]);
    }
    return {
      totalNotDue: notDue,
      totalDue,
      unbilled,
      totalReceivable,
    };
  })();

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {summary && hasKpi && (
        <div className="min-w-0">
          <p
            className={`mb-0 rounded-t-md px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wide ${HEADER_BG} ${HEADER_TEXT}`}
          >
            Summary
          </p>
          <div className={tableShell + " rounded-t-none border-t-0"}>
            <table
              className="w-full min-w-[280px] table-fixed border-collapse text-xs"
              aria-label="Receivables summary"
            >
              <caption className="mb-1.5 caption-top text-left text-[10px] font-normal leading-snug text-slate-600">
                (Not due + overdue + unbilled = total receivable)
              </caption>
              <colgroup>
                <col className="w-[25%]" />
                <col className="w-[25%]" />
                <col className="w-[25%]" />
                <col className="w-[25%]" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col" className={th}>
                    Total not due
                  </th>
                  <th scope="col" className={thCenter}>
                    <span className="block">Overdue</span>
                  </th>
                  <th scope="col" className={thCenter}>
                    Unbilled
                  </th>
                  <th scope="col" className={thRight + " rounded-tr-md"}>
                    Total receivable
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr className="bg-white">
                  <td className="border-t border-slate-200 p-2.5 text-left text-sm font-semibold tabular-nums text-slate-900">
                    {summary.totalNotDue != null ? formatLakh(summary.totalNotDue) : "—"}
                  </td>
                  <td className="border-t border-slate-200 p-2.5 text-center text-sm font-semibold tabular-nums text-slate-900">
                    {summary.totalDue != null ? formatLakh(summary.totalDue) : "—"}
                  </td>
                  <td className="border-t border-slate-200 p-2.5 text-center text-sm font-semibold tabular-nums text-slate-900">
                    {summary.unbilled != null ? formatLakh(summary.unbilled) : "—"}
                  </td>
                  <td className="border-t border-slate-200 bg-yellow-200 p-2.5 text-right text-sm font-bold tabular-nums text-slate-900">
                    {summary.totalReceivable != null
                      ? formatLakh(summary.totalReceivable)
                      : "—"}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}

      {dueAgeingRows.length > 0 && (
        <div className="min-w-0">
          <p
            className={`mb-0 rounded-t-md px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wide ${HEADER_BG} ${HEADER_TEXT}`}
          >
            Total due by ageing
          </p>
          <div className={tableShell + " rounded-t-none border-t-0"}>
            <table
              className="w-full min-w-[280px] table-fixed border-collapse text-xs"
              aria-label="Total due by ageing bucket"
            >
              <thead>
                <tr>
                  <th
                    scope="col"
                    className="w-10 pl-1 pr-0 text-center text-[9px] font-semibold text-white bg-[#1a365d]"
                  >
                    S.No
                  </th>
                  <th scope="col" className={th}>Ageing bucket</th>
                  <th scope="col" className={thRight}>
                    <span className="block">Due (L)</span>
                  </th>
                  <th scope="col" className={thRight + " rounded-tr-md"}>
                    % of total
                  </th>
                </tr>
              </thead>
              <tbody>
                {dueAgeingRows.map((row, i) => {
                  const pct =
                    dueAgeingSum > 0
                      ? (row.amount / dueAgeingSum) * 100
                      : 0;
                  return (
                    <tr
                      key={row.label}
                      className={
                        i % 2 === 0
                          ? "bg-white"
                          : "bg-slate-50/95"
                      }
                    >
                      <td className="border-t border-slate-200/80 p-2 text-center text-[11px] text-slate-600">
                        {i + 1}
                      </td>
                      <td className="border-t border-slate-200/80 p-2 text-left text-[11px] text-slate-800">
                        {row.label}
                      </td>
                      <td className="border-t border-slate-200/80 p-2 text-right text-[11px] font-medium tabular-nums text-slate-900">
                        {formatLakh(row.amount)}
                      </td>
                      <td className="border-t border-slate-200/80 p-2 text-right text-[11px] tabular-nums text-slate-700">
                        {formatPct(pct)}
                      </td>
                    </tr>
                  );
                })}
                <tr className="bg-slate-100/90 font-semibold">
                  <td
                    colSpan={2}
                    className="border-t border-slate-300 p-2 text-left text-[11px] text-slate-800"
                  >
                    Total
                  </td>
                  <td className="border-t border-slate-300 p-2 text-right text-[11px] tabular-nums text-slate-900">
                    {formatLakh(
                      dueAgeingRows.reduce((s, r) => s + r.amount, 0)
                    )}
                  </td>
                  <td className="border-t border-slate-300 p-2 text-right text-[11px] text-slate-800">
                    {dueAgeingSum > 0 ? "100.0%" : "—"}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
