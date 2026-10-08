import { describe, expect, it } from "vitest";
import {
  catalogLineSearchBlocked,
  catalogLineSearchPlaceholder,
  ensureFormLineCatalogGroups,
  lineCatalogGroup,
} from "./procurementCatalogGroup";

describe("procurementCatalogGroup", () => {
  it("blocks YUNB material without line or header material_group", () => {
    expect(catalogLineSearchBlocked("material", "YUNB", {}, {})).toBe(true);
    expect(catalogLineSearchBlocked("material", "YUNB", { material_group: "M001" }, {})).toBe(
      false
    );
    expect(catalogLineSearchBlocked("material", "YUNB", {}, { material_group: "M001" })).toBe(
      false
    );
  });

  it("prefers line group over header", () => {
    expect(
      lineCatalogGroup({ material_group: "LINE" }, { material_group: "HDR" }, "YUNB")
    ).toBe("LINE");
  });

  it("blocks YSER service without service_group", () => {
    expect(catalogLineSearchBlocked("service", "YSER", {}, {})).toBe(true);
    expect(catalogLineSearchBlocked("service", "YSER", { service_group: "S001" }, {})).toBe(
      false
    );
  });

  it("does not block unrelated fields", () => {
    expect(catalogLineSearchBlocked("vendor", "YUNB", {}, {})).toBe(false);
  });

  it("placeholder text", () => {
    expect(catalogLineSearchPlaceholder("material")).toContain("this line");
    expect(catalogLineSearchPlaceholder("service")).toContain("this line");
  });

  it("promotes legacy header group onto lines", () => {
    const out = ensureFormLineCatalogGroups(
      {
        header: { material_group: "HDR-A" },
        lines: [{ material: "M1" }, { material: "M2" }],
      },
      "YUNB"
    );
    expect(out.lines[0]).toMatchObject({ material_group: "HDR-A" });
    expect(out.lines[1]).toMatchObject({ material_group: "HDR-A" });
    expect(out.header).toMatchObject({ material_group: "HDR-A" });
  });
});
