"use client";

/**
 * The sortable-table kit: one hook, one comparator, one header row, one column
 * summer. Extracted from `FlockDayCard.tsx` — see the note in `verdict.ts`.
 *
 * Every Prosight table (day card, range summary, dimension breakdown, pair
 * drill-down) sorts the same way, so this is the piece with the most callers
 * and the least to do with any particular card.
 */
import { useMemo, useState } from "react";

import type { SortDirection, TableColumn } from "./types";

export function useSortableTable(
  defaultKey: string,
  defaultDir: SortDirection = "desc"
) {
  const [sortKey, setSortKey] = useState(defaultKey);
  const [sortDir, setSortDir] = useState<SortDirection>(defaultDir);
  const onSort = (key: string) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };
  return { sortKey, sortDir, onSort };
}

export function sortRows<T>(
  rows: T[],
  columns: TableColumn<T>[],
  sortKey: string,
  sortDir: SortDirection
): T[] {
  if (!sortKey) return rows;
  const col = columns.find((c) => c.key === sortKey);
  if (!col) return rows;
  const getter = col.getValue || ((r: T) => (r as Record<string, unknown>)?.[sortKey]);
  const isNumeric = rows.length ? typeof getter(rows[0]) === "number" : false;
  const sorted = [...rows].sort((a, b) => {
    const va = getter(a);
    const vb = getter(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (isNumeric) {
      const useAbs = col.sortByAbs !== false;
      const aa = useAbs ? Math.abs(va as number) : (va as number);
      const bb = useAbs ? Math.abs(vb as number) : (vb as number);
      return sortDir === "asc" ? aa - bb : bb - aa;
    }
    return sortDir === "asc"
      ? String(va).localeCompare(String(vb))
      : String(vb).localeCompare(String(va));
  });
  return sorted;
}

export function SortableHeader<T>({
  columns,
  sortKey,
  sortDir,
  onSort,
}: {
  columns: TableColumn<T>[];
  sortKey: string;
  sortDir: SortDirection;
  onSort: (key: string) => void;
}) {
  return (
    <tr className="text-[9px] uppercase tracking-wide text-ink-400 border-b border-ink-100">
      {columns.map((col) => {
        const align = col.align || "left";
        const alignClass =
          align === "right"
            ? "text-right"
            : align === "center"
              ? "text-center"
              : "text-left";
        const active = sortKey === col.key;
        const arrow = active ? (sortDir === "asc" ? " \u25B2" : " \u25BC") : "";
        return (
          <th
            key={col.key}
            title={col.title}
            onClick={() => onSort(col.key)}
            className={`${alignClass} py-1 px-1 font-medium cursor-pointer select-none hover:text-ink-700 ${active ? "text-ink-700" : ""}`}
          >
            {col.label}
            {arrow}
          </th>
        );
      })}
    </tr>
  );
}

export function sumField<T>(rows: T[] | undefined, key: keyof T): number | null {
  let any = false;
  let s = 0;
  for (const r of rows || []) {
    const v = r?.[key];
    if (v == null || Number.isNaN(v)) continue;
    any = true;
    s += v as number;
  }
  return any ? s : null;
}
