import { describe, expect, it } from "vitest";
import {
  isDueAgeingBucketKey,
  parseLakhFromPartyLine,
  resolveCollectionCellForFilter,
} from "./receivableDashboardUtils";

describe("isDueAgeingBucketKey", () => {
  it("includes standard ageing buckets only", () => {
    expect(isDueAgeingBucketKey("0-1 Months")).toBe(true);
    expect(isDueAgeingBucketKey("+1Yrs")).toBe(true);
    expect(isDueAgeingBucketKey("6-12 Months")).toBe(true);
  });

  it("excludes net/not due and finance summary overdue columns", () => {
    expect(isDueAgeingBucketKey("Net Due")).toBe(false);
    expect(isDueAgeingBucketKey("Not Due")).toBe(false);
    expect(isDueAgeingBucketKey("Receivables")).toBe(false);
    expect(isDueAgeingBucketKey("Final Overdue as on 30th June")).toBe(false);
    expect(isDueAgeingBucketKey("Final Overdues")).toBe(false);
    expect(isDueAgeingBucketKey("Overdue")).toBe(false);
  });
});

describe("parseLakhFromPartyLine", () => {
  it("parses en-IN style party lines from the API", () => {
    expect(
      parseLakhFromPartyLine("ACME (₹37.3 L, 2.00m)")
    ).toBe(37.3);
    expect(
      parseLakhFromPartyLine("Foo (₹1,23,456.5 L, 0.00m)")
    ).toBe(123456.5);
  });
  it("returns null when no ₹ L marker", () => {
    expect(parseLakhFromPartyLine("Plain name")).toBeNull();
  });
});

describe("resolveCollectionCellForFilter", () => {
  it("with filter inactive, uses full cell and all names", () => {
    const r = resolveCollectionCellForFilter(
      151,
      ["A (₹0.5 L, 0m)", "B (₹0.1 L, 0m)"],
      0,
      null
    );
    expect(r.displayAmount).toBe(151);
    expect(r.inRange).toBe(true);
    expect(r.visibleNames).toHaveLength(2);
  });

  it("active min: sums only lines in range; hides sub-threshold", () => {
    const r = resolveCollectionCellForFilter(
      151,
      [
        "A (₹10 L, 0m)",
        "B (₹5 L, 0m)",
        "C (₹80 L, 0m)",
      ],
      60,
      null
    );
    expect(r.displayAmount).toBe(80);
    expect(r.inRange).toBe(true);
    expect(r.visibleNames).toEqual(["C (₹80 L, 0m)"]);
  });

  it("active min: all lines below min → no amount, empty parties", () => {
    const r = resolveCollectionCellForFilter(
      151,
      [
        "A (₹0.5 L, 0m)",
        "B (₹37.3 L, 0m)",
      ],
      60,
      null
    );
    expect(r.displayAmount).toBe(0);
    expect(r.inRange).toBe(false);
    expect(r.visibleNames).toEqual([]);
  });

  it("min and max: line must satisfy both", () => {
    const r = resolveCollectionCellForFilter(
      100,
      [
        "A (₹50 L, 0m)",
        "B (₹70 L, 0m)",
      ],
      40,
      60
    );
    expect(r.displayAmount).toBe(50);
    expect(r.visibleNames).toEqual(["A (₹50 L, 0m)"]);
  });

  it("unparseable lines: falls back to whole-cell rule", () => {
    const r = resolveCollectionCellForFilter(80, ["Plain"], 10, null);
    expect(r.displayAmount).toBe(80);
    expect(r.inRange).toBe(true);
    expect(r.visibleNames).toEqual(["Plain"]);
  });
});
