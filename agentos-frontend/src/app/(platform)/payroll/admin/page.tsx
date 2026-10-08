"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

type RosterUser = {
  name: string;
  email: string;
  role: string;
  allowed_modules?: string[];
};

type RoutingRule = {
  initiator_ids: string[];
  approver: string;
};

type EarningHead = {
  name: string;
  employee_home: string;
  initiators: string[];
  hrbp: string;
  approver: string;
  input_type: string;
  routing_rules: RoutingRule[];
};

type ConfigData = {
  users: RosterUser[];
  earning_heads: EarningHead[];
};

type FormRoutingRule = {
  initiator_ids: string;
  approver: string;
};

type RoutingMatrixRule = {
  earning_head: string;
  employee_home: string;
  initiators: string[];
  hrbps: string[];
  approvers: string[];
};

export default function PayrollAdminPage() {
  const { user, refreshUser } = useAuth();
  const router = useRouter();

  const [activeTab, setActiveTab] = useState<"users" | "earning_heads" | "matrix" | "json" | "audit">("users");
  const [config, setConfig] = useState<ConfigData>({ users: [], earning_heads: [] });
  const [history, setHistory] = useState<any[]>([]);
  const [loadingHistory, setLoadingHistory] = useState<boolean>(false);
  const [routingMatrix, setRoutingMatrix] = useState<RoutingMatrixRule[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  // User Form State
  const [userEmail, setUserEmail] = useState("");
  const [userName, setUserName] = useState("");
  const [userRole, setUserRole] = useState("maker");
  const [userAllowedModules, setUserAllowedModules] = useState("");
  const [editingUserIndex, setEditingUserIndex] = useState<number | null>(null);

  // Earning Head Form State
  const [headName, setHeadName] = useState("");
  const [headInputType, setHeadInputType] = useState("amount");
  const [headEmpHome, setHeadEmpHome] = useState("ALL");
  const [headInitiators, setHeadInitiators] = useState("");
  const [headHrbp, setHeadHrbp] = useState("NA");
  const [headApprover, setHeadApprover] = useState("");
  const [headRoutingRules, setHeadRoutingRules] = useState<FormRoutingRule[]>([]);
  const [editingHeadIndex, setEditingHeadIndex] = useState<number | null>(null);

  // Matrix Form State
  const [matrixEarningHead, setMatrixEarningHead] = useState("");
  const [matrixEmployeeHome, setMatrixEmployeeHome] = useState("");
  const [matrixInitiators, setMatrixInitiators] = useState("");
  const [matrixHrbps, setMatrixHrbps] = useState("");
  const [matrixApprovers, setMatrixApprovers] = useState("");
  const [editingMatrixIndex, setEditingMatrixIndex] = useState<number | null>(null);

  // JSON Raw Editor State
  const [rawJson, setRawJson] = useState("");

  useEffect(() => {
    if (user) {
      const isAdmin = user.roles.some((r) => r.toLowerCase() === "payroll_admin");
      if (!isAdmin) {
        router.replace("/payroll");
        return;
      }
      fetchConfig();
    }
  }, [user]);

  useEffect(() => {
    if (activeTab === "audit") {
      fetchHistory();
    }
  }, [activeTab]);

  const fetchConfig = async () => {
    try {
      const res = await apiFetch("/api/workflow/admin/config");
      if (res.ok) {
        const data = await res.json();
        const rawCfg = data.workflow_config || {};
        const cfg: ConfigData = {
          users: Array.isArray(rawCfg.users) ? rawCfg.users : [],
          earning_heads: Array.isArray(rawCfg.earning_heads) ? rawCfg.earning_heads : [],
          ...rawCfg,
        };
        setConfig(cfg);
        setRoutingMatrix(Array.isArray(data.routing_matrix) ? data.routing_matrix : []);
        setRawJson(JSON.stringify(cfg, null, 2));
      }
    } catch (error) {
      console.error("Failed to fetch admin config:", error);
    } finally {
      setLoading(false);
    }
  };

  const fetchHistory = async () => {
    setLoadingHistory(true);
    try {
      const res = await apiFetch("/api/workflow/history");
      if (res.ok) {
        const data = await res.json();
        // Limit to 50 entries as requested
        setHistory((data || []).slice(0, 50));
      }
    } catch (error) {
      console.error("Failed to fetch history:", error);
    } finally {
      setLoadingHistory(false);
    }
  };

  const handleSaveConfig = async (newConfig: ConfigData, newMatrix?: RoutingMatrixRule[]) => {
    const targetMatrix = newMatrix !== undefined ? newMatrix : routingMatrix;
    try {
      const res = await apiFetch("/api/workflow/admin/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          workflow_config: newConfig,
          routing_matrix: targetMatrix 
        }),
      });
      if (res.ok) {
        setConfig(newConfig);
        setRoutingMatrix(targetMatrix);
        setRawJson(JSON.stringify(newConfig, null, 2));
        alert("Configuration updated successfully!");
        resetUserForm();
        resetHeadForm();
        resetMatrixForm();
        await refreshUser();
      } else {
        alert("Failed to save configuration.");
      }
    } catch (error) {
      console.error("Failed to save config:", error);
    }
  };

  // User Form Methods
  const resetUserForm = () => {
    setUserEmail("");
    setUserName("");
    setUserRole("maker");
    setUserAllowedModules("");
    setEditingUserIndex(null);
  };

  const handleAddOrEditUser = (e: React.FormEvent) => {
    e.preventDefault();
    if (!userEmail || !userName) {
      alert("Please fill all user fields.");
      return;
    }
    const updatedUsers = [...config.users];
    const allowedModulesArray = userAllowedModules
      ? userAllowedModules.split(",").map((m) => m.trim()).filter(Boolean)
      : ["*"];

    const newUserObj: RosterUser = {
      name: userName,
      email: userEmail.trim().toLowerCase(),
      role: userRole.trim().toLowerCase(),
      allowed_modules: allowedModulesArray,
    };

    if (editingUserIndex !== null) {
      updatedUsers[editingUserIndex] = newUserObj;
    } else {
      updatedUsers.push(newUserObj);
    }

    const updatedConfig = { ...config, users: updatedUsers };
    handleSaveConfig(updatedConfig);
  };

  const handleEditUser = (index: number) => {
    const editUser = config.users[index];
    setUserEmail(editUser.email);
    setUserName(editUser.name);
    setUserRole(editUser.role);
    setUserAllowedModules(editUser.allowed_modules ? editUser.allowed_modules.join(", ") : "*");
    setEditingUserIndex(index);
  };

  const handleDeleteUser = (index: number) => {
    if (!confirm("Are you sure you want to delete this user?")) return;
    const updatedUsers = config.users.filter((_, i) => i !== index);
    const updatedConfig = { ...config, users: updatedUsers };
    handleSaveConfig(updatedConfig);
  };

  // Earning Head Form Methods
  const resetHeadForm = () => {
    setHeadName("");
    setHeadInputType("amount");
    setHeadEmpHome("ALL");
    setHeadInitiators("");
    setHeadHrbp("NA");
    setHeadApprover("");
    setHeadRoutingRules([]);
    setEditingHeadIndex(null);
  };

  const handleAddOrEditHead = (e: React.FormEvent) => {
    e.preventDefault();
    if (!headName || !headApprover) {
      alert("Please fill head name and default approver.");
      return;
    }

    const parsedRules = headRoutingRules
      .filter((r) => r.initiator_ids.trim() || r.approver.trim())
      .map((r) => ({
        initiator_ids: r.initiator_ids.split(",").map((s) => s.trim()).filter(Boolean),
        approver: r.approver.trim(),
      }));

    const updatedHeads = [...config.earning_heads];
    const newHeadObj: EarningHead = {
      name: headName.toUpperCase().trim(),
      employee_home: headEmpHome,
      initiators: headInitiators.split(",").map((i) => i.trim()).filter(Boolean),
      hrbp: headHrbp.trim(),
      approver: headApprover.trim(),
      input_type: headInputType,
      routing_rules: parsedRules,
    };

    if (editingHeadIndex !== null) {
      updatedHeads[editingHeadIndex] = newHeadObj;
    } else {
      updatedHeads.push(newHeadObj);
    }

    const updatedConfig = { ...config, earning_heads: updatedHeads };
    handleSaveConfig(updatedConfig);
  };

  const handleEditHead = (index: number) => {
    const editHead = config.earning_heads[index];
    setHeadName(editHead.name);
    setHeadInputType(editHead.input_type || "amount");
    setHeadEmpHome(editHead.employee_home || "ALL");
    setHeadInitiators((editHead.initiators || []).join(", "));
    setHeadHrbp(editHead.hrbp || "NA");
    setHeadApprover(editHead.approver);
    setHeadRoutingRules(
      (editHead.routing_rules || []).map((r) => ({
        initiator_ids: (r.initiator_ids || []).join(", "),
        approver: r.approver || "",
      }))
    );
    setEditingHeadIndex(index);
  };

  const handleDeleteHead = (index: number) => {
    if (!confirm("Are you sure you want to delete this earning head?")) return;
    const updatedHeads = config.earning_heads.filter((_, i) => i !== index);
    const updatedConfig = { ...config, earning_heads: updatedHeads };
    handleSaveConfig(updatedConfig);
  };

  const handleAddRoutingRule = () => {
    setHeadRoutingRules([...headRoutingRules, { initiator_ids: "", approver: "" }]);
  };

  const handleRemoveRoutingRule = (index: number) => {
    setHeadRoutingRules(headRoutingRules.filter((_, i) => i !== index));
  };

  const handleRoutingRuleChange = (index: number, field: keyof FormRoutingRule, value: string) => {
    const updatedRules = [...headRoutingRules];
    updatedRules[index][field] = value;
    setHeadRoutingRules(updatedRules);
  };

  // Matrix Form Methods
  const resetMatrixForm = () => {
    setMatrixEarningHead("");
    setMatrixEmployeeHome("");
    setMatrixInitiators("");
    setMatrixHrbps("");
    setMatrixApprovers("");
    setEditingMatrixIndex(null);
  };

  const handleAddOrEditMatrix = (e: React.FormEvent) => {
    e.preventDefault();
    if (!matrixEarningHead || !matrixEmployeeHome) {
      alert("Please specify Earning Head and Employee Home.");
      return;
    }
    const updatedMatrix = [...routingMatrix];
    const newRule: RoutingMatrixRule = {
      earning_head: matrixEarningHead,
      employee_home: matrixEmployeeHome,
      initiators: matrixInitiators.split(",").map((s) => s.trim()).filter(Boolean),
      hrbps: matrixHrbps.split(",").map((s) => s.trim()).filter(Boolean),
      approvers: matrixApprovers.split(",").map((s) => s.trim()).filter(Boolean),
    };

    if (editingMatrixIndex !== null) {
      updatedMatrix[editingMatrixIndex] = newRule;
    } else {
      updatedMatrix.push(newRule);
    }

    handleSaveConfig(config, updatedMatrix);
  };

  const handleEditMatrix = (index: number) => {
    const rule = routingMatrix[index];
    setMatrixEarningHead(rule.earning_head);
    setMatrixEmployeeHome(rule.employee_home);
    setMatrixInitiators(rule.initiators.join(", "));
    setMatrixHrbps(rule.hrbps.join(", "));
    setMatrixApprovers(rule.approvers.join(", "));
    setEditingMatrixIndex(index);
  };

  const handleDeleteMatrix = (index: number) => {
    if (confirm("Are you sure you want to delete this routing rule?")) {
      const updatedMatrix = routingMatrix.filter((_, i) => i !== index);
      handleSaveConfig(config, updatedMatrix);
    }
  };

  const handleSaveRawJson = () => {
    try {
      const parsed = JSON.parse(rawJson);
      if (!parsed.users || !parsed.earning_heads) {
        alert("JSON must contain 'users' and 'earning_heads' keys.");
        return;
      }
      handleSaveConfig(parsed);
    } catch (e: any) {
      alert("Invalid JSON: " + e.message);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="text-center">
          <div className="w-8 h-8 border-2 border-[var(--accent-green)] border-t-transparent rounded-full animate-spin mx-auto mb-2"></div>
          <p className="text-xs text-[var(--text-muted)]">Loading Admin Configuration...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col space-y-6">
      <div className="flex justify-between items-center bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-[var(--text-primary)]">Payroll Workflow Configuration</h1>
          <p className="text-xs text-[var(--text-muted)] mt-1">Manage global users, heads, and routing matrices</p>
        </div>

        {/* Tab Controls */}
        <div className="flex border border-[var(--border)] rounded-lg p-1 bg-[var(--bg-primary)]">
          <button
            onClick={() => setActiveTab("users")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "users" ? "bg-white text-[var(--text-primary)] shadow-sm" : "text-[var(--text-secondary)]"
            }`}
          >
            👥 User Directory
          </button>
          <button
            onClick={() => setActiveTab("earning_heads")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "earning_heads" ? "bg-white text-[var(--text-primary)] shadow-sm" : "text-[var(--text-secondary)]"
            }`}
          >
            💰 Component Config
          </button>
          <button
            onClick={() => setActiveTab("matrix")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "matrix" ? "bg-white text-[var(--text-primary)] shadow-sm" : "text-[var(--text-secondary)]"
            }`}
          >
            🗺️ Routing Matrix
          </button>
          <button
            onClick={() => setActiveTab("json")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "json" ? "bg-white text-[var(--text-primary)] shadow-sm" : "text-[var(--text-secondary)]"
            }`}
          >
            📄 Raw JSON Editor
          </button>
          <button
            onClick={() => setActiveTab("audit")}
            className={`px-4 py-1.5 rounded-md text-xs font-bold transition ${
              activeTab === "audit" ? "bg-white text-[var(--text-primary)] shadow-sm" : "text-[var(--text-secondary)]"
            }`}
          >
            📜 Audit Logs
          </button>
        </div>
      </div>

      {activeTab === "users" && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* User Form */}
          <div className="lg:col-span-1 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm h-fit">
            <h2 className="text-sm font-bold text-[var(--text-primary)] mb-4">
              {editingUserIndex !== null ? "Edit User Account" : "Register New User"}
            </h2>
            <form onSubmit={handleAddOrEditUser} className="space-y-4">
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Full Name</label>
                <input
                  id="roster-user-name"
                  type="text"
                  value={userName}
                  onChange={(e) => setUserName(e.target.value)}
                  placeholder="John Doe"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                />
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Email Address</label>
                <input
                  id="roster-user-email"
                  type="email"
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                  placeholder="john.doe@1mg.com"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                />
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">System Role</label>
                <select
                  id="roster-user-role"
                  value={userRole}
                  onChange={(e) => setUserRole(e.target.value)}
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                >
                  <option value="maker">Maker</option>
                  <option value="hrbp">HRBP</option>
                  <option value="hod">HOD</option>
                  <option value="payroll">Payroll</option>
                </select>
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">
                  Allowed Modules (comma-separated, "*" for all)
                </label>
                <input
                  id="roster-user-allowed-modules"
                  type="text"
                  value={userAllowedModules}
                  onChange={(e) => setUserAllowedModules(e.target.value)}
                  placeholder="GRATUITY, RETENTION BONUS"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>

              <div className="flex space-x-2 pt-2">
                <button
                  id="roster-user-submit-btn"
                  type="submit"
                  className="flex-1 bg-[var(--accent-green)] hover:bg-[var(--accent-green)]/90 text-white text-xs font-bold py-2 rounded-lg transition"
                >
                  {editingUserIndex !== null ? "Update User" : "Add User"}
                </button>
                {editingUserIndex !== null && (
                  <button
                    id="roster-user-cancel-btn"
                    type="button"
                    onClick={resetUserForm}
                    className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-bold py-2 px-4 rounded-lg transition"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>
          </div>

          {/* Users List */}
          <div className="lg:col-span-2 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden">
            <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)]">
              <h2 className="text-sm font-bold text-[var(--text-primary)]">Configured Roster Users</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                    <th className="py-3 px-4">User</th>
                    <th className="py-3 px-3">Email</th>
                    <th className="py-3 px-3">Role</th>
                    <th className="py-3 px-3">Modules</th>
                    <th className="py-3 px-4 text-center">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {(config.users || []).map((u, index) => (
                    <tr key={`${u.email}-${index}`} className="hover:bg-black/5">
                      <td className="py-3 px-4 font-semibold text-[var(--text-primary)]">{u.name}</td>
                      <td className="py-3 px-3">{u.email}</td>
                      <td className="py-3 px-3">
                        <span className="bg-black/5 px-2 py-0.5 rounded text-[10px] uppercase font-bold text-[var(--text-secondary)]">
                          {u.role}
                        </span>
                      </td>
                      <td className="py-3 px-3 max-w-[150px] truncate">{u.allowed_modules?.join(", ") || "*"}</td>
                      <td className="py-3 px-4 text-center space-x-2">
                        <button
                          id={`edit-roster-user-${index}`}
                          data-testid={`edit-roster-user-${u.email}`}
                          onClick={() => handleEditUser(index)}
                          className="text-[var(--accent-blue)] hover:bg-[var(--accent-blue)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          ✏️ Edit
                        </button>
                        <button
                          id={`delete-roster-user-${index}`}
                          data-testid={`delete-roster-user-${u.email}`}
                          onClick={() => handleDeleteUser(index)}
                          className="text-[var(--accent-red)] hover:bg-[var(--accent-red)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          🗑️ Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {activeTab === "earning_heads" && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Earning Head Form */}
          <div className="lg:col-span-1 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm h-fit">
            <h2 className="text-sm font-bold text-[var(--text-primary)] mb-4">
              {editingHeadIndex !== null ? "Edit Component Config" : "Create New Pay Component"}
            </h2>
            <form onSubmit={handleAddOrEditHead} className="space-y-4">
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Component Name</label>
                <input
                  type="text"
                  value={headName}
                  onChange={(e) => setHeadName(e.target.value)}
                  placeholder="RETENTION BONUS"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Input Schema</label>
                  <select
                    value={headInputType}
                    onChange={(e) => setHeadInputType(e.target.value)}
                    className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  >
                    <option value="amount">Amount Only</option>
                    <option value="overtime">Overtime Hours</option>
                    <option value="holiday">Holiday Details</option>
                  </select>
                </div>
                <div>
                  <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Location Home</label>
                  <select
                    value={headEmpHome}
                    onChange={(e) => setHeadEmpHome(e.target.value)}
                    className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  >
                    <option value="ALL">All Homes</option>
                    <option value="Employee Home Wise">Employee Home Wise</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">
                  Default Initiators (Employee IDs comma-separated)
                </label>
                <input
                  type="text"
                  value={headInitiators}
                  onChange={(e) => setHeadInitiators(e.target.value)}
                  placeholder="10091, 10092"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">HRBP Email / ID</label>
                <input
                  type="text"
                  value={headHrbp}
                  onChange={(e) => setHeadHrbp(e.target.value)}
                  placeholder="hrbp@1mg.com or NA"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Default HOD Approver Email / ID</label>
                <input
                  type="text"
                  value={headApprover}
                  onChange={(e) => setHeadApprover(e.target.value)}
                  placeholder="hod@1mg.com"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                />
              </div>

              {/* Dynamic Routing Matrix Rules */}
              <div className="border-t border-[var(--border)] pt-3">
                <div className="flex justify-between items-center mb-2">
                  <label className="text-[11px] font-bold text-[var(--text-secondary)]">Location Wise Override HODs</label>
                  <button
                    type="button"
                    onClick={handleAddRoutingRule}
                    className="text-[var(--accent-green)] hover:underline text-[10px] font-bold"
                  >
                    + Override Rule
                  </button>
                </div>

                <div className="space-y-2">
                  {headRoutingRules.map((rule, idx) => (
                    <div key={idx} className="flex gap-2 items-center bg-[var(--bg-primary)] p-2 rounded-lg border border-[var(--border)]">
                      <div className="flex-1 space-y-1">
                        <input
                          type="text"
                          value={rule.initiator_ids}
                          onChange={(e) => handleRoutingRuleChange(idx, "initiator_ids", e.target.value)}
                          placeholder="Initiator IDs (e.g. 10091)"
                          className="w-full border border-[var(--border)] rounded px-1.5 py-1 text-[10px]"
                        />
                        <input
                          type="text"
                          value={rule.approver}
                          onChange={(e) => handleRoutingRuleChange(idx, "approver", e.target.value)}
                          placeholder="HOD Email Override"
                          className="w-full border border-[var(--border)] rounded px-1.5 py-1 text-[10px]"
                        />
                      </div>
                      <button
                        type="button"
                        onClick={() => handleRemoveRoutingRule(idx)}
                        className="text-[var(--accent-red)] hover:bg-[var(--accent-red)]/10 p-1 rounded text-xs"
                      >
                        🗑️
                      </button>
                    </div>
                  ))}
                </div>
              </div>

              <div className="flex space-x-2 pt-2">
                <button
                  type="submit"
                  className="flex-1 bg-[var(--accent-green)] hover:bg-[var(--accent-green)]/90 text-white text-xs font-bold py-2 rounded-lg transition"
                >
                  {editingHeadIndex !== null ? "Update Component" : "Create Component"}
                </button>
                {editingHeadIndex !== null && (
                  <button
                    type="button"
                    onClick={resetHeadForm}
                    className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-bold py-2 px-4 rounded-lg transition"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>
          </div>

          {/* Component List */}
          <div className="lg:col-span-2 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden">
            <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)]">
              <h2 className="text-sm font-bold text-[var(--text-primary)]">Active Pay Components</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                    <th className="py-3 px-4">Component</th>
                    <th className="py-3 px-3">Schema</th>
                    <th className="py-3 px-3">Location Home</th>
                    <th className="py-3 px-3">HRBP</th>
                    <th className="py-3 px-3">HOD Approver</th>
                    <th className="py-3 px-3 text-center">Overrides</th>
                    <th className="py-3 px-4 text-center">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {(config.earning_heads || []).map((h, index) => (
                    <tr key={`${h.name}-${index}`} className="hover:bg-black/5">
                      <td className="py-3 px-4 font-semibold text-[var(--text-primary)]">{h.name}</td>
                      <td className="py-3 px-3 font-mono">{h.input_type || "amount"}</td>
                      <td className="py-3 px-3">{h.employee_home}</td>
                      <td className="py-3 px-3">{h.hrbp}</td>
                      <td className="py-3 px-3">{h.approver}</td>
                      <td className="py-3 px-3 text-center font-bold text-[var(--accent-green)]">{h.routing_rules?.length || 0}</td>
                      <td className="py-3 px-4 text-center space-x-2">
                        <button
                          onClick={() => handleEditHead(index)}
                          className="text-[var(--accent-blue)] hover:bg-[var(--accent-blue)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          ✏️ Edit
                        </button>
                        <button
                          onClick={() => handleDeleteHead(index)}
                          className="text-[var(--accent-red)] hover:bg-[var(--accent-red)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          🗑️ Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {activeTab === "matrix" && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Matrix Form */}
          <div className="lg:col-span-1 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm h-fit">
            <h2 className="text-sm font-bold text-[var(--text-primary)] mb-4">
              {editingMatrixIndex !== null ? "Edit Routing Rule" : "Create Matrix Routing Rule"}
            </h2>
            <form onSubmit={handleAddOrEditMatrix} className="space-y-4">
              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Earning Head / Pay Component</label>
                <select
                  value={matrixEarningHead}
                  onChange={(e) => setMatrixEarningHead(e.target.value)}
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                >
                  <option value="">-- Select Component --</option>
                  {config.earning_heads.map((eh) => (
                    <option key={eh.name} value={eh.name}>
                      {eh.name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">Employee Home / Location</label>
                <select
                  value={matrixEmployeeHome}
                  onChange={(e) => setMatrixEmployeeHome(e.target.value)}
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                  required
                >
                  <option value="">-- Select Location --</option>
                  <option value="Clinical Excellence">Clinical Excellence</option>
                  <option value="Diagnostic Supply Chain">Diagnostic Supply Chain</option>
                  <option value="Pharmacy Supply Chain">Pharmacy Supply Chain</option>
                  <option value="Dataverse">Dataverse</option>
                  <option value="Corporate Health & Wellness">Corporate Health & Wellness</option>
                  <option value="Product & Technology_Engineering">Product & Technology_Engineering</option>
                  <option value="Product & Technology_Product-1">Product & Technology_Product-1</option>
                  <option value="Product & Technology_Product-2">Product & Technology_Product-2</option>
                  <option value="Hospitals">Hospitals</option>
                  <option value="Admin & IT">Admin & IT</option>
                  <option value="Finance">Finance</option>
                  <option value="Human Resources">Human Resources</option>
                  <option value="Category Management">Category Management</option>
                  <option value="Customer Experience">Customer Experience</option>
                  <option value="Founder's Office">Founder's Office</option>
                  <option value="B&M Retail">B&M Retail</option>
                  <option value="ALL">ALL (Wildcard)</option>
                </select>
              </div>

              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">
                  Makers / Initiators (comma-separated emails)
                </label>
                <input
                  type="text"
                  value={matrixInitiators}
                  onChange={(e) => setMatrixInitiators(e.target.value)}
                  placeholder="tanya.agrawal@1mg.com, maker@1mg.com"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>

              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">
                  HRBPs (comma-separated emails)
                </label>
                <input
                  type="text"
                  value={matrixHrbps}
                  onChange={(e) => setMatrixHrbps(e.target.value)}
                  placeholder="charvi.sarin@1mg.com, hrbp@1mg.com"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>

              <div>
                <label className="text-[11px] font-semibold text-[var(--text-secondary)] block mb-1">
                  HOD Approvers (comma-separated emails)
                </label>
                <input
                  type="text"
                  value={matrixApprovers}
                  onChange={(e) => setMatrixApprovers(e.target.value)}
                  placeholder="nikhil.doegar@1mg.com, hod@1mg.com"
                  className="w-full border border-[var(--border)] rounded-lg px-3 py-2 text-xs bg-[var(--bg-primary)] outline-none"
                />
              </div>

              <div className="flex space-x-2 pt-2">
                <button
                  type="submit"
                  className="flex-1 bg-[var(--accent-green)] hover:bg-[var(--accent-green)]/90 text-white text-xs font-bold py-2 rounded-lg transition"
                >
                  {editingMatrixIndex !== null ? "Update Rule" : "Create Rule"}
                </button>
                {editingMatrixIndex !== null && (
                  <button
                    type="button"
                    onClick={resetMatrixForm}
                    className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-bold py-2 px-4 rounded-lg transition"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </form>
          </div>

          {/* Matrix Rules List */}
          <div className="lg:col-span-2 bg-[var(--bg-card)] border border-[var(--border)] rounded-xl shadow-sm overflow-hidden">
            <div className="border-b border-[var(--border)] px-5 py-4 bg-[var(--bg-primary)]">
              <h2 className="text-sm font-bold text-[var(--text-primary)]">Active Routing Matrix Mappings</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                    <th className="py-3 px-4">Pay Component</th>
                    <th className="py-3 px-3">Location</th>
                    <th className="py-3 px-3">Makers</th>
                    <th className="py-3 px-3">HRBPs</th>
                    <th className="py-3 px-3">HOD Approvers</th>
                    <th className="py-3 px-4 text-center">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {routingMatrix.map((rule, idx) => (
                    <tr key={idx} className="hover:bg-black/5">
                      <td className="py-3 px-4 font-semibold text-[var(--accent-green)]">{rule.earning_head}</td>
                      <td className="py-3 px-3 font-medium text-[var(--text-primary)]">🏢 {rule.employee_home}</td>
                      <td className="py-3 px-3 max-w-[120px] truncate text-[var(--text-secondary)]" title={rule.initiators.join(", ")}>
                        {rule.initiators.join(", ") || "None"}
                      </td>
                      <td className="py-3 px-3 max-w-[120px] truncate text-[var(--accent-orange)]" title={rule.hrbps.join(", ")}>
                        {rule.hrbps.join(", ") || "None"}
                      </td>
                      <td className="py-3 px-3 max-w-[120px] truncate text-[var(--accent-blue)]" title={rule.approvers.join(", ")}>
                        {rule.approvers.join(", ") || "None"}
                      </td>
                      <td className="py-3 px-4 text-center space-x-2">
                        <button
                          onClick={() => handleEditMatrix(idx)}
                          className="text-[var(--accent-blue)] hover:bg-[var(--accent-blue)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          ✏️ Edit
                        </button>
                        <button
                          onClick={() => handleDeleteMatrix(idx)}
                          className="text-[var(--accent-red)] hover:bg-[var(--accent-red)]/10 px-2 py-1 rounded text-xs font-semibold"
                        >
                          🗑️ Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {activeTab === "json" && (
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm flex flex-col space-y-4">
          <div>
            <h2 className="text-sm font-bold text-[var(--text-primary)]">Raw Configuration Editor</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">Surgical JSON updates. Use caution.</p>
          </div>
          <textarea
            value={rawJson}
            onChange={(e) => setRawJson(e.target.value)}
            className="w-full border border-[var(--border)] rounded-lg p-4 font-mono text-xs bg-[var(--bg-primary)] h-[400px] outline-none"
          />
          <div className="flex space-x-2">
            <button
              onClick={handleSaveRawJson}
              className="bg-[var(--accent-green)] hover:bg-[var(--accent-green)]/90 text-white text-xs font-bold py-2 px-5 rounded-lg transition"
            >
              💾 Save JSON Config
            </button>
            <button
              onClick={fetchConfig}
              className="bg-black/5 hover:bg-black/10 border border-[var(--border)] text-xs font-bold py-2 px-5 rounded-lg transition"
            >
              🔄 Reset changes
            </button>
          </div>
        </div>
      )}

      {activeTab === "audit" && (
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-6 shadow-sm flex flex-col space-y-4">
          <div>
            <h2 className="text-sm font-bold text-[var(--text-primary)]">System Audit Logs (Last 50 Actions)</h2>
            <p className="text-[11px] text-[var(--text-muted)] mt-0.5">Chronological record of all actions across the payroll workflow platform</p>
          </div>
          {loadingHistory ? (
            <div className="text-center py-8 text-xs text-[var(--text-muted)]">Loading audit logs...</div>
          ) : history.length === 0 ? (
            <div className="text-center py-8 text-xs text-[var(--text-muted)]">No audit entries recorded yet.</div>
          ) : (
            <div className="overflow-x-auto border border-[var(--border)] rounded-lg">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="bg-[var(--bg-primary)] border-b border-[var(--border)] text-[var(--text-muted)] font-bold">
                    <th className="py-3 px-4 w-16 text-center">ID</th>
                    <th className="py-3 px-4">Action</th>
                    <th className="py-3 px-4">User</th>
                    <th className="py-3 px-4">Timestamp</th>
                    <th className="py-3 px-4">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((entry) => (
                    <tr key={entry.id} className="border-b border-[var(--border)] hover:bg-black/[0.01]">
                      <td className="py-3 px-4 text-center text-[var(--text-muted)] font-mono">{entry.id}</td>
                      <td className="py-3 px-4">
                        <span className="bg-black/5 px-2 py-0.5 rounded text-[var(--text-primary)] font-semibold">
                          {entry.action}
                        </span>
                      </td>
                      <td className="py-3 px-4 font-medium">{entry.user}</td>
                      <td className="py-3 px-4 text-[var(--text-muted)] font-mono">{entry.timestamp}</td>
                      <td className="py-3 px-4 italic text-[var(--text-secondary)]">{entry.details || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
