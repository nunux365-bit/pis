import { describe, expect, it } from "vitest";
import {
  userHasAnyRole,
  userHasRole,
  userIsAdmin,
  userRoleSet,
} from "./userAccess";

describe("userAccess", () => {
  it("single role behaves like before", () => {
    expect(userHasAnyRole(["dept_head"], "dept_head", "reviewer")).toBe(true);
    expect(userHasAnyRole(["employee"], "dept_head")).toBe(false);
  });

  it("multi-role any-match gates", () => {
    expect(userHasAnyRole(["dept_head", "reviewer"], "reviewer")).toBe(true);
    expect(
      userHasAnyRole(["employee", "email_agent_access"], "email_agent_access")
    ).toBe(true);
  });

  it("admin bypasses allowed lists", () => {
    expect(userHasAnyRole(["system_admin"], "dept_head")).toBe(true);
    expect(userIsAdmin(["system_admin", "reviewer"])).toBe(true);
  });

  it("userHasRole does not apply admin bypass", () => {
    expect(userHasRole(["system_admin"], "email_agent_access")).toBe(false);
    expect(userHasRole(["email_agent_access"], "email_agent_access")).toBe(true);
  });

  it("defaults empty to employee", () => {
    expect(userRoleSet(undefined)).toEqual(new Set(["employee"]));
  });
});
