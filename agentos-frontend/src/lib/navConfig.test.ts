import { describe, expect, it } from "vitest";
import { navItemsForUser } from "./navConfig";

const ids = (roles: string[] | undefined) =>
  navItemsForUser(roles).map((i) => i.id);

describe("navItemsForUser", () => {
  it("shows open-to-all agents to a plain employee", () => {
    const shown = ids(["employee"]);
    for (const id of ["prosight", "optimus", "s2p", "order-rca"]) {
      expect(shown).toContain(id);
    }
  });

  it("still hides role-gated items from a plain employee", () => {
    const shown = ids(["employee"]);
    for (const id of ["o2c", "admin", "pnl-dashboard", "payroll"]) {
      expect(shown).not.toContain(id);
    }
  });

  it("shows prosight when roles are absent", () => {
    expect(ids(undefined)).toContain("prosight");
  });

  it("keeps prosight visible for previously granted roles", () => {
    // `prosight_user` no longer exists as a role; stale copies linger in
    // users.role until migration 060 runs, and must not affect visibility.
    expect(ids(["prosight_user"])).toContain("prosight");
    expect(ids(["optimus_admin"])).toContain("prosight");
    expect(ids(["system_admin"])).toContain("prosight");
  });

  it("omits comingSoon items", () => {
    expect(ids(["system_admin"])).not.toContain("chat");
  });
});
