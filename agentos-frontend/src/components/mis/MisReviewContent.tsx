"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  listMisRuns,
  getMisRun,
  deleteMisSummaryRow,
  restoreMisSummaryRow,
  saveMis,
  approveMis,
  setMisStatus,
  insertMisSummaryRow,
  listAvailableRateLines,
  listAlternateContractTerms,
  copyContractRateLinesToMisRun,
  type AlternateContractTerm,
  apiJson,
  rerunMisRun,
  type MisRunListItem,
  type MisRunDetail,
  type MisRunStatusCounts,
  type MisSiteLabelFields,
  type MisSummaryRow,
  type MisAutoApproveAudit,
  type AvailableRateLine,
  isMisAutoApproved,
  MIS_AUTO_APPROVE_ACTOR,
} from "@/lib/api";

/* ─── Formatting ─────────────────────────────────────────────────────── */

const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
function fmtAmt(v: number | string | null | undefined): string {
  if (v == null) return "—";
  const n = Number(v);
  return isNaN(n) ? "—" : INR.format(n);
}
function fmtNum(v: number | string | null | undefined): string {
  if (v == null) return "—";
  const n = Number(v);
  return isNaN(n) ? "—" : String(n);
}
function fmtPeriod(s: string, e: string): string {
  try {
    const fmt = (d: Date) => d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
    return `${fmt(new Date(s + "T00:00:00"))} — ${fmt(new Date(e + "T00:00:00"))}`;
  } catch { return `${s} — ${e}`; }
}

/** ceil(inclusive calendar days / 7): 28-day Feb → 4 weeks; 29–31 day months → 5. */
function billingWeeksInclusive(start: string, end: string): number | null {
  const d0 = new Date(`${start}T00:00:00`);
  const d1 = new Date(`${end}T00:00:00`);
  if (Number.isNaN(d0.getTime()) || Number.isNaN(d1.getTime())) return null;
  const days = Math.round((d1.getTime() - d0.getTime()) / 86400000) + 1;
  if (days < 1) return 1;
  return Math.ceil(days / 7);
}

function visitWeekCreateHint(periodStart: string, periodEnd: string, visitsPerWeekRaw: string): string {
  const weeks = billingWeeksInclusive(periodStart, periodEnd);
  const n = Number(visitsPerWeekRaw);
  if (weeks == null) {
    return "This month’s week count (4 or 5) is applied automatically.";
  }
  if (Number.isFinite(n) && n > 0) {
    return `This MIS period is ${weeks} weeks. Billable visits = ${n * weeks}.`;
  }
  return `This MIS period is ${weeks} weeks. Amount = rate × visits/week × weeks.`;
}

/**
 * MIS runs are tied to the OHC workbook `client_site_key` (e.g. TCS-Noida-Sector 157-TCS Building).
 * `service_site.display_name` is often generic across sites, so prefer the workbook key first; then
 * DB site fields; still append city when it adds disambiguation.
 */
function formatMisSiteHeadline(d: MisSiteLabelFields): string {
  const ck = typeof d.client_site_key === "string" ? d.client_site_key.trim() : "";
  const fromDb =
    [d.site_name, d.site_canonical_name, d.service_site_key]
      .map((s) => (typeof s === "string" ? s.trim() : ""))
      .find(Boolean) || "";
  const name = ck || fromDb || "—";
  const city = typeof d.site_city === "string" ? d.site_city.trim() : "";
  if (city && !name.toLowerCase().includes(city.toLowerCase())) {
    return `${name} · ${city}`;
  }
  return name;
}

/** Case-insensitive match on workbook key, DB site fields, client name, and headline text. */
function misRunMatchesSiteSearch(it: MisRunListItem, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    it.client_site_key,
    it.site_name,
    it.site_canonical_name,
    it.service_site_key,
    it.site_city,
    it.client_name,
    formatMisSiteHeadline(it),
  ]
    .map((s) => (typeof s === "string" ? s.toLowerCase() : ""))
    .join("\n");
  return hay.includes(q);
}

/** JSON / drivers sometimes coerce tier; strict === in the UI dropped rows from counters. */
function normalizeMisTier(t: unknown): 1 | 2 | 3 {
  const n = typeof t === "number" && Number.isFinite(t) ? t : Number(t);
  if (n === 1 || n === 2 || n === 3) return n;
  return 3;
}

/** Treat only real booleans / obvious truthy strings as omitted (avoid Boolean("false") === true). */
function isSummaryRowOmitted(r: { is_omitted?: unknown }): boolean {
  const v = r.is_omitted;
  if (v === true) return true;
  if (v === false || v == null) return false;
  if (typeof v === "string") return v.toLowerCase() === "true" || v === "1";
  if (typeof v === "number") return v === 1;
  return false;
}

/* ─── Tier visuals ───────────────────────────────────────────────────── */

const TIER = {
  1: { color: "#15a373", label: "✓" },
  2: { color: "#f59e0b", label: "!" },
  3: { color: "#ef4444", label: "?" },
} as const;

const TIER_TITLE: Record<1 | 2 | 3, string> = {
  1: "Looks good — no action needed",
  2: "Please review this line",
  3: "Needs a closer check",
};

