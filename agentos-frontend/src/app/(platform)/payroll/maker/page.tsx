"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import * as XLSX from "@e965/xlsx";
import type { EarningHead, PayrollRow, AuditItem } from "../types";
import WorkflowTimeline from "../WorkflowTimeline";

import PayrollSummaryCards from "../PayrollSummaryCards";

type NotificationItem = {
  id: number;
  text: string;
  timestamp: string;
  isRead: number;
};

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
};

export default function MakerPage() {
  const { user, refreshUser } = useAuth();
  const router = useRouter();

  const [modules, setModules] = useState<EarningHead[]>([]);
  const [rows, setRows] = useState<PayrollRow[]>([]);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [activeModule, setActiveModule] = useState<string>("");
  const [activeEmployeeHome, setActiveEmployeeHome] = useState<string>("");
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [showErrorPopup, setShowErrorPopup] = useState<boolean>(false);
  const [pendingCounts, setPendingCounts] = useState<{module: string; employeeHome: string; count: number}[]>([]);
  const [homeSearch, setHomeSearch] = useState<string>("");
  const [homeDropdownOpen, setHomeDropdownOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"active" | "submitted">("active");
  const [submittedData, setSubmittedData] = useState<ClosedData>({ months: [] });
  const [selectedSubmittedMonth, setSelectedSubmittedMonth] = useState<string | null>(null);
  const [expandedSubmittedModule, setExpandedSubmittedModule] = useState<string | null>(null);
  const homeSearchRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    refreshUser();
  }, [refreshUser]);

  useEffect(() => {
    if (user) {
      const isMaker = user.roles.some((r) => r.toLowerCase() === "maker" || r.toLowerCase() === "payroll_admin");
      if (!isMaker) {
        router.replace("/payroll");
        return;
      }
      fetchConfig();
      fetchNotifications();
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
      if (activeModule.toUpperCase() === "RETENTION BONUS") {
        setActiveEmployeeHome("ALL");
        return;
      }
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

  const fetchSubmittedData = async () => {
    try {
      const res = await apiFetch("/api/workflow/closed?role=maker&page=1&limit=100");
      if (res.ok) {
        const data = (await res.json()) as ClosedData;
        setSubmittedData(data);
        if (data.months && data.months.length > 0 && !selectedSubmittedMonth) {
          setSelectedSubmittedMonth(data.months[0].monthKey);
        }
      }
    } catch (error) {
      console.error("Failed to fetch submitted sheets:", error);
    }
  };

  useEffect(() => {
    if (user && activeTab === "submitted") {
      fetchSubmittedData();
    }
  }, [user, activeTab]);

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
        setPendingCounts(data.maker || []);
      }
    } catch (error) {
      console.error("Failed to fetch pending counts:", error);
    }
  };

  const fetchNotifications = async () => {
    try {
      const res = await apiFetch("/api/workflow/notifications");
      if (res.ok) {
        const data = await res.json();
        setNotifications(data || []);
      }
    } catch (error) {
      console.error("Failed to fetch notifications:", error);
    }
  };

  const handleMarkNotificationsRead = async () => {
    try {
      await apiFetch("/api/workflow/notifications/mark-read", { method: "POST" });
      fetchNotifications();
    } catch (error) {
      console.error(error);
    }
  };

  const fetchPayrollData = async (moduleName: string, homeName: string) => {
    const mod = moduleName || activeModule;
    const home = homeName || activeEmployeeHome;
    if (!mod || !home) return;
    try {
      const res = await apiFetch(`/api/workflow/maker?module=${encodeURIComponent(mod)}&employeeHome=${encodeURIComponent(home)}`);
      if (res.ok) {
        const data = await res.json();
        setRows(data || []);
      }
    } catch (error) {
      console.error(error);
    }
  };

  const saveSheet = async () => {
    if (!activeModule || !activeEmployeeHome) return;
    setSaving(true);
    try {
      const res = await apiFetch(
        `/api/workflow/save-maker?module=${encodeURIComponent(activeModule)}&employeeHome=${encodeURIComponent(activeEmployeeHome)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(rows),
        }
      );
      if (res.ok) {
        alert("Draft saved successfully.");
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to save draft.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setSaving(false);
    }
  };

  const discardDraft = async () => {
    if (!activeModule || !activeEmployeeHome) return;
    if (!confirm("Are you sure you want to discard this draft? This will clear all rows from the active sheet in the UI and delete any saved drafts from the system.")) {
      return;
    }
    setSaving(true);
    try {
      const res = await apiFetch(
        `/api/workflow/save-maker?module=${encodeURIComponent(activeModule)}&employeeHome=${encodeURIComponent(activeEmployeeHome)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify([]),
        }
      );
      if (res.ok) {
        setRows([]);
        alert("Draft discarded successfully.");
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to discard draft.");
      }
    } catch (error) {
      console.error("Failed to discard draft:", error);
      alert("An error occurred while discarding the draft.");
    } finally {
      setSaving(false);
    }
  };

  const submitSheet = async () => {
    if (!activeModule || !activeEmployeeHome) return;

    // Check Referral Bonus mismatches
    const referralRows = rows.filter(r => r.module === "REFERRAL BONUS");
    const hasMismatch = referralRows.some(r => {
      const num = parseInt(r.overtimeHours) || 0;
      const amt = parseInt(r.amount) || 0;
      return amt !== num * 4000;
    });

    if (hasMismatch) {
      setShowErrorPopup(true);
      return;
    }

    if (!confirm("Are you sure you want to submit this sheet to HRBP/HOD review?")) return;
    setSubmitting(true);
    try {
      const res = await apiFetch(
        `/api/workflow/submit-hrbp?module=${encodeURIComponent(activeModule)}&employeeHome=${encodeURIComponent(activeEmployeeHome)}`,
        { method: "POST" }
      );
      if (res.ok) {
        alert("Payroll Sheet successfully submitted.");
        await fetchPayrollData(activeModule, activeEmployeeHome);
        fetchNotifications();
      } else {
        const err = await res.json();
        alert(err.detail || "Failed to submit sheet.");
      }
    } catch (error) {
      console.error(error);
    } finally {
      setSubmitting(false);
    }
  };

  const handleChange = (rowId: number, field: keyof PayrollRow, value: any) => {
    const updated = rows.map((r) => {
      if (r.id === rowId) {
        const updatedRow = { ...r, [field]: value };
        if (r.module === "REFERRAL BONUS" && field === "overtimeHours") {
          const num = parseInt(value) || 0;
          updatedRow.amount = (num * 4000).toString();
        }
        return updatedRow;
      }
      return r;
    });
    setRows(updated);
  };

  const addRow = () => {
    if (!activeModule || !activeEmployeeHome) return;
    const newRow: PayrollRow = {
      id: Date.now(),
      empCode: "",
      empName: "",
      grade: "",
      designation: "",
      effectiveFrom: "",
      effectiveTo: "",
      employeeHome: activeEmployeeHome,
      type: activeModule,
      module: activeModule,
      amount: "",
      overtimeHours: "",
      holidayDate: "",
      remarks: "",
      status: "MAKER",
      history: [],
      flaggedColumns: [],
    };
    setRows([...rows, newRow]);
  };

  const deleteRow = (rowId: number) => {
    setRows(rows.filter((r) => r.id !== rowId));
  };

  const downloadTemplate = () => {
    let headers: string[];
    if (activeModule === "REFERRAL BONUS") {
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
        "Remarks (if any)"
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
        "Remarks (if any)"
      ];
    }

    const worksheet = XLSX.utils.json_to_sheet([], { header: headers });
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "Template");
    XLSX.writeFile(workbook, "payroll_template.xlsx");
  };

  const normalizeDate = (dateVal: any) => {
    if (!dateVal) return "";
    if (typeof dateVal === "number") {
      const date = new Date((dateVal - 25569) * 86400 * 1000);
      if (!isNaN(date.getTime())) {
        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, "0");
        const dd = String(date.getDate()).padStart(2, "0");
        return `${yyyy}-${mm}-${dd}`;
      }
    }
    const str = dateVal.toString().trim();
    if (!str) return "";

    // Support DD-MM-YYYY, DD/MM/YYYY, and DD.MM.YYYY formats specifically
    // to prevent JavaScript from swapping day/month or failing
    const dmyRegex = /^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{4})$/;
    const dmyMatch = str.match(dmyRegex);
    if (dmyMatch) {
      const dd = dmyMatch[1].padStart(2, "0");
      const mm = dmyMatch[2].padStart(2, "0");
      const yyyy = dmyMatch[3];
      return `${yyyy}-${mm}-${dd}`;
    }

    const d = new Date(str);
    if (!isNaN(d.getTime())) {
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      return `${yyyy}-${mm}-${dd}`;
    }
    return str;
  };

  const uploadTemplate = () => {
    if (!selectedFile) {
      alert("Please select a file to upload.");
      return;
    }

    const reader = new FileReader();
    reader.onload = (e: any) => {
      const data = new Uint8Array(e.target.result);
      const workbook = XLSX.read(data, { type: "array" });
      const sheetName = workbook.SheetNames[0];
      const worksheet = workbook.Sheets[sheetName];
      const jsonData = XLSX.utils.sheet_to_json(worksheet) as any[];

      const getRowValue = (row: any, ...keys: string[]) => {
        const normalizedRow: any = {};
        for (const k of Object.keys(row)) {
          const cleanKey = k.toLowerCase().replace(/[\s\-_"'\uFEFF\u200B]/g, "").trim();
          normalizedRow[cleanKey] = row[k];
        }
        for (const key of keys) {
          const cleanKey = key.toLowerCase().replace(/[\s\-_"']/g, "").trim();
          if (normalizedRow[cleanKey] !== undefined && normalizedRow[cleanKey] !== null) {
            return normalizedRow[cleanKey];
          }
        }
        return "";
      };

      const formattedRows = jsonData.map((row: any, index: number) => {
        const empCode = (getRowValue(row, "Emp code", "Employee Code", "ecode", "empCode") ?? "").toString().trim();
        const empName = (getRowValue(row, "Emp name", "Employee Name", "name", "empName") ?? "").toString().trim();
        const grade = (getRowValue(row, "Grade", "grade") ?? "").toString().trim();
        const designation = (getRowValue(row, "Designation", "designation") ?? "").toString().trim();
        const employeeHome = (getRowValue(row, "Employee home", "Home", "employeeHome") ?? "").toString().trim() || activeEmployeeHome;
        const remarks = (getRowValue(row, "Remarks (if any)", "Remarks", "remarks") ?? "").toString().trim();

        let overtimeHours = "";
        let holidayDate = "";
        let amount = "";

        const cleanAmount = (raw: any): string => {
          if (raw === undefined || raw === null) return "";
          const s = raw.toString().trim();
          if (!s) return "";
          const n = Number(s);
          return isNaN(n) ? s : Math.round(n).toString();
        };

        if (activeModule === "REFERRAL BONUS") {
          overtimeHours = (getRowValue(row, "No. of referred emp.", "noOfReferredEmp", "overtimeHours") ?? "").toString().trim();
          holidayDate = (getRowValue(row, "Emp code & name of referred employees", "referredEmployees", "holidayDate") ?? "").toString().trim();
          
          const rawAmount = getRowValue(row, "Amount", "amount");
          if (rawAmount !== undefined && rawAmount !== null && rawAmount.toString().trim() !== "") {
            amount = cleanAmount(rawAmount);
          } else {
            const num = parseInt(overtimeHours) || 0;
            amount = (num * 4000).toString();
          }
        } else {
          const rawAmount = getRowValue(row, "Amount", "amount");
          amount = cleanAmount(rawAmount);
        }

        return {
          id: Date.now() + index,
          empCode,
          empName,
          grade,
          designation,
          effectiveFrom: "",
          effectiveTo: "",
          employeeHome,
          type: activeModule,
          module: activeModule,
          amount,
          overtimeHours,
          remarks,
          holidayDate,
          status: "MAKER",
          flaggedColumns: [],
          history: [],
        };
      });

      setRows(formattedRows);

      const hasMismatch = formattedRows.some(r => {
        const num = parseInt(r.overtimeHours) || 0;
        const amt = parseInt(r.amount) || 0;
        return r.module === "REFERRAL BONUS" && amt !== num * 4000;
      });

      if (hasMismatch) {
        setShowErrorPopup(true);
      } else {
        alert("Template Uploaded Successfully");
      }
    };

    reader.readAsArrayBuffer(selectedFile);
  };

  const filteredRows = rows.filter((row) => row.module === activeModule);

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

  const returnedHRBPComment = filteredRows.find(r => r.hrbpComments)?.hrbpComments;
  const returnedHODComment = filteredRows.find(r => r.hodComments)?.hodComments;
  const activeFlags = filteredRows.find(r => r.flaggedColumns && r.flaggedColumns.length > 0)?.flaggedColumns;
  const isSubmitted = filteredRows.length > 0 && filteredRows.some(r => r.status && r.status !== "MAKER");

  return (
    <div className="flex flex-col space-y-6">
      {/* Upper header action section */}
      <div className="flex justify-between items-center bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">Payroll Maker Dashboard</h1>
          <p className="text-xs text-[var(--text-muted)] mt-1">
            Logged in as Maker: <span className="font-medium">{user?.full_name}</span> ({user?.email})
          </p>
        </div>
        <div className="flex items-center space-x-3">
          <button
            onClick={downloadTemplate}
            className="px-3 py-1.5 rounded-lg border border-[var(--border)] text-xs font-semibold hover:bg-black/5 transition flex items-center gap-1.5"
          >
            📥 Download Template
          </button>
          <div className="flex items-center border border-[var(--border)] rounded-lg px-2 py-1 bg-[var(--bg-primary)]">
            <input
              type="file"
              accept=".xlsx, .xls, .csv"
              onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
              className="text-xs max-w-[150px] outline-none cursor-pointer"
            />
            <button
              onClick={uploadTemplate}
              className="bg-[var(--accent-green)] hover:bg-[var(--accent-green)]/90 text-white px-2.5 py-1 rounded-md text-[11px] font-semibold transition"
            >
              Upload
            </button>
          </div>
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="flex justify-between items-center bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-2 shadow-sm">
        <div className="flex space-x-1">
          <button
            onClick={() => setActiveTab("active")}
            className={`px-4 py-1.5 rounded-lg text-xs font-bold transition ${
              activeTab === "active"
                ? "bg-[var(--accent-green)] text-white shadow-sm"
                : "text-[var(--text-secondary)] hover:bg-black/5"
            }`}
          >
            ✏️ Active Drafts ({filteredRows.length})
          </button>
          <button
            onClick={() => setActiveTab("submitted")}
            className={`px-4 py-1.5 rounded-lg text-xs font-bold transition ${
              activeTab === "submitted"
                ? "bg-[var(--accent-green)] text-white shadow-sm"
                : "text-[var(--text-secondary)] hover:bg-black/5"
            }`}
          >
            📁 Submitted & Closed Sheets
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Modules secondary sidebar */}
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
            {activeModule.toUpperCase() === "RETENTION BONUS" ? null : (
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
                          return (
                            <button
                              key={home}
                              type="button"
                              onClick={() => {
                                setActiveEmployeeHome(home);
                                setHomeDropdownOpen(false);
                              }}
                              className={`w-full text-left px-3 py-2 text-xs hover:bg-black/5 flex justify-between items-center ${
                                activeEmployeeHome === home ? "font-bold text-[var(--accent-green)]" : "text-[var(--text-primary)]"
                              }`}
                            >
                              <span className={homeCount > 0 ? "font-bold text-[var(--text-primary)]" : ""}>
                                {home}{homeCount > 0 ? ` (${homeCount})` : ""}
                              </span>
                              {homeCount > 0 && (
                                <span className="bg-[var(--accent-green)]/15 text-[var(--accent-green)] text-[10px] font-bold px-1.5 py-0.5 rounded-full">
                                  {homeCount}
                                </span>
                              )}
                            </button>
                          );
                        })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Form and queue table */}
        <div className="lg:col-span-3 flex flex-col space-y-6">
          {activeTab === "submitted" ? (
            <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm space-y-6">
              <div>
                <h2 className="text-base font-bold text-[var(--text-primary)]">Submitted & Processed Sheets</h2>
                <p className="text-xs text-[var(--text-muted)] mt-0.5">Track all input sheets submitted by you across approval stages and closed months.</p>
              </div>

              <div className="flex space-x-2 border-b border-[var(--border)] pb-3 overflow-x-auto">
                {submittedData.months.map((m) => (
                  <button
                    key={m.monthKey}
                    onClick={() => setSelectedSubmittedMonth(m.monthKey)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition ${
                      selectedSubmittedMonth === m.monthKey
                        ? "bg-[var(--accent-green)] text-white font-bold"
                        : "bg-black/5 hover:bg-black/10 text-[var(--text-secondary)]"
                    }`}
                  >
                    📅 {m.month} ({m.modules.reduce((sum, mod) => sum + mod.rowCount, 0)} entries)
                  </button>
                ))}
                {submittedData.months.length === 0 && (
                  <p className="text-xs text-[var(--text-muted)]">No submitted or closed sheets found.</p>
                )}
              </div>

              {selectedSubmittedMonth && (() => {
                const activeMonthData = submittedData.months.find((m) => m.monthKey === selectedSubmittedMonth);
                if (!activeMonthData || activeMonthData.modules.length === 0) {
                  return <p className="text-xs text-[var(--text-muted)] py-6 text-center">No submitted sheets for this month.</p>;
                }
                return (
                  <div className="space-y-4">
                    {activeMonthData.modules.map((mod) => {
                      const isExpanded = expandedSubmittedModule === mod.moduleName;
                      const latestRow = mod.rows[0];
                      return (
                        <div key={mod.moduleName} className="border border-[var(--border)] rounded-xl p-4 bg-[var(--bg-primary)] space-y-4">
                          <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
                            <div>
                              <div className="flex items-center gap-2">
                                <span className="font-bold text-sm text-[var(--text-primary)]">💰 {mod.moduleName}</span>
                                <span className="bg-black/5 px-2 py-0.5 rounded text-[11px] font-semibold text-[var(--text-secondary)]">
                                  {mod.rowCount} rows
                                </span>
                                <span className="bg-[var(--accent-blue)]/10 text-[var(--accent-blue)] px-2 py-0.5 rounded text-[10px] font-bold uppercase">
                                  Status: {latestRow?.status || "SUBMITTED"}
                                </span>
                              </div>
                              <p className="text-[11px] text-[var(--text-muted)] mt-1">
                                Employee Homes: {mod.employeeHomes.join(", ") || "ALL"}
                              </p>
                            </div>
                            <button
                              onClick={() => setExpandedSubmittedModule(isExpanded ? null : mod.moduleName)}
                              className="px-3 py-1.5 bg-white border border-[var(--border)] text-xs font-semibold rounded-lg hover:bg-black/5 transition"
                            >
                              {isExpanded ? "Hide Details & Flow" : "View Details & Flow"}
                            </button>
                          </div>

                          {isExpanded && (
                            <div className="pt-2 border-t border-[var(--border)] space-y-4">
                              <WorkflowTimeline
                                status={latestRow?.status || "MAKER"}
                                history={latestRow?.history || []}
                              />
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
            </div>
          ) : (
            <>
          {(returnedHRBPComment || returnedHODComment || (activeFlags && activeFlags.length > 0)) && (
            <div className="bg-[var(--accent-red)]/10 border border-[var(--accent-red)]/20 rounded-xl p-5 text-xs shadow-sm">
              <h3 className="font-bold text-[var(--accent-red)] mb-2 flex items-center gap-1.5 text-sm">
                ⚠️ Reviewer Feedback (Sheet Returned for Correction)
              </h3>
              <div className="space-y-2 text-[var(--text-secondary)]">
                {returnedHRBPComment && (
                  <p>
                    <strong className="text-[var(--text-primary)]">HRBP Remarks:</strong> "{returnedHRBPComment}"
                  </p>
                )}
                {returnedHODComment && (
                  <p>
                    <strong className="text-[var(--text-primary)]">HOD Remarks:</strong> "{returnedHODComment}"
                  </p>
                )}
                {activeFlags && activeFlags.length > 0 && (
                  <div className="flex items-center gap-2">
                    <strong className="text-[var(--text-primary)]">Flagged Columns to Verify:</strong>
                    <div className="flex flex-wrap gap-1.5">
                      {activeFlags.map((col) => (
                        <span key={col} className="bg-[var(--accent-red)]/15 text-[var(--accent-red)] px-2 py-0.5 rounded text-[10px] font-semibold uppercase">
                          {col}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {activeModule && activeEmployeeHome && (
            <WorkflowTimeline
              status={rows[0]?.status || "MAKER"}
              hrbpBypassed={activeHeadObj?.hrbp === "NA"}
              history={rows[0]?.history || []}
            />
          )}

          {isSubmitted && (
            <div className="bg-[var(--accent-blue)]/10 border border-[var(--accent-blue)]/30 rounded-xl p-4 flex items-center justify-between shadow-sm">
              <div className="flex items-center gap-3">
                <span className="text-xl">ℹ️</span>
                <div>
                  <h4 className="text-xs font-bold text-[var(--text-primary)]">
                    You have already submitted this sheet for review
                  </h4>
                  <p className="text-[11px] text-[var(--text-secondary)] mt-0.5">
                    Current Progress: <span className="font-bold text-[var(--accent-blue)]">{filteredRows[0]?.status === "HRBP" ? "Pending HRBP Review" : filteredRows[0]?.status === "HOD" ? "Pending HOD Approval" : "Pending Payroll Ops Finalization"}</span>
                  </p>
                </div>
              </div>
              <div className="text-xs font-bold bg-[var(--accent-blue)] text-white px-3 py-1.5 rounded-lg shadow-sm">
                Status: {filteredRows[0]?.status}
              </div>
            </div>
          )}

          <PayrollSummaryCards
            totalEntries={filteredRows.length}
            totalAmount={filteredRows.reduce((sum, r) => sum + (parseInt(r.amount) || 0), 0)}
            averagePayout={filteredRows.length > 0 ? Math.round(filteredRows.reduce((sum, r) => sum + (parseInt(r.amount) || 0), 0) / filteredRows.length) : 0}
          />

          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden flex flex-col">
            <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)] flex justify-between items-center">
              <div>
                <h2 className="text-sm font-bold text-[var(--text-primary)]">
                  Active Sheet: {activeModule} — {activeEmployeeHome}
                </h2>
                <p className="text-[11px] text-[var(--text-muted)] mt-0.5">
                  {isSubmitted ? "Sheet submitted and locked for review" : "Manage rows and submit for verification"}
                </p>
              </div>
              <div className="flex space-x-2">
                {!isSubmitted && (
                  <button
                    onClick={addRow}
                    className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-semibold px-3 py-1.5 rounded-lg transition"
                  >
                    ➕ Add Row
                  </button>
                )}
                <button
                  onClick={saveSheet}
                  disabled={saving || isSubmitted}
                  className="bg-[var(--accent-blue)] text-white hover:bg-[var(--accent-blue)]/90 text-xs font-semibold px-3 py-1.5 rounded-lg transition disabled:opacity-50"
                >
                  {saving ? "Saving..." : "💾 Save Draft"}
                </button>
                {!isSubmitted && (
                  <button
                    onClick={discardDraft}
                    disabled={saving || submitting || rows.length === 0}
                    className="bg-[var(--accent-red)] text-white hover:bg-[var(--accent-red)]/90 text-xs font-semibold px-3 py-1.5 rounded-lg transition disabled:opacity-50"
                  >
                    🗑️ Discard Draft
                  </button>
                )}
                <button
                  onClick={submitSheet}
                  disabled={submitting || isSubmitted}
                  className="bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green)]/90 text-xs font-semibold px-3.5 py-1.5 rounded-lg transition disabled:opacity-50"
                >
                  {isSubmitted ? "Submitted" : submitting ? "Submitting..." : "📤 Submit Sheet"}
                </button>
              </div>
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
                      <th className="py-3 px-3 text-center">Action</th>
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
                      <th className="py-3 px-3 text-center">Action</th>
                    </tr>
                  )}
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {filteredRows.length > 0 ? (
                    filteredRows.map((row, index) => {
                      const isReferral = row.module === "REFERRAL BONUS";
                      const num = parseInt(row.overtimeHours) || 0;
                      const amt = parseInt(row.amount) || 0;
                      const isMismatched = isReferral && (amt !== num * 4000);

                      return (
                        <tr key={row.id} className="hover:bg-black/5">
                          <td className="py-2.5 px-4 text-center font-medium text-[var(--text-muted)]">{index + 1}</td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.empCode || ""}
                              onChange={(e) => handleChange(row.id, "empCode", e.target.value)}
                              placeholder="Emp code"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-28 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.empName || ""}
                              onChange={(e) => handleChange(row.id, "empName", e.target.value)}
                              placeholder="Emp name"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-36 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.grade || ""}
                              onChange={(e) => handleChange(row.id, "grade", e.target.value)}
                              placeholder="Grade"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-24 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.designation || ""}
                              onChange={(e) => handleChange(row.id, "designation", e.target.value)}
                              placeholder="Designation"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-36 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.employeeHome || ""}
                              onChange={(e) => handleChange(row.id, "employeeHome", e.target.value)}
                              placeholder="Employee home"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-36 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          {isReferral && (
                            <>
                              <td className="py-2.5 px-2">
                                <input
                                  type="text"
                                  disabled={isSubmitted}
                                  value={row.overtimeHours || ""}
                                  onChange={(e) => handleChange(row.id, "overtimeHours", e.target.value)}
                                  placeholder="No. of referred emp."
                                  className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-36 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                                />
                              </td>
                              <td className="py-2.5 px-2">
                                <input
                                  type="text"
                                  disabled={isSubmitted}
                                  value={row.holidayDate || ""}
                                  onChange={(e) => handleChange(row.id, "holidayDate", e.target.value)}
                                  placeholder="Emp code & name of referred employees"
                                  className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-56 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                                />
                              </td>
                            </>
                          )}
                          <td className="py-2.5 px-2">
                            <div className="flex flex-col">
                              <input
                                type="number"
                                disabled={isSubmitted}
                                value={row.amount || ""}
                                onChange={(e) => handleChange(row.id, "amount", e.target.value)}
                                placeholder="Amount"
                                className={`border ${isMismatched ? "border-[var(--accent-red)] bg-[var(--accent-red)]/5 text-[var(--accent-red)] focus:border-[var(--accent-red)] text-[var(--accent-red)]" : "border-[var(--border)] focus:border-[var(--accent-green)] text-[var(--text-primary)]"} rounded px-1.5 py-1 text-xs w-24 bg-transparent outline-none disabled:opacity-75`}
                              />
                              {isMismatched && (
                                <span className="text-[10px] text-[var(--accent-red)] font-semibold mt-1">
                                  ⚠️ Mismatch (Expected: {num * 4000})
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="py-2.5 px-2">
                            <input
                              type="text"
                              disabled={isSubmitted}
                              value={row.remarks || ""}
                              onChange={(e) => handleChange(row.id, "remarks", e.target.value)}
                              placeholder="Remarks (if any)"
                              className="border border-[var(--border)] rounded px-1.5 py-1 text-xs w-48 bg-transparent outline-none focus:border-[var(--accent-green)] text-[var(--text-primary)] disabled:opacity-75"
                            />
                          </td>
                          <td className="py-2.5 px-2 text-center">
                            {!isSubmitted && (
                              <button
                                onClick={() => deleteRow(row.id)}
                                className="text-[var(--accent-red)] hover:bg-[var(--accent-red)]/10 px-2 py-1 rounded transition text-xs font-semibold"
                              >
                                🗑️ Delete
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })
                  ) : (
                    <tr>
                      <td colSpan={activeModule === "REFERRAL BONUS" ? 11 : 9} className="py-8 text-center text-[var(--text-muted)] font-medium">
                        No rows added. Click "+ Add Row" or upload an Excel sheet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
        </div>
      </div>

      {showErrorPopup && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 animate-fadeIn">
          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-2xl shadow-xl max-w-md w-full p-6 mx-4 transform scale-100 transition-all">
            <div className="flex items-center gap-3 text-[var(--accent-red)] mb-4">
              <span className="text-2xl">⚠️</span>
              <h3 className="text-base font-bold">Referral Bonus Mismatch</h3>
            </div>
            <p className="text-xs text-[var(--text-secondary)] leading-relaxed mb-6">
              One or more rows have been flagged because the entered <strong>Amount</strong> does not match the formula: <strong>No. of referred emp. * 4000</strong>.
              <br/><br/>
              Please correct the amount or number of referred employees in the table before submitting the sheet.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => {
                  const updated = rows.map(r => {
                    if (r.module === "REFERRAL BONUS") {
                      const num = parseInt(r.overtimeHours) || 0;
                      return { ...r, amount: (num * 4000).toString() };
                    }
                    return r;
                  });
                  setRows(updated);
                  setShowErrorPopup(false);
                }}
                className="bg-[var(--accent-green)] text-white hover:bg-[var(--accent-green)]/90 text-xs font-semibold px-4 py-2 rounded-lg transition"
              >
                Auto-Fix All Amounts
              </button>
              <button
                onClick={() => setShowErrorPopup(false)}
                className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-semibold px-4 py-2 rounded-lg transition"
              >
                Close & Review
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
