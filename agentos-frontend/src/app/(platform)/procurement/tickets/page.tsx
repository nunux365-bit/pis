"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "@/components/ErrorBanner";
import { ProcurementBackLink } from "@/components/procurement/ProcurementChrome";
import {
  getProcurementSchema,
  humanizeProcurementUnknownError,
  listMyTickets,
  type ProcurementTicket,
} from "@/lib/procurementApi";
import { PROCUREMENT_HOME_HREF } from "@/lib/procurementRoutes";
import { ticketSapListStatus } from "@/lib/procurementTicketUi";
import { TicketLinksListCell } from "@/components/procurement/TicketLinkSummary";

type KindFilter = "all" | "PR" | "PO";

function rowMatchesQuery(t: ProcurementTicket, q: string, sapMaxAttempts: number): boolean {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  const blob = [t.kind, t.document_type, t.sap_id ?? "", t.id, ticketSapListStatus(t, sapMaxAttempts).label]
    .join(" ")
    .toLowerCase();
  return blob.includes(s);
}

function matchesFilters(t: ProcurementTicket, q: string, kind: KindFilter, sapMaxAttempts: number): boolean {
  if (kind !== "all" && t.kind !== kind) return false;
  return rowMatchesQuery(t, q, sapMaxAttempts);
}

