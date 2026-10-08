"use client";

import { useEffect, useState } from "react";
import {
  listOutreachCampaigns,
  getOutreachSummary,
  getOutreachLeads,
  getOutreachThreadMessages,
  type OutreachCampaign,
  type OutreachSummaryResponse,
  type OutreachLeadRow,
  type EmailThreadMessage,
} from "@/lib/api";
import { ReplyBreakdownTable } from "@/components/email-agent/ReplyBreakdownTable";
import { getCategoryMeta, formatCategorySlug } from "@/lib/collectionsCategories";

// ── Mock data — remove when backend is live ─────────────────────────────────
const MOCK_CAMPAIGNS: OutreachCampaign[] = [
  { id: "1", campaign_name: "chw_cold_outreach", dashboard_display_name: "CHW Cold Outreach", visible_columns: [] },
];

const MOCK_SUMMARY: OutreachSummaryResponse = {
  campaign_name: "chw_cold_outreach",
  total_leads: 48,
  pending: 12,
  sent: 28,
  failed: 2,
  hold: 4,
  duplicate: 1,
  skipped: 1,
  removed: 0,
  reply_count: 7,
  reply_breakdown: [
    { category: "interested", count: 3 },
    { category: "needs_more_info", count: 2 },
    { category: "not_interested", count: 1 },
    { category: "already_covered", count: 1 },
  ],
};

