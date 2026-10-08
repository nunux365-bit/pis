import { describe, expect, it } from "vitest";

import { ensureFormLineTaxCodes, lineTaxCode, syncHeaderTaxCode } from "./procurementLineTax";

describe("procurementLineTax", () => {
  it("prefers line tax over header", () => {
    expect(lineTaxCode({ tax_code: "FA" }, { tax_code: "XE" })).toBe("FA");
  });

  it("falls back to header tax", () => {
    expect(lineTaxCode({ tax_code: "" }, { tax_code: "XE" })).toBe("XE");
  });

  it("promotes header tax onto lines", () => {
    const form = ensureFormLineTaxCodes({
      header: { tax_code: "V18" },
      lines: [{ tax_code: "" }, { tax_code: "" }],
    });
    expect(form.lines[0]).toMatchObject({ tax_code: "V18" });
    expect(form.header).toMatchObject({ tax_code: "V18" });
  });

  it("syncs header from first line with tax", () => {
    const header: Record<string, unknown> = { tax_code: "" };
    syncHeaderTaxCode(header, [{ tax_code: "" }, { tax_code: "FB" }]);
    expect(header.tax_code).toBe("FB");
  });
});
