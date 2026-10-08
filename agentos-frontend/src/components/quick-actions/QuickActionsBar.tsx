"use client";

import Link from "next/link";
import type { QuickPersona } from "@/lib/persona";
import { WORKFLOW_DEFAULT_PATH } from "@/lib/workflowNav";

type Action = { label: string; href: string; count?: number };

const FINANCE_ACTIONS: Action[] = [
  { label: "My pending reviews", href: WORKFLOW_DEFAULT_PATH },
  { label: "GST / tax", href: "/chat?q=GST%20status" },
  { label: "Skills catalog", href: "/skills" },
];

const HR_ACTIONS: Action[] = [
  { label: "Onboarding", href: "/chat?q=onboarding%20status" },
  { label: "Payroll review queue", href: WORKFLOW_DEFAULT_PATH },
  { label: "Leave policy", href: "/chat?q=leave%20policy" },
];

const WAREHOUSE_ACTIONS: Action[] = [
  { label: "Expiry / alerts", href: WORKFLOW_DEFAULT_PATH },
  { label: "Inventory", href: "/chat?q=inventory%20gaps" },
  { label: "Cold chain", href: "/chat?q=cold%20chain" },
];

const DEFAULT_ACTIONS: Action[] = [
  { label: "Chat assistant", href: "/chat" },
  { label: "O2C & S2P", href: WORKFLOW_DEFAULT_PATH },
  { label: "Agent sessions", href: "/sessions" },
];

function isWorkflowQueueHref(href: string) {
  return href.startsWith("/o2c") || href.startsWith("/s2p");
}

type Props = {
  persona?: QuickPersona;
  /** Live pending count for the first workflow-queue action when set. */
  pendingApprovalCount?: number | null;
  /** When false, links to O2C / S2P routes are omitted (e.g. general `employee` role). */
  allowWorkflowQueueLinks?: boolean;
};

function stripWorkflowQueueActions(actions: Action[]): Action[] {
  return actions.filter((a) => !isWorkflowQueueHref(a.href));
}

export function QuickActionsBar({
  persona = "default",
  pendingApprovalCount,
  allowWorkflowQueueLinks = true,
}: Props) {
  let actions: Action[] =
    persona === "hr"
      ? HR_ACTIONS
      : persona === "warehouse"
        ? WAREHOUSE_ACTIONS
        : persona === "finance"
          ? FINANCE_ACTIONS
          : DEFAULT_ACTIONS;

  if (!allowWorkflowQueueLinks) {
    actions = stripWorkflowQueueActions(actions);
  }

  let pendingBadgeApplied = false;
  actions = actions.map((a) => {
    if (
      !pendingBadgeApplied &&
      pendingApprovalCount != null &&
      pendingApprovalCount > 0 &&
      isWorkflowQueueHref(a.href)
    ) {
      pendingBadgeApplied = true;
      return { ...a, count: pendingApprovalCount };
    }
    return a;
  });

  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-2xl p-4">
      <div className="text-xs font-semibold text-[var(--text-muted)] uppercase tracking-wide mb-3">
        Quick actions
      </div>
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <Link
            key={a.label}
            href={a.href}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border)] text-sm font-medium text-[var(--text-primary)] hover:border-[var(--border-active)]/50 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-active)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-card)]"
          >
            {a.label}
            {a.count != null && a.count > 0 && (
              <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-[var(--accent-blue)]/20 text-[var(--accent-blue)]">
                {a.count}
              </span>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}
