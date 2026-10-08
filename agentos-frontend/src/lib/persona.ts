/**
 * Map profile department → quick-action persona (product-spec §13.3).
 */

export type QuickPersona = "finance" | "hr" | "warehouse" | "default";

export function quickPersonaFromDepartment(
  department: string | undefined
): QuickPersona {
  const d = (department || "").toLowerCase();
  if (
    d.includes("hr") ||
    d.includes("people") ||
    d.includes("talent")
  ) {
    return "hr";
  }
  if (
    d.includes("warehouse") ||
    d.includes("supply") ||
    d.includes("logistics") ||
    d.includes("fulfill")
  ) {
    return "warehouse";
  }
  if (
    d.includes("finance") ||
    d.includes("account") ||
    d.includes("controller") ||
    d.includes("tax")
  ) {
    return "finance";
  }
  return "default";
}
