import { describe, expect, it } from "vitest";
import {
  checkParenBalance,
  extractColumnRefs,
  validateFormula,
} from "./sbiFormulaUtils";

// ── extractColumnRefs ────────────────────────────────────────────────────────

describe("extractColumnRefs", () => {
  it("returns empty array for empty string", () => {
    expect(extractColumnRefs("")).toEqual([]);
  });

  it("returns empty array when formula does not start with =", () => {
    expect(extractColumnRefs("AA+AC")).toEqual([]);
  });

  it("extracts single-letter refs", () => {
    expect(extractColumnRefs("=Z+Y")).toEqual(["Y", "Z"]);
  });

  it("extracts double-letter refs", () => {
    expect(extractColumnRefs("=(AA+AC)/Z")).toEqual(["AA", "AC", "Z"]);
  });

  it("excludes function names (followed by '(')", () => {
    expect(extractColumnRefs("=IF(Z>0,SUM(AA,AB),0)")).toEqual(["AA", "AB", "Z"]);
  });

  it("deduplicates refs used multiple times", () => {
    expect(extractColumnRefs("=Z+Z+Z")).toEqual(["Z"]);
  });

  it("extracts cross-sheet refs after '.'", () => {
    // Dump.AF → AF
    expect(extractColumnRefs("=Dump.AF+Z")).toContain("AF");
    expect(extractColumnRefs("=Dump.AF+Z")).toContain("Z");
  });

  it("does not extract letters inside string literals", () => {
    // "SBI" should not contribute S, B, I as refs
    const refs = extractColumnRefs('=IF(AH="SBI",Z*0.18,0)');
    expect(refs).toContain("AH");
    expect(refs).toContain("Z");
    expect(refs).not.toContain("S");
    expect(refs).not.toContain("B");
  });

  it("returns refs in sorted order", () => {
    const refs = extractColumnRefs("=Z+AA+B");
    expect(refs).toEqual([...refs].sort());
  });
});

// ── checkParenBalance ────────────────────────────────────────────────────────

describe("checkParenBalance", () => {
  it("returns null for balanced formula", () => {
    expect(checkParenBalance("=(AA+AC)/Z")).toBeNull();
    expect(checkParenBalance("=IF(A>0,SUM(B,C),0)")).toBeNull();
  });

  it("detects missing closing paren", () => {
    const result = checkParenBalance("=((AA+AC)/Z");
    expect(result).not.toBeNull();
    expect(result).toMatch(/closing/i);
  });

  it("detects unexpected closing paren", () => {
    const result = checkParenBalance("=(AA+AC))/Z");
    expect(result).not.toBeNull();
    expect(result).toMatch(/unexpected/i);
  });

  it("accepts formula with no parens", () => {
    expect(checkParenBalance("=Z+AA")).toBeNull();
  });
});

// ── validateFormula ──────────────────────────────────────────────────────────

describe("validateFormula", () => {
  it("returns valid=true for empty string (no formula entered)", () => {
    const r = validateFormula("");
    expect(r.valid).toBe(true);
    expect(r.error).toBeNull();
  });

  it("returns error when formula doesn't start with =", () => {
    const r = validateFormula("AA+AC");
    expect(r.valid).toBe(false);
    expect(r.error).toMatch(/must start with/i);
  });

  it("returns valid with refs for a well-formed formula", () => {
    const r = validateFormula("=(AA+AC)/Z");
    expect(r.valid).toBe(true);
    expect(r.error).toBeNull();
    expect(r.refs).toContain("AA");
    expect(r.refs).toContain("AC");
    expect(r.refs).toContain("Z");
  });

  it("returns error and refs for formula with unbalanced parens", () => {
    const r = validateFormula("=(AA+AC/Z");
    expect(r.valid).toBe(false);
    expect(r.error).not.toBeNull();
    // refs are still extracted even when there's a syntax error
    expect(r.refs.length).toBeGreaterThan(0);
  });
});
