"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  getIntelligenceSummary,
  getReceivableIntelligence,
  exportReceivableIntelligence,
  type IntelligenceSummaryResponse,
  type ReceivableIntelligenceRow,
  type EmailThreadMessage,
  type ReplyTrackerWindowDays,
} from "@/lib/api";
import { CollectionsExecutiveSummary } from "@/components/email-agent/CollectionsExecutiveSummary";
import { ThreadSidebar } from "@/components/email-agent/ThreadSidebar";
import { OutreachTabContent } from "@/components/email-agent/OutreachTabContent";
import {
  COLLECTIONS_CATEGORY_META,
  getCategoryMeta,
  formatCategorySlug,
} from "@/lib/collectionsCategories";

const PAGE_SIZE_OPTIONS = [25, 100] as const;
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

/** Build the list of page numbers + ellipsis markers to render in the paginator. */
function buildPageWindows(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);

  const pages = new Set<number>();
  pages.add(1);
  pages.add(total);
  for (let d = -2; d <= 2; d++) {
    const p = current + d;
    if (p >= 1 && p <= total) pages.add(p);
  }

  const sorted = Array.from(pages).sort((a, b) => a - b);
  const result: (number | "…")[] = [];
  for (let i = 0; i < sorted.length; i++) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) result.push("…");
    result.push(sorted[i]);
  }
  return result;
}

