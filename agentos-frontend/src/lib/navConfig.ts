/**
 * Primary nav; items filtered by backend roles.
 * - **Source to pay (S2P)**: visible to everyone — opens the S2P shell; **Programs** lists per-program
 *   (see ``WorkflowNavItem.roles`` in ``workflowNav.ts`` for future limited programs).
 * - **Optimus / Prosight / Order RCA**: visible to everyone — open to all signed-in (1mg SSO) users.
 * - O2C / review queue remains `dept_head` / `reviewer` / `system_admin` only.
 */

import { userHasAnyRole, userHasAnyRoleStrict } from "./userAccess";
import { WORKFLOW_DEFAULT_PATH } from "./workflowNav";

export type NavItem = {
  id: string;
  label: string;
  icon: string;
  href: string;
  badge?: number;
  /** If set, only these roles (plus system_admin) see the item. Omit = everyone. */
  roles?: string[];
  /** Omitted from the sidebar until the feature ships (see `navItemsForUser`). */
  comingSoon?: boolean;
};

export const NAV_ITEMS: NavItem[] = [
  // ── Hidden until shipped (comingSoon) ─────────────────────────
  { id: "chat", label: "Chat", icon: "💬", href: "/chat", comingSoon: true },
  { id: "sessions", label: "Sessions", icon: "⏱️", href: "/sessions", comingSoon: true },
  {
    id: "skills",
    label: "Skills",
    icon: "🧩",
    href: "/skills",
    roles: ["system_admin", "dept_head"],
    comingSoon: true,
  },

  // ── Agents (display order: O2C → S2P → Call → Email → RCA → Optimus → new… → Admin) ──
  {
    id: "o2c",
    label: "Order to cash (O2C)",
    icon: "🔄",
    href: WORKFLOW_DEFAULT_PATH,
    roles: ["dept_head", "reviewer"],
  },
  { id: "s2p", label: "Source to pay (S2P)", icon: "📑", href: "/s2p/procurement" },
  {
    id: "call-quality",
    label: "GLP 1 compliance",
    icon: "📞",
    href: "/compliance/call-quality",
    roles: ["glp_compliance_reviewer"],
  },
  {
    id: "email-agent",
    label: "AR Automation",
    icon: "✉️",
    href: "/admin/email-agent",
    roles: ["system_admin", "email_agent_access"],
  },
  { id: "order-rca", label: "Order RCA", icon: "🔍", href: "/order-rca" },
  {
    id: "responder-evals",
    label: "Responder",
    icon: "📊",
    href: "/responder-evals",
    roles: ["responder_eval_reviewer"],
  },
  // ── Add new agents below this line ────────────────────────────
  {
    id: "optimus",
    label: "Optimus",
    icon: "🧠",
    href: "/optimus",
  },

  {
    id: "prosight",
    label: "Prosight",
    icon: "📉",
    href: "/prosight",
  },
  {
    id: "pnl-dashboard",
    label: "PnL Dashboard",
    icon: "📊",
    href: "/pnl-dashboard",
    roles: ["system_admin", "pnl_dashboard_access"],
  },
  {
    id: "payroll",
    label: "Payroll (PIS)",
    icon: "💰",
    href: "/payroll",
    roles: ["maker", "hrbp", "hod", "payroll", "payroll_admin", "system_admin"],
  },

  // ── Admin always last ──────────────────────────────────────────
  {
    id: "admin",
    label: "Admin",
    icon: "🛡️",
    href: "/admin",
    roles: ["system_admin", "optimus_admin"],
  },
];

export function navItemsForUser(userRoles: string[] | undefined): NavItem[] {
  return NAV_ITEMS.filter((item) => {
    if (item.comingSoon) return false;
    if (!item.roles?.length) return true;
    if (item.id === "payroll") {
      return userHasAnyRoleStrict(userRoles, ...item.roles);
    }
    return userHasAnyRole(userRoles, ...item.roles);
  });
}
