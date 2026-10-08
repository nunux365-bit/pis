import { describe, expect, it } from "vitest";
import { applyProcurementHeaderCascade } from "./procurementFieldCascade";

describe("applyProcurementHeaderCascade cost centre plant scope", () => {
  it("clears YUNB cost centres when plant changes", () => {
    const result = applyProcurementHeaderCascade(
      "YUNB",
      "plant",
      "H001",
      "H002",
      { plant: "H001", storage_location: "H001|3021", purchasing_org: "1MGH" },
      [{ allocations: [{ cost_center: "HBM11001A0", qty: "1" }] }]
    );
    expect(result).not.toBeNull();
    expect(result!.header.plant).toBe("H002");
    expect(result!.header.storage_location).toBe("");
    expect(result!.lines[0]?.allocations).toEqual([{ cost_center: "", qty: "1" }]);
  });

  it("does not clear YUNB cost centres when only storage location changes", () => {
    const result = applyProcurementHeaderCascade(
      "YUNB",
      "storage_location",
      "H001|3021",
      "H001|3045",
      { plant: "H001", storage_location: "H001|3021", purchasing_org: "1MGH" },
      [{ allocations: [{ cost_center: "HBM11001A0", qty: "1" }] }]
    );
    expect(result).toBeNull();
  });

  it("clears YSER cost centres when plant changes", () => {
    const result = applyProcurementHeaderCascade(
      "YSER",
      "plant",
      "H001",
      "H007",
      { plant: "H001", storage_location: "H001|3021", purchasing_org: "1MGH" },
      [{ allocations: [{ cost_center: "HBM11001A0", qty: "2" }] }]
    );
    expect(result).not.toBeNull();
    expect(result!.lines[0]?.allocations).toEqual([{ cost_center: "", qty: "2" }]);
  });

  it("does not clear YAST assets when plant changes (only clears storage location)", () => {
    const result = applyProcurementHeaderCascade(
      "YAST",
      "plant",
      "H001",
      "H002",
      { plant: "H001", storage_location: "H001|3021", purchasing_org: "1MGH" },
      [{ asset: "003500008160", allocations: [{ asset: "003500008160", qty: "1" }] }]
    );
    expect(result).not.toBeNull();
    expect(result!.header.storage_location).toBe("");
    expect(result!.lines[0]?.asset).toBe("003500008160");
    expect(result!.lines[0]?.allocations).toEqual([{ asset: "003500008160", qty: "1" }]);
  });
});