export default function EmailAgentStatusPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ── Tab switcher — persisted in ?tab= so reload keeps the selection ──
  const tabParam = searchParams.get("tab");
  const [activeTab, setActiveTab] = useState<"collections" | "outreach">(
    tabParam === "outreach" ? "outreach" : "collections",
  );

  function handleTabChange(tab: "collections" | "outreach") {
    setActiveTab(tab);
    router.replace(`?tab=${tab}`, { scroll: false });
  }

  // ── Shared BU filter ──
  const [buList, setBuList] = useState<string[]>([]);
  const [selectedBu, setSelectedBu] = useState("All");

  // ── Reply Tracker view window (send-anchored): 7 / 14 / 30 days, default 7 ──
  const [view, setView] = useState<ReplyTrackerWindowDays>(7);

  // ── Category filter — set by clicking a breakdown row; sent to server ──
  const [activeCategoryFilter, setActiveCategoryFilter] = useState<string | null>(null);

  // ── Email-sent filter — "yes" | "no" | "all"; default yes ──
  const [emailSentFilter, setEmailSentFilter] = useState<"yes" | "no" | "all">("yes");

  // ── Search — raw input value + debounced value sent to API ──
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function handleSearchChange(value: string) {
    setSearchInput(value);
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    searchDebounceRef.current = setTimeout(() => setSearchQuery(value), 300);
  }

  // ── Summary (executive section) ──
  const [summary, setSummary] = useState<IntelligenceSummaryResponse | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  // ── Thread sidebar ──
  const [selectedThread, setSelectedThread] = useState<{
    threadId: string;
    partyName: string;
    fetchFn?: (id: string) => Promise<{ messages: EmailThreadMessage[] }>;
  } | null>(null);

  // ── Party detail table ──
  const [receivables, setReceivables] = useState<ReceivableIntelligenceRow[]>([]);
  const [detailLoading, setDetailLoading] = useState(true);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [recvTotal, setRecvTotal] = useState(0);

  // ── Pagination ──
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize>(25);

  // ── Export ──
  const [exporting, setExporting] = useState(false);

  const totalPages = Math.max(1, Math.ceil(recvTotal / pageSize));

  // Reset to page 1 whenever any filter, view, or page-size changes.
  useEffect(() => {
    setCurrentPage(1);
  }, [selectedBu, activeCategoryFilter, emailSentFilter, pageSize, view]);

  // Fetch summary once per BU change.
  useEffect(() => {
    let cancelled = false;
    setSummaryLoading(true);
    setSummaryError(null);

    const buParam = selectedBu === "All" ? undefined : selectedBu;
    getIntelligenceSummary({ businessUnit: buParam, windowDays: view })
      .then((sum) => {
        if (cancelled) return;
        setSummary(sum);
        setSummaryLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setSummaryError(e instanceof Error ? e.message : "Could not load summary.");
        setSummaryLoading(false);
      });

    return () => { cancelled = true; };
  }, [selectedBu, view]);

  // Fetch detail page whenever any pagination/filter param changes.
  // Page resets to 1 when any filter changes (but not when currentPage itself changes).
  const [prevFilters, setPrevFilters] = useState(() => ({
    bu: selectedBu, cat: activeCategoryFilter, sent: emailSentFilter, q: searchQuery, ps: pageSize, view,
  }));
  useEffect(() => {
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);

    const filtersChanged =
      selectedBu !== prevFilters.bu ||
      activeCategoryFilter !== prevFilters.cat ||
      emailSentFilter !== prevFilters.sent ||
      searchQuery !== prevFilters.q ||
      pageSize !== prevFilters.ps ||
      view !== prevFilters.view;

    const page = filtersChanged ? 1 : currentPage;
    if (filtersChanged) {
      setCurrentPage(1);
      setPrevFilters({ bu: selectedBu, cat: activeCategoryFilter, sent: emailSentFilter, q: searchQuery, ps: pageSize, view });
    }

    const buParam = selectedBu === "All" ? undefined : selectedBu;
    const offset = (page - 1) * pageSize;

    getReceivableIntelligence({
      businessUnit: buParam,
      replyCategory: activeCategoryFilter ?? undefined,
      emailSent: emailSentFilter === "all" ? undefined : emailSentFilter,
      search: searchQuery || undefined,
      windowDays: view,
      limit: pageSize,
      offset,
    })
      .then((detail) => {
        if (cancelled) return;
        if (detail.business_units.length > 0) setBuList(detail.business_units);
        setReceivables(detail.items);
        setRecvTotal(detail.total);
        setDetailLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setDetailError(e instanceof Error ? e.message : "Could not load data.");
        setDetailLoading(false);
      });

    return () => { cancelled = true; };
  }, [selectedBu, activeCategoryFilter, emailSentFilter, searchQuery, currentPage, pageSize, view]);

  function handleCategoryFilter(cat: string | null) {
    setActiveCategoryFilter(cat);
  }

  function handlePageSizeChange(size: PageSize) {
    setPageSize(size);
    // currentPage resets via the separate useEffect above.
  }

  async function handleExportView() {
    setExporting(true);
    try {
      await exportReceivableIntelligence({
        windowDays: view,
        businessUnit: selectedBu === "All" ? undefined : selectedBu,
      });
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : "Export failed.");
    } finally {
      setExporting(false);
    }
  }

  const pageWindows = buildPageWindows(currentPage, totalPages);
  const rangeStart = recvTotal === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, recvTotal);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto overscroll-y-none p-3 sm:p-4">
      {/* ── Tab switcher ── */}
      <div className="flex gap-1 rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-1 shadow-sm">
        {(["collections", "outreach"] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => handleTabChange(tab)}
            className={`flex-1 rounded-lg px-4 py-2 text-[13px] font-semibold transition ${
              activeTab === tab
                ? "bg-[var(--accent-green)]/10 text-[var(--accent-green)] border border-[var(--accent-green)]/30"
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            {tab === "collections" ? "Collections" : "Cold Outreach"}
          </button>
        ))}
      </div>

      {activeTab === "outreach" && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-4 shadow-sm ring-1 ring-black/[0.02]">
          <OutreachTabContent
            onOpenThread={(threadId, label, fetchFn) =>
              setSelectedThread({ threadId, partyName: label, fetchFn })
            }
          />
        </div>
      )}

      {activeTab === "collections" && (
      <>
      {/* ── Executive Summary ── */}
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-4 shadow-sm ring-1 ring-black/[0.02]">
        {summaryError && (
          <div
            className="mb-3 rounded-lg border border-red-200/90 bg-red-50/80 px-3 py-2 text-[13px] text-red-950 ring-1 ring-red-900/10"
            role="alert"
          >
            {summaryError}
          </div>
        )}
        <CollectionsExecutiveSummary
          summary={summary}
          loading={summaryLoading}
          buList={buList}
          selectedBu={selectedBu}
          onBuChange={setSelectedBu}
          onCategoryFilter={handleCategoryFilter}
          activeCategoryFilter={activeCategoryFilter}
          view={view}
          onViewChange={setView}
        />
      </div>

      {/* ── Party-Level Detail ── */}
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-3 shadow-sm ring-1 ring-black/[0.02] sm:p-4">
        <div className="flex flex-col gap-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
              Party-Level Detail
            </p>
            <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              Overdue balances with email and reply status per party.
              Amounts are TDS-adjusted and may differ from figures quoted in sent reminder emails.
            </p>
          </div>

          {/* Controls — full-width row; search grows, filters + export on the right */}
          <div className="flex w-full flex-wrap items-center gap-3">
            {/* Search — takes the remaining space */}
            <div className="relative flex flex-1 items-center min-w-[240px]">
              <svg
                className="pointer-events-none absolute left-2.5 text-[var(--text-muted)]"
                width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true"
              >
                <circle cx="5.5" cy="5.5" r="4" stroke="currentColor" strokeWidth="1.3" />
                <path d="M8.5 8.5L11 11" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
              <input
                type="search"
                value={searchInput}
                onChange={(e) => handleSearchChange(e.target.value)}
                placeholder="Search name or HANA code…"
                className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1.5 pl-8 pr-3 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition placeholder:text-[var(--text-muted)] focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20 hover:border-[var(--border-active)]/50"
              />
            </div>

            {/* Email-sent filter */}
            <div className="flex items-center gap-1.5 shrink-0">
              <label
                htmlFor="email-sent-filter"
                className="text-[11px] font-medium text-[var(--text-muted)] whitespace-nowrap"
              >
                Email sent
              </label>
              <select
                id="email-sent-filter"
                value={emailSentFilter}
                onChange={(e) =>
                  setEmailSentFilter(e.target.value as "yes" | "no" | "all")
                }
                className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20 hover:border-[var(--border-active)]/50"
              >
                <option value="yes">Yes</option>
                <option value="no">No</option>
                <option value="all">All</option>
              </select>
            </div>

            {/* Reply-category filter */}
            <div className="flex items-center gap-1.5 shrink-0">
              <label
                htmlFor="category-filter"
                className="text-[11px] font-medium text-[var(--text-muted)] whitespace-nowrap"
              >
                Reply category
              </label>
              <select
                id="category-filter"
                value={activeCategoryFilter ?? ""}
                onChange={(e) => handleCategoryFilter(e.target.value || null)}
                className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20 hover:border-[var(--border-active)]/50"
              >
                <option value="">All categories</option>
                {Object.entries(COLLECTIONS_CATEGORY_META).map(([slug, meta]) => (
                  <option key={slug} value={slug}>
                    {meta.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Divider */}
            <div className="hidden h-5 w-px bg-[var(--border)] sm:block" aria-hidden="true" />

            {/* Export — current view (send-anchored to the selected window + BU) */}
            <button
              type="button"
              onClick={handleExportView}
              disabled={exporting}
              title={`Exports the current ${view}-day view (this BU) as shown in the table`}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3 py-1.5 text-[12px] font-semibold text-[var(--text-primary)] shadow-sm transition hover:border-[var(--accent-green)]/40 hover:bg-[var(--bg-elev)] hover:text-[var(--accent-green)] disabled:opacity-50"
            >
              {exporting ? (
                "Exporting…"
              ) : (
                <>
                  <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
                    <path d="M6.5 1v7.5M3.5 6l3 3 3-3M1.5 10.5h10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  Export
                </>
              )}
            </button>
          </div>
        </div>

        {detailError && (
          <div
            className="mt-3 rounded-lg border border-red-200/90 bg-red-50/80 px-3 py-2 text-[13px] text-red-950 ring-1 ring-red-900/10"
            role="alert"
          >
            {detailError}
          </div>
        )}

        {/* Skeleton only on first load when there's nothing to show yet */}
        {detailLoading && !detailError && receivables.length === 0 && (
          <div
            className="mt-3 h-48 animate-pulse rounded-lg bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
            aria-busy="true"
            aria-label="Loading party detail"
          />
        )}

        {!detailLoading && !detailError && receivables.length === 0 && (
          <p className="py-4 text-center text-[13px] text-[var(--text-muted)]">
            {activeCategoryFilter
              ? "No parties match this category filter."
              : "No receivables found for this business unit."}
          </p>
        )}

        {receivables.length > 0 && (
          <div className={`mt-3 space-y-3 transition-opacity duration-150 ${detailLoading ? "opacity-60" : "opacity-100"}`}>
            {detailLoading && (
              <div className="h-0.5 w-full overflow-hidden rounded-full bg-[var(--border)]">
                <div className="h-full w-1/3 animate-[slide_1.2s_ease-in-out_infinite] rounded-full bg-[var(--accent-green)]/60" />
              </div>
            )}
            {/* Table */}
            <div className="overflow-hidden rounded-lg border border-[var(--border)]">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[960px] table-fixed text-left text-[13px]">
                  <caption className="sr-only">Party-level receivables detail</caption>
                  <colgroup>
                    <col style={{ width: "220px" }} />
                    <col />
                    <col style={{ width: "136px" }} />
                    <col style={{ width: "88px" }} />
                    <col style={{ width: "112px" }} />
                    <col style={{ width: "128px" }} />
                    <col style={{ width: "76px" }} />
                  </colgroup>
                  <thead className="border-b border-[var(--border)] bg-[var(--bg-elev)]/95 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)] shadow-[0_1px_0_0_var(--border)] backdrop-blur-sm">
                    <tr>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">HANA Code</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Party Name</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5 text-right">
                        <span className="block truncate">Total Overdue (₹ L)</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Email Sent?</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Reply Received?</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Reply Category</span>
                      </th>
                      <th className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate">Thread</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {receivables.map((row, idx) => {
                      const sentAt = row.email_sent_at ? new Date(row.email_sent_at) : null;
                      const replyAt = row.reply_received_at
                        ? new Date(row.reply_received_at)
                        : null;
                      const sentLabel = sentAt
                        ? sentAt.toLocaleString(undefined, {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                        : null;
                      const replyLabel = replyAt
                        ? replyAt.toLocaleString(undefined, {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                        : null;
                      const catMeta = row.reply_category
                        ? getCategoryMeta(row.reply_category)
                        : null;
                      return (
                        <tr
                          key={row.hana_code + idx}
                          className={
                            idx % 2 === 0
                              ? "border-b border-[var(--border)]/50 bg-[var(--bg-card)] hover:bg-[var(--bg-elev)]/50"
                              : "border-b border-[var(--border)]/50 bg-[var(--bg-primary)]/40 hover:bg-[var(--bg-elev)]/50"
                          }
                        >
                          <td className="overflow-hidden px-3 py-2.5" title={row.hana_code}>
                            <span className="block truncate font-mono text-[12px] text-[var(--text-secondary)]">
                              {row.hana_code}
                            </span>
                          </td>
                          <td className="overflow-hidden px-3 py-2.5" title={row.party_name}>
                            <span className="block truncate font-medium text-[var(--text-primary)]">
                              {row.party_name}
                            </span>
                          </td>
                          <td className="overflow-hidden px-3 py-2.5 text-right tabular-nums text-[var(--text-primary)]">
                            {row.total_overdue_lakh.toFixed(2)}
                          </td>
                          <td className="overflow-hidden px-3 py-2.5">
                            {sentLabel ? (
                              <span
                                className="inline-flex rounded-full border border-emerald-200/90 bg-emerald-50/90 px-2 py-0.5 text-[11px] font-semibold text-emerald-900 ring-1 ring-emerald-900/10"
                                title={sentLabel}
                              >
                                Yes
                              </span>
                            ) : (
                              <span className="text-[12px] text-[var(--text-muted)]">No</span>
                            )}
                          </td>
                          <td className="overflow-hidden px-3 py-2.5">
                            {replyLabel ? (
                              <span
                                className="inline-flex rounded-full border border-emerald-200/90 bg-emerald-50/90 px-2 py-0.5 text-[11px] font-semibold text-emerald-900 ring-1 ring-emerald-900/10"
                                title={replyLabel}
                              >
                                Yes
                              </span>
                            ) : (
                              <span className="text-[12px] text-[var(--text-muted)]">No</span>
                            )}
                          </td>
                          <td className="overflow-hidden px-3 py-2.5">
                            {catMeta ? (
                              <span className="block truncate text-[var(--text-secondary)]">
                                {catMeta.label || formatCategorySlug(row.reply_category!)}
                              </span>
                            ) : (
                              <span className="text-[var(--text-muted)]">—</span>
                            )}
                          </td>
                          <td className="overflow-hidden px-3 py-2.5">
                            {row.gmail_thread_id ? (
                              <button
                                type="button"
                                onClick={() =>
                                  setSelectedThread({
                                    threadId: row.gmail_thread_id!,
                                    partyName: row.party_name,
                                  })
                                }
                                className="text-[11px] font-semibold text-[var(--accent-green)] hover:underline"
                              >
                                View
                              </button>
                            ) : (
                              <span className="text-[var(--text-muted)]">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* ── Pagination bar ── */}
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              {/* Row count + page-size selector + export */}
              <div className="flex items-center gap-2">
                <p className="text-[11px] tabular-nums text-[var(--text-muted)]">
                  {recvTotal === 0
                    ? "No rows"
                    : `Showing ${rangeStart}–${rangeEnd} of ${recvTotal} row${recvTotal === 1 ? "" : "s"}`}
                </p>
                <span className="text-[11px] text-[var(--text-muted)]">·</span>
                <div className="flex items-center gap-1">
                  {PAGE_SIZE_OPTIONS.map((size) => (
                    <button
                      key={size}
                      type="button"
                      onClick={() => handlePageSizeChange(size)}
                      className={
                        pageSize === size
                          ? "rounded px-2 py-0.5 text-[11px] font-semibold bg-[var(--accent-green)]/10 text-[var(--accent-green)] border border-[var(--accent-green)]/30"
                          : "rounded px-2 py-0.5 text-[11px] text-[var(--text-muted)] hover:text-[var(--text-primary)] border border-transparent hover:border-[var(--border)]"
                      }
                    >
                      {size}
                    </button>
                  ))}
                  <span className="text-[11px] text-[var(--text-muted)]">per page</span>
                </div>
              </div>

              {/* Page number controls */}
              {totalPages > 1 && (
                <div className="flex items-center gap-1">
                  {/* Prev */}
                  <button
                    type="button"
                    disabled={currentPage === 1}
                    onClick={() => setCurrentPage((p) => p - 1)}
                    className="flex h-7 w-7 items-center justify-center rounded border border-[var(--border)] text-[12px] text-[var(--text-secondary)] transition hover:border-[var(--border-active)]/45 hover:bg-[var(--bg-elev)] disabled:pointer-events-none disabled:opacity-35"
                    aria-label="Previous page"
                  >
                    ‹
                  </button>

                  {pageWindows.map((item, i) =>
                    item === "…" ? (
                      <span
                        key={`ellipsis-${i}`}
                        className="flex h-7 w-7 items-center justify-center text-[12px] text-[var(--text-muted)]"
                      >
                        …
                      </span>
                    ) : (
                      <button
                        key={item}
                        type="button"
                        onClick={() => setCurrentPage(item)}
                        className={
                          item === currentPage
                            ? "flex h-7 w-7 items-center justify-center rounded border border-[var(--accent-green)]/40 bg-[var(--accent-green)]/10 text-[12px] font-semibold text-[var(--accent-green)]"
                            : "flex h-7 w-7 items-center justify-center rounded border border-[var(--border)] text-[12px] text-[var(--text-secondary)] transition hover:border-[var(--border-active)]/45 hover:bg-[var(--bg-elev)]"
                        }
                        aria-label={`Go to page ${item}`}
                        aria-current={item === currentPage ? "page" : undefined}
                      >
                        {item}
                      </button>
                    )
                  )}

                  {/* Next */}
                  <button
                    type="button"
                    disabled={currentPage === totalPages}
                    onClick={() => setCurrentPage((p) => p + 1)}
                    className="flex h-7 w-7 items-center justify-center rounded border border-[var(--border)] text-[12px] text-[var(--text-secondary)] transition hover:border-[var(--border-active)]/45 hover:bg-[var(--bg-elev)] disabled:pointer-events-none disabled:opacity-35"
                    aria-label="Next page"
                  >
                    ›
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      </>
      )}

      {/* ── Thread sidebar ── */}
      <ThreadSidebar
        threadId={selectedThread?.threadId ?? null}
        partyName={selectedThread?.partyName ?? ""}
        fetchFn={selectedThread?.fetchFn}
        onClose={() => setSelectedThread(null)}
      />
    </div>
  );
}
