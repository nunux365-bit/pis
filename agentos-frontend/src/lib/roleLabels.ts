/** Backend ``User.roles`` strings → display labels */

const LABELS: Record<string, string> = {
  system_admin: "System administrator",
  payroll_admin: "Payroll administrator",
  dept_head: "Department head",
  reviewer: "Reviewer",
  glp_compliance_reviewer: "GLP compliance reviewer",
  responder_eval_reviewer: "Responder eval reviewer",
  email_agent_access: "Email agent",
  optimus_admin: "Optimus administrator",
  employee: "Employee",
};

export function roleDisplayLabel(role: string | undefined): string {
  if (!role) return "Member";
  return LABELS[role] ?? role;
}

export function roleDisplayLabels(roles: string[] | undefined): string {
  if (!roles?.length) return roleDisplayLabel("employee");
  return roles.map((r) => roleDisplayLabel(r)).join(", ");
}
