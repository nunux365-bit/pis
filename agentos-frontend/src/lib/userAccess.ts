/**
 * User RBAC — ``/api/auth/me`` returns ``roles: string[]``.
 */

const DEFAULT_ROLES = ["employee"] as const;

export function userRoleSet(roles: string[] | undefined): Set<string> {
  if (!roles?.length) return new Set(DEFAULT_ROLES);
  return new Set(
    roles.map((r) => r.trim().toLowerCase()).filter(Boolean)
  );
}

export function userIsAdmin(roles: string[] | undefined): boolean {
  return userRoleSet(roles).has("system_admin");
}

export function userIsOptimusAdmin(roles: string[] | undefined): boolean {
  return userIsAdmin(roles) || userHasRole(roles, "optimus_admin");
}

/** Any of ``allowed`` roles (system_admin always passes when ``adminBypass``). */
export function userHasAnyRole(
  roles: string[] | undefined,
  ...allowed: string[]
): boolean {
  const rs = userRoleSet(roles);
  if (userIsAdmin(roles)) return true;
  return allowed.some((a) => rs.has(a.trim().toLowerCase()));
}

/** Any of ``allowed`` roles strictly (system_admin does NOT bypass). */
export function userHasAnyRoleStrict(
  roles: string[] | undefined,
  ...allowed: string[]
): boolean {
  const rs = userRoleSet(roles);
  return allowed.some((a) => rs.has(a.trim().toLowerCase()));
}

/** Gate check without admin bypass (e.g. path-specific admin layout). */
export function userHasRole(roles: string[] | undefined, role: string): boolean {
  return userRoleSet(roles).has(role.trim().toLowerCase());
}
