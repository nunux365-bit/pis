/** Client-side blur validity — no catalogue API calls; optional ✓ feedback only. */

export type BlurValidityCheck =
  | { kind: "nonEmpty"; value: string; required?: boolean }
  | { kind: "positiveQty"; value: string; required?: boolean }
  | { kind: "positiveDecimal"; value: string; required?: boolean }
  | { kind: "date"; value: string; required?: boolean }
  | { kind: "staticOption"; value: string; optionCodes: string[]; required?: boolean }
  | { kind: "picked"; value: string; picked: boolean; required?: boolean };

export function isValidNonEmpty(value: string): boolean {
  return Boolean(String(value ?? "").trim());
}

export function isValidPositiveQty(value: string): boolean {
  const v = String(value ?? "").trim();
  if (!v) return false;
  const n = Number.parseFloat(v.replace(",", "."));
  return Number.isFinite(n) && n > 0;
}

export function isValidPositiveDecimal(value: string): boolean {
  return isValidPositiveQty(value);
}

export function isValidDateInput(value: string): boolean {
  const v = String(value ?? "").trim();
  if (!v) return false;
  return Number.isFinite(Date.parse(v));
}

export function isValidStaticOption(
  value: string,
  optionCodes: string[],
  opts?: { required?: boolean }
): boolean {
  const v = String(value ?? "").trim();
  if (!v) return opts?.required ? false : false;
  return optionCodes.some((c) => String(c).trim() === v);
}

export function shouldShowValidOnBlur(check: BlurValidityCheck): boolean {
  const required = check.required ?? true;
  const value = "value" in check ? String(check.value ?? "").trim() : "";

  switch (check.kind) {
    case "nonEmpty":
      if (!value) return !required ? false : false;
      return isValidNonEmpty(value);
    case "positiveQty":
      if (!value) return false;
      return isValidPositiveQty(value);
    case "positiveDecimal":
      if (!value) return false;
      return isValidPositiveDecimal(value);
    case "date":
      if (!value) return false;
      return isValidDateInput(value);
    case "staticOption":
      if (!value) return false;
      return isValidStaticOption(value, check.optionCodes, { required });
    case "picked":
      if (!value) return false;
      return check.picked;
    default:
      return false;
  }
}
