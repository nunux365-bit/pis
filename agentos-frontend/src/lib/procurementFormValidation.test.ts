import { describe, expect, it } from "vitest";
import {
  resolveProcurementAllocationFields,
  validateProcurementForm,
} from "./procurementFormValidation";
import type { ProcurementFormSchemaSlice } from "./procurementFormValidation";

const yunbSlice: ProcurementFormSchemaSlice = {
  code: "YUNB",
  header_fields: [],
  line_fields: [],
  block_fields: [
    {
      key: "material",
      label: "Material",
      widget: "search",
      reference_domain: "material",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "short_text",
      label: "Material description",
      widget: "textarea",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: false,
    },
    {
      key: "unit_price",
      label: "Unit price",
      widget: "number",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "valuation_price",
      label: "Valuation price",
      widget: "number",
      reference_domain: null,
      required_pr: false,
      required_po: false,
      ui_visible: true,
    },
    {
      key: "material_group",
      label: "Material group",
      widget: "select",
      reference_domain: "material_group",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
  ],
  allocation_fields: [
    {
      key: "cost_center",
      label: "Cost center",
      widget: "search",
      reference_domain: "cost_center",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "qty",
      label: "Quantity",
      widget: "number",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
  ],
};

describe("validateProcurementForm allocation qty", () => {
  it("flags empty quantity on PR and PO", () => {
    const form = {
      header: {},
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "" }],
        },
      ],
    };
    const prIssues = validateProcurementForm("PR", form, yunbSlice);
    expect(prIssues.some((i) => i.scope === "allocation" && i.key === "qty")).toBe(true);

    const poIssues = validateProcurementForm("PO", form, yunbSlice);
    expect(poIssues.some((i) => i.scope === "allocation" && i.key === "qty")).toBe(true);
  });

  it("flags empty unit price on PR when valuation is computed", () => {
    const form = {
      header: {},
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "",
          valuation_price: "",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yunbSlice);
    expect(issues.some((i) => i.scope === "block" && i.key === "unit_price")).toBe(true);
    expect(issues.some((i) => i.key === "valuation_price")).toBe(false);
  });

  it("accepts positive decimal quantity for YSER", () => {
    const yserSlice: ProcurementFormSchemaSlice = {
      ...yunbSlice,
      code: "YSER",
      block_fields: [
        {
          key: "service",
          label: "Service",
          widget: "search",
          reference_domain: "service",
          required_pr: true,
          required_po: true,
          ui_visible: true,
        },
        {
          key: "short_text",
          label: "Service short text",
          widget: "textarea",
          reference_domain: null,
          required_pr: true,
          required_po: true,
          ui_visible: false,
        },
        {
          key: "unit_price",
          label: "Unit price",
          widget: "number",
          reference_domain: null,
          required_pr: true,
          required_po: true,
          ui_visible: true,
        },
        {
          key: "valuation_price",
          label: "Valuation price",
          widget: "number",
          reference_domain: null,
          required_pr: false,
          required_po: false,
          ui_visible: true,
        },
      ],
    };
    const form = {
      header: {},
      lines: [
        {
          service: "1000000001",
          short_text: "Test service",
          unit_price: "50",
          allocations: [
            { cost_center: "HBM11001A0", qty: "0.999" },
            { cost_center: "HBM11001A1", qty: "2.001" },
          ],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yserSlice);
    expect(issues.filter((i) => i.key === "qty")).toEqual([]);
  });

  it("allows duplicate service codes on separate YSER lines", () => {
    const yserSlice: ProcurementFormSchemaSlice = {
      ...yunbSlice,
      code: "YSER",
      block_fields: [
        {
          key: "service",
          label: "Service",
          widget: "search",
          reference_domain: "service",
          required_pr: true,
          required_po: true,
          ui_visible: true,
        },
        {
          key: "short_text",
          label: "Service short text",
          widget: "textarea",
          reference_domain: null,
          required_pr: true,
          required_po: true,
          ui_visible: false,
        },
        {
          key: "unit_price",
          label: "Unit price",
          widget: "number",
          reference_domain: null,
          required_pr: true,
          required_po: true,
          ui_visible: true,
        },
      ],
    };
    const form = {
      header: {},
      lines: [
        {
          service: "000000001000000065",
          short_text: "Genset line 1",
          unit_price: "250",
          allocations: [{ cost_center: "HBM11001A0", qty: "1" }],
        },
        {
          service: "000000001000000065",
          short_text: "Genset line 2",
          unit_price: "250",
          allocations: [{ cost_center: "HBM11002A0", qty: "1" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yserSlice);
    expect(issues.some((i) => i.message.includes("Duplicate"))).toBe(false);
  });
});

describe("validateProcurementForm requestor email", () => {
  it("flags invalid PO requestor email", () => {
    const form = {
      header: { requestor_email: "not-an-email" },
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PO", form, yunbSlice);
    expect(issues.some((i) => i.scope === "header" && i.key === "requestor_email")).toBe(true);
  });

  it("does not flag optional empty requestor email on PO", () => {
    const form = {
      header: {},
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PO", form, yunbSlice);
    expect(issues.some((i) => i.key === "requestor_email")).toBe(false);
  });
});

describe("validateProcurementForm header note", () => {
  it("flags header note longer than 40 characters on PR", () => {
    const form = {
      header: { header_note: "x".repeat(41) },
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yunbSlice);
    expect(issues.some((i) => i.scope === "header" && i.key === "header_note")).toBe(true);
  });

  it("flags header note longer than 12 characters on PO", () => {
    const form = {
      header: { header_note: "x".repeat(13) },
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PO", form, yunbSlice);
    expect(issues.some((i) => i.scope === "header" && i.key === "header_note")).toBe(true);
  });
});

describe("validateProcurementForm storage location", () => {
  it("flags storage location that does not match plant", () => {
    const form = {
      header: { plant: "H001", storage_location: "H002|3021" },
      lines: [
        {
          material: "4000000002",
          short_text: "A4 COPIER PAPER",
          unit_price: "10",
          allocations: [{ cost_center: "HBM11001A0", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yunbSlice);
    expect(issues.some((i) => i.scope === "header" && i.key === "storage_location")).toBe(true);
  });
});

describe("validateProcurementForm legacy header catalogue group", () => {
  it("accepts header-only material group when line group is empty", () => {
    const form = {
      header: { material_group: "M020-0001" },
      lines: [
        {
          material: "4000000002",
          short_text: "line",
          unit_price: "10",
          material_group: "",
          allocations: [{ cost_center: "HBM11001A0", qty: "1" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yunbSlice);
    expect(issues.some((i) => i.key === "material_group")).toBe(false);
  });
});

describe("resolveProcurementAllocationFields", () => {
  it("forces asset splits when YAST schema still has cost_center", () => {
    const forced = resolveProcurementAllocationFields("YAST", [
      {
        key: "cost_center",
        label: "Cost center",
        scope: "allocation",
        widget: "search",
        reference_domain: "cost_center",
        required_pr: true,
        required_po: true,
      },
      {
        key: "qty",
        label: "Quantity",
        scope: "allocation",
        widget: "number",
        reference_domain: null,
        required_pr: true,
        required_po: true,
      },
    ]);
    expect(forced.map((f) => f.key)).toEqual(["asset", "qty"]);
    expect(forced[0].reference_domain).toBe("asset");
  });
});

const yastSlice: ProcurementFormSchemaSlice = {
  code: "YAST",
  header_fields: [],
  line_fields: [],
  block_fields: [
    {
      key: "material",
      label: "Material",
      widget: "search",
      reference_domain: "material",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "short_text",
      label: "Material description",
      widget: "textarea",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: false,
    },
    {
      key: "unit_price",
      label: "Unit price",
      widget: "number",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "material_group",
      label: "Material group",
      widget: "select",
      reference_domain: "material_group",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
  ],
  allocation_fields: [
    {
      key: "asset",
      label: "Asset",
      widget: "search",
      reference_domain: "asset",
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
    {
      key: "qty",
      label: "Quantity",
      widget: "number",
      reference_domain: null,
      required_pr: true,
      required_po: true,
      ui_visible: true,
    },
  ],
};

describe("validateProcurementForm YAST duplicate assets", () => {
  it("rejects the same asset twice in one block (leading zeros ignored)", () => {
    const form = {
      header: { material_group: "M020-0001" },
      lines: [
        {
          material: "4000000002",
          short_text: "Asset line",
          unit_price: "10",
          material_group: "M020-0001",
          allocations: [
            { asset: "003500008160", qty: "1" },
            { asset: "3500008160", qty: "1" },
          ],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yastSlice);
    expect(
      issues.some(
        (i) =>
          i.scope === "allocation" &&
          i.key === "asset" &&
          i.message.includes("Duplicate asset")
      )
    ).toBe(true);
  });

  it("allows the same asset on different material lines", () => {
    const form = {
      header: { material_group: "M020-0001" },
      lines: [
        {
          material: "4000000002",
          short_text: "Line A",
          unit_price: "10",
          material_group: "M020-0001",
          allocations: [{ asset: "003500008160", qty: "1" }],
        },
        {
          material: "4000000003",
          short_text: "Line B",
          unit_price: "20",
          material_group: "M020-0001",
          allocations: [{ asset: "003500008160", qty: "2" }],
        },
      ],
    };
    const issues = validateProcurementForm("PR", form, yastSlice);
    expect(issues.some((i) => i.message.includes("Duplicate asset"))).toBe(false);
  });
});
