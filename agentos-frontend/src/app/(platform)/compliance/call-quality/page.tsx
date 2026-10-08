"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ComplianceCallQualityCharts } from "@/components/compliance/ComplianceCallQualityCharts";
import { ComplianceRunDetailModal } from "@/components/compliance/ComplianceRunDetailModal";
import {
  type ComplianceDoctorOption,
  type ComplianceRunRow,
  exportComplianceRunsCsv,
  getComplianceDoctors,
  getComplianceRun,
  getComplianceRunsGradeDistribution,
  getComplianceRunsSummary,
  getComplianceRunsTimeseries,
  listComplianceRuns,
} from "@/lib/api";
import { formatIst, istDayEndUtc, istDayStartUtc, istLastNDays, istToday } from "@/lib/istDate";

type SortKey = "created_at" | "doctor_slug" | "status" | "composite_pct" | "grade" | "filename";

const PAGE_SIZES = [25, 50, 100] as const;

/** Letter grades from the GLP-1 rubric grading bands (includes the D+ band). */
const GRADE_OPTIONS = ["A", "B", "C", "D", "D+", "F"] as const;

const STATUS_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "", label: "All statuses" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
];

const SHEET_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "", label: "All" },
  { value: "yes", label: "Yes — appended" },
  { value: "no", label: "No — not appended" },
];

/**
 * Preset score brackets. Bounds are half-open ([min, max)) server-side so a run
 * sitting exactly on a boundary lands in one bracket only; 75-100 is closed.
 */
const SCORE_BRACKETS: ReadonlyArray<{
  value: string;
  label: string;
  min: number | null;
  max: number | null;
}> = [
  { value: "", label: "Any score", min: null, max: null },
  { value: "0-25", label: "0–25%", min: 0, max: 25 },
  { value: "25-50", label: "25–50%", min: 25, max: 50 },
  { value: "50-75", label: "50–75%", min: 50, max: 75 },
  { value: "75-100", label: "75–100%", min: 75, max: 100 },
];