export default function TicketsListPage() {
  const [rows, setRows] = useState<ProcurementTicket[] | null>(null);
  const [sapMaxAttempts, setSapMaxAttempts] = useState(6);
  const [listErr, setListErr] = useState<string | null>(null);
  const [tableQuery, setTableQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");

  const load = useCallback(() => {
    Promise.allSettled([listMyTickets(), getProcurementSchema()])
      .then(([ticketsRes, schemaRes]) => {
        if (ticketsRes.status === "rejected") {
          setRows(null);
          const e = ticketsRes.reason;
          setListErr(humanizeProcurementUnknownError(e, "Could not load your tickets."));
          return;
        }
        setListErr(null);
        setRows(ticketsRes.value);
        if (schemaRes.status === "fulfilled") {
          const cap = schemaRes.value.sap_max_attempts;
          if (typeof cap === "number" && cap >= 1) setSapMaxAttempts(cap);
        }
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const visible = useMemo(() => {
    if (!rows) return [];
    return rows.filter((t) => matchesFilters(t, tableQuery, kindFilter, sapMaxAttempts));
  }, [rows, tableQuery, kindFilter, sapMaxAttempts]);

  const counts = useMemo(() => {
    if (!rows) return { all: 0, pr: 0, po: 0 };
    return {
      all: rows.length,
      pr: rows.filter((t) => t.kind === "PR").length,
      po: rows.filter((t) => t.kind === "PO").length,
    };
  }, [rows]);

  const chip = (id: KindFilter, label: string, count: number) => (
    <button
      type="button"
      onClick={() => setKindFilter(id)}
      className={`rounded-full border px-4 py-2 text-xs font-bold transition focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/35 ${
        kindFilter === id
          ? "border-[var(--accent-green)] bg-[rgba(21,163,115,0.12)] text-[var(--accent-green)] shadow-sm"
          : "border-[var(--border)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-[var(--border-active)]"
      }`}
    >
      {label}
      <span className="ml-1.5 rounded-md bg-[var(--bg-elev)] px-1.5 py-px font-mono text-[10px] text-[var(--text-muted)]">
        {count}
      </span>
    </button>
  );

  return (
    <div className="mx-auto max-w-6xl space-y-8 pb-12">
      <header className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <ProcurementBackLink href={PROCUREMENT_HOME_HREF}>Procurement home</ProcurementBackLink>
          <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)] sm:text-3xl">Your tickets</h1>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-[var(--text-secondary)]">
            Tap any row to open it. Use the chips to show only requests or only orders — then search if the list is
            long.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Link
            href="/procurement/pr/new"
            className="inline-flex min-h-[44px] items-center justify-center rounded-xl bg-app-gradient-1 px-5 text-sm font-semibold text-white shadow-md transition hover:opacity-[0.97] focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/40"
          >
            New request
          </Link>
          <Link
            href="/procurement/po/new"
            className="inline-flex min-h-[44px] items-center justify-center rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-5 text-sm font-semibold text-[var(--text-primary)] shadow-sm transition hover:border-[var(--border-active)] hover:bg-[var(--bg-elev)]"
          >
            New order
          </Link>
        </div>
      </header>

      {listErr ? <ErrorBanner onRetry={load}>{listErr}</ErrorBanner> : null}

      {!rows && !listErr ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="h-28 animate-pulse rounded-2xl bg-[var(--bg-secondary)]" />
          ))}
        </div>
      ) : null}

      {rows && rows.length === 0 ? (
        <div className="relative overflow-hidden rounded-3xl border border-dashed border-[var(--border)] bg-[var(--bg-card)] px-8 py-16 text-center shadow-sm">
          <div
            className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-[var(--bg-elev)] text-[var(--accent-green)] shadow-inner ring-1 ring-[var(--border)]"
            aria-hidden
          >
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" className="opacity-90">
              <path
                d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinejoin="round"
              />
              <path d="M14 2v6h6M8 13h8M8 17h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </div>
          <p className="text-lg font-semibold text-[var(--text-primary)]">Nothing here yet</p>
          <p className="mx-auto mt-2 max-w-md text-sm text-[var(--text-muted)]">
            When you send your first purchase request, it will show up here so you can track SAP and open it again
            anytime.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <Link
              href="/procurement/pr/new"
              className="inline-flex min-h-[44px] items-center justify-center rounded-xl bg-app-gradient-1 px-6 text-sm font-semibold text-white shadow-md"
            >
              Create your first request
            </Link>
            <Link
              href={PROCUREMENT_HOME_HREF}
              className="inline-flex min-h-[44px] items-center justify-center rounded-xl border border-[var(--border)] px-6 text-sm font-semibold text-[var(--text-secondary)]"
            >
              Procurement home
            </Link>
          </div>
        </div>
      ) : null}

      {rows && rows.length > 0 ? (
        <div className="space-y-5">
          <div className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
            <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by ticket type">
              {chip("all", "All", counts.all)}
              {chip("PR", "Requests", counts.pr)}
              {chip("PO", "Orders", counts.po)}
            </div>
            <label className="sr-only" htmlFor="ticket-search">
              Search tickets
            </label>
            <input
              id="ticket-search"
              type="search"
              placeholder="Search SAP number, type, or status…"
              value={tableQuery}
              onChange={(e) => setTableQuery(e.target.value)}
              className="w-full min-w-0 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] px-4 py-2.5 text-sm shadow-sm outline-none transition placeholder:text-[var(--text-muted)] focus:border-[var(--border-active)] focus:ring-2 focus:ring-[var(--accent-green)]/25 sm:max-w-xs"
            />
          </div>

          {visible.length === 0 ? (
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] px-6 py-10 text-center text-sm text-[var(--text-muted)]">
              No tickets match.{" "}
              <button type="button" className="font-semibold text-[var(--accent-blue)] hover:underline" onClick={() => {
                setTableQuery("");
                setKindFilter("all");
              }}>
                Clear filters
              </button>
            </div>
          ) : (
            <>
              <div className="space-y-3 md:hidden">
                {visible.map((t) => {
                  const sap = ticketSapListStatus(t, sapMaxAttempts);
                  const related = <TicketLinksListCell ticket={t} />;
                  const hasRelated =
                    (t.kind === "PO" && t.parent_pr_summary) ||
                    (t.kind === "PR" && (t.linked_pos?.length ?? 0) > 0);
                  return (
                    <div
                      key={t.id}
                      className="rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm transition hover:border-[var(--border-active)] hover:shadow-md"
                    >
                      <Link
                        href={`/procurement/tickets/${t.id}`}
                        className="block p-4 active:scale-[0.99]"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <span className="text-xs font-bold uppercase tracking-wide text-[var(--text-muted)]">
                              {t.kind === "PR" ? "Request" : "Order"}
                            </span>
                            <p className="mt-1 font-mono text-sm font-semibold text-[var(--text-primary)]">
                              {t.sap_id ?? "SAP id pending"}
                            </p>
                            <p className="mt-0.5 text-xs text-[var(--text-secondary)]">{t.document_type}</p>
                          </div>
                          <span className={`shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold ${sap.tone}`}>
                            {sap.label}
                          </span>
                        </div>
                        <p className="mt-3 text-[10px] text-[var(--text-muted)]">
                          Updated {new Date(t.updated_at).toLocaleString()}
                        </p>
                      </Link>
                      {hasRelated ? (
                        <div className="border-t border-[var(--border)]/80 px-4 py-2.5">{related}</div>
                      ) : null}
                    </div>
                  );
                })}
              </div>

              <div className="hidden overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-md ring-1 ring-black/[0.03] md:block">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-[var(--border)] bg-[var(--bg-elev)]/90 text-[11px] font-bold uppercase tracking-wider text-[var(--text-muted)]">
                    <tr>
                      <th scope="col" className="px-5 py-4">
                        Kind
                      </th>
                      <th scope="col" className="px-5 py-4">
                        Type
                      </th>
                      <th scope="col" className="px-5 py-4">
                        SAP document
                      </th>
                      <th scope="col" className="px-5 py-4">
                        Links
                      </th>
                      <th scope="col" className="px-5 py-4">
                        Status
                      </th>
                      <th scope="col" className="px-5 py-4">
                        Last updated
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((t) => {
                      const sap = ticketSapListStatus(t, sapMaxAttempts);
                      return (
                        <tr
                          key={t.id}
                          className="border-b border-[var(--border)]/90 last:border-0 transition hover:bg-[var(--bg-primary)]/35"
                        >
                          <td className="px-5 py-4">
                            <Link
                              href={`/procurement/tickets/${t.id}`}
                              className="font-semibold text-[var(--accent-blue)] hover:underline focus-visible:rounded-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent-blue)]"
                            >
                              {t.kind === "PR" ? "Request" : "Order"}
                            </Link>
                          </td>
                          <td className="px-5 py-4 text-[var(--text-secondary)]">{t.document_type}</td>
                          <td className="px-5 py-4 font-mono text-xs font-medium text-[var(--text-primary)]">
                            {t.sap_id ?? "—"}
                          </td>
                          <td className="px-5 py-4">
                            <TicketLinksListCell ticket={t} />
                          </td>
                          <td className="px-5 py-4">
                            <span className={`inline-flex rounded-full px-2.5 py-1 text-[10px] font-bold ${sap.tone}`}>
                              {sap.label}
                            </span>
                          </td>
                          <td className="px-5 py-4 text-xs text-[var(--text-muted)]">
                            {new Date(t.updated_at).toLocaleString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
