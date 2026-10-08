import { describe, expect, it } from "vitest";
import {
  isValidDateInput,
  isValidNonEmpty,
  isValidPositiveDecimal,
  isValidPositiveQty,
  isValidStaticOption,
  shouldShowValidOnBlur,
} from "./fieldBlurValidity";

describe("fieldBlurValidity", () => {
  it("positive qty accepts decimals", () => {
    expect(isValidPositiveQty("0.999")).toBe(true);
    expect(isValidPositiveQty("0")).toBe(false);
    expect(isValidPositiveQty("")).toBe(false);
  });

  it("positive decimal for unit price", () => {
    expect(isValidPositiveDecimal("12.5")).toBe(true);
    expect(isValidPositiveDecimal("-1")).toBe(false);
  });

  it("date input validity", () => {
    expect(isValidDateInput("2026-04-20")).toBe(true);
    expect(isValidDateInput("")).toBe(false);
  });

  it("static option must match loaded code", () => {
    expect(isValidStaticOption("H001", ["H001", "T001"])).toBe(true);
    expect(isValidStaticOption("X", ["H001"])).toBe(false);
    expect(isValidStaticOption("", ["H001"], { required: false })).toBe(false);
  });

  it("picked code requires value and picked flag", () => {
    expect(isValidNonEmpty("8002")).toBe(true);
    expect(shouldShowValidOnBlur({ kind: "picked", value: "8002", picked: true })).toBe(true);
    expect(shouldShowValidOnBlur({ kind: "picked", value: "8002", picked: false })).toBe(false);
  });

  it("optional empty fields show no valid icon", () => {
    expect(shouldShowValidOnBlur({ kind: "nonEmpty", value: "", required: false })).toBe(false);
  });
});
