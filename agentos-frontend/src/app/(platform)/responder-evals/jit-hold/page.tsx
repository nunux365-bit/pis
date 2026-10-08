"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  listJitHoldOrders,
  type JitHoldOrderList,
  type JitHoldOrderStatus,
} from "@/lib/responderEval";

const PAGE_SIZES = [25, 50, 100] as const;
const DAY_OPTIONS = [7, 30, 90] as const;
const EMPTY_STATS = { total: 0, triggered: 0, split_done: 0, kept_original: 0 };

const STATUS_LABEL: Record<JitHoldOrderStatus, string> = {
  triggered: "Triggered",
  split_done: "Split done",
  kept_original: "Kept original",
};

const STATUS_CLASS: Record<JitHoldOrderStatus, string> = {
  triggered: "bg-[var(--bg-elev)] text-[var(--text-secondary)]",
  split_done: "bg-emerald-100 text-emerald-800",
  kept_original: "bg-sky-100 text-sky-800",
};

function fmtWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function JitHoldOrdersPage() {
  const [days, setDays] = useState<(typeof DAY_OPTIONS)[number]>(7);
  const [status, setStatus] = useState<JitHoldOrderStatus | "">("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(50);
  const [data, setData] = useState<JitHoldOrderList>({ items: [], total: 0, stats: EMPTY_STATS });
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      const next = searchInput.trim();
      if (next !== search) setPage(0);
      setSearch(next);
    }, 300);
    return () => clearTimeout(t);
  }, [searchInput, search]);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(
        await listJitHoldOrders({
          days,
          status,
          search: search || undefined,
          limit: pageSize,
          offset: page * pageSize,
        })
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load JIT hold orders");
    } finally {
      setLoading(false);
    }
  }, [days, status, search, page, pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  const pageCount = Math.max(1, Math.ceil(data.total / pageSize));
  const fromIdx = data.total === 0 ? 0 : page * pageSize + 1;
  const toIdx = Math.min(data.total, (page + 1) * pageSize);
  const cards = useMemo(
    () =>
      [
        ["Total", data.stats.total],
        ["Triggered", data.stats.triggered],
        ["Split done", data.stats.split_done],
        ["Kept original", data.stats.kept_original],
      ] as const,
    [data.stats]
  );

  return (
    <div className="mx-auto flex w-full max-w-[110rem] flex-col gap-4 px-3 py-4 sm:px-5 sm:py-5">
      <header className="border-b border-[var(--border)]/70 pb-4">
        <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-violet-700/90">
          Support quality
        </p>
        <h1 className="mt-0.5 text-xl font-semibold tracking-tight text-[var(--text-primary)] sm:text-2xl">
          JIT Hold
        </h1>
        <p className="mt-1.5 max-w-3xl text-xs leading-relaxed text-[var(--text-secondary)]">
          WhatsApp hold messages that went out, and whether the customer split or kept the order.
        </p>
      </header>

      <div className="flex flex-wrap items-end gap-2 sm:gap-3">
        <label className="text-[11px] font-medium text-[var(--text-secondary)]">
          Range
          <select
            className="mt-1 block rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value) as (typeof DAY_OPTIONS)[number]);
              setPage(0);
            }}
          >
            {DAY_OPTIONS.map((n) => (
              <option key={n} value={n}>
                Last {n} days
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] font-medium text-[var(--text-secondary)]">
          Page size
          <select
            className="mt-1 block rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number]);
              setPage(0);
            }}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1.5 text-sm font-medium text-[var(--text-primary)] shadow-sm transition hover:bg-[var(--bg-secondary)] disabled:opacity-50"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {cards.map(([label, value]) => (
          <div
            key={label}
            className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 shadow-sm"
          >
            <p className="text-[10px] font-medium uppercase tracking-wide text-[var(--text-muted)]">
              {label}
            </p>
            <p className="mt-0.5 text-lg font-semibold tabular-nums text-[var(--text-primary)]">
              {loading ? "—" : value}
            </p>
          </div>
        ))}
      </div>

      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-3 py-3 shadow-sm sm:px-4">
        <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
          <p className="text-[11px] text-[var(--text-muted)]">Orders in the selected range</p>
          <div className="flex flex-wrap items-end gap-2 sm:gap-3">
            <label className="min-w-[10rem] flex-1 text-[11px] font-medium text-[var(--text-secondary)] sm:min-w-[14rem]">
              Search
              <input
                type="search"
                className="mt-1 block w-full rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-violet-500/50 focus:ring-1 focus:ring-violet-500/30"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Parent order id"
                aria-label="Search parent order id"
              />
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Status
              <select
                className="mt-1 block min-w-[8rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-sm outline-none focus:border-violet-500/50"
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value as JitHoldOrderStatus | "");
                  setPage(0);
                }}
              >
                <option value="">All</option>
                <option value="triggered">Triggered</option>
                <option value="split_done">Split done</option>
                <option value="kept_original">Kept original</option>
              </select>
            </label>
          </div>
        </div>

        {err ? (
          <div
            className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700"
            role="alert"
          >
            {err}
          </div>
        ) : null}

        <div className="overflow-x-auto rounded-lg border border-[var(--border)]/80">
          <table className="w-full min-w-[36rem] border-collapse text-left text-[13px]">
            <caption className="sr-only">JIT hold parent orders</caption>
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--bg-primary)]/60 text-[10px] uppercase tracking-wide text-[var(--text-muted)]">
                {["Parent order", "Status", "Triggered"].map((h) => (
                  <th key={h} className="px-3 py-2.5 font-medium" scope="col">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={3} className="px-3 py-12 text-center text-sm text-[var(--text-muted)]">
                    Loading orders…
                  </td>
                </tr>
              ) : data.items.length === 0 ? (
                <tr>
                  <td colSpan={3} className="px-3 py-12 text-center">
                    <p className="text-sm font-medium text-[var(--text-secondary)]">
                      No JIT hold messages yet
                    </p>
                    <p className="mx-auto mt-1 max-w-md text-xs text-[var(--text-muted)]">
                      Rows appear after a hold WhatsApp is sent in this window.
                    </p>
                  </td>
                </tr>
              ) : (
                data.items.map((row) => (
                  <tr
                    key={row.parent_order_id}
                    className="border-b border-[var(--border)]/50 last:border-0"
                  >
                    <td className="px-3 py-2.5 font-mono text-[11px]">
                      <Link
                        href={`/order-rca?po=${encodeURIComponent(row.parent_order_id)}`}
                        className="text-violet-700 underline-offset-2 hover:underline"
                      >
                        {row.parent_order_id}
                      </Link>
                    </td>
                    <td className="px-3 py-2.5">
                      <span
                        className={`inline-flex rounded-md px-2 py-0.5 text-[11px] font-semibold ${STATUS_CLASS[row.status] ?? "bg-[var(--bg-elev)] text-[var(--text-secondary)]"}`}
                      >
                        {STATUS_LABEL[row.status] ?? row.status}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 tabular-nums text-[var(--text-secondary)]">
                      {fmtWhen(row.triggered_at)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="mt-3 flex flex-col gap-2 border-t border-[var(--border)]/60 pt-3 text-sm sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-[var(--text-muted)]">
            {data.total === 0 ? "0 orders" : `Showing ${fromIdx}–${toIdx} of ${data.total}`}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1 text-xs font-medium disabled:opacity-40"
              disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </button>
            <span className="text-xs text-[var(--text-secondary)]">
              Page {page + 1} of {pageCount}
            </span>
            <button
              type="button"
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-1 text-xs font-medium disabled:opacity-40"
              disabled={page + 1 >= pageCount}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
