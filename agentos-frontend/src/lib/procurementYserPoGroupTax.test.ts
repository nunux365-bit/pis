import { describe, expect, it } from "vitest";
import {
  yserPoGroupTaxBannerContent,
  yserPoGroupItemTaxResolution,
  yserPoGroupTaxBannerMessage,
  yserPoGroupTaxHintByFirstLine,
  yserPoGroupTaxHints,
} from "./procurementYserPoGroupTax";

describe("yserPoGroupItemTaxResolution", () => {
  const header = { tax_code: "XE", service_group: "G-DEFAULT" };

  it("uses shared tax when all lines agree", () => {
    const lines = [
      { service_group: "G1", tax_code: "FA" },
      { service_group: "G1", tax_code: "FA" },
    ];
    const r = yserPoGroupItemTaxResolution([0, 1], lines, header);
    expect(r).toEqual({ tax: "FA", sourceLineIndex: 0, hasConflict: false });
  });

  it("last line wins when subline taxes conflict", () => {
    const lines = [
      { service_group: "G1", tax_code: "FA" },
      { service_group: "G1", tax_code: "WA" },
    ];
    const r = yserPoGroupItemTaxResolution([0, 1], lines, header);
    expect(r).toEqual({ tax: "WA", sourceLineIndex: 1, hasConflict: true });
  });
});

describe("yserPoGroupTaxHints", () => {
  it("skips single-line groups", () => {
    const lines = [{ service_group: "G1", tax_code: "FA" }];
    expect(yserPoGroupTaxHints(lines, {})).toEqual([]);
  });

  it("buckets non-contiguous lines in the same service group", () => {
    const lines = [
      { service_group: "G1", tax_code: "FA" },
      { service_group: "G2", tax_code: "FB" },
      { service_group: "G1", tax_code: "WA" },
    ];
    const hints = yserPoGroupTaxHints(lines, {});
    expect(hints).toHaveLength(1);
    expect(hints[0].lineIndices).toEqual([0, 2]);
    expect(hints[0].hasConflict).toBe(true);
    expect(hints[0].resolvedTax).toBe("WA");
  });

  it("maps banner to first line in group when taxes agree", () => {
    const lines = [
      { service_group: "G1", tax_code: "FA" },
      { service_group: "G2", tax_code: "FB" },
      { service_group: "G1", tax_code: "FA" },
    ];
    const map = yserPoGroupTaxHintByFirstLine(lines, {});
    expect(map.has(0)).toBe(true);
    expect(map.has(2)).toBe(false);
    expect(map.get(0)?.lineIndices).toEqual([0, 2]);
  });
});

describe("yserPoGroupTaxBannerContent", () => {
  it("describes conflict with ignored lines", () => {
    const content = yserPoGroupTaxBannerContent({
      groupKey: "S079-0001",
      lineIndices: [0, 1],
      resolvedTax: "1B",
      sourceLineIndex: 1,
      hasConflict: true,
    });
    expect(content.title).toMatch(/different tax/i);
    expect(content.body).toMatch(/lines 1 and 2/i);
    expect(content.body).toMatch(/1B/);
    expect(content.body).toMatch(/line 2/);
    expect(content.body).toMatch(/line 1 is not sent/i);
  });

  it("describes agreement without conflict wording", () => {
    const content = yserPoGroupTaxBannerContent({
      groupKey: "G1",
      lineIndices: [0, 1],
      resolvedTax: "FA",
      sourceLineIndex: 0,
      hasConflict: false,
    });
    expect(content.title).toMatch(/shared tax/i);
    expect(content.body).toMatch(/FA/);
    expect(content.body).not.toMatch(/not sent/i);
  });
});

describe("yserPoGroupTaxHintByFirstLine", () => {
  it("anchors conflict banner on the source line", () => {
    const lines = [
      { service_group: "G1", tax_code: "FA" },
      { service_group: "G1", tax_code: "WA" },
    ];
    const map = yserPoGroupTaxHintByFirstLine(lines, {});
    expect(map.has(0)).toBe(false);
    expect(map.has(1)).toBe(true);
  });
});

describe("yserPoGroupTaxBannerMessage", () => {
  it("describes conflict vs agreement", () => {
    expect(
      yserPoGroupTaxBannerMessage({
        groupKey: "G1",
        lineIndices: [0, 1],
        resolvedTax: "FA",
        sourceLineIndex: 0,
        hasConflict: false,
      })
    ).toMatch(/tax code FA/);

    expect(
      yserPoGroupTaxBannerMessage({
        groupKey: "G1",
        lineIndices: [0, 1],
        resolvedTax: "WA",
        sourceLineIndex: 1,
        hasConflict: true,
      })
    ).toMatch(/line 2/);
    expect(
      yserPoGroupTaxBannerMessage({
        groupKey: "G1",
        lineIndices: [0, 1],
        resolvedTax: "WA",
        sourceLineIndex: 1,
        hasConflict: true,
      })
    ).toMatch(/WA/);
  });
});
