"use client";

import React from "react";

interface WorkflowTimelineProps {
  status?: string;
  hrbpBypassed?: boolean;
  history?: any[];
}

export default function WorkflowTimeline({
  status = "MAKER",
  hrbpBypassed: propHrbpBypassed = false,
  history = [],
}: WorkflowTimelineProps) {
  // Define helper to find history logs for specific action states
  const findHistoryLog = (actionPatterns: string[]) => {
    if (!history || history.length === 0) return null;
    // Iterate from newest to oldest to get the latest transition for that step
    for (let i = history.length - 1; i >= 0; i--) {
      const log = history[i];
      const actionName = (log.action || "").toLowerCase();
      if (actionPatterns.some((p) => actionName.includes(p.toLowerCase()))) {
        return log;
      }
    }
    return null;
  };

  // Determine if HRBP review is bypassed for this sheet
  let hrbpBypassed = false;
  
  if (propHrbpBypassed) {
    hrbpBypassed = true;
  } else {
    // Auto-detect based on history:
    // If the sheet has reached HOD/PAYROLL/CLOSED, but there is no HRBP action in history,
    // we can deduce that the HRBP review was bypassed for this sheet's config.
    const hasHrbpHistory = history.some(log => {
      const action = (log.action || "").toLowerCase();
      return action.includes("hrbp");
    });
    
    if (["HOD", "PAYROLL", "CLOSED"].includes(status) && !hasHrbpHistory) {
      hrbpBypassed = true;
    }
  }

  // Define steps
  interface Step {
    id: string;
    label: string;
    description: string;
    isActive: boolean;
    isCompleted: boolean;
    actor?: string;
    timestamp?: string;
  }

  const steps: Step[] = [];

  // 1. Maker Step
  const makerLog = findHistoryLog(["submitted", "saved", "draft"]);
  steps.push({
    id: "maker",
    label: "Maker Submit",
    description: "Sheet initiated",
    isActive: status === "MAKER",
    isCompleted: status !== "MAKER",
    actor: makerLog?.user,
    timestamp: makerLog?.timestamp,
  });

  // 2. HRBP Step (Conditional)
  if (!hrbpBypassed) {
    const hrbpLog = findHistoryLog(["approved by hrbp", "returned to maker by hrbp"]);
    steps.push({
      id: "hrbp",
      label: "HRBP Review",
      description: "HR business partner check",
      isActive: status === "HRBP",
      isCompleted: ["HOD", "PAYROLL", "CLOSED"].includes(status),
      actor: hrbpLog?.user,
      timestamp: hrbpLog?.timestamp,
    });
  }

  // 3. HOD Step
  const hodLog = findHistoryLog(["approved by hod", "returned to hrbp by hod", "returned to maker by hod"]);
  steps.push({
    id: "hod",
    label: "HOD Approval",
    description: "Head of department sign-off",
    isActive: status === "HOD",
    isCompleted: ["PAYROLL", "CLOSED"].includes(status),
    actor: hodLog?.user,
    timestamp: hodLog?.timestamp,
  });

  // 4. Payroll Step
  const payrollLog = findHistoryLog(["closed", "archived"]);
  steps.push({
    id: "payroll",
    label: "Payroll Queue",
    description: "Final payroll processing",
    isActive: status === "PAYROLL",
    isCompleted: status === "CLOSED",
    actor: payrollLog?.user,
    timestamp: payrollLog?.timestamp,
  });

  // 5. Closed Step
  steps.push({
    id: "closed",
    label: "Finalized",
    description: "Closed & archived",
    isActive: status === "CLOSED",
    isCompleted: status === "CLOSED",
    actor: payrollLog?.user,
    timestamp: payrollLog?.timestamp,
  });

  // Check if sheet was returned/rejected (re-review)
  const latestLog = history && history.length > 0 ? history[history.length - 1] : null;
  const isReturned = latestLog && (latestLog.action || "").toLowerCase().includes("returned");

  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-5 shadow-sm flex flex-col space-y-4 w-full mb-6">
      <div className="flex items-center justify-between border-b border-[var(--border)] pb-3">
        <div>
          <h3 className="text-xs font-bold uppercase tracking-wider text-[var(--text-muted)]">
            Workflow Progress Timeline
          </h3>
          {isReturned && (
            <span className="inline-flex items-center gap-1 text-[10px] font-bold text-[var(--accent-red)] uppercase mt-0.5 bg-[var(--accent-red)]/10 px-2 py-0.5 rounded">
              🔄 Returned for Corrections
            </span>
          )}
        </div>
        <div className="text-[10px] font-bold text-[var(--text-secondary)] flex items-center gap-1.5">
          <span>Current State:</span>
          <span className="font-bold text-[var(--text-primary)] bg-[var(--bg-primary)] px-2 py-0.5 rounded border border-[var(--border)] uppercase">
            {status}
          </span>
        </div>
      </div>

      {/* Timeline steps */}
      <div className="relative flex flex-col md:flex-row justify-between items-start md:items-center w-full pt-2">
        {/* Connection line background */}
        <div className="absolute left-[15px] top-[15px] bottom-6 md:bottom-auto md:left-4 md:right-4 md:top-[16px] h-[calc(100%-24px)] md:h-[2px] w-[2px] md:w-auto bg-black/5 -z-10" />

        {steps.map((step, idx) => {
          const isDone = step.isCompleted;
          const isCurrent = step.isActive;
          
          return (
            <div 
              key={step.id} 
              className={`flex md:flex-col items-center md:text-center relative z-10 w-full mb-4 md:mb-0 last:mb-0`}
            >
              {/* Step indicator node */}
              <div 
                className={`w-8 h-8 rounded-full flex items-center justify-center border-2 transition-all duration-300 ${
                  isDone 
                    ? "bg-[var(--accent-green)] border-[var(--accent-green)] text-white shadow-sm font-bold" 
                    : isCurrent
                      ? "bg-blue-50 border-[var(--accent-blue)] text-[var(--accent-blue)] font-bold ring-4 ring-blue-100/50 scale-105"
                      : "bg-white border-[var(--border)] text-[var(--text-muted)]"
                }`}
              >
                {isDone ? (
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                ) : (
                  <span className="text-xs">{idx + 1}</span>
                )}
              </div>

              {/* Step Info */}
              <div className="ml-4 md:ml-0 md:mt-2 flex flex-col items-start md:items-center">
                <span className={`text-xs font-bold ${isCurrent ? "text-[var(--accent-blue)]" : isDone ? "text-[var(--text-primary)]" : "text-[var(--text-muted)]"}`}>
                  {step.label}
                </span>
                <span className="text-[10px] text-[var(--text-muted)] mt-0.5 max-w-[140px] md:line-clamp-2">
                  {step.description}
                </span>
                
                {/* Completed log info (actor/time) */}
                {step.timestamp && (
                  <div className="mt-1 flex flex-col items-start md:items-center text-[9px] text-[var(--text-secondary)] opacity-80 max-w-[120px] truncate leading-tight">
                    <span className="font-semibold">{step.actor?.split("@")[0]}</span>
                    <span>{step.timestamp ? step.timestamp.substring(0, 16) : ""}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