export default function ComplianceCallQualityPage() {
  const [summary30, setSummary30] = useState<Awaited<ReturnType<typeof getComplianceRunsSummary>> | null>(null);
  const [runs, setRuns] = useState<ComplianceRunRow[]>([]);
  const [total, setTotal] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof getComplianceRun>> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [searchApplied, setSearchApplied] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [gradeFilter, setGradeFilter] = useState<string[]>([]);
  const [doctorInput, setDoctorInput] = useState("");
  const [doctorApplied, setDoctorApplied] = useState("");
  const [doctorOptions, setDoctorOptions] = useState<ComplianceDoctorOption[]>([]);
  const [scoreBracket, setScoreBracket] = useState("");
  const [sheetFilter, setSheetFilter] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportErr, setExportErr] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(25);
  const [timeseries, setTimeseries] = useState<Awaited<ReturnType<typeof getComplianceRunsTimeseries>> | null>(
    null
  );
  const [grades, setGrades] = useState<Awaited<ReturnType<typeof getComplianceRunsGradeDistribution>> | null>(null);
  const [chartsLoading, setChartsLoading] = useState(true);
  const [chartsErr, setChartsErr] = useState<string | null>(null);
  const [chartDays, setChartDays] = useState(30);
  const [granularity, setGranularity] = useState<"day" | "week">("day");

  useEffect(() => {
    const t = window.setTimeout(() => setSearchApplied(searchInput.trim()), 400);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  useEffect(() => {
    const t = window.setTimeout(() => setDoctorApplied(doctorInput.trim()), 400);
    return () => window.clearTimeout(t);
  }, [doctorInput]);

  // Dates are picked as IST calendar days; convert to the matching UTC instants.
  const sinceIso = useMemo(() => (dateFrom ? istDayStartUtc(dateFrom) : null), [dateFrom]);
  const untilIso = useMemo(() => (dateTo ? istDayEndUtc(dateTo) : null), [dateTo]);

  const bracket = useMemo(
    () => SCORE_BRACKETS.find((b) => b.value === scoreBracket) ?? SCORE_BRACKETS[0],
    [scoreBracket]
  );
  const sheetAppended = useMemo(
    () => (sheetFilter === "yes" ? true : sheetFilter === "no" ? false : null),
    [sheetFilter]
  );

  const gradeSig = useMemo(() => [...gradeFilter].sort().join(","), [gradeFilter]);

  /** Every server-side filter, so a change resets pagination back to page 1. */
  const filterSig = useMemo(
    () =>
      [
        searchApplied,
        dateFrom,
        dateTo,
        statusFilter,
        gradeSig,
        doctorApplied,
        scoreBracket,
        sheetFilter,
        sortKey,
        sortDir,
        pageSize,
      ].join("\u{1f}"),
    [
      searchApplied,
      dateFrom,
      dateTo,
      statusFilter,
      gradeSig,
      doctorApplied,
      scoreBracket,
      sheetFilter,
      sortKey,
      sortDir,
      pageSize,
    ]
  );

  /** Filter set shared by the table query and the CSV export. */
  const activeFilters = useMemo(
    () => ({
      since: sinceIso,
      until: untilIso,
      search: searchApplied || null,
      status: statusFilter || null,
      grades: gradeFilter.length ? gradeFilter : null,
      doctor: doctorApplied || null,
      score_min: bracket.min,
      score_max: bracket.max,
      sheet_appended: sheetAppended,
      sort_key: sortKey,
      sort_dir: sortDir,
    }),
    [
      sinceIso,
      untilIso,
      searchApplied,
      statusFilter,
      gradeFilter,
      doctorApplied,
      bracket,
      sheetAppended,
      sortKey,
      sortDir,
    ]
  );

  const filtersActive =
    Boolean(searchApplied) ||
    Boolean(dateFrom) ||
    Boolean(dateTo) ||
    Boolean(statusFilter) ||
    gradeFilter.length > 0 ||
    Boolean(doctorApplied) ||
    Boolean(scoreBracket) ||
    Boolean(sheetFilter);

  const toggleGrade = (g: string) =>
    setGradeFilter((prev) => (prev.includes(g) ? prev.filter((x) => x !== g) : [...prev, g]));

  const clearFilters = () => {
    setSearchInput("");
    setSearchApplied("");
    setDateFrom("");
    setDateTo("");
    setStatusFilter("");
    setGradeFilter([]);
    setDoctorInput("");
    setDoctorApplied("");
    setScoreBracket("");
    setSheetFilter("");
  };

  const applyDatePreset = (days: number) => {
    const { from, to } = istLastNDays(days);
    setDateFrom(from);
    setDateTo(to);
  };

  const handleExport = async () => {
    setExporting(true);
    setExportErr(null);
    try {
      const { blob, filename } = await exportComplianceRunsCsv(activeFilters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setExportErr(e instanceof Error ? e.message : "Export failed.");
    } finally {
      setExporting(false);
    }
  };

  const lastFilterSig = useRef<string | null>(null);
  const lastLoadKey = useRef<string>("");

  const loadSummaries = useCallback(async () => {
    const s30 = await getComplianceRunsSummary(30);
    setSummary30(s30);
  }, []);

  const loadCharts = useCallback(async () => {
    setChartsLoading(true);
    setChartsErr(null);
    try {
      const [ts, gd] = await Promise.all([
        getComplianceRunsTimeseries(chartDays, granularity),
        getComplianceRunsGradeDistribution(chartDays),
      ]);
      setTimeseries(ts);
      setGrades(gd);
    } catch (e) {
      setTimeseries(null);
      setGrades(null);
      setChartsErr(e instanceof Error ? e.message : "Failed to load charts.");
    } finally {
      setChartsLoading(false);
    }
  }, [chartDays, granularity]);

  useEffect(() => {
    const prev = lastFilterSig.current;
    const filterChanged = prev !== null && prev !== filterSig;
    lastFilterSig.current = filterSig;

    const offset = filterChanged ? 0 : page * pageSize;
    if (filterChanged && page !== 0) {
      setPage(0);
    }

    const loadKey = `${filterSig}\u{1f}${offset}`;
    if (lastLoadKey.current === loadKey) {
      return;
    }

    let cancelled = false;
    setLoading(true);
    setErr(null);

    void (async () => {
      try {
        const res = await listComplianceRuns({
          limit: pageSize,
          offset,
          ...activeFilters,
        });
        if (!cancelled) {
          lastLoadKey.current = loadKey;
          setRuns(res.runs);
          setTotal(res.total);
        }
      } catch (e) {
        if (!cancelled) {
          setErr(e instanceof Error ? e.message : "Failed to load compliance data.");
        }
      } finally {
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [filterSig, page, pageSize, activeFilters]);

  useEffect(() => {
    void (async () => {
      try {
        const { doctors } = await getComplianceDoctors();
        setDoctorOptions(doctors);
      } catch {
        /* doctor suggestions are optional — the field still accepts free text */
      }
    })();
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        await loadSummaries();
      } catch {
        /* summaries optional */
      }
    })();
  }, [loadSummaries]);

  useEffect(() => {
    void loadCharts();
  }, [loadCharts]);

  useEffect(() => {
    if (!detailId) {
      setDetail(null);
      setDetailErr(null);
      return;
    }
    let c = false;
    setDetailLoading(true);
    setDetailErr(null);
    getComplianceRun(detailId)
      .then((d) => {
        if (!c) setDetail(d);
      })
      .catch((e) => {
        if (!c) {
          setDetail(null);
          setDetailErr(e instanceof Error ? e.message : "Failed to load run detail.");
        }
      })
      .finally(() => {
        if (!c) setDetailLoading(false);
      });
    return () => {
      c = true;
    };
  }, [detailId]);

  const toggleSort = (k: SortKey) => {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir(k === "created_at" ? "desc" : "asc");
    }
  };

  const driveUrl = (id: string) => `https://drive.google.com/file/d/${id}/view`;

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const fromIdx = total === 0 ? 0 : page * pageSize + 1;
  const toIdx = Math.min(total, page * pageSize + runs.length);

  const sortAria = (k: SortKey) =>
    sortKey === k ? (sortDir === "asc" ? "ascending" : "descending") : "none";

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-3 px-3 py-3 sm:px-4 sm:py-4">
      <header className="border-b border-[var(--border)]/70 pb-3">
        <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-sky-700/90 dark:text-sky-400/90">
          Compliance
        </p>
        <h1 className="mt-0.5 text-xl font-semibold tracking-tight text-[var(--text-primary)] sm:text-[1.35rem]">
          Call quality
        </h1>
        <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[var(--text-secondary)]">
          <code className="rounded bg-[var(--bg-primary)] px-1 py-px font-mono text-[11px] text-[var(--text-primary)]">
            compliance_call
          </code>{" "}
          — transcript + rubric. Row or <strong className="font-medium text-[var(--text-primary)]">Details</strong> opens
          a run. When <strong className="font-medium text-[var(--text-primary)]">source_type</strong> is{" "}
          <code className="rounded bg-[var(--bg-primary)] px-1 py-px font-mono text-[11px]">drive</code>,{" "}
          <strong className="font-medium text-[var(--text-primary)]">Open</strong> uses Google Drive in a new tab; other
          sources show type only (no Drive URL).
        </p>
      </header>

      {err && (
        <div
          className="rounded-lg border border-red-500/30 bg-red-500/10 px-2.5 py-1.5 text-sm text-red-200"
          role="alert"
        >
          {err}
        </div>
      )}

      {chartsErr && (
        <div
          className="rounded-lg border border-amber-500/35 bg-amber-500/10 px-2.5 py-1.5 text-sm text-amber-100"
          role="alert"
        >
          Charts could not load: {chartsErr}
        </div>
      )}

      {exportErr && (
        <div
          className="rounded-lg border border-red-500/30 bg-red-500/10 px-2.5 py-1.5 text-sm text-red-200"
          role="alert"
        >
          Export failed: {exportErr}
        </div>
      )}

      <section
        className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-2.5 py-2 shadow-sm"
        aria-label="Filters"
      >
        <div className="flex flex-col gap-2.5">
          {/* Row 1 — free text, doctor, score bracket */}
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end sm:gap-x-4 sm:gap-y-2">
            <label className="min-w-[min(100%,12rem)] flex-1 text-[11px] font-medium text-[var(--text-secondary)] sm:min-w-[14rem]">
              Search
              <input
                type="search"
                className="mt-0.5 block w-full rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-sm text-[var(--text-primary)] outline-none focus:border-sky-500/50 focus:ring-1 focus:ring-sky-500/30"
                placeholder="Filename, doctor, source file id, SO conv id…"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                aria-label="Search runs on server"
              />
            </label>
            <label className="min-w-[min(100%,12rem)] flex-1 text-[11px] font-medium text-[var(--text-secondary)] sm:min-w-[13rem] sm:max-w-[18rem]">
              Doctor
              <input
                type="text"
                list="compliance-doctor-options"
                className="mt-0.5 block w-full rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-sm text-[var(--text-primary)] outline-none focus:border-sky-500/50 focus:ring-1 focus:ring-sky-500/30"
                placeholder="Type or pick a doctor…"
                value={doctorInput}
                onChange={(e) => setDoctorInput(e.target.value)}
                aria-label="Filter by doctor name"
              />
              <datalist id="compliance-doctor-options">
                {doctorOptions.map((d) => (
                  <option key={`${d.doctor_slug ?? ""}|${d.doctor_name ?? ""}`} value={d.label}>
                    {d.run_count} run{d.run_count === 1 ? "" : "s"}
                  </option>
                ))}
              </datalist>
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Score range
              <select
                className="mt-0.5 block min-w-[8.5rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-sm text-[var(--text-primary)] outline-none focus:border-sky-500/50"
                value={scoreBracket}
                onChange={(e) => setScoreBracket(e.target.value)}
                aria-label="Filter by composite score bracket"
              >
                {SCORE_BRACKETS.map((b) => (
                  <option key={b.value || "any"} value={b.value}>
                    {b.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Row 2 — date range with quick presets, status, sheet */}
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end sm:gap-x-4 sm:gap-y-2">
            <div className="flex flex-wrap items-end gap-2 sm:gap-3">
              <label className="text-[11px] font-medium text-[var(--text-secondary)]">
                Date from <span className="font-normal text-[var(--text-muted)]">(IST)</span>
                <input
                  type="date"
                  max={dateTo || istToday()}
                  className="mt-0.5 block w-[9.5rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-1 font-mono text-xs text-[var(--text-primary)] outline-none focus:border-sky-500/50"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  aria-label="Start date, IST calendar day, inclusive"
                />
              </label>
              <label className="text-[11px] font-medium text-[var(--text-secondary)]">
                Date to <span className="font-normal text-[var(--text-muted)]">(IST)</span>
                <input
                  type="date"
                  min={dateFrom || undefined}
                  max={istToday()}
                  className="mt-0.5 block w-[9.5rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-1 font-mono text-xs text-[var(--text-primary)] outline-none focus:border-sky-500/50"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  aria-label="End date, IST calendar day, inclusive"
                />
              </label>
              <div className="flex items-center gap-1 pb-0.5">
                {[7, 30, 90].map((d) => (
                  <button
                    key={d}
                    type="button"
                    className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-1 text-[11px] font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                    onClick={() => applyDatePreset(d)}
                  >
                    {d}d
                  </button>
                ))}
              </div>
            </div>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Status
              <select
                className="mt-0.5 block min-w-[9rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-sm text-[var(--text-primary)] outline-none focus:border-sky-500/50"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                aria-label="Filter by run status"
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value || "all"} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-[11px] font-medium text-[var(--text-secondary)]">
              Sheet
              <select
                className="mt-0.5 block min-w-[9rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-sm text-[var(--text-primary)] outline-none focus:border-sky-500/50"
                value={sheetFilter}
                onChange={(e) => setSheetFilter(e.target.value)}
                aria-label="Filter by whether the run was appended to the sheet"
              >
                {SHEET_OPTIONS.map((o) => (
                  <option key={o.value || "all"} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Row 3 — grade multi-select, clear, export */}
          <div className="flex flex-col gap-2 border-t border-[var(--border)]/60 pt-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:gap-x-4">
            <fieldset className="flex flex-wrap items-center gap-1.5">
              <legend className="sr-only">Filter by grade</legend>
              <span className="text-[11px] font-medium text-[var(--text-secondary)]">Grade</span>
              {GRADE_OPTIONS.map((g) => {
                const on = gradeFilter.includes(g);
                return (
                  <label
                    key={g}
                    className={`cursor-pointer select-none rounded-md border px-2 py-0.5 text-xs font-semibold transition-colors ${
                      on
                        ? "border-sky-500/60 bg-sky-500/15 text-sky-300"
                        : "border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={on}
                      onChange={() => toggleGrade(g)}
                    />
                    {g}
                  </label>
                );
              })}
              {gradeFilter.length > 0 && (
                <button
                  type="button"
                  className="text-[11px] font-medium text-sky-400 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => setGradeFilter([])}
                >
                  reset
                </button>
              )}
            </fieldset>

            <div className="flex flex-wrap items-center gap-2">
              {filtersActive && (
                <button
                  type="button"
                  className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-elev)] hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={clearFilters}
                >
                  Clear filters
                </button>
              )}
              <button
                type="button"
                className="rounded-md border border-sky-500/40 bg-sky-500/10 px-2.5 py-1 text-xs font-semibold text-sky-300 hover:bg-sky-500/20 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                onClick={() => void handleExport()}
                disabled={exporting || loading || total === 0}
                title="Download the currently filtered rows as CSV"
              >
                {exporting ? "Exporting…" : `Export CSV${total ? ` (${total})` : ""}`}
              </button>
            </div>
          </div>
        </div>
      </section>

      <ComplianceCallQualityCharts
        timeseries={timeseries}
        grades={grades}
        loading={chartsLoading}
        chartDays={chartDays}
        onChartDaysChange={setChartDays}
        granularity={granularity}
        onGranularityChange={setGranularity}
        summary30={summary30}
        tableTotal={total}
        tableLoading={loading}
      />

      <div className="overflow-x-auto rounded-lg border border-[var(--border)] bg-[var(--bg-card)] shadow-sm">
        <table className="w-full min-w-[880px] border-collapse text-left text-[13px]">
          <caption className="sr-only">Compliance call workflow runs, sortable columns with pagination</caption>
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] uppercase tracking-wide text-[var(--text-muted)]">
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("created_at")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("created_at")}
                >
                  Time (IST) {sortKey === "created_at" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("doctor_slug")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("doctor_slug")}
                >
                  Doctor {sortKey === "doctor_slug" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col">
                Sheet
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("status")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("status")}
                >
                  Status {sortKey === "status" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("composite_pct")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("composite_pct")}
                >
                  Score {sortKey === "composite_pct" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("grade")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("grade")}
                >
                  Grade {sortKey === "grade" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col" aria-sort={sortAria("filename")}>
                <button
                  type="button"
                  className="font-medium hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                  onClick={() => toggleSort("filename")}
                >
                  File {sortKey === "filename" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col">
                Source
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col">
                SO Conv ID
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col">
                Details
              </th>
              <th className="px-2.5 py-1.5 font-medium" scope="col">
                Error
              </th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={11} className="px-3 py-8 text-center text-[var(--text-muted)]">
                  Loading…
                </td>
              </tr>
            ) : runs.length === 0 && total === 0 ? (
              <tr>
                <td colSpan={11} className="px-3 py-8 text-center text-[var(--text-muted)]">
                  No runs yet. Trigger{" "}
                  <code className="rounded bg-[var(--bg-primary)] px-1 text-xs">compliance_call</code> or enable the
                  scheduler.
                </td>
              </tr>
            ) : runs.length === 0 ? (
              <tr>
                <td colSpan={11} className="px-3 py-8 text-center text-[var(--text-muted)]">
                  No rows match these filters. Clear search or adjust the date range.
                </td>
              </tr>
            ) : (
              runs.map((r) => (
                <tr
                  key={r.id}
                  className="cursor-pointer border-b border-[var(--border)]/60 hover:bg-[var(--bg-elev)]/50"
                  onClick={() => setDetailId(r.id)}
                >
                  <td className="px-2.5 py-1.5 whitespace-nowrap text-xs text-[var(--text-secondary)]">
                    {formatIst(r.created_at)}
                  </td>
                  <td className="px-2.5 py-1.5 text-sm text-[var(--text-primary)]" title={r.doctor_name || r.doctor_slug || ""}>
                    {r.doctor_name || r.doctor_slug || "—"}
                  </td>
                  <td className="px-2.5 py-1.5 text-xs">
                    {r.sheet_appended === true ? "Yes" : r.sheet_appended === false ? "No" : "—"}
                  </td>
                  <td className="px-2.5 py-1.5">
                    <span
                      className={
                        r.status === "completed"
                          ? "font-medium text-emerald-400"
                          : r.status === "failed"
                            ? "font-medium text-red-300"
                            : "text-[var(--text-secondary)]"
                      }
                    >
                      {r.status}
                    </span>
                  </td>
                  <td className="px-2.5 py-1.5 tabular-nums text-[var(--text-primary)]">
                    {r.composite_pct != null ? `${r.composite_pct}%` : "—"}
                  </td>
                  <td className="px-2.5 py-1.5 font-medium text-[var(--text-primary)]">{r.grade ?? "—"}</td>
                  <td className="max-w-[200px] truncate px-2.5 py-1.5 text-xs text-[var(--text-secondary)]" title={r.filename ?? ""}>
                    {r.filename ?? "—"}
                  </td>
                  <td className="px-2.5 py-1.5">
                    {r.source_type === "drive" && r.source_file_id ? (
                      <a
                        href={driveUrl(r.source_file_id)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs font-medium text-sky-400 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                        onClick={(e) => e.stopPropagation()}
                      >
                        Open
                      </a>
                    ) : r.source_file_id ? (
                      <span
                        className="text-xs text-[var(--text-secondary)]"
                        title={`${r.source_type}: ${r.source_file_id}`}
                      >
                        {r.source_type}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-2.5 py-1.5 tabular-nums text-xs text-[var(--text-secondary)]">
                    {r.mysql_second_opinion_conversation_id ?? "—"}
                  </td>
                  <td className="px-2.5 py-1.5">
                    <button
                      type="button"
                      className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-xs font-semibold text-[var(--text-primary)] hover:bg-[var(--bg-elev)] focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
                      onClick={(e) => {
                        e.stopPropagation();
                        setDetailId(r.id);
                      }}
                    >
                      Details
                    </button>
                  </td>
                  <td className="max-w-[140px] truncate px-2.5 py-1.5 text-xs text-red-300/90" title={r.error_message ?? ""}>
                    {r.error_message ? r.error_message.slice(0, 80) : "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <nav
        className="flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-xs sm:flex-row sm:items-center sm:justify-between"
        aria-label="Run results pages"
      >
        <p className="text-[var(--text-secondary)]">
          Showing{" "}
          <span className="font-medium tabular-nums text-[var(--text-primary)]">
            {fromIdx}–{toIdx}
          </span>{" "}
          of <span className="font-medium tabular-nums text-[var(--text-primary)]">{total}</span>
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-[var(--text-secondary)]">
            Rows
            <select
              className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-0.5 text-xs outline-none focus:border-[var(--border-active)]"
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number])}
              aria-label="Rows per page"
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
            className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1 text-xs font-medium disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
            disabled={page <= 0 || loading}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            Previous
          </button>
          <span className="tabular-nums text-[var(--text-muted)]">
            Page {page + 1} / {pageCount}
          </span>
          <button
            type="button"
            className="rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1 text-xs font-medium disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50"
            disabled={page + 1 >= pageCount || loading}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      </nav>

      <ComplianceRunDetailModal
        key={detailId ?? "closed"}
        open={Boolean(detailId)}
        onClose={() => setDetailId(null)}
        detail={detail}
        loading={detailLoading}
        error={detailErr}
      />
    </div>
  );
}
