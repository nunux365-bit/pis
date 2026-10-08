"use client";

import { getCategoryMeta, formatCategorySlug } from "@/lib/collectionsCategories";

export type BreakdownRow = {
  category: string;
  count: number;
  amount_lakh?: number;
};

type Props = {
  rows: BreakdownRow[];
  totalCount: number;
  totalAmountLakh?: number;
  showMoney?: boolean;
  activeCategoryFilter: string | null;
  onCategoryFilter: (cat: string | null) => void;
};

function fmtLakh2(n: number): string {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function ReplyBreakdownTable({
  rows,
  totalCount,
  totalAmountLakh,
  showMoney = false,
  activeCategoryFilter,
  onCategoryFilter,
}: Props) {
  const computePct = (count: number) =>
    totalCount === 0 ? 0 : (count / totalCount) * 100;

  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-hidden rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-[12px]">
          <caption className="sr-only">Reply counts by category</caption>
          <thead
            className="border-b border-[var(--border)] text-[11px] font-semibold uppercase tracking-wide text-white"
            style={{ backgroundColor: "#1B2A4A" }}
          >
            <tr>
              <th className="px-3 py-2.5">Category</th>
              <th className="px-3 py-2.5 text-right"># of Replies</th>
              <th className="px-3 py-2.5 text-right">% of Replies</th>
              {showMoney && (
                <th className="px-3 py-2.5 text-right">Amount Due (₹ L)</th>
              )}
              <th className="px-3 py-2.5">Next Step</th>
              <th className="px-3 py-2.5">What it means</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const meta = getCategoryMeta(row.category);
              const isActive = activeCategoryFilter === row.category;
              const pct = computePct(row.count);
              return (
                <tr
                  key={row.category}
                  onClick={() =>
                    onCategoryFilter(isActive ? null : row.category)
                  }
                  className={`cursor-pointer border-b border-[var(--border)]/50 transition-colors hover:brightness-95 ${meta.rowBgClass} ${
                    isActive
                      ? "outline outline-2 outline-offset-[-2px] outline-[var(--accent-green)]/60"
                      : ""
                  }`}
                  title={
                    isActive
                      ? "Click to clear filter"
                      : `Filter by "${meta.label || formatCategorySlug(row.category)}"`
                  }
                >
                  <td className="px-3 py-2 font-medium text-[var(--text-primary)]">
                    {meta.label || formatCategorySlug(row.category)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-[var(--text-primary)]">
                    {row.count}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-[var(--text-secondary)]">
                    {pct.toFixed(1)}%
                  </td>
                  {showMoney && (
                    <td className="px-3 py-2 text-right tabular-nums text-[var(--text-primary)]">
                      ₹{fmtLakh2(row.amount_lakh ?? 0)}
                    </td>
                  )}
                  <td className="px-3 py-2 text-[var(--text-secondary)]">
                    {meta.nextStep}
                  </td>
                  <td className="px-3 py-2 text-[var(--text-muted)]">
                    {meta.whatItMeans}
                  </td>
                </tr>
              );
            })}
            <tr className="border-t border-[var(--border)] bg-[var(--bg-elev)]/60 font-bold">
              <td className="px-3 py-2 text-[var(--text-primary)]">Total</td>
              <td className="px-3 py-2 text-right tabular-nums text-[var(--text-primary)]">
                {totalCount}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-[var(--text-secondary)]">
                {totalCount === 0 ? "0.0%" : "100.0%"}
              </td>
              {showMoney && (
                <td className="px-3 py-2 text-right tabular-nums text-[var(--text-primary)]">
                  ₹{fmtLakh2(totalAmountLakh ?? 0)}
                </td>
              )}
              <td className="px-3 py-2" />
              <td className="px-3 py-2" />
            </tr>
          </tbody>
        </table>
      </div>

      {activeCategoryFilter && (
        <p className="text-[11px] text-[var(--text-muted)]">
          Filtering by{" "}
          <strong>
            {getCategoryMeta(activeCategoryFilter).label ||
              formatCategorySlug(activeCategoryFilter)}
          </strong>{" "}
          —{" "}
          <button
            type="button"
            onClick={() => onCategoryFilter(null)}
            className="text-[var(--accent-green)] underline-offset-2 hover:underline"
          >
            clear filter
          </button>
        </p>
      )}
    </div>
  );
}
