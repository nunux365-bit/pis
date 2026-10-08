"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import type { EarningHead, PayrollRow, AuditItem } from "../types";
import PayrollSummaryCards from "../PayrollSummaryCards";
import WorkflowTimeline from "../WorkflowTimeline";

export default function HODPage() {
  const { user, refreshUser } = useAuth();
  const router = useRouter();

  const [modules, setModules] = useState<EarningHead[]>([]);
  const [rows, setRows] = useState<PayrollRow[]>([]);
  const [activeModule, setActiveModule] = useState<string>("");
  const [activeEmployeeHome, setActiveEmployeeHome] = useState<string>("");
  const [hodComments, setHodComments] = useState<string>("");
  const [flaggedColumns, setFlaggedColumns] = useState<string[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [pendingCounts, setPendingCounts] = useState<{module: string; employeeHome: string; count: number}[]>([]);
  const [homeSearch, setHomeSearch] = useState<string>("");
  const [homeDropdownOpen, setHomeDropdownOpen] = useState(false);
  const homeSearchRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    refreshUser();
  }, [refreshUser]);

  useEffect(() => {
    if (user) {
      const isHOD = user.roles.some((r) => r.toLowerCase() === "hod" || r.toLowerCase() === "payroll_admin");
      if (!isHOD) {
        router.replace("/payroll");
        return;
      }
      fetchConfig();
      fetchPendingCounts();
    }
  }, [user]);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (homeSearchRef.current && !homeSearchRef.current.contains(e.target as Node)) {
        setHomeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    if (activeModule && activeEmployeeHome) {
      fetchPayrollData(activeModule, activeEmployeeHome);
    }
  }, [activeModule, activeEmployeeHome]);

  useEffect(() => {
    if (activeModule && modules.length > 0) {
      const activeHead = modules.find((m) => m.name === activeModule);
      if (activeHead && activeHead.allowed_homes && activeHead.allowed_homes.length > 0) {
        if (!activeHead.allowed_homes.includes(activeEmployeeHome)) {
          setActiveEmployeeHome(activeHead.allowed_homes[0]);
        }
      } else {
        setActiveEmployeeHome("ALL");
      }
    }
  }, [activeModule, modules]);

  const fetchConfig = async () => {
    try {
      const res = await apiFetch("/api/workflow/config");
      if (res.ok) {
        const data = await res.json();
        const allHeads = data.earning_heads || [];
        setModules(allHeads);
        if (allHeads.length > 0) {
          setActiveModule(allHeads[0].name);
          if (allHeads[0].allowed_homes && allHeads[0].allowed_homes.length > 0) {
            setActiveEmployeeHome(allHeads[0].allowed_homes[0]);
          } else {
            setActiveEmployeeHome("ALL");
          }
        }
      }
    } catch (error) {
      console.error("Failed to fetch configuration:", error);
    } finally {
      setLoading(false);
    }
  };

  const fetchPendingCounts = async () => {
    try {
      const res = await apiFetch("/api/workflow/pending-counts");
      if (res.ok) {
        const data = await res.json();
        setPendingCounts(data.hod || []);
      }
    } catch (error) {
      console.error("Failed to fetch pending counts:", error);
    }
  };

  const fetchPayrollData = async (moduleName: string, homeName: string) => {
    const mod = moduleName || activeModule;
    const home = homeName || activeEmployeeHome;
    if (!mod || !home) return;
    try {
      const res = await apiFetch(`/api/workflow/hod?module=${encodeURIComponent(mod)}&employeeHome=${encodeURIComponent(home)}`);
      if (res.ok) {
        const data = (await res.json()) as PayrollRow[];
        setRows(data || []);
        if (data && data.length > 0) {
          setHodComments(data[0].hodComments || "");
          setFlaggedColumns(data[0].flaggedColumns || []);
        } else {
          setHodComments("");
          setFlaggedColumns([]);
        }
      }
    } catch (error) {
      console.error(error);
    }
  };

  const saveReview = async () => {
    if (!activeModule || !activeEmployeeHome) return;
    setSaving(true);
    try {
      const res = await apiFetch("/api/workflow/save-hod-review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          comments: hodComments,
          module: activeModule,
          employeeHome: activeEmployeeHome,
        }),
      });
      if (res.ok) {
        alert("Review Saved successfully.");
      } else {
        alert("Failed to save review.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setSaving(false);
    }
  };

  const submitPayroll = async () => {
    if (!activeModule || !activeEmployeeHome) return;
    setSubmitting(true);
    try {
      const res = await apiFetch(
        `/api/workflow/submit-payroll?module=${encodeURIComponent(activeModule)}&employeeHome=${encodeURIComponent(activeEmployeeHome)}`,
        { method: "POST" }
      );
      if (res.ok) {
        alert("Payroll Sheet successfully approved & sent to Payroll Admin.");
        await fetchPayrollData(activeModule, activeEmployeeHome);
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to forward sheet.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setSubmitting(false);
    }
  };

  const returnHRBP = async () => {
    if (!activeModule || !activeEmployeeHome) return;
    const reason = hodComments.trim() || prompt("Enter reason for returning to HRBP:");
    if (!reason) return;
    setSubmitting(true);
    try {
      const res = await apiFetch(
        `/api/workflow/return-hrbp?module=${encodeURIComponent(activeModule)}&employeeHome=${encodeURIComponent(activeEmployeeHome)}&comments=${encodeURIComponent(reason)}`,
        { method: "POST" }
      );
      if (res.ok) {
        alert("Payroll Sheet successfully returned to HRBP.");
        await fetchPayrollData(activeModule, activeEmployeeHome);
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to return sheet.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setSubmitting(false);
    }
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

  const activeHeadObj = modules.find((m) => m.name === activeModule);
  const homesList = activeHeadObj?.allowed_homes || [];
  const filteredRows = rows;

  const totalAmount = filteredRows.reduce((sum, row) => sum + Number(row.amount || 0), 0);
  const averagePayout = filteredRows.length > 0 ? Math.round(totalAmount / filteredRows.length) : 0;

  return (
    <div className="flex flex-col space-y-6">
      <div className="flex justify-between items-center bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">HOD Approval Dashboard</h1>
          <p className="text-xs text-[var(--text-muted)] mt-1">
            Logged in as HOD: <span className="font-medium">{user?.full_name}</span> ({user?.email})
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        <div className="lg:col-span-1 flex flex-col space-y-4">
          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm">
            <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-muted)] mb-3">Earning Head Modules</h3>
            <div className="space-y-1.5">
              {modules.map((m) => {
                const moduleCount = pendingCounts
                  .filter((p) => p.module === m.name)
                  .reduce((sum, p) => sum + p.count, 0);
                const hasPending = moduleCount > 0;
                return (
                  <button
                    key={m.name}
                    onClick={() => setActiveModule(m.name)}
                    className={`w-full text-left px-3 py-2 rounded-lg text-xs transition-all flex items-center justify-between ${
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
                      <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full min-w-[20px] text-center ${
                        activeModule === m.name
                          ? "bg-white/25 text-white"
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

          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm">
            <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-muted)] mb-3">Employee Home Filter</h3>
            <div className="space-y-2" ref={homeSearchRef}>
              <label className="text-[11px] font-medium text-[var(--text-secondary)] block">Search & Select Employee Home</label>
              <div className="relative">
                <div className="flex">
                  <input
                    type="text"
                    value={homeDropdownOpen ? homeSearch : activeEmployeeHome}
                    onChange={(e) => { setHomeSearch(e.target.value); setHomeDropdownOpen(true); }}
                    onFocus={() => { setHomeDropdownOpen(true); setHomeSearch(""); }}
                    placeholder="🔍 Type to search..."
                    className="flex-1 border border-[var(--border)] border-r-0 rounded-l-lg px-2.5 py-2 text-xs bg-[var(--bg-primary)] focus:outline-none focus:border-[var(--accent-green)] transition"
                  />
                  <button
                    type="button"
                    onClick={() => { setHomeDropdownOpen(!homeDropdownOpen); setHomeSearch(""); }}
                    className="px-2 border border-[var(--border)] rounded-r-lg bg-[var(--bg-primary)] hover:bg-black/5 text-[var(--text-secondary)] transition text-xs"
                  >
                    ▾
                  </button>
                </div>
                {homeDropdownOpen && (
                  <div className="absolute z-20 top-full left-0 right-0 mt-1 bg-[var(--bg-card)] border border-[var(--border)] rounded-lg shadow-lg max-h-48 overflow-y-auto">
                    {homesList
                      .filter((home) => home.toLowerCase().includes(homeSearch.toLowerCase()))
                      .map((home) => {
                        const homeCount = pendingCounts.find(
                          (p) => p.module === activeModule && p.employeeHome === home
                        )?.count || 0;
                        const hasPending = homeCount > 0;
                        return (
                          <button
                            key={home}
                            onClick={() => { setActiveEmployeeHome(home); setHomeDropdownOpen(false); setHomeSearch(""); }}
                            className={`w-full text-left px-3 py-2 text-xs transition-all flex items-center justify-between hover:bg-[var(--accent-green)]/10 ${
                              activeEmployeeHome === home ? "bg-[var(--accent-green)]/10" : ""
                            } ${
                              hasPending ? "font-bold text-[var(--text-primary)]" : "font-medium text-[var(--text-secondary)]"
                            }`}
                          >
                            <span className={hasPending ? "font-bold" : ""}>
                              🏢 {home}{hasPending ? ` (${homeCount})` : ""}
                            </span>
                            {hasPending && (
                              <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-[var(--accent-green)]/15 text-[var(--accent-green)] min-w-[20px] text-center">
                                {homeCount}
                              </span>
                            )}
                          </button>
                        );
                      })}
                    {homesList.filter((home) => home.toLowerCase().includes(homeSearch.toLowerCase())).length === 0 && (
                      <div className="px-3 py-2 text-xs text-[var(--text-muted)] text-center">No matches found</div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* HOD Comments Box */}
          {filteredRows.length > 0 && (
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-4 shadow-sm flex flex-col space-y-4">
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-secondary)] mb-2">Approval Comments</h3>
                <textarea
                  value={hodComments}
                  onChange={(e) => setHodComments(e.target.value)}
                  placeholder="Enter HOD remarks..."
                  className="w-full border border-[var(--border)] rounded-lg p-2.5 text-xs bg-[var(--bg-primary)] h-24 focus:outline-none resize-none"
                />
                <button
                  onClick={saveReview}
                  disabled={saving}
                  className="mt-2 w-full bg-black text-white hover:opacity-90 text-xs font-semibold py-2 rounded-lg transition"
                >
                  {saving ? "Saving..." : "Save Review"}
                </button>
              </div>

              {flaggedColumns.length > 0 && (
                <div className="border border-[var(--accent-red)]/20 bg-[var(--accent-red)]/5 rounded-lg p-3">
                  <span className="text-[10px] font-bold text-[var(--accent-red)] uppercase tracking-wider">⚠️ Flagged by HRBP</span>
                  <div className="flex gap-1.5 flex-wrap mt-1.5">
                    {flaggedColumns.map((col) => (
                      <span key={col} className="bg-[var(--accent-red)] text-white text-[9px] px-1.5 py-0.5 rounded font-bold">
                        {col}
                      </span>
                    ))}
                  </div>
                  {filteredRows[0].hrbpComments && (
                    <p className="text-[11px] text-[var(--accent-red)] mt-2 italic">"{filteredRows[0].hrbpComments}"</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="lg:col-span-3 flex flex-col space-y-6">
          {/* Summary Stat Cards */}
          <PayrollSummaryCards
            totalEntries={filteredRows.length}
            totalAmount={totalAmount}
            averagePayout={averagePayout}
          />

          {activeModule && activeEmployeeHome && (
            <WorkflowTimeline
              status={rows[0]?.status || "HOD"}
              hrbpBypassed={activeHeadObj?.hrbp === "NA"}
              history={rows[0]?.history || []}
            />
          )}

          {filteredRows.length > 0 && filteredRows[0]?.status !== "HOD" && (
            <div className="bg-[var(--accent-blue)]/10 border border-[var(--accent-blue)]/30 rounded-xl p-4 flex items-center justify-between shadow-sm">
              <div className="flex items-center gap-3">
                <span className="text-xl">ℹ️</span>
                <div>
                  <h4 className="text-xs font-bold text-[var(--text-primary)]">
                    You have already approved and forwarded this sheet
                  </h4>
                  <p className="text-[11px] text-[var(--text-secondary)] mt-0.5">
                    Current Progress: <span className="font-bold text-[var(--accent-blue)]">Pending Payroll Ops Finalization</span>
                  </p>
                </div>
              </div>
              <div className="text-xs font-bold bg-[var(--accent-blue)] text-white px-3 py-1.5 rounded-lg shadow-sm">
                Status: {filteredRows[0]?.status}
              </div>
            </div>
          )}

          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden flex flex-col">
            <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)] flex justify-between items-center">
              <div>
                <h2 className="text-sm font-bold text-[var(--text-primary)]">
                  Active HOD Review: {activeModule} — {activeEmployeeHome}
                </h2>
                <p className="text-[11px] text-[var(--text-muted)] mt-0.5">
                  {filteredRows[0]?.status !== "HOD" ? "Sheet approved and locked for Payroll Ops finalization" : "Verify and approve for payroll release"}
                </p>
              </div>
              {filteredRows.length > 0 && filteredRows[0]?.status === "HOD" && (
                <div className="flex space-x-2">
                  <button
                    onClick={returnHRBP}
                    disabled={submitting}
                    className="bg-[var(--accent-red)] text-white hover:bg-[var(--accent-red)]/90 text-xs font-semibold px-3.5 py-1.5 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    🗑️ Reject & Return HRBP
                  </button>
                  <button
                    onClick={submitPayroll}
                    disabled={submitting}
                    className="bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green)]/90 text-xs font-semibold px-3.5 py-1.5 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    🚀 Approve & Release
                  </button>
                </div>
              )}
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
                    </tr>
                  )}
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {filteredRows.length > 0 ? (
                    filteredRows.map((row, index) => {
                      const codeFlagged = flaggedColumns.includes("Employee Code") || flaggedColumns.includes("Emp code");
                      const amountFlagged = flaggedColumns.includes("Amount");
                      const isReferral = row.module === "REFERRAL BONUS";

                      return (
                        <tr key={row.id} className="hover:bg-black/5">
                          <td className="py-3 px-4 text-center font-medium text-[var(--text-muted)]">{index + 1}</td>
                          <td className={`py-3 px-3 font-semibold ${codeFlagged ? "text-[var(--accent-red)] bg-[var(--accent-red)]/5" : ""}`}>
                            {row.empCode}
                          </td>
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
                          <td className={`py-3 px-3 font-bold ${amountFlagged ? "text-[var(--accent-red)] bg-[var(--accent-red)]/5" : ""}`}>
                            ₹ {Number(row.amount).toLocaleString()}
                          </td>
                          <td className="py-3 px-3 text-[var(--text-secondary)] italic">{row.remarks || "No remarks"}</td>
                        </tr>
                      );
                    })
                  ) : (
                    <tr>
                      <td colSpan={activeModule === "REFERRAL BONUS" ? 10 : 8} className="py-8 text-center text-[var(--text-muted)] font-medium">
                        No sheets pending HOD approval for this selection.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
