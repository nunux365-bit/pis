/**
 * RBAC for the O2C / S2P **review queue** (nav, dashboard, `/o2c/*`).
 * **`/s2p/*`**: the S2P shell is open to every signed-in user; individual **programs** in the sidebar
 * may be restricted via ``WorkflowNavItem.roles`` (see ``workflowProgramVisible`` in ``workflowNav``).
 */

import { userHasAnyRole } from "./userAccess";

/**
 * Whether the user may see O2C / S2P **review queue** navigation and open **`/o2c/*`** routes.
 * Does **not** gate **`/s2p/*`** — use per-program ``roles`` on workflow nav items instead.
 * `system_admin` always returns true; `employee` returns false.
 */
export function canAccessWorkflowQueue(userRoles: string[] | undefined): boolean {
  return userHasAnyRole(userRoles, "dept_head", "reviewer");
}

/** True for any path under the Source to pay area (shell is open to all authenticated users). */
export function isS2pShellPath(pathname: string): boolean {
  return pathname === "/s2p" || pathname.startsWith("/s2p/");
}

/**
 * Whether the current route may render the shared ``(workflows)`` shell.
 * O2C is queue-restricted; the whole S2P tree is open to any signed-in user (per-program RBAC in the sidebar + pages).
 */
export function mayEnterWorkflowsLayout(
  pathname: string,
  userRoles: string[] | undefined
): boolean {
  if (pathname === "/o2c" || pathname.startsWith("/o2c/")) {
    return canAccessWorkflowQueue(userRoles);
  }
  if (isS2pShellPath(pathname)) {
    return true;
  }
  if (pathname === "/admin/email-agent" || pathname.startsWith("/admin/email-agent/")) {
    return userHasAnyRole(userRoles, "system_admin", "email_agent_access");
  }
  return canAccessWorkflowQueue(userRoles);
}