const MOCK_LEADS: OutreachLeadRow[] = [
  { id: 1, account_name: "Apollo Hospitals", spoc: "Dr. Priya Sharma", primary_email: "priya.sharma@apollo.com", source: "LinkedIn", status: "sent", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: "2026-05-05T09:15:00Z", subject_sent: "Introducing Tata 1mg's Corporate Health and Wellness Services", gmail_thread_id: "thread_apollo_001", last_reply_category: "interested", last_reply_at: "2026-05-06T14:32:00Z", reply_count: 2, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 2, account_name: "Medanta Hospital", spoc: "Vikram Singh", primary_email: "vikram.singh@medanta.org", source: "LinkedIn", status: "sent", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: "2026-05-05T09:18:00Z", subject_sent: "Introducing Tata 1mg's Corporate Health and Wellness Services", gmail_thread_id: "thread_medanta_002", last_reply_category: "needs_more_info", last_reply_at: "2026-05-07T10:05:00Z", reply_count: 1, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 3, account_name: "Max Healthcare", spoc: "Ananya Kapoor", primary_email: "ananya.kapoor@maxhealthcare.com", source: "Email Campaign", status: "sent", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: "2026-05-05T09:22:00Z", subject_sent: "Introducing Tata 1mg's Corporate Health and Wellness Services", gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 4, account_name: "Fortis Healthcare", spoc: "Rajesh Mehta", primary_email: "rajesh.mehta@fortis.com", source: "LinkedIn", status: "pending", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: null, subject_sent: null, gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 5, account_name: "Hinduja Hospital", spoc: "Sneha Patil", primary_email: "sneha.patil@hinduja.com", source: "LinkedIn", status: "pending", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: null, subject_sent: null, gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 6, account_name: "Narayana Health", spoc: "Deepak Rao", primary_email: "deepak.rao@narayanahealth.org", source: "Referral", status: "hold", tab_name: "chw_cold_outreach_2026_05", issues: ["missing_spoc"], sent_at: null, subject_sent: null, gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 7, account_name: "Manipal Hospitals", spoc: "Kiran Nair", primary_email: "kiran.nair@manipal.edu", source: "LinkedIn", status: "sent", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: "2026-05-05T09:35:00Z", subject_sent: "Introducing Tata 1mg's Corporate Health and Wellness Services", gmail_thread_id: "thread_manipal_007", last_reply_category: "not_interested", last_reply_at: "2026-05-08T08:20:00Z", reply_count: 1, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 8, account_name: "Wockhardt Hospitals", spoc: "Sunita Joshi", primary_email: "sunita.joshi@wockhardt.com", source: "Email Campaign", status: "failed", tab_name: "chw_cold_outreach_2026_05", issues: ["send_failed"], sent_at: null, subject_sent: null, gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 9, account_name: "Kokilaben Hospital", spoc: "Amit Desai", primary_email: "amit.desai@kokilabenhospital.com", source: "LinkedIn", status: "sent", tab_name: "chw_cold_outreach_2026_05", issues: [], sent_at: "2026-05-05T09:42:00Z", subject_sent: "Introducing Tata 1mg's Corporate Health and Wellness Services", gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
  { id: 10, account_name: "Lilavati Hospital", spoc: "", primary_email: "contact@lilavati.org", source: "Referral", status: "hold", tab_name: "chw_cold_outreach_2026_05", issues: ["missing_spoc"], sent_at: null, subject_sent: null, gmail_thread_id: null, last_reply_category: null, last_reply_at: null, reply_count: 0, gsheet_synced_at: "2026-05-05T01:30:00Z" },
];
// ── End mock data ────────────────────────────────────────────────────────────

const PAGE_SIZE_OPTIONS = [25, 100] as const;
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

const STATUS_OPTIONS = [
  "pending",
  "sent",
  "failed",
  "hold",
  "duplicate",
  "skipped",
  "removed_from_sheet",
] as const;

const REPLY_CATEGORY_OPTIONS = [
  "schedule_meeting",
  "share_proposal",
  "marked_team_member",
  "follow_up_no_response",
  "wrong_contact",
  "not_interested",
] as const;

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

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    sent: "border-emerald-200/90 bg-emerald-50/90 text-emerald-900 ring-emerald-900/10",
    pending: "border-amber-200/90 bg-amber-50/90 text-amber-900 ring-amber-900/10",
    failed: "border-red-200/90 bg-red-50/90 text-red-900 ring-red-900/10",
    hold: "border-slate-200/90 bg-slate-50/90 text-slate-800 ring-slate-900/10",
    duplicate: "border-orange-200/90 bg-orange-50/90 text-orange-900 ring-orange-900/10",
    skipped: "border-slate-200/90 bg-slate-50/90 text-slate-700 ring-slate-900/10",
    removed_from_sheet:
      "border-[var(--border)] bg-[var(--bg-elev)]/60 text-[var(--text-muted)] ring-black/10",
  };
  const cls = styles[status] || styles.hold;
  return (
    <span
      className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold ring-1 ${cls}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function StatusReason({ status, issues }: { status: string; issues: string[] }) {
  if (status === "pending") {
    return (
      <span className="inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium ring-1 ring-inset bg-amber-50 text-amber-700 ring-amber-600/15">
        next batch
      </span>
    );
  }

  if (issues.length === 0) return null;

  return (
    <span className="inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium ring-1 ring-inset bg-red-50 text-red-700 ring-red-600/15">
      {issues[0].replace(/_/g, " ")}
    </span>
  );
}

type Props = {
  onOpenThread: (
    threadId: string,
    label: string,
    fetchFn: (id: string) => Promise<{ messages: EmailThreadMessage[] }>,
  ) => void;
};

export function OutreachTabContent({ onOpenThread }: Props) {
  const [campaigns, setCampaigns] = useState<OutreachCampaign[]>([]);
  const [selectedCampaign, setSelectedCampaign] = useState<string | null>(null);
  const [campaignsError, setCampaignsError] = useState<string | null>(null);

  const [summary, setSummary] = useState<OutreachSummaryResponse | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [activeCategoryFilter, setActiveCategoryFilter] = useState<string | null>(null);

  const [leads, setLeads] = useState<OutreachLeadRow[]>([]);
  const [leadsTotal, setLeadsTotal] = useState(0);
  const [leadsLoading, setLeadsLoading] = useState(false);
  const [leadsError, setLeadsError] = useState<string | null>(null);

  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize>(25);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  // Load campaigns on mount.
  useEffect(() => {
    listOutreachCampaigns().then((data) => {
      setCampaigns(data);
      if (data.length > 0) setSelectedCampaign(data[0].campaign_name);
    });
  }, []);

  // Reset page/filters when campaign changes.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCurrentPage(1);
    setActiveCategoryFilter(null);
    setStatusFilter(null);
  }, [selectedCampaign]);

  // Reset to page 1 on filter/pageSize change.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCurrentPage(1);
  }, [activeCategoryFilter, statusFilter, pageSize]);

  // Fetch summary on campaign change.
  useEffect(() => {
    if (!selectedCampaign) return;
    setSummaryLoading(true);
    setSummaryError(null);
    getOutreachSummary(selectedCampaign)
      .then(setSummary)
      .catch((e) => setSummaryError(String(e)))
      .finally(() => setSummaryLoading(false));
  }, [selectedCampaign]);

  // Fetch leads on any param change.
  useEffect(() => {
    if (!selectedCampaign) return;
    setLeadsLoading(true);
    setLeadsError(null);
    getOutreachLeads(selectedCampaign, {
      page: currentPage,
      page_size: pageSize,
      status: statusFilter ?? undefined,
      reply_category: activeCategoryFilter ?? undefined,
    })
      .then((data) => {
        setLeads(data.items);
        setLeadsTotal(data.total);
      })
      .catch((e) => setLeadsError(String(e)))
      .finally(() => setLeadsLoading(false));
  }, [selectedCampaign, currentPage, pageSize, statusFilter, activeCategoryFilter]);

  const totalPages = Math.max(1, Math.ceil(leadsTotal / pageSize));
  const pageWindows = buildPageWindows(currentPage, totalPages);
  const rangeStart = leadsTotal === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, leadsTotal);

  return (
    <div className="flex flex-col gap-4">
      {/* Campaign selector — pinned to the right */}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <label
          htmlFor="outreach-campaign-select"
          className="text-[11px] font-medium text-[var(--text-muted)]"
        >
          Campaign
        </label>
        <select
          id="outreach-campaign-select"
          value={selectedCampaign ?? ""}
          onChange={(e) => setSelectedCampaign(e.target.value || null)}
          className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20"
        >
          {campaigns.length === 0 && <option value="">—</option>}
          {campaigns.map((c) => (
            <option key={c.campaign_name} value={c.campaign_name}>
              {c.dashboard_display_name || c.campaign_name}
            </option>
          ))}
        </select>
        {campaignsError && (
          <span className="text-[11px] text-red-700">{campaignsError}</span>
        )}
      </div>

      {summaryError && (
        <div
          className="rounded-lg border border-red-200/90 bg-red-50/80 px-3 py-2 text-[13px] text-red-950 ring-1 ring-red-900/10"
          role="alert"
        >
          {summaryError}
        </div>
      )}

      {summaryLoading && !summary && (
        <div
          className="h-32 animate-pulse rounded-xl bg-[var(--bg-elev)] ring-1 ring-black/[0.03]"
          aria-busy="true"
          aria-label="Loading summary"
        />
      )}

      {/* Summary cards — Pending + Sent + Needs Attention = Total Leads */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            { label: "Total Leads", value: summary.total_leads, sub: "All leads in campaign" },
            { label: "Pending", value: summary.pending, sub: "Ready to send" },
            { label: "Sent", value: summary.sent, sub: "Email delivered" },
            { label: "Replies", value: summary.reply_count, sub: "Inbound responses" },
            {
              label: "Reply Rate",
              value: summary.sent > 0 ? `${((summary.reply_count / summary.sent) * 100).toFixed(1)}%` : "—",
              sub: "Replies ÷ Sent",
            },
            {
              label: "Needs Attention",
              value: summary.hold + summary.failed + summary.duplicate + summary.skipped + summary.removed,
              sub: "Hold · Failed · Skipped · Dup",
            },
          ].map(({ label, value, sub }) => (
            <div
              key={label}
              className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 shadow-sm"
            >
              <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-[var(--text-muted)]">
                {label}
              </p>
              <p className="mt-1 text-[22px] font-extrabold tabular-nums text-[var(--text-primary)]">
                {value}
              </p>
              <p className="mt-0.5 text-[10px] text-[var(--text-muted)]">{sub}</p>
            </div>
          ))}
        </div>
      )}

      {/* Reply breakdown */}
      {summary && summary.reply_breakdown.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-4 shadow-sm">
          <p className="mb-2 text-[12px] font-bold text-[var(--text-primary)]">
            Reply Breakdown by Category
          </p>
          <ReplyBreakdownTable
            rows={summary.reply_breakdown.map((r) => ({
              category: r.category,
              count: r.count,
            }))}
            totalCount={summary.reply_count}
            showMoney={false}
            activeCategoryFilter={activeCategoryFilter}
            onCategoryFilter={setActiveCategoryFilter}
          />
        </div>
      )}

      {/* Leads table */}
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]/80 p-3 shadow-sm sm:p-4">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-muted)]">
              Lead Detail
            </p>
            <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              All leads with status and reply detail.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3 shrink-0">
            <div className="flex items-center gap-1.5">
              <label
                htmlFor="outreach-status-filter"
                className="text-[11px] font-medium text-[var(--text-muted)]"
              >
                Status
              </label>
              <select
                id="outreach-status-filter"
                value={statusFilter ?? ""}
                onChange={(e) => setStatusFilter(e.target.value || null)}
                className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20"
              >
                <option value="">All statuses</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex items-center gap-1.5">
              <label
                htmlFor="outreach-category-filter"
                className="text-[11px] font-medium text-[var(--text-muted)]"
              >
                Reply category
              </label>
              <select
                id="outreach-category-filter"
                value={activeCategoryFilter ?? ""}
                onChange={(e) => setActiveCategoryFilter(e.target.value || null)}
                className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-[12px] text-[var(--text-primary)] shadow-sm outline-none transition focus:border-[var(--accent-green)]/60 focus:ring-2 focus:ring-[var(--accent-green)]/20"
              >
                <option value="">All categories</option>
                {REPLY_CATEGORY_OPTIONS.map((cat) => (
                  <option key={cat} value={cat}>
                    {getCategoryMeta(cat).label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {leadsError && (
          <div
            className="mb-3 rounded-lg border border-red-200/90 bg-red-50/80 px-3 py-2 text-[13px] text-red-950 ring-1 ring-red-900/10"
            role="alert"
          >
            {leadsError}
          </div>
        )}

        <div className="overflow-hidden rounded-lg border border-[var(--border)]">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] table-fixed text-left text-[13px]">
              <caption className="sr-only">Outreach lead detail</caption>
              <thead className="border-b border-[var(--border)] bg-[var(--bg-elev)]/95 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                <tr>
                  <th className="px-3 py-2.5 w-[180px]">Account Name</th>
                  <th className="px-3 py-2.5 w-[120px]">SPOC</th>
                  <th className="px-3 py-2.5 w-[140px]">Status</th>
                  <th className="px-3 py-2.5 w-[100px]">Source</th>
                  <th className="px-3 py-2.5 w-[88px]">Email Sent?</th>
                  <th className="px-3 py-2.5 w-[112px]">Reply Received?</th>
                  <th className="px-3 py-2.5 w-[128px]">Reply Category</th>
                  <th className="px-3 py-2.5 w-[76px]">Thread</th>
                </tr>
              </thead>
              <tbody>
                {leadsLoading && !leads.length && (
                  <tr>
                    <td
                      colSpan={8}
                      className="px-3 py-8 text-center text-[var(--text-muted)]"
                    >
                      <div className="mx-auto h-8 w-48 animate-pulse rounded bg-[var(--bg-elev)]" />
                    </td>
                  </tr>
                )}
                {!leadsLoading && leads.length === 0 && (
                  <tr>
                    <td
                      colSpan={8}
                      className="px-3 py-8 text-center text-[13px] text-[var(--text-muted)]"
                    >
                      No leads found.
                    </td>
                  </tr>
                )}
                {leads.map((lead, idx) => {
                  const catMeta = lead.last_reply_category
                    ? getCategoryMeta(lead.last_reply_category)
                    : null;
                  return (
                    <tr
                      key={lead.id}
                      className={
                        idx % 2 === 0
                          ? "border-b border-[var(--border)]/50 bg-[var(--bg-card)] hover:bg-[var(--bg-elev)]/50"
                          : "border-b border-[var(--border)]/50 bg-[var(--bg-primary)]/40 hover:bg-[var(--bg-elev)]/50"
                      }
                    >
                      <td className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate font-medium text-[var(--text-primary)]">
                          {lead.account_name || "—"}
                        </span>
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        <span className="block truncate text-[var(--text-secondary)]">
                          {lead.spoc || "—"}
                        </span>
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex flex-col items-start gap-1">
                          <StatusBadge status={lead.status} />
                          <StatusReason status={lead.status} issues={lead.issues} />
                        </div>
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        <span className="text-[var(--text-secondary)]">
                          {lead.source || "—"}
                        </span>
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        {lead.sent_at ? (
                          <span className="inline-flex rounded-full border border-emerald-200/90 bg-emerald-50/90 px-2 py-0.5 text-[11px] font-semibold text-emerald-900 ring-1 ring-emerald-900/10">
                            Yes
                          </span>
                        ) : (
                          <span className="text-[12px] text-[var(--text-muted)]">
                            No
                          </span>
                        )}
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        {lead.last_reply_at ? (
                          <span className="inline-flex rounded-full border border-emerald-200/90 bg-emerald-50/90 px-2 py-0.5 text-[11px] font-semibold text-emerald-900 ring-1 ring-emerald-900/10">
                            Yes
                          </span>
                        ) : (
                          <span className="text-[12px] text-[var(--text-muted)]">
                            No
                          </span>
                        )}
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        {catMeta ? (
                          <span className="block truncate text-[var(--text-secondary)]">
                            {catMeta.label ||
                              formatCategorySlug(lead.last_reply_category!)}
                          </span>
                        ) : (
                          <span className="text-[var(--text-muted)]">—</span>
                        )}
                      </td>
                      <td className="overflow-hidden px-3 py-2.5">
                        {lead.gmail_thread_id && selectedCampaign ? (
                          <button
                            type="button"
                            onClick={() => {
                              const campaign = selectedCampaign;
                              onOpenThread(
                                lead.gmail_thread_id!,
                                lead.account_name ||
                                  lead.primary_email ||
                                  "Lead",
                                (tid) =>
                                  getOutreachThreadMessages(campaign, tid),
                              );
                            }}
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

        {/* Pagination bar */}
        <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <p className="text-[11px] tabular-nums text-[var(--text-muted)]">
              {leadsTotal === 0
                ? "No rows"
                : `Showing ${rangeStart}–${rangeEnd} of ${leadsTotal} row${leadsTotal === 1 ? "" : "s"}`}
            </p>
            <span className="text-[11px] text-[var(--text-muted)]">·</span>
            <div className="flex items-center gap-1">
              {PAGE_SIZE_OPTIONS.map((size) => (
                <button
                  key={size}
                  type="button"
                  onClick={() => setPageSize(size)}
                  className={
                    pageSize === size
                      ? "rounded px-2 py-0.5 text-[11px] font-semibold bg-[var(--accent-green)]/10 text-[var(--accent-green)] border border-[var(--accent-green)]/30"
                      : "rounded px-2 py-0.5 text-[11px] text-[var(--text-muted)] hover:text-[var(--text-primary)] border border-transparent hover:border-[var(--border)]"
                  }
                >
                  {size}
                </button>
              ))}
              <span className="text-[11px] text-[var(--text-muted)]">
                per page
              </span>
            </div>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center gap-1">
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
                ),
              )}
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
    </div>
  );
}
