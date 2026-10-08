"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import type { EarningHead, PayrollRow } from "../types";
import PayrollSummaryCards from "../PayrollSummaryCards";
import WorkflowTimeline from "../WorkflowTimeline";

type ClosedModule = {
  moduleName: string;
  rowCount: number;
  employeeHomes: string[];
  closedAt: string;
  closedByEmail: string;
  rows: PayrollRow[];
};

type ClosedMonth = {
  monthKey: string;
  month: string;
  modules: ClosedModule[];
};

type ClosedData = {
  months: ClosedMonth[];
  totalCount?: number;
};

type InProcessModule = {
  module: string;
  totalRows: number;
  currentStage: string;
  stages: {
    MAKER: number;
    HRBP: number;
    HOD: number;
    PAYROLL: number;
  };
};

type InProcessData = {
  modules: InProcessModule[];
};

export default function PayrollOpsPage() {
  const { user, refreshUser } = useAuth();
  const router = useRouter();

  const [modules, setModules] = useState<EarningHead[]>([]);
  const [rows, setRows] = useState<PayrollRow[]>([]);
  const [activeModule, setActiveModule] = useState<string>("");
  const [activeTab, setActiveTab] = useState<"queue" | "closed" | "inprocess">("queue");
  const [closedData, setClosedData] = useState<ClosedData>({ months: [] });
  const [inProcessData, setInProcessData] = useState<InProcessData>({ modules: [] });
  const [selectedClosedMonth, setSelectedClosedMonth] = useState<string | null>(null);
  const [expandedClosedModule, setExpandedClosedModule] = useState<string | null>(null);
  const [expandedInProcessModule, setExpandedInProcessModule] = useState<string | null>(null);
  const [closingModule, setClosingModule] = useState<string | null>(null);
  const [rejectingModule, setRejectingModule] = useState<string | null>(null);
  const [showRejectModal, setShowRejectModal] = useState<boolean>(false);
  const [targetRejectModule, setTargetRejectModule] = useState<string>("");
  const [rejectComments, setRejectComments] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [closedPage, setClosedPage] = useState<number>(1);
  const [payrollLoaded, setPayrollLoaded] = useState<boolean>(false);
  const initialModuleSelected = useRef(false);

  useEffect(() => {
    refreshUser();
  }, [refreshUser]);

  useEffect(() => {
    if (user) {
      const isPayroll = user.roles.some((r) => r.toLowerCase() === "payroll" || r.toLowerCase() === "payroll_admin");
      if (!isPayroll) {
        router.replace("/payroll");
        return;
      }
      fetchConfig();
      fetchPayrollData();
      fetchInProcessData();
    }
  }, [user]);

  useEffect(() => {
    setClosedPage(1);
  }, [selectedClosedMonth, expandedClosedModule]);

  useEffect(() => {
    if (user && activeTab === "closed") {
      fetchClosedData();
    }
  }, [user, activeTab, closedPage]);

  const fetchConfig = async () => {
    try {
      const res = await apiFetch("/api/workflow/config");
      if (res.ok) {
        const data = await res.json();
        const allHeads = data.earning_heads || [];
        setModules(allHeads);
        // Initial module selection is deferred to the effect below so we can
        // prefer a module that actually has queued sheets.
      }
    } catch (error) {
      console.error("Failed to fetch config:", error);
    } finally {
      setLoading(false);
    }
  };

  const fetchPayrollData = async () => {
    try {
      const res = await apiFetch("/api/workflow/payroll");
      if (res.ok) {
        const data = await res.json();
        setRows(data || []);
      }
    } catch (error) {
      console.error("Failed to fetch payroll data:", error);
    } finally {
      setPayrollLoaded(true);
    }
  };

  // On first load, land on a module that actually has queued sheets (falling
  // back to the first configured module) so freshly-approved work is visible
  // without hunting through module tabs. Runs once; manual selection wins after.
  useEffect(() => {
    if (initialModuleSelected.current) return;
    if (modules.length === 0 || !payrollLoaded) return;
    const firstWithRows = modules.find((m) => rows.some((r) => r.module === m.name));
    setActiveModule((firstWithRows || modules[0]).name);
    initialModuleSelected.current = true;
  }, [modules, rows, payrollLoaded]);

  const fetchClosedData = async () => {
    try {
      const res = await apiFetch(`/api/workflow/closed?page=${closedPage}&limit=100`);
      if (res.ok) {
        const data = (await res.json()) as ClosedData;
        setClosedData(data);
        if (data.months && data.months.length > 0 && !selectedClosedMonth) {
          setSelectedClosedMonth(data.months[0].monthKey);
        }
      }
    } catch (error) {
      console.error("Failed to fetch closed data:", error);
    }
  };

  const fetchInProcessData = async () => {
    try {
      const res = await apiFetch("/api/workflow/in-process");
      if (res.ok) {
        const data = (await res.json()) as InProcessData;
        setInProcessData(data);
      }
    } catch (error) {
      console.error("Failed to fetch in-process data:", error);
    }
  };

  const handleCloseModule = async (moduleName: string) => {
    if (!confirm(`Are you sure you want to finalize and close all "${moduleName}" sheets? This action cannot be undone.`)) {
      return;
    }
    setClosingModule(moduleName);
    try {
      const res = await apiFetch("/api/workflow/payroll/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ module: moduleName }),
      });
      if (res.ok) {
        alert("Payroll finalized and closed successfully.");
        await Promise.all([fetchPayrollData(), fetchClosedData(), fetchInProcessData()]);
      } else {
        alert("Failed to close sheets.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setClosingModule(null);
    }
  };

  const handleRejectModule = async () => {
    if (!targetRejectModule) return;
    setRejectingModule(targetRejectModule);
    try {
      const res = await apiFetch("/api/workflow/payroll/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ module: targetRejectModule, comments: rejectComments }),
      });
      if (res.ok) {
        alert(`Payroll Sheet "${targetRejectModule}" returned to Maker.`);
        setShowRejectModal(false);
        setRejectComments("");
        await Promise.all([fetchPayrollData(), fetchClosedData(), fetchInProcessData()]);
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to return sheet to Maker.");
      }
    } catch (error) {
      console.error(error);
      alert("An error occurred while returning sheet to Maker.");
    } finally {
      setRejectingModule(null);
    }
  };

  const exportExcel = (rowsToExport: PayrollRow[], filename?: string, moduleName?: string) => {
    const csvRows: string[] = [];
    const targetModule = moduleName || activeModule;

    let headers: string[];
    if (targetModule === "REFERRAL BONUS") {
      headers = [
        "S.no.",
        "Emp code",
        "Emp name",
        "Grade",
        "Designation",
        "Employee home",
        "No. of referred emp.",
        "Emp code & name of referred employees",
        "Amount",
        "Remarks (if any)",
        "Maker"
      ];
    } else {
      headers = [
        "S.no.",
        "Emp code",
        "Emp name",
        "Grade",
        "Designation",
        "Employee home",
        "Amount",
        "Remarks (if any)",
        "Maker"
      ];
    }

    csvRows.push(headers.join(","));

    rowsToExport.forEach((row, index) => {
      if (targetModule === "REFERRAL BONUS") {
        csvRows.push(
          [
            `"${index + 1}"`,
            `"${row.empCode || ""}"`,
            `"${row.empName || ""}"`,
            `"${row.grade || ""}"`,
            `"${row.designation || ""}"`,
            `"${row.employeeHome || ""}"`,
            `"${row.overtimeHours || ""}"`,
            `"${row.holidayDate || ""}"`,
            `"${row.amount || ""}"`,
            `"${row.remarks || ""}"`,
            `"${row.initiatorEmail || ""}"`,
          ].join(",")
        );
      } else {
        csvRows.push(
          [
            `"${index + 1}"`,
            `"${row.empCode || ""}"`,
            `"${row.empName || ""}"`,
            `"${row.grade || ""}"`,
            `"${row.designation || ""}"`,
            `"${row.employeeHome || ""}"`,
            `"${row.amount || ""}"`,
            `"${row.remarks || ""}"`,
            `"${row.initiatorEmail || ""}"`,
          ].join(",")
        );
      }
    });

    const blob = new Blob([csvRows.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || `${targetModule}.csv`;
    link.click();
  };

  const downloadClosedCSV = async (moduleName: string, monthKey: string) => {
    try {
      const res = await apiFetch(`/api/workflow/closed?limit=100000`);
      if (res.ok) {
        const data = (await res.json()) as ClosedData;
        const monthData = data.months.find(m => m.monthKey === monthKey);
        const modData = monthData?.modules.find(m => m.moduleName === moduleName);
        if (modData && modData.rows) {
          exportExcel(modData.rows, `${moduleName}_closed.csv`, moduleName);
        } else {
          alert("No rows found for this closed sheet.");
        }
      } else {
        alert("Failed to load closed sheet rows.");
      }
    } catch (err) {
      console.error(err);
      alert("An error occurred while loading closed sheet rows.");
    }
  };

  const getProgressColor = (progress: number) => {
    if (progress <= 25) return "bg-[var(--accent-red)]";
    if (progress <= 50) return "bg-[var(--accent-orange)]";
    if (progress <= 75) return "bg-[var(--accent-blue)]";
    return "bg-[var(--accent-green)]";
  };

  const getProgressBg = (progress: number) => {
    if (progress <= 25) return "bg-[var(--accent-red)]/10 text-[var(--accent-red)]";
    if (progress <= 50) return "bg-[var(--accent-orange)]/10 text-[var(--accent-orange)]";
    if (progress <= 75) return "bg-[var(--accent-blue)]/10 text-[var(--accent-blue)]";
    return "bg-[var(--accent-green)]/10 text-[var(--accent-green)]";
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="text-center">
          <div className="w-8 h-8 border-2 border-[var(--accent-green)] border-t-transparent rounded-full animate-spin mx-auto mb-2"></div>
          <p className="text-xs text-[var(--text-muted)]">Loading configuration...</p>
        </div>
      </div>
    );
  }

  const activeMonthData = closedData.months.find((m) => m.monthKey === selectedClosedMonth);
  const activeClosedModuleObj = activeMonthData?.modules.find((m) => m.moduleName === expandedClosedModule);

  const activeQueueRows = rows.filter((row) => row.module === activeModule);
  const activeHeadObj = modules.find((m) => m.name === activeModule);

  return (
    <div className="flex flex-col space-y-6">
      <div className="flex justify-between items-center bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">Payroll Ops Dashboard</h1>
          <p className="text-xs text-[var(--text-muted)] mt-1">
            Logged in as Payroll Admin: <span className="font-medium">{user?.full_name}</span> ({user?.email})
          </p>
        </div>

        {/* Tab Controls */}
        <div className="flex border border-[var(--border)] rounded-lg p-1 bg-[var(--bg-primary)]">
          <button
            onClick={() => setActiveTab("queue")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "queue"
                ? "bg-white text-[var(--text-primary)] shadow-sm"
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            💳 Active Queue ({rows.length})
          </button>
          <button
            onClick={() => setActiveTab("closed")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "closed"
                ? "bg-white text-[var(--text-primary)] shadow-sm"
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            📁 Closed Sheets
          </button>
          <button
            onClick={() => setActiveTab("inprocess")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "inprocess"
                ? "bg-white text-[var(--text-primary)] shadow-sm"
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            ⏱️ In Process ({inProcessData.modules.length})
          </button>
        </div>
      </div>

      {activeTab === "queue" && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
          <div className="lg:col-span-1">
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm">
              <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-muted)] mb-3">Earning Head Modules</h3>
              <div className="space-y-1.5">
                {modules.map((m) => {
                  const moduleCount = rows.filter((r) => r.module === m.name).length;
                  const hasPending = moduleCount > 0;
                  return (
                    <button
                      key={m.name}
                      onClick={() => setActiveModule(m.name)}
                      className={`w-full text-left px-3 py-2 rounded-lg text-xs transition-all flex justify-between items-center ${
                        activeModule === m.name
                          ? "bg-[var(--accent-green)] text-white shadow-sm font-bold"
                          : hasPending
                            ? "text-[var(--text-primary)] font-bold hover:bg-black/5"
                            : "text-[var(--text-secondary)] font-semibold hover:bg-black/5"
                      }`}
                    >
                      <span className={hasPending || activeModule === m.name ? "font-bold" : ""}>
                        💰 {m.name}{hasPending ? ` (${moduleCount})` : ""}
                      </span>
                      {hasPending && (
                        <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
                          activeModule === m.name
                            ? "bg-white text-[var(--accent-green)]"
                            : "bg-[var(--accent-green)]/15 text-[var(--accent-green)]"
                        }`}>
                          {moduleCount}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="lg:col-span-3 flex flex-col space-y-6">
            {activeModule && (
              <WorkflowTimeline
                status={activeQueueRows[0]?.status || "PAYROLL"}
                hrbpBypassed={activeHeadObj?.hrbp === "NA"}
                history={activeQueueRows[0]?.history || []}
              />
            )}

            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden flex flex-col">
              <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)] flex justify-between items-center">
                <div>
                  <h2 className="text-sm font-bold text-[var(--text-primary)]">
                    Active Queue: {activeModule}
                  </h2>
                  <p className="text-[11px] text-[var(--text-muted)] mt-0.5">Approved sheets waiting to be finalized</p>
                </div>
                {activeQueueRows.length > 0 && (
                  <div className="flex space-x-2">
                    <button
                      onClick={() => exportExcel(activeQueueRows, `${activeModule}_payroll.csv`)}
                      className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-semibold px-3 py-1.5 rounded-lg transition flex items-center gap-1"
                    >
                      📥 Export CSV
                    </button>
                    <button
                      onClick={() => {
                        setTargetRejectModule(activeModule);
                        setShowRejectModal(true);
                      }}
                      className="bg-[var(--accent-red)]/10 text-[var(--accent-red)] hover:bg-[var(--accent-red)]/20 border border-[var(--accent-red)]/30 text-xs font-semibold px-3.5 py-1.5 rounded-lg transition flex items-center gap-1.5"
                    >
                      🛑 Reject & Return to Maker
                    </button>
                    <button
                      onClick={() => handleCloseModule(activeModule)}
                      disabled={closingModule === activeModule}
                      className="bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green)]/90 text-xs font-semibold px-3.5 py-1.5 rounded-lg transition disabled:opacity-50 flex items-center gap-1.5"
                    >
                      {closingModule === activeModule ? "Closing..." : "🔒 Close & Archive"}
                    </button>
                  </div>
                )}
              </div>

              <div className="p-4">
                <PayrollSummaryCards
                  totalEntries={activeQueueRows.length}
                  totalAmount={activeQueueRows.reduce((sum, r) => sum + (parseInt(r.amount) || 0), 0)}
                  averagePayout={activeQueueRows.length > 0 ? Math.round(activeQueueRows.reduce((sum, r) => sum + (parseInt(r.amount) || 0), 0) / activeQueueRows.length) : 0}
                />
              </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  {activeModule === "REFERRAL BONUS" ? (
                    <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                      <th className="py-3 px-4 w-12 text-center">S.no.</th>
                      <th className="py-3 px-3">Emp code</th>
                      <th className="py-3 px-3">Emp name</th>
                      <th className="py-3 px-3">Grade</th>
                      <th className="py-3 px-3">Designation</th>
                      <th className="py-3 px-3">Employee home</th>
                      <th className="py-3 px-3">No. of referred emp.</th>
                      <th className="py-3 px-3">Emp code & name of referred employees</th>
                      <th className="py-3 px-3">Amount</th>
                      <th className="py-3 px-3">Remarks (if any)</th>
                      <th className="py-3 px-3">Maker</th>
                      <th className="py-3 px-3">HRBP Remarks</th>
                      <th className="py-3 px-3">HOD Remarks</th>
                    </tr>
                  ) : (
                    <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                      <th className="py-3 px-4 w-12 text-center">S.no.</th>
                      <th className="py-3 px-3">Emp code</th>
                      <th className="py-3 px-3">Emp name</th>
                      <th className="py-3 px-3">Grade</th>
                      <th className="py-3 px-3">Designation</th>
                      <th className="py-3 px-3">Employee home</th>
                      <th className="py-3 px-3">Amount</th>
                      <th className="py-3 px-3">Remarks (if any)</th>
                      <th className="py-3 px-3">Maker</th>
                      <th className="py-3 px-3">HRBP Remarks</th>
                      <th className="py-3 px-3">HOD Remarks</th>
                    </tr>
                  )}
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {activeQueueRows.length > 0 ? (
                    activeQueueRows.map((row, index) => {
                      const isReferral = row.module === "REFERRAL BONUS";

                      return (
                        <tr key={row.id} className="hover:bg-black/5">
                          <td className="py-3 px-4 text-center font-medium text-[var(--text-muted)]">{index + 1}</td>
                          <td className="py-3 px-3 font-semibold">{row.empCode}</td>
                          <td className="py-3 px-3">{row.empName}</td>
                          <td className="py-3 px-3">{row.grade}</td>
                          <td className="py-3 px-3">{row.designation}</td>
                          <td className="py-3 px-3">🏢 {row.employeeHome}</td>
                          {isReferral && (
                            <>
                              <td className="py-3 px-3">{row.overtimeHours || "0"}</td>
                              <td className="py-3 px-3">{row.holidayDate || "N/A"}</td>
                            </>
                          )}
                          <td className="py-3 px-3 font-bold text-[var(--accent-green)]">₹ {Number(row.amount).toLocaleString()}</td>
                          <td className="py-3 px-3 text-[var(--text-secondary)] italic">{row.remarks || "No remarks"}</td>
                          <td className="py-3 px-3 text-[var(--text-muted)] font-mono">{row.initiatorEmail ? row.initiatorEmail.split('@')[0] : "N/A"}</td>
                          <td className="py-3 px-3 text-[var(--accent-orange)] italic">{row.hrbpComments || "None"}</td>
                          <td className="py-3 px-3 text-[var(--accent-blue)] italic">{row.hodComments || "None"}</td>
                        </tr>
                      );
                    })
                  ) : (
                    <tr>
                      <td colSpan={activeModule === "REFERRAL BONUS" ? 13 : 11} className="py-8 text-center text-[var(--text-muted)] font-medium">
                        No approved sheets in queue for this module.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            </div>
          </div>
        </div>
      )}

      {activeTab === "closed" && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
          {/* Months list */}
          <div className="lg:col-span-1">
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm">
              <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-muted)] mb-3">Closed Months</h3>
              <div className="space-y-1.5">
                {closedData.months.map((m) => (
                  <button
                    key={m.monthKey}
                    onClick={() => {
                      setSelectedClosedMonth(m.monthKey);
                      setExpandedClosedModule(null);
                    }}
                    className={`w-full text-left px-3 py-2 rounded-lg text-xs font-semibold transition-all ${
                      selectedClosedMonth === m.monthKey
                        ? "bg-[var(--accent-green)] text-white shadow-sm"
                        : "text-[var(--text-secondary)] hover:bg-black/5"
                    }`}
                  >
                    📅 {m.month}
                  </button>
                ))}
                {closedData.months.length === 0 && (
                  <p className="text-xs text-[var(--text-muted)] py-4 text-center">No closed records found.</p>
                )}
              </div>
            </div>
          </div>

          <div className="lg:col-span-3 flex flex-col space-y-6">
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
              <h2 className="text-sm font-bold text-[var(--text-primary)] mb-4">
                Closed Components for Month: {activeMonthData?.month || "Select Month"}
              </h2>

              <div className="space-y-3">
                {activeMonthData?.modules.map((mod) => (
                  <div key={mod.moduleName} className="border border-[var(--border)] rounded-xl overflow-hidden">
                    <div className="bg-[var(--bg-primary)] px-4 py-3 flex justify-between items-center">
                      <div>
                        <span className="font-bold text-xs">💰 {mod.moduleName}</span>
                        <p className="text-[10px] text-[var(--text-muted)] mt-0.5">
                          {mod.rowCount} employees • Homes: {mod.employeeHomes.join(", ")}
                        </p>
                      </div>
                      <div className="flex space-x-2">
                        <button
                          onClick={() => downloadClosedCSV(mod.moduleName, selectedClosedMonth!)}
                          className="bg-black/5 hover:bg-black/10 border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold rounded-lg transition"
                        >
                          📥 Download CSV
                        </button>
                        <button
                          onClick={() => setExpandedClosedModule(expandedClosedModule === mod.moduleName ? null : mod.moduleName)}
                          className="bg-white border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold rounded-lg transition"
                        >
                          {expandedClosedModule === mod.moduleName ? "Hide Details" : "Show Details"}
                        </button>
                      </div>
                    </div>

                    {expandedClosedModule === mod.moduleName && activeClosedModuleObj && (
                      <div className="border-t border-[var(--border)] p-4 flex flex-col space-y-4">
                        <WorkflowTimeline
                          status="CLOSED"
                          hrbpBypassed={modules.find((m) => m.name === mod.moduleName)?.hrbp === "NA"}
                          history={activeClosedModuleObj.rows[0]?.history || []}
                        />
                        <div className="overflow-x-auto border border-[var(--border)] rounded-lg">
                          <table className="w-full text-left text-xs border-collapse">
                          <thead>
                            {mod.moduleName === "REFERRAL BONUS" ? (
                              <tr className="bg-[var(--bg-primary)]/50 border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                                <th className="py-2.5 px-4 w-12 text-center">S.no.</th>
                                <th className="py-2.5 px-3">Emp code</th>
                                <th className="py-2.5 px-3">Emp name</th>
                                <th className="py-2.5 px-3">Grade</th>
                                <th className="py-2.5 px-3">Designation</th>
                                <th className="py-2.5 px-3">Employee home</th>
                                <th className="py-2.5 px-3">No. of referred emp.</th>
                                <th className="py-2.5 px-3">Emp code & name of referred employees</th>
                                <th className="py-2.5 px-3">Amount</th>
                                <th className="py-2.5 px-3">Remarks (if any)</th>
                                <th className="py-2.5 px-3">Closed At</th>
                                <th className="py-2.5 px-3">Closed By</th>
                              </tr>
                            ) : (
                              <tr className="bg-[var(--bg-primary)]/50 border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                                <th className="py-2.5 px-4 w-12 text-center">S.no.</th>
                                <th className="py-2.5 px-3">Emp code</th>
                                <th className="py-2.5 px-3">Emp name</th>
                                <th className="py-2.5 px-3">Grade</th>
                                <th className="py-2.5 px-3">Designation</th>
                                <th className="py-2.5 px-3">Employee home</th>
                                <th className="py-2.5 px-3">Amount</th>
                                <th className="py-2.5 px-3">Remarks (if any)</th>
                                <th className="py-2.5 px-3">Closed At</th>
                                <th className="py-2.5 px-3">Closed By</th>
                              </tr>
                            )}
                          </thead>
                          <tbody className="divide-y divide-[var(--border)]">
                            {activeClosedModuleObj.rows.map((row, idx) => {
                              const isReferral = mod.moduleName === "REFERRAL BONUS";
                              const startIndex = (closedPage - 1) * 100;
                              return (
                                <tr key={row.id}>
                                  <td className="py-2 px-4 text-center font-medium text-[var(--text-muted)]">{startIndex + idx + 1}</td>
                                  <td className="py-2 px-3 font-semibold">{row.empCode}</td>
                                  <td className="py-2 px-3">{row.empName}</td>
                                  <td className="py-2 px-3">{row.grade}</td>
                                  <td className="py-2 px-3">{row.designation}</td>
                                  <td className="py-2 px-3">🏢 {row.employeeHome}</td>
                                  {isReferral && (
                                    <>
                                      <td className="py-2 px-3">{row.overtimeHours || "0"}</td>
                                      <td className="py-2 px-3">{row.holidayDate || "N/A"}</td>
                                    </>
                                  )}
                                  <td className="py-2 px-3 font-bold">₹ {Number(row.amount).toLocaleString()}</td>
                                  <td className="py-2 px-3 text-[var(--text-secondary)] italic">{row.remarks || "No remarks"}</td>
                                  <td className="py-2 px-3 text-[var(--text-muted)] font-mono">{row.closedAt}</td>
                                  <td className="py-2 px-3 text-[var(--text-muted)]">{row.closedByEmail}</td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              
              {/* Global Pagination controls for closed sheets */}
              {(() => {
                const totalClosedCount = closedData.totalCount ?? 0;
                if (totalClosedCount <= 100) return null;
                const totalClosedPages = Math.ceil(totalClosedCount / 100);
                return (
                  <div className="flex justify-between items-center px-4 py-3 bg-[var(--bg-primary)] border border-[var(--border)] rounded-xl text-xs mt-4">
                    <span className="text-[var(--text-secondary)]">
                      Showing <strong>{(closedPage - 1) * 100 + 1}</strong> - <strong>{Math.min(closedPage * 100, totalClosedCount)}</strong> of <strong>{totalClosedCount}</strong> archived entries
                    </span>
                    <div className="flex space-x-2">
                      <button
                        onClick={() => setClosedPage(p => Math.max(p - 1, 1))}
                        disabled={closedPage === 1}
                        className="px-3 py-1 border border-[var(--border)] rounded bg-white hover:bg-black/5 disabled:opacity-50 font-semibold"
                      >
                        Previous Page
                      </button>
                      <span className="flex items-center px-2 font-medium text-[var(--text-primary)]">
                        Page {closedPage} of {totalClosedPages}
                      </span>
                      <button
                        onClick={() => setClosedPage(p => Math.min(p + 1, totalClosedPages))}
                        disabled={closedPage === totalClosedPages}
                        className="px-3 py-1 border border-[var(--border)] rounded bg-white hover:bg-black/5 disabled:opacity-50 font-semibold"
                      >
                        Next Page
                      </button>
                    </div>
                  </div>
                );
              })()}
            </div>
          </div>
        </div>
      )}

      {activeTab === "inprocess" && (
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
          <h2 className="text-sm font-bold text-[var(--text-primary)] mb-4">In-Flight Payroll Sheets Status</h2>
          <div className="space-y-4">
            {inProcessData.modules.map((mod) => {
              // Calculate progress percentage based on stage counts
              const total = mod.totalRows;
              const maker = mod.stages.MAKER;
              const hrbp = mod.stages.HRBP;
              const hod = mod.stages.HOD;
              const payroll = mod.stages.PAYROLL;

              // Assign progress percentage
              let pct = 0;
              if (payroll > 0) pct = 90;
              else if (hod > 0) pct = 70;
              else if (hrbp > 0) pct = 45;
              else pct = 20;

              const isExpanded = expandedInProcessModule === mod.module;
              return (
                <div key={mod.module} className="border border-[var(--border)] rounded-xl p-4 flex flex-col space-y-4">
                  <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                    <div className="space-y-1">
                      <span className="font-bold text-xs">💰 {mod.module}</span>
                      <div className="flex gap-2 flex-wrap text-[10px]">
                        <span className="bg-black/5 px-2 py-0.5 rounded text-[var(--text-secondary)] font-medium">Total: {total} rows</span>
                        <span className="bg-blue-500/10 text-blue-600 px-2 py-0.5 rounded font-medium">Maker: {maker}</span>
                        <span className="bg-yellow-500/10 text-yellow-600 px-2 py-0.5 rounded font-medium">HRBP: {hrbp}</span>
                        <span className="bg-purple-500/10 text-purple-600 px-2 py-0.5 rounded font-medium">HOD: {hod}</span>
                        <span className="bg-green-500/10 text-green-600 px-2 py-0.5 rounded font-medium">Payroll: {payroll}</span>
                      </div>
                    </div>

                    <div className="flex items-center space-x-4 min-w-[270px] justify-between">
                      <div className="flex-1 mr-4">
                        <div className="flex justify-between text-[10px] font-bold text-[var(--text-muted)] mb-1">
                          <span>Current: {mod.currentStage}</span>
                          <span>{pct}%</span>
                        </div>
                        <div className="w-full bg-black/5 h-2 rounded-full overflow-hidden">
                          <div className={`h-full rounded-full ${getProgressColor(pct)}`} style={{ width: `${pct}%` }}></div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setExpandedInProcessModule(isExpanded ? null : mod.module)}
                          className="bg-white border border-[var(--border)] px-2.5 py-1 text-[10px] font-semibold rounded-lg transition"
                        >
                          {isExpanded ? "Hide Flow" : "Show Flow"}
                        </button>
                        <span className={`px-2.5 py-1 rounded-lg text-[10px] font-bold ${getProgressBg(pct)}`}>
                          {mod.currentStage}
                        </span>
                      </div>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="border-t border-[var(--border)] pt-4 w-full">
                      <WorkflowTimeline
                        status={mod.currentStage}
                        hrbpBypassed={modules.find((m) => m.name === mod.module)?.hrbp === "NA"}
                      />
                    </div>
                  )}
                </div>
              );
            })}
            {inProcessData.modules.length === 0 && (
              <p className="text-xs text-[var(--text-muted)] py-8 text-center">No sheets are currently in process.</p>
            )}
          </div>
        </div>
      )}
      {showRejectModal && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl max-w-md w-full p-6 shadow-xl space-y-4">
            <h3 className="text-base font-bold text-[var(--text-primary)]">
              Reject & Return Sheet to Maker
            </h3>
            <p className="text-xs text-[var(--text-muted)]">
              Returning <strong className="text-[var(--text-primary)]">{targetRejectModule}</strong> back to the Maker for corrections. An email notification will be sent to the Maker with HRBP and HOD in CC.
            </p>
            <div>
              <label className="block text-xs font-semibold text-[var(--text-secondary)] mb-1">
                Rejection Reason / Comments <span className="text-red-500">*</span>
              </label>
              <textarea
                value={rejectComments}
                onChange={(e) => setRejectComments(e.target.value)}
                placeholder="Enter details on why this sheet is being returned..."
                className="w-full border border-[var(--border)] rounded-lg p-2.5 text-xs focus:ring-2 focus:ring-[var(--accent-red)] focus:outline-none h-24"
              />
            </div>
            <div className="flex justify-end space-x-2 pt-2">
              <button
                onClick={() => {
                  setShowRejectModal(false);
                  setRejectComments("");
                }}
                className="px-4 py-2 border border-[var(--border)] text-xs font-semibold rounded-lg hover:bg-black/5 transition"
              >
                Cancel
              </button>
              <button
                onClick={handleRejectModule}
                disabled={!rejectComments.trim() || rejectingModule === targetRejectModule}
                className="px-4 py-2 bg-[var(--accent-red)] text-white text-xs font-semibold rounded-lg hover:bg-[var(--accent-red)]/90 transition disabled:opacity-50"
              >
                {rejectingModule === targetRejectModule ? "Returning..." : "Confirm Rejection"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