function TierDot({ tier }: { tier: 1 | 2 | 3 }) {
  const t = TIER[tier];
  return (
    <span
      className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold text-white"
      style={{ background: t.color }}
      title={TIER_TITLE[tier]}
    >
      {t.label}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const [bg, fg] = s === "approved" ? ["rgba(21,163,115,0.15)", "#0d8a5e"]
    : s === "rejected" ? ["rgba(239,68,68,0.15)", "#ef4444"]
      : ["rgba(245,158,11,0.15)", "#d97706"];
  return (
    <span
      className="inline-flex max-w-full items-center rounded-full px-1.5 py-px text-[10px] font-medium capitalize leading-tight"
      style={{ background: bg, color: fg }}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function AutoApprovedBadge() {
  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full bg-indigo-100 px-1.5 py-px text-[10px] font-semibold leading-tight text-indigo-800"
      title={`Approved automatically (${MIS_AUTO_APPROVE_ACTOR})`}
    >
      Auto
    </span>
  );
}

function formatAutoApproveReason(code: string): string {
  const key = code.trim();
  const known: Record<string, string> = {
    auto_approve_disabled: "Auto-approve is disabled",
    no_prior_approved_mis: "No approved MIS for the prior month",
    employee_count_by_role_mismatch: "Headcount by role differs from prior month",
    line_item_count_mismatch: "Non-staff line count differs from prior month",
    contract_rate_line_fingerprint_mismatch: "Active contract lines differ from prior month",
    open_attendance_recon: "Open attendance reconciliation for this period",
    human_correction_present: "Human edits on this run",
    duplicate_staffing_billing: "Duplicate staffing billing anomaly",
    lwd_doj_handover_applied: "LWD/DOJ handover merged attendance (human review required)",
    not_all_tier_1: "Not all active lines are tier 1",
    line_item_total_prior_zero: "Non-staff billed total was zero last month",
    prior_total_zero: "Prior month total is zero",
    current_total_zero: "Current month total is zero",
    site_blocklisted: "Site is on the auto-approve blocklist",
  };
  if (known[key]) return known[key];
  if (key.startsWith("amount_delta_") && key.includes("pct_gt_")) {
    return "Billed total change exceeds tolerance";
  }
  if (key.startsWith("line_item_total_delta_") && key.includes("pct_gt_")) {
    return "Non-staff billed total change exceeds tolerance";
  }
  if (key.startsWith("validation_")) {
    return `Validation: ${key.slice("validation_".length).replace(/_/g, " ")}`;
  }
  if (key.startsWith("status_")) {
    return `Run status: ${key.slice("status_".length).replace(/_/g, " ")}`;
  }
  return key.replace(/_/g, " ");
}

function MisAutoApproveBanner({
  status,
  approvedBy,
  audit,
}: {
  status: string;
  approvedBy?: string | null;
  audit?: MisAutoApproveAudit | null;
}) {
  const autoApproved = isMisAutoApproved(approvedBy);
  const reasons = (audit?.reasons ?? []).filter((r) => typeof r === "string" && r.trim());
  const pct = audit?.pct_delta;
  const tolerance = audit?.tolerance_pct;

  if (!autoApproved && reasons.length === 0) return null;

  if (autoApproved && status === "approved") {
    const pctNote =
      pct != null && Number.isFinite(pct)
        ? ` (${pct.toFixed(1)}% vs prior month${tolerance != null ? `, limit ${tolerance}%` : ""})`
        : "";
    return (
      <div className="border-b border-indigo-200/80 bg-indigo-50/90 px-3 py-2 text-xs text-indigo-950">
        <span className="font-semibold">Auto-approved</span>
        <span className="text-indigo-800">
          {" "}
          — system matched prior approved month within tolerance{pctNote}.
        </span>
        {audit?.xlsx_error && (
          <span className="mt-1 block text-amber-800">Drive upload issue: {audit.xlsx_error}</span>
        )}
      </div>
    );
  }

  if (reasons.length === 0) return null;

  return (
    <div className="border-b border-amber-200/80 bg-amber-50/90 px-3 py-2 text-xs text-amber-950">
      <span className="font-semibold">Auto-approve not applied</span>
      <ul className="mt-1 list-inside list-disc space-y-0.5 text-amber-900">
        {reasons.map((r) => (
          <li key={r}>{formatAutoApproveReason(r)}</li>
        ))}
      </ul>
    </div>
  );
}

function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-[var(--bg-elev)] ${className}`} />;
}

/* ─── Per-cell editing types ─────────────────────────────────────────── */

type EditField = "contractual_rate" | "contracted_count" | "absent_days" | "final_amount";
const EDIT_FIELDS: EditField[] = ["contractual_rate", "contracted_count", "absent_days", "final_amount"];

const HEADCOUNT_BILLING_MODELS = new Set(["per_head", "rate_attendance"]);

function isStaffingMaxPostsRow(row: MisSummaryRow): boolean {
  const bm = (row.billing_model || "").trim().toLowerCase();
  if (HEADCOUNT_BILLING_MODELS.has(bm)) return true;
  const emp = (row.employee_external_id || "").trim();
  return !!emp && !bm;
}

function isLineQtyRow(row: MisSummaryRow): boolean {
  const bm = (row.billing_model || "").trim().toLowerCase();
  if (bm === "per_visit" || bm === "fixed_monthly" || bm === "as_per_actuals") return true;
  return !isStaffingMaxPostsRow(row);
}

function lineQtyDisplayValue(row: MisSummaryRow): number | null {
  if ((row.billing_model || "").trim().toLowerCase() === "per_visit") {
    if (row.visits_per_month != null) return row.visits_per_month;
  }
  const c = row.contracted_count;
  return c != null ? Number(c) : null;
}

/* ─── Main ───────────────────────────────────────────────────────────── */

const STATUS_FILTERS = [
  { id: "pending_human", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "rejected", label: "Rejected" },
  { id: null, label: "All" },
] as const;

function misTabLabel(filter: (typeof STATUS_FILTERS)[number], counts: MisRunStatusCounts | null): string {
  if (!counts) return filter.label;
  const key = filter.id === null ? "all" : filter.id;
  return `${filter.label} (${counts[key as keyof MisRunStatusCounts]})`;
}

/** Previous calendar month label in Asia/Kolkata (matches MIS list default `period_scope`). */
function formatPriorMonthLabelIst(): string {
  try {
    const ymd = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Kolkata" }).slice(0, 10);
    const [y, m] = ymd.split("-").map(Number);
    const pm = m === 1 ? 12 : m - 1;
    const py = m === 1 ? y - 1 : y;
    const mid = new Date(Date.UTC(py, pm - 1, 15));
    return new Intl.DateTimeFormat("en-IN", {
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(mid);
  } catch {
    return "last month";
  }
}

export function MisReviewContent() {
  const [statusFilter, setStatusFilter] = useState<string | null>("pending_human");
  /** false = API period_scope prior_month (IST); true = all periods */
  const [showAllPeriods, setShowAllPeriods] = useState(false);
  const priorMonthLabel = useMemo(() => formatPriorMonthLabelIst(), []);
  const [siteSearch, setSiteSearch] = useState("");
  const [items, setItems] = useState<MisRunListItem[]>([]);
  const [statusCounts, setStatusCounts] = useState<MisRunStatusCounts | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");

  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<MisRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  const [actionBusy, setActionBusy] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [rejectionNotes, setRejectionNotes] = useState("");
  const [toast, setToast] = useState<{ msg: string; type: "ok" | "err" } | null>(null);

  // Per-cell editing: which cell is active
  const [activeCell, setActiveCell] = useState<{ rowId: string; field: EditField } | null>(null);
  // Accumulated edits across all rows: { rowId: { field: "newValue" } }
  const [cellEdits, setCellEdits] = useState<Record<string, Partial<Record<EditField, string>>>>({});
  /** True after add/delete/restore/import — row set changed in DB but MIS not recalculated until Save. */
  const [needsRecalc, setNeedsRecalc] = useState(false);
  const [expandedNotes, setExpandedNotes] = useState<Set<string>>(new Set());

  // Add line item
  const [addMode, setAddMode] = useState<"pick" | "create" | null>(null);
  const [availableLines, setAvailableLines] = useState<AvailableRateLine[]>([]);
  const [alternateContracts, setAlternateContracts] = useState<AlternateContractTerm[]>([]);
  const [addLineLoading, setAddLineLoading] = useState(false);
  const [newLine, setNewLine] = useState({
    description: "",
    role_code: "",
    rate_amount: "",
    contracted_quantity: "1",
    /** Per-visit: exactly one of week or month. */
    visit_cadence: "week" as "week" | "month",
    visits_per_week: "",
    visit_per_month: "",
    billing_model: "fixed_monthly",
    line_kind: "standard" as "standard" | "invoice_admin",
    invoice_admin_pct: "10",
    invoice_admin_base: "staffing_only" as "staffing_only" | "staffing_plus_fixed_monthly",
  });
  /** Progressive disclosure: dense default, full tips on demand (common pattern for data-heavy UIs). */
  const [howToOpen, setHowToOpen] = useState(false);

  const cellInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(null), 4000); return () => clearTimeout(t); } }, [toast]);

  /** Run list: status tab + optional period_scope (prior IST month vs all history). Tab counts match the same scope. */
  const loadList = useCallback(async () => {
    setListLoading(true); setListError("");
    try {
      const { items: next, status_counts } = await listMisRuns({
        status: statusFilter,
        limit: 200,
        period_scope: showAllPeriods ? "all" : "prior_month",
      });
      setItems(next);
      setStatusCounts(status_counts);
    } catch (e) { setListError(e instanceof Error ? e.message : "Failed"); }
    finally { setListLoading(false); }
  }, [statusFilter, showAllPeriods]);

  useEffect(() => { void loadList(); }, [loadList]);

  const filteredItems = useMemo(
    () => items.filter((it) => misRunMatchesSiteSearch(it, siteSearch)),
    [items, siteSearch],
  );

  useEffect(() => {
    if (filteredItems.length > 0 && (!selectedId || !filteredItems.some((x) => x.id === selectedId))) {
      setSelectedId(filteredItems[0].id);
    }
    if (filteredItems.length === 0) {
      setSelectedId("");
      setDetail(null);
    }
  }, [filteredItems, selectedId]);

  const loadDetail = useCallback(async (id: string) => {
    if (!id) return;
    setDetailLoading(true); setDetailError(""); setShowReject(false); setRejectionNotes("");
    setActiveCell(null);
    setCellEdits({});
    setNeedsRecalc(false);
    setExpandedNotes(new Set());
    setAddMode(null);
    try { setDetail(await getMisRun(id)); }
    catch (e) { setDetail(null); setDetailError(e instanceof Error ? e.message : "Failed"); }
    finally { setDetailLoading(false); }
  }, []);

  useEffect(() => { if (selectedId) void loadDetail(selectedId); }, [selectedId, loadDetail]);

  /* ── Cell editing helpers ── */

  function getOriginal(row: MisSummaryRow, field: EditField): string {
    const v = row[field];
    return v != null ? String(Number(v)) : "";
  }

  function getCellValue(rowId: string, field: EditField, row: MisSummaryRow): string {
    return cellEdits[rowId]?.[field] ?? getOriginal(row, field);
  }

  function isCellChanged(rowId: string, field: EditField, row: MisSummaryRow): boolean {
    const edited = cellEdits[rowId]?.[field];
    if (edited === undefined) return false;
    return edited !== getOriginal(row, field);
  }

  function setCellValue(rowId: string, field: EditField, value: string) {
    setCellEdits((p) => {
      const next: Record<string, Partial<Record<EditField, string>>> = {
        ...p,
        [rowId]: { ...p[rowId], [field]: value },
      };
      if (field === "contracted_count" && detail) {
        const src = detail.summary_rows.find((r) => r.id === rowId);
        const crl = src?.contract_rate_line_id;
        if (crl) {
          for (const r of detail.summary_rows) {
            if (r.contract_rate_line_id === crl && isStaffingMaxPostsRow(r)) {
              next[r.id] = { ...next[r.id], contracted_count: value };
            }
          }
        }
      }
      return next;
    });
  }

  function startCellEdit(rowId: string, field: EditField, row: MisSummaryRow) {
    if (!isPending || isSummaryRowOmitted(row)) return;
    if (field === "contracted_count" && !isStaffingMaxPostsRow(row)) return;
    setActiveCell({ rowId, field });
    if (cellEdits[rowId]?.[field] === undefined) {
      setCellValue(rowId, field, getOriginal(row, field));
    }
    setTimeout(() => cellInputRef.current?.select(), 20);
  }

  function confirmFinalAmountEditIfNeeded(rowId: string): boolean {
    if (!detail) return true;
    const row = detail.summary_rows.find((r) => r.id === rowId);
    if (!row || !isCellChanged(rowId, "final_amount", row)) return true;
    const ok = window.confirm(
      "You are overriding the line amount. After Save, other lines are recalculated from attendance; this line's amount will be kept. Make all edits, then press Save. Continue?",
    );
    if (!ok) {
      setCellEdits((p) => {
        const rowEdits = { ...p[rowId] };
        delete rowEdits.final_amount;
        const next = { ...p };
        if (Object.keys(rowEdits).length === 0) delete next[rowId];
        else next[rowId] = rowEdits;
        return next;
      });
      return false;
    }
    return true;
  }

  function commitCell() {
    if (activeCell?.field === "final_amount" && !confirmFinalAmountEditIfNeeded(activeCell.rowId)) {
      setActiveCell(null);
      return;
    }
    setActiveCell(null);
  }

  function handleCellKeyDown(e: React.KeyboardEvent, rowId: string, field: EditField) {
    if (e.key === "Escape") { setActiveCell(null); return; }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      if (field === "final_amount" && !confirmFinalAmountEditIfNeeded(rowId)) {
        setActiveCell(null);
        return;
      }
      setActiveCell(null);
      const fi = EDIT_FIELDS.indexOf(field);
      const rows = detail?.summary_rows.filter((r) => !isSummaryRowOmitted(r)) || [];
      const ri = rows.findIndex((r) => r.id === rowId);
      if (e.key === "Tab" && !e.shiftKey) {
        if (fi < EDIT_FIELDS.length - 1) {
          startCellEdit(rowId, EDIT_FIELDS[fi + 1], rows[ri]);
        } else if (ri < rows.length - 1) {
          startCellEdit(rows[ri + 1].id, EDIT_FIELDS[0], rows[ri + 1]);
        }
      } else if (e.key === "Tab" && e.shiftKey) {
        if (fi > 0) {
          startCellEdit(rowId, EDIT_FIELDS[fi - 1], rows[ri]);
        } else if (ri > 0) {
          startCellEdit(rows[ri - 1].id, EDIT_FIELDS[EDIT_FIELDS.length - 1], rows[ri - 1]);
        }
      }
    }
  }

  /* ── Actions ── */

  function buildRowEditsFromGrid(): Array<Record<string, unknown>> {
    if (!detail) return [];
    const toNum = (v: string) => {
      if (!v) return undefined;
      const n = Number(v);
      return isNaN(n) ? undefined : n;
    };
    const edits: Array<Record<string, unknown>> = [];
    for (const [rowId, fields] of Object.entries(cellEdits)) {
      const row = detail.summary_rows.find((r) => r.id === rowId);
      if (!row) continue;
      const changed: Record<string, unknown> = { id: rowId };
      let hasChange = false;
      for (const f of EDIT_FIELDS) {
        if (fields[f] !== undefined && fields[f] !== getOriginal(row, f)) {
          changed[f] = toNum(fields[f]!);
          hasChange = true;
        }
      }
      if (hasChange) edits.push(changed);
    }
    return edits;
  }

  async function handleSave() {
    if (!detail || !isDraftDirty) return;
    const edits = buildRowEditsFromGrid();
    setActionBusy(true);
    try {
      const result = await saveMis({ mis_run_id: detail.id, row_edits: edits });
      const base =
        result.correction_count > 0
          ? `Saved with ${result.correction_count} correction${result.correction_count > 1 ? "s" : ""}`
          : "MIS saved and recalculated";
      setToast({ msg: base, type: "ok" });
      setCellEdits({});
      setNeedsRecalc(false);
      setActiveCell(null);
      await loadList();
      await loadDetail(detail.id);
    } catch (e) {
      const errDetail = e instanceof Error ? e.message : "Save failed";
      setToast({
        msg: `${errDetail} Your edits are still here — press Save again. If the line list looks empty, use Re-run.`,
        type: "err",
      });
    } finally {
      setActionBusy(false);
    }
  }

  async function handleApprove() {
    if (!detail || isDraftDirty) return;
    setActionBusy(true);
    try {
      const result = await approveMis({ mis_run_id: detail.id });
      const base = result.idempotent ? "Already approved" : "MIS approved";
      if (result.xlsx_gdrive_error) {
        const tries = result.xlsx_gdrive_upload_attempts ?? 3;
        setToast({
          msg: `${base}, but Google Drive upload failed after ${tries} attempts. Click Approve again to retry upload. If this run is still Pending, use Re-run to recalculate and upload.`,
          type: "err",
        });
      } else if (result.xlsx_error) {
        setToast({
          msg: `${base}. Excel was not built: ${result.xlsx_error}`,
          type: "err",
        });
      } else {
        setToast({ msg: base, type: "ok" });
      }
      await loadList();
      await loadDetail(detail.id);
    } catch (e) {
      setToast({ msg: e instanceof Error ? e.message : "Approve failed", type: "err" });
    } finally {
      setActionBusy(false);
    }
  }

  async function handleRejectSubmit() {
    if (!detail || !rejectionNotes.trim()) return;
    setActionBusy(true);
    try {
      await apiJson("/api/o2c/mis/status", { method: "POST", body: JSON.stringify({ mis_run_id: detail.id, status: "rejected", rejection_notes: rejectionNotes.trim() }) });
      setToast({ msg: "MIS rejected", type: "ok" });
      setShowReject(false); setRejectionNotes("");
      await loadList(); await loadDetail(detail.id);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  async function handleReopen() {
    if (!detail || detail.status !== "approved") return;
    const ok = window.confirm(
      "Reopen this MIS for editing? It will move back to Pending. Lines become editable; you must Approve again after changes. The existing Drive Excel file is not removed.",
    );
    if (!ok) return;
    setActionBusy(true);
    try {
      await setMisStatus({ mis_run_id: detail.id, status: "pending_human" });
      setToast({ msg: "MIS reopened for editing", type: "ok" });
      await loadList();
      await loadDetail(detail.id);
    } catch (e) {
      setToast({ msg: e instanceof Error ? e.message : "Reopen failed", type: "err" });
    } finally {
      setActionBusy(false);
    }
  }

  async function handleDeleteRow(rowId: string) {
    if (!detail) return;
    setActionBusy(true);
    try {
      await deleteMisSummaryRow(rowId);
      setToast({
        msg: "Row removed for this site. Shared contract lines stay active for other sites. Save will not re-add this row here.",
        type: "ok",
      });
      if (activeCell?.rowId === rowId) setActiveCell(null);
      await loadList();
      await loadDetail(detail.id);
      setNeedsRecalc(true);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  async function handleRestoreRow(rowId: string) {
    if (!detail) return;
    setActionBusy(true);
    try {
      const r = await restoreMisSummaryRow(rowId);
      const amt =
        r.restored_final_amount != null
          ? ` Prior amount ₹${Number(r.restored_final_amount).toLocaleString("en-IN", { maximumFractionDigits: 0 })} restored.`
          : " Check Rate / Amount — set values if the model had this line at ₹0.";
      setToast({
        msg: `Line back in billing; Max Posts +1 for this contract line where applicable.${amt} Save to recalculate totals.`,
        type: "ok",
      });
      if (activeCell?.rowId === rowId) setActiveCell(null);
      await loadList();
      await loadDetail(detail.id);
      setNeedsRecalc(true);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  async function handleOpenPickLine() {
    if (!detail) return;
    setAddMode("pick"); setAddLineLoading(true);
    try {
      const [lines, alts] = await Promise.all([
        listAvailableRateLines(detail.id),
        listAlternateContractTerms(detail.id),
      ]);
      setAvailableLines(lines);
      setAlternateContracts(alts);
    }
    catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); setAddMode(null); }
    finally { setAddLineLoading(false); }
  }

  async function handleImportContractLines(sourceCtvId: string) {
    if (!detail) return;
    setActionBusy(true);
    try {
      const res = await copyContractRateLinesToMisRun({
        mis_run_id: detail.id,
        source_contract_terms_version_id: sourceCtvId,
      });
      setToast({
        msg: res.cloned_count > 0
          ? `Imported ${res.cloned_count} line(s) from contract`
          : "No new lines to import (already on this contract)",
        type: "ok",
      });
      setAvailableLines(await listAvailableRateLines(detail.id));
      setAlternateContracts(await listAlternateContractTerms(detail.id));
      await loadDetail(detail.id);
      setNeedsRecalc(true);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Import failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  async function handlePickLine(rateLineId: string) {
    if (!detail) return;
    setActionBusy(true);
    try {
      await insertMisSummaryRow({ mis_run_id: detail.id, contract_rate_line_id: rateLineId });
      setToast({ msg: "Line added", type: "ok" });
      setAddMode(null);
      await loadDetail(detail.id);
      setNeedsRecalc(true);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  async function handleCreateNewLine() {
    if (!detail) return;
    const isAdmin = newLine.line_kind === "invoice_admin";
    if (!newLine.description.trim()) {
      setToast({ msg: "Service name is required", type: "err" }); return;
    }
    if (isAdmin) {
      const pct = Number(newLine.invoice_admin_pct);
      if (!Number.isFinite(pct) || pct <= 0 || pct > 100) {
        setToast({ msg: "Invoice admin % must be between 0 and 100 (exclusive of 0)", type: "err" }); return;
      }
    } else if (!newLine.rate_amount.trim()) {
      setToast({ msg: "Service name and rate are required", type: "err" }); return;
    }
    const vpw = Number(newLine.visits_per_week);
    const vpm = Number(newLine.visit_per_month);
    const useWeek = newLine.visit_cadence === "week";
    const hasWeek = useWeek && Number.isFinite(vpw) && vpw > 0;
    const hasMonth = !useWeek && Number.isFinite(vpm) && vpm > 0;
    if (newLine.billing_model === "per_visit") {
      if (!hasWeek && !hasMonth) {
        setToast({
          msg: useWeek
            ? "Per visit: enter visits per week"
            : "Per visit: enter visits per month",
          type: "err",
        });
        return;
      }
    }
    const headOrQty = Number(newLine.contracted_quantity);
    if (newLine.billing_model === "per_head") {
      if (!Number.isFinite(headOrQty) || headOrQty <= 0) {
        setToast({ msg: "Per head: enter headcount (contracted quantity)", type: "err" });
        return;
      }
    }
    setActionBusy(true);
    try {
      const body: Record<string, unknown> = {
        mis_run_id: detail.id,
        description: newLine.description.trim(),
        billing_model: "fixed_monthly",
      };
      if (isAdmin) {
        body.contracted_quantity = 1;
        body.role_code = "OHC_ADMIN_INVOICE_PCT";
        body.invoice_admin_pct = Number(newLine.invoice_admin_pct);
        if (newLine.invoice_admin_base === "staffing_plus_fixed_monthly") {
          body.invoice_admin_base = "staffing_plus_fixed_monthly";
        }
      } else {
        body.role_code = newLine.role_code.trim() || newLine.description.trim().toUpperCase().replace(/\s+/g, "_");
        body.rate_amount = Number(newLine.rate_amount);
        body.billing_model = newLine.billing_model;
        if (newLine.billing_model === "per_visit") {
          if (hasWeek) body.visits_per_week = vpw;
          else body.visit_per_month = vpm;
        } else {
          body.contracted_quantity = Number(newLine.contracted_quantity) || 1;
        }
      }
      await apiJson("/api/o2c/mis/summary-row/create-new", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setToast({ msg: "New line created and added", type: "ok" });
      setAddMode(null);
      setNewLine({
        description: "",
        role_code: "",
        rate_amount: "",
        contracted_quantity: "1",
        visit_cadence: "week",
        visits_per_week: "",
        visit_per_month: "",
        billing_model: "fixed_monthly",
        line_kind: "standard",
        invoice_admin_pct: "10",
        invoice_admin_base: "staffing_only",
      });
      await loadDetail(detail.id);
      setNeedsRecalc(true);
    } catch (e) { setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" }); }
    finally { setActionBusy(false); }
  }

  /* ── Derived ── */

  const hasGridEdits = useMemo(() => {
    if (!detail) return false;
    return Object.entries(cellEdits).some(([rowId, fields]) => {
      const row = detail.summary_rows.find((r) => r.id === rowId);
      if (!row) return false;
      return EDIT_FIELDS.some((f) => fields[f] !== undefined && fields[f] !== getOriginal(row, f));
    });
  }, [detail, cellEdits]);

  const isDraftDirty = hasGridEdits || needsRecalc;

  const savedBillTotal = useMemo(() => {
    if (!detail) return 0;
    return detail.summary_rows
      .filter((r) => !isSummaryRowOmitted(r))
      .reduce((s, r) => {
        const val = Number(r.final_amount ?? 0);
        return s + (isNaN(val) ? 0 : val);
      }, 0);
  }, [detail]);

  /** When draft is dirty, show best-known total (Amt edits applied; full recalc on Save). */
  const displayBillTotal = useMemo(() => {
    if (!detail) return 0;
    if (!isDraftDirty) return savedBillTotal;
    return detail.summary_rows
      .filter((r) => !isSummaryRowOmitted(r))
      .reduce((s, r) => {
        const raw = cellEdits[r.id]?.final_amount;
        const val = Number(raw !== undefined ? raw : r.final_amount ?? 0);
        return s + (isNaN(val) ? 0 : val);
      }, 0);
  }, [detail, cellEdits, isDraftDirty, savedBillTotal]);

  /** Draft = all table rows; in-billing = not omitted; tier OK/review/check = in-billing only (approval signal). */
  const lineStats = useMemo(() => {
    if (!detail) return { total: 0, active: 0, omitted: 0 };
    const ts = detail.tier_summary;
    if (
      ts.summary_row_count != null &&
      ts.active_row_count != null &&
      ts.omitted_row_count != null
    ) {
      return {
        total: ts.summary_row_count,
        active: ts.active_row_count,
        omitted: ts.omitted_row_count,
      };
    }
    const rows = detail.summary_rows;
    let omitted = 0;
    for (const r of rows) {
      if (isSummaryRowOmitted(r)) omitted += 1;
    }
    return { total: rows.length, active: rows.length - omitted, omitted };
  }, [detail]);

  const tierCounts = useMemo(() => {
    if (!detail) return { t1: 0, t2: 0, t3: 0 };
    const ts = detail.tier_summary;
    const n = detail.summary_rows.length;
    if (
      ts.active_row_count != null &&
      ts.summary_row_count != null &&
      ts.summary_row_count === n &&
      n > 0
    ) {
      return { t1: ts.tier_1_count, t2: ts.tier_2_count, t3: ts.tier_3_count };
    }
    const rows = detail.summary_rows.filter((r) => !isSummaryRowOmitted(r));
    return {
      t1: rows.filter((r) => normalizeMisTier(r.tier) === 1).length,
      t2: rows.filter((r) => normalizeMisTier(r.tier) === 2).length,
      t3: rows.filter((r) => normalizeMisTier(r.tier) === 3).length,
    };
  }, [detail]);

  const billingTableSections = useMemo(() => {
    if (!detail) return { inBilling: [] as MisSummaryRow[], removed: [] as MisSummaryRow[] };
    const inBilling: MisSummaryRow[] = [];
    const removed: MisSummaryRow[] = [];
    for (const r of detail.summary_rows) {
      if (isSummaryRowOmitted(r)) removed.push(r);
      else inBilling.push(r);
    }
    return { inBilling, removed };
  }, [detail]);

  const isPending = detail?.status === "pending_human";
  const needsAttention = tierCounts.t2 + tierCounts.t3;
  const tierSum = tierCounts.t1 + tierCounts.t2 + tierCounts.t3;
  const inBillingCount = lineStats.active;

  const billingColSpan = isPending ? 9 : 8;

  /* ── Cell render ── */

  function renderCell(row: MisSummaryRow, field: EditField, isCurrency: boolean) {
    const isActive = activeCell?.rowId === row.id && activeCell.field === field;
    const changed = isCellChanged(row.id, field, row);
    const displayVal = getCellValue(row.id, field, row);

    if (isActive) {
      return (
        <input
          ref={cellInputRef}
          type="text"
          inputMode="decimal"
          value={displayVal}
          onChange={(e) => setCellValue(row.id, field, e.target.value)}
          onBlur={commitCell}
          onKeyDown={(e) => handleCellKeyDown(e, row.id, field)}
          autoFocus
          className="box-border w-full min-w-0 max-w-full rounded border-2 border-[var(--accent-green)] bg-white px-1.5 py-0.5 text-right text-[11px] font-medium tabular-nums focus:outline-none focus:ring-1 focus:ring-[var(--accent-green)]/40"
        />
      );
    }

    const formatted = isCurrency ? fmtAmt(displayVal || row[field]) : fmtNum(displayVal || row[field]);

    if (!isPending || isSummaryRowOmitted(row)) {
      return (
        <span
          className={`tabular-nums text-[11px] ${isSummaryRowOmitted(row) ? "line-through text-[var(--text-muted)]" : ""}`}
        >
          {formatted}
        </span>
      );
    }

    return (
      <button
        type="button"
        onClick={() => startCellEdit(row.id, field, row)}
        className={`min-h-[1.4rem] w-full min-w-0 max-w-full cursor-text rounded border border-dashed border-gray-300/80 bg-[var(--bg-elev)]/40 px-1 py-0.5 text-right text-[11px] tabular-nums transition-colors hover:border-[var(--accent-green)]/50 hover:bg-[rgba(21,163,115,0.06)] ${
          changed
            ? "bg-amber-50 font-semibold text-amber-900 ring-1 ring-amber-200 border-amber-400"
            : "text-[var(--text-primary)]"
        }`}
        title="Click to edit this number"
      >
        {formatted}
      </button>
    );
  }

  function renderMaxPostsCell(row: MisSummaryRow) {
    if (!isStaffingMaxPostsRow(row)) {
      return <span className="text-[11px] text-[var(--text-muted)]">—</span>;
    }
    return renderCell(row, "contracted_count", false);
  }

  function renderQtyCell(row: MisSummaryRow) {
    if (!isLineQtyRow(row)) {
      return <span className="text-[11px] text-[var(--text-muted)]">—</span>;
    }
    const v = lineQtyDisplayValue(row);
    const formatted = v != null ? fmtNum(String(v)) : "—";
    const bm = (row.billing_model || "").trim().toLowerCase();
    const title =
      bm === "per_visit"
        ? "Visits this period (weekly cadence × 4 or 5 weeks). Read-only — edit contract line or Add line, then Save."
        : "Contract quantity / units. Read-only — edit contract line or Add line, then Save.";
    return (
      <span
        className={`tabular-nums text-[11px] ${
          isSummaryRowOmitted(row) ? "line-through text-[var(--text-muted)]" : "text-[var(--text-secondary)]"
        }`}
        title={title}
      >
        {formatted}
      </span>
    );
  }

  function renderSummaryRow(row: MisSummaryRow) {
    const omitted = isSummaryRowOmitted(row);
    const tierN = normalizeMisTier(row.tier);
    const tierColor = TIER[tierN].color;
    const calcOpen = expandedNotes.has(row.id + ":f") && !!row.calc_notes;

    return (
      <Fragment key={row.id}>
      <tr
        className={`transition-colors ${omitted ? "bg-amber-50/50" : "hover:bg-[var(--bg-elev)]/40"}`}
        style={{ borderLeft: `3px solid ${omitted ? "#d97706" : tierColor}` }}
      >
          <td className="px-0.5 py-0.5 text-center align-top"><TierDot tier={tierN} /></td>

          <td className="max-w-0 overflow-hidden px-1.5 py-0.5 align-top">
            <div className="min-w-0 space-y-0.5">
              <div className="flex min-w-0 flex-wrap items-center gap-1">
                <span
                  className={`min-w-0 break-words text-xs font-semibold leading-snug text-[var(--text-primary)] [overflow-wrap:anywhere] sm:text-[13px] ${omitted ? "text-[var(--text-muted)] line-through" : ""}`}
                  title={row.description || undefined}
                >
                  {row.description || "—"}
                </span>
                {omitted && (
                  <span className="shrink-0 rounded border border-amber-600/90 bg-amber-100 px-1.5 py-0.5 text-xs font-bold uppercase text-amber-950">
                    Removed
                  </span>
                )}
              </div>
              {omitted && row.omit_reason && (
                <div className="line-clamp-2 text-[10px] leading-snug font-medium text-amber-900" title={row.omit_reason}>{row.omit_reason}</div>
              )}
              {row.calc_notes && (
                  <button
                    type="button"
                    aria-expanded={calcOpen}
                    aria-controls={calcOpen ? `mis-calc-${row.id}` : undefined}
                    aria-label={calcOpen ? "Hide calculation notes for this line" : "Show calculation notes for this line"}
                    onClick={() => setExpandedNotes((p) => {
                      const n = new Set(p);
                      const k = row.id + ":f";
                      if (n.has(k)) n.delete(k);
                      else n.add(k);
                      return n;
                    })}
                    className="mt-px flex w-full items-center justify-between gap-1.5 rounded border border-emerald-500/35 bg-emerald-50/90 px-1.5 py-1 text-left text-[10px] font-semibold text-emerald-950 transition-colors hover:bg-emerald-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-emerald-500/40 sm:text-[11px]"
                  >
                    <span className="inline-flex min-w-0 flex-1 items-center gap-0.5">
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-emerald-700" aria-hidden>
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                        <polyline points="14 2 14 8 20 8" />
                        <line x1="16" y1="13" x2="8" y2="13" />
                      </svg>
                      <span className="truncate">{calcOpen ? "Hide" : "Calculation"}</span>
                    </span>
                    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" className={`shrink-0 text-emerald-700 transition-transform ${calcOpen ? "rotate-180" : ""}`} aria-hidden>
                      <path d="M2.5 4.5L6 8l3.5-3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
              )}
            </div>
          </td>

          <td className="max-w-0 overflow-hidden px-1.5 py-0.5 align-top text-[var(--text-secondary)]">
            {row.employee_name ? (
              <span
                className="block min-w-0 break-words text-[11px] leading-tight [overflow-wrap:anywhere] line-clamp-3"
                title={row.employee_name}
              >
                {row.employee_name}
              </span>
            ) : (
              <span className="text-[var(--text-muted)]">—</span>
            )}
          </td>

          <td className="min-w-0 overflow-hidden px-1 py-0.5 text-right align-top">{renderCell(row, "contractual_rate", true)}</td>
          <td className="min-w-0 overflow-hidden px-1 py-0.5 text-right align-top">{renderMaxPostsCell(row)}</td>
          <td className="min-w-0 overflow-hidden px-1 py-0.5 text-right align-top">{renderQtyCell(row)}</td>
          <td className="min-w-0 overflow-hidden px-1 py-0.5 text-right align-top">{renderCell(row, "absent_days", false)}</td>
          <td className="min-w-0 overflow-hidden px-1 py-0.5 text-right align-top font-semibold">{renderCell(row, "final_amount", true)}</td>

          {isPending && (
            <td className="min-w-0 overflow-hidden px-0.5 py-0.5 align-top text-center">
              {omitted ? (
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={() => {
                    if (confirm("Put this line back on the bill? You can change Rate or Amount after.")) void handleRestoreRow(row.id);
                  }}
                  className="inline-flex w-full items-center justify-center rounded px-1.5 py-1 text-[9px] font-bold border border-emerald-500 text-emerald-800 bg-emerald-50 hover:bg-emerald-100 disabled:opacity-40 whitespace-nowrap"
                  title="Restore to billing (recovers amount if you had removed it; otherwise edit cells)"
                >
                  Restore
                </button>
              ) : (
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={() => {
                    const msg =
                      "Remove this row from this site's MIS? Other sites are not changed. Last person on a site-specific line turns that line off here. A shared line stays on the contract; this site will not re-bill it on Save. Staffing Max Posts decreases by 1 when people remain.";
                    if (confirm(msg)) void handleDeleteRow(row.id);
                  }}
                  className="inline-flex w-full items-center justify-center gap-0.5 px-1.5 py-1 rounded text-[9px] font-semibold border border-red-200 text-red-600 bg-red-50/80 hover:bg-red-100 disabled:opacity-40 whitespace-nowrap"
                  title="Removes this row for this site only. Shared contract lines stay active for other sites."
                >
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" className="shrink-0"><path d="M5 2V1h6v1h4v1H1V2h4zM3 5h10l-.8 9.4a1 1 0 01-1 .6H4.8a1 1 0 01-1-.6L3 5z" stroke="currentColor" strokeWidth="1.2"/></svg>
                  Remove
                </button>
              )}
            </td>
          )}
      </tr>
      {calcOpen && row.calc_notes ? (
        <tr
          className={`${omitted ? "bg-amber-50/40" : "bg-emerald-50/25"}`}
          style={{ borderLeft: `3px solid ${omitted ? "#d97706" : tierColor}` }}
        >
          <td
            colSpan={billingColSpan}
            className="border-t border-[var(--border)]/50 px-2 py-2 align-top"
          >
            <div
              id={`mis-calc-${row.id}`}
              className="rounded-md border border-emerald-200/80 bg-white px-2.5 py-2 shadow-sm"
              role="region"
              aria-label="System calculation notes"
            >
              <p className="text-[10px] font-bold uppercase tracking-wide text-emerald-900/90 sm:text-[11px]">
                System calculation
              </p>
              <div className="mt-1.5 w-full min-w-0 rounded-sm bg-emerald-50/40 px-1.5 py-1.5">
                <pre className="m-0 w-full min-w-0 max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere] font-sans text-xs leading-relaxed text-[var(--text-primary)] sm:text-[13px]">
                  {row.calc_notes}
                </pre>
              </div>
            </div>
          </td>
        </tr>
      ) : null}
      </Fragment>
    );
  }

  /* ── Render ── */

  return (
    <div className="flex min-h-0 min-w-0 max-w-full flex-1 flex-col gap-2 overflow-hidden lg:min-h-0">
      {/* Layout targets ~1280px (13″): no horizontal page scroll; line items scroll vertically only */}
      <div className="shrink-0 flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)]/90 pb-2">
        <div className="min-w-0">
          <h1 className="text-base font-bold tracking-tight text-[var(--text-primary)]">MIS review</h1>
          <p className="mt-0.5 text-xs leading-snug text-[var(--text-secondary)]">
            Draft — edit cells, Save to recalculate, then Approve.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setHowToOpen((v) => !v)}
          aria-expanded={howToOpen}
          className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-1.5 text-xs font-semibold text-[var(--accent-green)] hover:bg-[var(--bg-elev)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30"
        >
          {howToOpen ? "Hide tips" : "How to use"}
        </button>
      </div>
      {howToOpen && (
        <div className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-2 shadow-sm">
          <div className="grid min-w-0 grid-cols-1 gap-1.5 sm:grid-cols-3 sm:gap-2">
            <div className="sm:border-l sm:border-[var(--border)] sm:pl-2 first:sm:border-l-0 first:sm:pl-0">
              <p className="text-[10px] leading-snug text-[var(--text-primary)] sm:text-[11px]">
                <span className="font-semibold text-[var(--accent-green)]">1.</span>{" "}
                <span className="font-semibold">Runs</span> — Tabs filter by status; by default the list is{" "}
                <span className="font-medium">last completed billing month (IST)</span> — use{" "}
                <span className="font-medium">All periods</span> for every month.
              </p>
            </div>
            <div className="sm:border-l sm:border-[var(--border)] sm:pl-2">
              <p className="text-[10px] leading-snug text-[var(--text-primary)] sm:text-[11px]">
                <span className="font-semibold text-[var(--accent-green)]">2.</span>{" "}
                <span className="font-semibold">Edit</span> — Tap dashed cells; <kbd className="rounded border border-[var(--border)] bg-[var(--bg-elev)] px-1.5 py-0.5 text-xs font-semibold">Tab</kbd> next.
              </p>
            </div>
            <div className="sm:border-l sm:border-[var(--border)] sm:pl-2">
              <p className="text-[10px] leading-snug text-[var(--text-primary)] sm:text-[11px]">
                <span className="font-semibold text-[var(--accent-green)]">3.</span>{" "}
                <span className="font-semibold">Removed rows</span> — <strong className="text-emerald-800">Restore</strong> in the last column before duplicating a service.
              </p>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className={`shrink-0 flex items-center justify-between rounded-lg px-3 py-2 text-xs font-medium shadow-sm ${
          toast.type === "ok" ? "bg-emerald-50 text-emerald-800 border border-emerald-200" : "bg-red-50 text-red-700 border border-red-200"
        }`}>
          <span>{toast.msg}</span>
          <button type="button" onClick={() => setToast(null)} className="ml-2 opacity-60 hover:opacity-100 text-xs" aria-label="Dismiss">✕</button>
        </div>
      )}

      <div className="grid min-h-0 min-w-0 flex-1 grid-cols-1 gap-3 lg:min-h-[calc(100dvh-12rem)] lg:grid-cols-[280px_minmax(0,1fr)] lg:grid-rows-1 lg:items-stretch lg:gap-4 lg:overflow-hidden">
        {/* ── Run list ── */}
        <div className="flex min-h-0 flex-col gap-2 overflow-hidden lg:h-full">
          <div className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-1 shadow-sm">
            {/* 2×2 on narrow run column (~200px) so tab labels (e.g. All (35)) are not clipped */}
            <div className="grid grid-cols-2 gap-1 sm:grid-cols-4 sm:gap-0.5 lg:grid-cols-2 lg:gap-1">
              {STATUS_FILTERS.map((f) => (
                <button
                  key={f.label}
                  type="button"
                  title={misTabLabel(f, statusCounts)}
                  onClick={() => {
                    setStatusFilter(f.id);
                    setSelectedId("");
                    setItems([]);
                    setDetail(null);
                  }}
                  className={`min-h-[2.25rem] w-full rounded-md px-1 py-1.5 text-center text-[10px] font-semibold leading-tight transition-colors sm:text-[11px] ${
                    statusFilter === f.id
                      ? "bg-[var(--accent-green)] text-white shadow-sm"
                      : "text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                  }`}
                >
                  {misTabLabel(f, statusCounts)}
                </button>
              ))}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center justify-between gap-1.5 border-t border-[var(--border)]/60 pt-1.5">
              <span className="min-w-0 flex-1 px-0.5 text-[9px] font-medium leading-snug text-[var(--text-muted)] sm:text-[10px]">
                {showAllPeriods ? "All billing periods" : `Prior month · ${priorMonthLabel} (IST)`}
              </span>
              <button
                type="button"
                onClick={() => {
                  setShowAllPeriods((v) => !v);
                  setSelectedId("");
                  setItems([]);
                  setDetail(null);
                }}
                className="shrink-0 rounded border border-[var(--border)] bg-[var(--bg-elev)] px-1.5 py-0.5 text-[9px] font-semibold text-[var(--text-primary)] hover:bg-[var(--bg-card)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30 sm:text-[10px]"
              >
                {showAllPeriods ? "Prior month only" : "All periods"}
              </button>
            </div>
          </div>
          <div className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2 py-2">
            <label htmlFor="mis-site-search" className="sr-only">Search by site</label>
            <input
              id="mis-site-search"
              type="search"
              value={siteSearch}
              onChange={(e) => setSiteSearch(e.target.value)}
              placeholder="Search site…"
              autoComplete="off"
              disabled={listLoading || !!listError}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-elev)] px-2 py-1 text-xs text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/35 disabled:opacity-50 sm:text-sm"
            />
            {siteSearch.trim() && !listLoading && !listError && items.length > 0 && (
              <p className="mt-1.5 text-xs text-[var(--text-muted)] tabular-nums px-0.5">
                Showing {filteredItems.length} of {items.length}
              </p>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain rounded-lg border border-[var(--border)] bg-[var(--bg-card)] shadow-sm">
            {listLoading ? <div className="p-3 space-y-2">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-16" />)}</div>
              : listError ? <div className="p-6 text-center"><p className="text-sm text-red-500">{listError}</p><button type="button" onClick={() => void loadList()} className="text-xs text-[var(--accent-green)] hover:underline mt-2">Retry</button></div>
                : items.length === 0 ? (
                    <div className="p-10 text-center text-[var(--text-muted)] text-sm space-y-2">
                      <p>{showAllPeriods ? "No MIS runs found." : `No MIS runs for ${priorMonthLabel} (prior IST month).`}</p>
                      {!showAllPeriods && (
                        <button
                          type="button"
                          onClick={() => { setShowAllPeriods(true); setSelectedId(""); setItems([]); setDetail(null); }}
                          className="text-xs font-semibold text-[var(--accent-green)] hover:underline"
                        >
                          Show all periods
                        </button>
                      )}
                    </div>
                  )
                  : filteredItems.length === 0 ? (
                    <div className="p-10 text-center text-[var(--text-muted)] text-sm">
                      No runs match “{siteSearch.trim()}”. Try another search or clear the filter.
                    </div>
                  )
                  : filteredItems.map((it) => (
                    <button key={it.id} type="button" onClick={() => setSelectedId(it.id)}
                      className={`w-full border-b border-[var(--border)] px-3 py-2.5 text-left transition-colors last:border-b-0 ${
                        selectedId === it.id
                          ? "border-l-[3px] border-l-[var(--accent-green)] bg-[rgba(21,163,115,0.08)]"
                          : "border-l-[3px] border-l-transparent hover:bg-[var(--bg-elev)]"
                      }`}>
                      <div className="flex items-start justify-between gap-2">
                        <span className="min-w-0 truncate text-sm font-semibold leading-snug text-[var(--text-primary)]">{formatMisSiteHeadline(it)}</span>
                        <span className="flex shrink-0 items-center gap-1">
                          {isMisAutoApproved(it.approved_by) && <AutoApprovedBadge />}
                          <StatusBadge status={it.status} />
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] leading-snug text-[var(--text-muted)]">{it.client_name}</div>
                      <div className="mt-0.5 text-[11px] tabular-nums text-[var(--text-muted)]">{fmtPeriod(it.billing_period_start, it.billing_period_end)}</div>
                      {it.summary_line_count != null && (
                        <div className="mt-1.5 text-xs font-medium tabular-nums text-[var(--text-secondary)]">
                          {it.summary_line_count} line{it.summary_line_count !== 1 ? "s" : ""} in draft
                          {(it.omitted_line_count ?? 0) > 0 && (
                            <span className="text-amber-700 font-medium"> · {it.omitted_line_count} removed</span>
                          )}
                        </div>
                      )}
                    </button>
                  ))}
          </div>
        </div>

        {/* ── Right: detail (line items fill remaining height) ── */}
        <div className="flex min-h-0 min-w-0 flex-col overflow-hidden lg:h-full">
          {!selectedId ? (
            <div className="rounded-lg border border-dashed border-[var(--border)] bg-[var(--bg-card)] p-12 text-center">
              <p className="text-sm text-[var(--text-muted)]">Select a site from the left to review billing.</p>
            </div>
          ) : detailLoading ? (
            <div className="space-y-4 rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-sm"><Skeleton className="h-9 w-2/3" /><Skeleton className="h-72" /></div>
          ) : detailError ? (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-10 text-center shadow-sm">
              <p className="text-sm text-red-600">{detailError}</p>
              <button
                type="button"
                onClick={() => void loadDetail(selectedId)}
                className="mt-3 text-xs font-semibold text-[var(--accent-green)] hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-green)]/30 rounded"
              >
                Retry
              </button>
            </div>
          ) : detail ? (
            <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-hidden lg:min-h-0">

              {/* Site + review actions — one panel, less vertical stack (13″ laptops) */}
              <div className="shrink-0 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)] shadow-sm">
                <div className="flex flex-wrap items-start justify-between gap-2 border-b border-[var(--border)]/80 px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate text-[15px] font-bold leading-tight text-[var(--text-primary)]">{formatMisSiteHeadline(detail)}</h2>
                      {isMisAutoApproved(detail.approved_by) && <AutoApprovedBadge />}
                      <StatusBadge status={detail.status} />
                    </div>
                    <p className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                      <span className="truncate">{detail.client_name}</span>
                      <span className="mx-1.5 text-[var(--border)]" aria-hidden>|</span>
                      <span className="tabular-nums">{fmtPeriod(detail.billing_period_start, detail.billing_period_end)}</span>
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {detail.xlsx_path && (
                      <a
                        href={detail.xlsx_path}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded-lg border border-[var(--border)] px-2 py-1 text-xs font-semibold text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                      >
                        Excel
                      </a>
                    )}
                    {isPending && (
                      <button
                        type="button"
                        disabled={actionBusy}
                        onClick={async () => {
                          const rerunMsg = isDraftDirty
                            ? "You have unsaved grid edits. Re-run will discard them and recalculate from attendance. Use Save first to apply your edits, then recalculate. Continue with Re-run?"
                            : "Re-run MIS generation for this site?";
                          if (!confirm(rerunMsg)) return;
                          setActionBusy(true);
                          try {
                            await rerunMisRun({ mis_run_id: detail.id });
                            setToast({ msg: "MIS re-run triggered", type: "ok" });
                            await loadList();
                            await loadDetail(detail.id);
                          } catch (e) {
                            setToast({ msg: e instanceof Error ? e.message : "Failed", type: "err" });
                          } finally {
                            setActionBusy(false);
                          }
                        }}
                        className="rounded-lg border border-[var(--border)] px-2 py-1 text-xs font-semibold text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] disabled:opacity-50"
                      >
                        Re-run
                      </button>
                    )}
                    {!isPending && detail.status === "approved" && (
                      <>
                        <button
                          type="button"
                          disabled={actionBusy}
                          title="Move back to Pending so lines can be edited"
                          onClick={() => void handleReopen()}
                          className="rounded-lg border border-amber-400 bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-950 hover:bg-amber-100 disabled:opacity-50"
                        >
                          Reopen
                        </button>
                        <button
                          type="button"
                          disabled={actionBusy}
                          title="Rebuild Excel and upload to Google Drive (3 attempts)"
                          onClick={() => void handleApprove()}
                          className="rounded-lg border border-orange-400 bg-orange-50 px-2 py-1 text-xs font-semibold text-orange-900 hover:bg-orange-100 disabled:opacity-50"
                        >
                          Retry upload
                        </button>
                      </>
                    )}
                  </div>
                </div>

                <MisAutoApproveBanner
                  status={detail.status}
                  approvedBy={detail.approved_by}
                  audit={detail.auto_approve}
                />

                {(() => {
                  const ho = detail.summary_json?.handover;
                  if (!ho || typeof ho !== "object" || !(ho as { applied?: boolean }).applied) return null;
                  const pairs = Array.isArray((ho as { pairs?: unknown }).pairs)
                    ? (ho as { pairs: { joiner_display_name?: string; leaver_id?: string; joiner_id?: string }[] }).pairs
                    : [];
                  return (
                    <div className="border-b border-sky-200/80 bg-sky-50/90 px-3 py-2 text-xs text-sky-950">
                      <span className="font-semibold">LWD/DOJ handover applied.</span>{" "}
                      Leaver attendance was merged onto the joiner row for this month
                      {pairs.length > 0 ? (
                        <>
                          {" "}
                          ({pairs.map((p) => p.joiner_display_name || p.joiner_id || "joiner").join("; ")})
                        </>
                      ) : null}
                      . Leaver summary lines are marked not billed.
                    </div>
                  );
                })()}

                {isPending && (
                  <div
                    className={`flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center sm:justify-between ${
                      needsAttention === 0 ? "bg-emerald-50/60" : "bg-amber-50/70"
                    }`}
                  >
                    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                      <span className={`font-semibold ${needsAttention === 0 ? "text-emerald-900" : "text-amber-950"}`}>
                        {needsAttention === 0 ? (
                          <>
                            All <span className="tabular-nums">{inBillingCount}</span> lines look good
                          </>
                        ) : (
                          <>
                            <span className="tabular-nums">{needsAttention}</span> of{" "}
                            <span className="tabular-nums">{inBillingCount}</span> need review
                          </>
                        )}
                      </span>
                      {lineStats.omitted > 0 ? (
                        <span className="inline-flex items-center gap-1 rounded-md border border-amber-200/80 bg-white/90 px-1.5 py-0.5 text-[10px] font-medium tabular-nums text-amber-950">
                          <span className="tabular-nums">{lineStats.omitted}</span> not billed
                        </span>
                      ) : (
                        <span className="text-[10px] font-medium tabular-nums text-[var(--text-secondary)]">
                          <span className="tabular-nums">{lineStats.active}</span> in billing
                        </span>
                      )}
                      <span className="hidden h-3 w-px bg-[var(--border)] sm:inline" aria-hidden />
                      <span className="text-[10px] font-medium tabular-nums text-[var(--text-secondary)]">
                        <span className="text-emerald-700">{tierCounts.t1} OK</span>
                        <span className="mx-1 text-[var(--text-muted)]">·</span>
                        <span className="text-amber-800">{tierCounts.t2} review</span>
                        <span className="mx-1 text-[var(--text-muted)]">·</span>
                        <span className="text-red-700">{tierCounts.t3} check</span>
                      </span>
                      {tierSum !== inBillingCount && (
                        <span className="text-[10px] font-semibold text-red-700">· Tier mismatch</span>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center justify-end gap-3">
                      <div className="text-right">
                        <div
                          className={`text-[10px] font-semibold uppercase tracking-wide ${isDraftDirty ? "text-orange-700" : "text-[var(--text-muted)]"}`}
                        >
                          Bill total{isDraftDirty ? " (unsaved)" : ""}
                        </div>
                        <div
                          className={`text-lg font-bold tabular-nums ${isDraftDirty ? "text-orange-600" : "text-[var(--text-primary)]"}`}
                          title={
                            isDraftDirty
                              ? "Draft has unsaved edits — Save recalculates all lines and this total from attendance"
                              : undefined
                          }
                        >
                          {isDraftDirty ? `~${fmtAmt(displayBillTotal)}` : fmtAmt(displayBillTotal)}
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          disabled={actionBusy}
                          onClick={() => setShowReject((v) => !v)}
                          className="rounded-lg border border-red-300 px-3 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-50 disabled:opacity-50"
                        >
                          Reject
                        </button>
                        <button
                          type="button"
                          disabled={actionBusy || !isDraftDirty}
                          title={
                            !isDraftDirty
                              ? "Edit cells or add/remove lines to enable Save"
                              : undefined
                          }
                          onClick={() => void handleSave()}
                          className="rounded-lg border border-[var(--accent-green)] bg-white px-3 py-1.5 text-xs font-bold text-[var(--accent-green)] shadow-sm hover:bg-emerald-50/80 disabled:opacity-50"
                        >
                          {actionBusy ? "…" : "Save"}
                        </button>
                        <button
                          type="button"
                          disabled={actionBusy || isDraftDirty}
                          title={isDraftDirty ? "Save your edits first" : undefined}
                          onClick={() => void handleApprove()}
                          className="rounded-lg bg-[var(--accent-green)] px-4 py-1.5 text-xs font-bold text-white shadow-sm hover:brightness-110 disabled:opacity-50"
                        >
                          {actionBusy ? "…" : needsAttention === 0 ? "Approve" : "Approve"}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {showReject && (
                <div className="shrink-0 border border-red-200 rounded-lg bg-red-50/50 p-3 space-y-2">
                  <label className="text-xs font-semibold text-red-700">Why is this being rejected?</label>
                  <textarea value={rejectionNotes} onChange={(e) => setRejectionNotes(e.target.value)} placeholder="Explain…" rows={2}
                    className="w-full px-2 py-1.5 rounded-md border border-red-200 bg-white text-sm focus:outline-none focus:border-red-400" />
                  <div className="flex gap-2 justify-end">
                    <button type="button" onClick={() => { setShowReject(false); setRejectionNotes(""); }} className="px-3 py-1.5 rounded-md border border-[var(--border)] text-xs">Cancel</button>
                    <button type="button" disabled={!rejectionNotes.trim() || actionBusy} onClick={() => void handleRejectSubmit()}
                      className="px-4 py-1.5 rounded-md bg-red-500 text-white text-xs font-semibold disabled:opacity-50">{actionBusy ? "…" : "Confirm"}</button>
                  </div>
                </div>
              )}

              {isDraftDirty && isPending && (
                <div className="shrink-0 rounded-lg border border-orange-400 bg-orange-50 px-3 py-2 text-sm font-semibold leading-snug text-orange-900">
                  Draft — line and bill totals update when you <strong>Save</strong> (recalculates from attendance).
                </div>
              )}

              {/* ── Billing table (fills remaining height on large screens) ── */}
              <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)] shadow-sm">
                <div className="flex shrink-0 flex-wrap items-center justify-between gap-1.5 border-b border-[var(--border)] bg-[var(--bg-elev)]/50 px-2 py-1">
                  <h3 className="text-[9px] font-bold uppercase tracking-wide text-[var(--text-primary)] sm:text-[10px]">
                    {isPending ? "Line items" : "Billing summary"}
                  </h3>
                  {isPending ? (
                    <span className="text-[9px] font-medium text-emerald-800" title="Tap a dashed cell to edit">
                      Dashed = edit
                    </span>
                  ) : (
                    <div
                      className={`text-xs font-bold tabular-nums sm:text-sm ${isDraftDirty ? "text-orange-600" : "text-[var(--text-primary)]"}`}
                      title={isDraftDirty ? "Unsaved draft — Save to refresh total" : undefined}
                    >
                      {isDraftDirty ? `~${fmtAmt(displayBillTotal)}` : fmtAmt(displayBillTotal)}
                    </div>
                  )}
                </div>

                <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain">
                  {/* Table fits the pane width (13″); no min-width — wrapping + overflow-hidden on cells prevent overlap */}
                  <table className="w-full min-w-0 max-w-full table-fixed text-[11px] leading-snug">
                    <colgroup>
                      <col className="w-8" />
                      {isPending ? (
                        <>
                          <col className="min-w-0 w-[32%]" />
                          <col className="min-w-0 w-[22%]" />
                          <col className="w-[8%]" />
                          <col className="w-[7%]" />
                          <col className="w-[7%]" />
                          <col className="w-[6%]" />
                          <col className="w-[10%]" />
                          <col className="min-w-0 w-[12%]" />
                        </>
                      ) : (
                        <>
                          <col className="min-w-0 w-[37%]" />
                          <col className="min-w-0 w-[26%]" />
                          <col className="w-[9%]" />
                          <col className="w-[8%]" />
                          <col className="w-[8%]" />
                          <col className="w-[7%]" />
                          <col className="w-[11%]" />
                        </>
                      )}
                    </colgroup>
                    <thead className="sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--bg-elev)]/95 shadow-[0_1px_0_0_var(--border)]">
                      <tr>
                        <th className="px-0.5 py-1" title="Line status" />
                        <th className="min-w-0 overflow-hidden px-1 py-1 text-left text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]">
                          Service
                        </th>
                        <th className="min-w-0 overflow-hidden px-1 py-1 text-left text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]">
                          Employee
                        </th>
                        <th
                          className="min-w-0 overflow-hidden px-1 py-1 text-right text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]"
                          title={isPending ? "Click dashed cell to edit" : undefined}
                        >
                          Rate ₹
                        </th>
                        <th
                          className="min-w-0 overflow-hidden px-1 py-1 text-right text-[8px] font-semibold normal-case leading-tight text-[var(--text-muted)] sm:text-[9px]"
                          title="Staffing headcount cap (MO slots). Editable; syncs all rows on same contract line. Or use Remove/Restore/Add, then Save."
                        >
                          Max Posts
                        </th>
                        <th
                          className="min-w-0 overflow-hidden px-1 py-1 text-right text-[8px] font-semibold normal-case leading-tight text-[var(--text-muted)] sm:text-[9px]"
                          title="Fixed-line units or visits/mo. Read-only — change on contract line or Add line, then Save."
                        >
                          Qty
                        </th>
                        <th
                          className="min-w-0 overflow-hidden px-1 py-1 text-right text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]"
                          title={isPending ? "Absent — click dashed cell to edit" : "Absent"}
                        >
                          Abs
                        </th>
                        <th
                          className="min-w-0 overflow-hidden px-1 py-1 text-right text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]"
                          title={isPending ? "Click dashed cell to edit" : undefined}
                        >
                          Amt ₹
                        </th>
                        {isPending && (
                          <th
                            className="min-w-0 overflow-hidden px-0.5 py-1 text-center text-[8px] font-bold uppercase tracking-wide text-[var(--text-muted)] sm:text-[9px]"
                            title="Actions"
                          >
                            Act
                          </th>
                        )}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border)]/35">
                      {detail.summary_rows.length === 0 ? (
                        <tr><td colSpan={billingColSpan} className="px-5 py-10 text-center text-[var(--text-muted)] text-sm">No billing items</td></tr>
                      ) : (
                        <>
                          {billingTableSections.inBilling.map((row) => renderSummaryRow(row))}
                          {billingTableSections.removed.length > 0 && (
                            <tr className="bg-amber-50/90 border-y border-amber-200">
                              <td colSpan={billingColSpan} className="px-3 py-1">
                                <span className="inline-flex items-center gap-2 text-[11px] font-semibold text-amber-950">
                                  <span className="h-px w-4 shrink-0 bg-amber-400" aria-hidden />
                                  Removed rows ({billingTableSections.removed.length})
                                </span>
                              </td>
                            </tr>
                          )}
                          {billingTableSections.removed.map((row) => renderSummaryRow(row))}
                        </>
                      )}
                      {detail.summary_rows.length > 0 && (
                        <tr className="border-t-2 border-[var(--border)] bg-[var(--bg-elev)]/70" style={{ borderLeft: "3px solid transparent" }}>
                          <td className="px-0.5 py-1" /><td className="px-2 py-1 text-[11px] font-bold sm:text-[12px]" colSpan={6}>Total</td>
                          <td
                            className={`px-1.5 py-1 text-right text-[11px] font-bold tabular-nums sm:text-[12px] ${isDraftDirty ? "text-orange-600" : ""}`}
                          >
                            {isDraftDirty ? `~${fmtAmt(displayBillTotal)}` : fmtAmt(displayBillTotal)}
                          </td>
                          {isPending && <td />}
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {/* ── Add line item ── */}
                {isPending && (
                  <div className="shrink-0 border-t border-[var(--border)]">
                    {!addMode ? (
                      <div className="flex">
                        <button type="button" onClick={() => void handleOpenPickLine()}
                          className="flex flex-1 items-center justify-center gap-1.5 border-r border-[var(--border)] px-3 py-2 text-xs font-semibold text-[var(--accent-green)] transition-colors hover:bg-emerald-50/50 sm:text-[13px]">
                          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg>
                          Add from contract
                        </button>
                        <button type="button" onClick={() => setAddMode("create")}
                          className="flex flex-1 items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold text-blue-600 transition-colors hover:bg-blue-50/50 sm:text-[13px]">
                          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg>
                          Create new billing line
                        </button>
                      </div>
                    ) : addMode === "pick" ? (
                      <div className="bg-emerald-50/30">
                        <div className="px-5 py-3 flex items-center justify-between border-b border-emerald-100">
                          <span className="text-xs font-bold text-emerald-800">Choose from existing contract lines</span>
                          <button type="button" onClick={() => setAddMode(null)} className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]">✕</button>
                        </div>
                        {addLineLoading ? <div className="p-4"><Skeleton className="h-16" /></div>
                          : <>
                          {alternateContracts.length > 0 && (
                            <div className="px-5 py-3 border-b border-emerald-100 bg-white/60">
                              <div className="text-[11px] font-semibold text-emerald-900 mb-2">Import from another pending contract</div>
                              <div className="space-y-2 max-h-32 overflow-auto">
                                {alternateContracts.map((c) => {
                                  const canImport = (c.importable_line_count ?? 0) > 0;
                                  return (
                                  <div key={c.contract_terms_version_id} className="flex items-center justify-between gap-2 text-xs">
                                    <span className="truncate">
                                      {(c.title || c.contract_terms_version_id.slice(0, 8))} · {c.line_count} lines
                                      {!canImport && (
                                        <span className="text-[var(--text-muted)]"> (already on this contract)</span>
                                      )}
                                    </span>
                                    {canImport ? (
                                      <button
                                        type="button"
                                        disabled={actionBusy}
                                        onClick={() => void handleImportContractLines(c.contract_terms_version_id)}
                                        className="shrink-0 rounded px-2 py-1 font-semibold text-emerald-800 hover:bg-emerald-100"
                                      >
                                        Import{c.importable_line_count < c.line_count
                                          ? ` (${c.importable_line_count})`
                                          : ""}
                                      </button>
                                    ) : (
                                      <span
                                        className="shrink-0 max-w-[9rem] text-right text-[10px] font-medium text-[var(--text-muted)]"
                                        title="All active lines from this contract already exist on the MIS contract"
                                      >
                                        Nothing new to import
                                      </span>
                                    )}
                                  </div>
                                  );
                                })}
                              </div>
                            </div>
                          )}
                          {availableLines.length === 0 && alternateContracts.length === 0 ? (
                            <div className="px-5 py-4 text-xs text-[var(--text-muted)]">No contract lines found.</div>
                          ) : (
                            <div className="divide-y divide-emerald-100 max-h-64 overflow-auto">
                              {availableLines.map((rl) => (
                                <button key={rl.id} type="button" disabled={actionBusy || rl.already_in_summary} onClick={() => void handlePickLine(rl.id)}
                                  className={`w-full text-left px-5 py-3 flex items-center justify-between gap-4 text-xs transition-colors ${rl.already_in_summary ? "opacity-40 cursor-not-allowed" : "hover:bg-emerald-100/50"}`}>
                                  <div className="min-w-0 flex-1">
                                    <span className="font-semibold text-sm">{rl.description || rl.role_code}</span>
                                    {rl.role_code && <span className="ml-2 text-xs text-[var(--text-muted)] font-mono">{rl.role_code}</span>}
                                  </div>
                                  <div className="flex items-center gap-4 shrink-0 text-[var(--text-secondary)]">
                                    <span className="font-medium">{rl.rate_amount != null ? fmtAmt(rl.rate_amount) : "—"}</span>
                                    <span className="shrink-0 tabular-nums text-xs text-[var(--text-secondary)]" title="Contracted quantity">
                                      Contracted: {rl.contracted_quantity ?? "—"}
                                    </span>
                                    {!rl.is_active && !rl.already_in_summary && (
                                      <span className="text-sky-700 font-semibold">
                                        {rl.overrides_contract_rate_line_id &&
                                        rl.contracted_quantity != null &&
                                        Number(rl.contracted_quantity) === 0
                                          ? "Removed from this site"
                                          : "Imported"}
                                      </span>
                                    )}
                                    {rl.already_in_summary && <span className="text-amber-600 font-semibold">Already billed</span>}
                                  </div>
                                </button>
                              ))}
                            </div>
                          )}
                          </>}
                      </div>
                    ) : (
                      /* Create new line form */
                      <div className="bg-blue-50/30">
                        <div className="px-5 py-3 flex items-center justify-between border-b border-blue-100">
                          <span className="text-xs font-bold text-blue-800">Create a new billing line (will also add to contract)</span>
                          <button type="button" onClick={() => setAddMode(null)} className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]">✕</button>
                        </div>
                        <div className="px-5 py-4 space-y-3">
                          <div>
                            <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Line type</label>
                            <div className="flex flex-wrap gap-3 text-sm">
                              <label className="inline-flex items-center gap-2 cursor-pointer">
                                <input type="radio" name="newLineKind" checked={newLine.line_kind === "standard"} onChange={() => setNewLine((p) => ({ ...p, line_kind: "standard" }))} />
                                Standard rate line
                              </label>
                              <label className="inline-flex items-center gap-2 cursor-pointer" title="Percentage of sum of rate_attendance line totals for this MIS (invoice-level admin)">
                                <input type="radio" name="newLineKind" checked={newLine.line_kind === "invoice_admin"} onChange={() => setNewLine((p) => ({ ...p, line_kind: "invoice_admin" }))} />
                                Invoice administration (% of staffing)
                              </label>
                            </div>
                          </div>
                          <div className="grid grid-cols-2 gap-3">
                            <div>
                              <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Service Name *</label>
                              <input type="text" value={newLine.description} onChange={(e) => setNewLine((p) => ({ ...p, description: e.target.value }))}
                                placeholder={newLine.line_kind === "invoice_admin" ? "e.g. Administration (% of invoice)" : "e.g. Nurse (GNM)"} className="w-full px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400" />
                            </div>
                            <div>
                              <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Role Code</label>
                              <input type="text" value={newLine.line_kind === "invoice_admin" ? "OHC_ADMIN_INVOICE_PCT" : newLine.role_code} onChange={(e) => setNewLine((p) => ({ ...p, role_code: e.target.value }))}
                                placeholder="e.g. NURSE_GNM" disabled={newLine.line_kind === "invoice_admin"}
                                className="w-full px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400 disabled:bg-[var(--bg-muted)] disabled:text-[var(--text-muted)]" />
                            </div>
                          </div>
                          {newLine.line_kind === "invoice_admin" ? (
                            <div className="max-w-md space-y-3">
                              <div>
                                <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Admin % *</label>
                                <input type="text" inputMode="decimal" value={newLine.invoice_admin_pct} onChange={(e) => setNewLine((p) => ({ ...p, invoice_admin_pct: e.target.value }))}
                                  placeholder="10" className="w-full max-w-xs px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400" />
                              </div>
                              <div>
                                <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Apply % to</label>
                                <select
                                  value={newLine.invoice_admin_base}
                                  onChange={(e) =>
                                    setNewLine((p) => ({
                                      ...p,
                                      invoice_admin_base: e.target.value as "staffing_only" | "staffing_plus_fixed_monthly",
                                    }))
                                  }
                                  className="w-full max-w-md px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400"
                                >
                                  <option value="staffing_only">Staffing only (rate_attendance)</option>
                                  <option value="staffing_plus_fixed_monthly">Staffing + other fixed monthly (excl. admin line)</option>
                                </select>
                                <p className="text-xs text-[var(--text-muted)] mt-1">
                                  Default matches generic MIS Phase B. Use the second option when admin % must include ambulance / driver / other <code className="text-xs">fixed_monthly</code> lines on the same run.
                                </p>
                              </div>
                            </div>
                          ) : (
                          <>
                          <div className={`grid gap-3 ${newLine.billing_model === "per_visit" ? "grid-cols-2" : "grid-cols-3"}`}>
                            <div>
                              <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Rate (₹) *</label>
                              <input type="text" inputMode="decimal" value={newLine.rate_amount} onChange={(e) => setNewLine((p) => ({ ...p, rate_amount: e.target.value }))}
                                placeholder="27000" className="w-full px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400" />
                            </div>
                            <div>
                              <label className="text-sm font-semibold text-[var(--text-secondary)] mb-1 block">Billing Model</label>
                              <select value={newLine.billing_model} onChange={(e) => setNewLine((p) => ({ ...p, billing_model: e.target.value }))}
                                className="w-full px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400">
                                <option value="fixed_monthly">Fixed Monthly</option>
                                <option value="rate_attendance">Rate × Attendance</option>
                                <option value="as_per_actuals">As Per Actuals</option>
                                <option value="per_visit">Per Visit</option>
                                <option value="per_head">Per Head</option>
                              </select>
                            </div>
                            {newLine.billing_model !== "per_visit" ? (
                            <div>
                                  <label
                                    className="text-xs font-semibold text-[var(--text-secondary)] mb-1 block"
                                    title={
                                      newLine.billing_model === "per_head"
                                        ? "Headcount; amount = rate × headcount for per head."
                                        : "Headcount or units on the contract"
                                    }
                                  >
                                    {newLine.billing_model === "per_head" ? "Headcount *" : "Contracted qty"}
                                  </label>
                                  <input
                                    type="text"
                                    inputMode="decimal"
                                    value={newLine.contracted_quantity}
                                    onChange={(e) => setNewLine((p) => ({ ...p, contracted_quantity: e.target.value }))}
                                    placeholder="1"
                                    className="w-full px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400"
                                  />
                            </div>
                            ) : null}
                          </div>
                          {newLine.billing_model === "per_visit" ? (
                            <div className="space-y-2">
                              <div className="flex gap-4 text-xs">
                                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                                  <input
                                    type="radio"
                                    name="visit_cadence"
                                    checked={newLine.visit_cadence === "week"}
                                    onChange={() =>
                                      setNewLine((p) => ({
                                        ...p,
                                        visit_cadence: "week",
                                        visit_per_month: "",
                                      }))
                                    }
                                  />
                                  Visits per week
                                </label>
                                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                                  <input
                                    type="radio"
                                    name="visit_cadence"
                                    checked={newLine.visit_cadence === "month"}
                                    onChange={() =>
                                      setNewLine((p) => ({
                                        ...p,
                                        visit_cadence: "month",
                                        visits_per_week: "",
                                      }))
                                    }
                                  />
                                  Visits per month
                                </label>
                              </div>
                              {newLine.visit_cadence === "week" ? (
                                <div>
                                  <label className="text-xs font-semibold text-[var(--text-secondary)] mb-1 block">
                                    Visits per week *
                                  </label>
                                  <input
                                    type="text"
                                    inputMode="decimal"
                                    value={newLine.visits_per_week}
                                    onChange={(e) => setNewLine((p) => ({ ...p, visits_per_week: e.target.value }))}
                                    placeholder="e.g. 2"
                                    className="w-full max-w-xs px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400"
                                  />
                                  {detail ? (
                                    <p className="mt-1 text-[11px] leading-snug text-[var(--text-muted)]">
                                      {visitWeekCreateHint(
                                        detail.billing_period_start,
                                        detail.billing_period_end,
                                        newLine.visits_per_week,
                                      )}
                                    </p>
                                  ) : null}
                                </div>
                              ) : (
                                <div>
                                  <label className="text-xs font-semibold text-[var(--text-secondary)] mb-1 block">
                                    Visits per month *
                                  </label>
                                  <input
                                    type="text"
                                    inputMode="decimal"
                                    value={newLine.visit_per_month}
                                    onChange={(e) => setNewLine((p) => ({ ...p, visit_per_month: e.target.value }))}
                                    placeholder="e.g. 8"
                                    className="w-full max-w-xs px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-white focus:outline-none focus:border-blue-400"
                                  />
                                  <p className="mt-1 text-[11px] leading-snug text-[var(--text-muted)]">
                                    Same visit count every month (no 4-vs-5 week change).
                                  </p>
                                </div>
                              )}
                            </div>
                          ) : null}
                          </>
                          )}
                          <div className="flex gap-2 justify-end pt-1">
                            <button type="button" onClick={() => setAddMode(null)} className="px-3 py-1.5 rounded-lg border border-[var(--border)] text-xs">Cancel</button>
                            <button type="button" disabled={actionBusy || !newLine.description.trim() || (newLine.line_kind === "standard" && !newLine.rate_amount.trim())} onClick={() => void handleCreateNewLine()}
                              className="px-4 py-1.5 rounded-lg bg-blue-600 text-white text-xs font-semibold disabled:opacity-50 hover:bg-blue-700">
                              {actionBusy ? "Creating…" : "Create & Add"}
                            </button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
