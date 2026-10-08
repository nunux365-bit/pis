/**
 * O2C / S2P workflow routes. Kept in sync with pages under
 * `src/app/(platform)/(workflows)/`.
 *
 * **S2P programs:** omit ``roles`` on a ``WorkflowNavItem`` for all users. Set ``roles`` when a program
 * should appear only for some roles; add the same check on the route’s page if the URL might be opened directly.
 */

import { userHasAnyRole } from "./userAccess";

/** Default O2C landing when opening Order to cash from the shell. */
export const WORKFLOW_DEFAULT_PATH = "/o2c/ohc-mis" as const;

export type WorkflowNavItem = {
  href: string;
  label: string;
  /**
   * When set, only these roles (plus ``system_admin``) see this program in the sidebar.
   * Omit for programs open to all signed-in users (e.g. Finance Requisition today).
   */
  roles?: string[];
};

/**
 * Whether an S2P program link should appear for this user in the Programs column.
 * Pages for restricted programs should also enforce access (e.g. redirect) if the URL is guessed.
 */
export function workflowProgramVisible(
  program: WorkflowNavItem,
  userRoles: string[] | undefined
): boolean {
  if (!program.roles?.length) return true;
  return userHasAnyRole(userRoles, ...program.roles);
}

export type WorkflowDomainNav = {
  id: "o2c" | "s2p" | "email-agent";
  label: string;
  shortLabel: string;
  prefix: string;
  workflows: WorkflowNavItem[];
};

export const WORKFLOW_DOMAINS: WorkflowDomainNav[] = [
  {
    id: "o2c",
    label: "Order to cash",
    shortLabel: "O2C",
    prefix: "/o2c",
    workflows: [
      { href: WORKFLOW_DEFAULT_PATH, label: "OHC MIS" },
      { href: "/o2c/diagnostics-mis", label: "Diagnostics MIS" },
      { href: "/o2c/pharma-mis", label: "Pharma MIS", roles: ["pharma_mis_operator"] },
    ],
  },
  {
    id: "s2p",
    label: "Source to pay",
    shortLabel: "S2P",
    prefix: "/s2p",
    workflows: [{ href: "/s2p/procurement", label: "Finance Requisition" }],
  },
  {
    id: "email-agent",
    label: "Email Agent",
    shortLabel: "EA",
    prefix: "/admin/email-agent",
    workflows: [
          { href: "/admin/email-agent/receivables", label: "Data Viz" },
      // { href: "/admin/email-agent/replies", label: "Replies" }, // DEPRECATED: tab slated for removal
      { href: "/admin/email-agent/status", label: "Reply tracker" },
      { href: "/admin/email-agent/operational", label: "Outbound tracker" },
      { href: "/admin/email-agent/kam", label: "KAM Dashboard" },
    ],
  },
];

export function getDomainForPath(pathname: string): WorkflowDomainNav | undefined {
  return WORKFLOW_DOMAINS.find(
    (d) => pathname === d.prefix || pathname.startsWith(`${d.prefix}/`)
  );
}

export function isWorkflowQueueNavId(id: string): boolean {
  return id === "o2c" || id === "s2p" || id === "email-agent";
}
