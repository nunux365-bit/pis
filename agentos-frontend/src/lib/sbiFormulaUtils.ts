/**
 * Lightweight formula utilities for the SBI MIS formula editor.
 *
 * Our formula dialect uses bare column letters as current-row references:
 *   =(AA + AC) / Z        →  refs: Z, AA, AC
 *   =IF(AH="SBI", Z*0.18, 0)  →  refs: Z, AH
 *
 * This is NOT Excel A1-notation. We deliberately avoid HyperFormula (GPL-3.0)
 * and write a small parser that understands exactly our syntax.
 */

// ── Known tokens that look like column refs but aren't ──────────────────────
// Function names (followed by '(') are already excluded by the regex lookahead,
// but a few constants / keywords appear without parens.
const NON_REF_TOKENS = new Set([
  "TRUE", "FALSE", "NULL",
  // Cross-sheet sheet-name prefixes — match the dot-separated ref instead
  "DUMP", "SUMMARY", "PF", "AHC",
]);

/**
 * Extract every unique column-letter reference from a formula string.
 * Returns sorted, deduplicated list — e.g. ["AA", "AC", "Z"].
 *
 * Handles:
 *  - Single-letter refs:  A–Z
 *  - Double-letter refs:  AA–AZ, BA–BZ … (max two uppercase letters)
 *  - Cross-sheet refs:    Dump.AF  →  extracts "AF"
 *  - Quoted sheet refs:   'PF Summary'.K  →  extracts "K"
 *
 * Excludes: function names (followed by `(`), string literals, numbers.
 */
export function extractColumnRefs(formula: string): string[] {
  if (!formula.trim().startsWith("=")) return [];

  // Strip string literals so we don't pick up letters inside "SBI" etc.
  const stripped = formula.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");

  const found = new Set<string>();

  // Direct column refs: 1–2 uppercase letters NOT followed by '('
  const directRe = /\b([A-Z]{1,2})\b(?!\s*\()/g;
  let m: RegExpExecArray | null;
  while ((m = directRe.exec(stripped)) !== null) {
    const tok = m[1];
    if (!NON_REF_TOKENS.has(tok)) found.add(tok);
  }

  // Cross-sheet refs after '.':  Dump.AF  or  'PF Summary'.K
  const crossRe = /\.([A-Z]{1,2})\b/g;
  while ((m = crossRe.exec(stripped)) !== null) {
    found.add(m[1]);
  }

  return [...found].sort();
}

/**
 * Check that all parentheses in the formula are balanced.
 * Returns an error string, or null if OK.
 */
export function checkParenBalance(formula: string): string | null {
  // Strip string literals to avoid counting parens inside strings
  const stripped = formula.replace(/"[^"]*"/g, '""').replace(/'[^']*'/g, "''");
  let depth = 0;
  for (const ch of stripped) {
    if (ch === "(") depth++;
    else if (ch === ")") {
      depth--;
      if (depth < 0) return "Unexpected closing parenthesis";
    }
  }
  if (depth > 0)
    return `Missing ${depth} closing parenthes${depth === 1 ? "is" : "es"}`;
  return null;
}

/**
 * Full formula validation result.
 */
export interface FormulaValidation {
  valid: boolean;
  error: string | null;   // syntax error message, or null
  refs: string[];         // sorted list of column letters referenced
}

export function validateFormula(formula: string): FormulaValidation {
  const trimmed = formula.trim();

  if (!trimmed) return { valid: true, error: null, refs: [] };

  if (!trimmed.startsWith("="))
    return { valid: false, error: 'Formula must start with "="', refs: [] };

  const error = checkParenBalance(trimmed);
  const refs = extractColumnRefs(trimmed);

  return { valid: !error, error, refs };
}
