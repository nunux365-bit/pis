import { describe, expect, it } from "vitest";

import {
  istDateOf,
  istDayEndUtc,
  istDayStartUtc,
  istLastNDays,
  istToday,
} from "./istDate";

describe("istDayStartUtc", () => {
  it("maps an IST day start back to the previous UTC day at 18:30Z", () => {
    expect(istDayStartUtc("2026-07-23")).toBe("2026-07-22T18:30:00.000Z");
  });

  it("handles month boundaries", () => {
    expect(istDayStartUtc("2026-08-01")).toBe("2026-07-31T18:30:00.000Z");
  });

  it("handles year boundaries", () => {
    expect(istDayStartUtc("2026-01-01")).toBe("2025-12-31T18:30:00.000Z");
  });

  it("returns null for malformed input", () => {
    expect(istDayStartUtc("")).toBeNull();
    expect(istDayStartUtc("23-07-2026")).toBeNull();
  });
});

describe("istDayEndUtc", () => {
  it("maps an IST day end to 18:29:59.999Z the same UTC day", () => {
    expect(istDayEndUtc("2026-07-23")).toBe("2026-07-23T18:29:59.999Z");
  });

  it("returns null for malformed input", () => {
    expect(istDayEndUtc("not-a-date")).toBeNull();
  });
});

describe("IST day window", () => {
  it("covers exactly one day with no gap or overlap between consecutive days", () => {
    const endOf22 = istDayEndUtc("2026-07-22")!;
    const startOf23 = istDayStartUtc("2026-07-23")!;
    // 1ms apart: no gap, and no row can match both days.
    expect(new Date(startOf23).getTime() - new Date(endOf22).getTime()).toBe(1);
  });

  it("includes a run that is late-evening IST but already the next UTC day", () => {
    // 2026-07-23 23:00 IST == 2026-07-23T17:30:00Z — still the 23rd in IST.
    const run = new Date("2026-07-23T17:30:00.000Z").getTime();
    const start = new Date(istDayStartUtc("2026-07-23")!).getTime();
    const end = new Date(istDayEndUtc("2026-07-23")!).getTime();
    expect(run).toBeGreaterThanOrEqual(start);
    expect(run).toBeLessThanOrEqual(end);
  });

  it("excludes a run from 00:30 IST the following day", () => {
    // 2026-07-24 00:30 IST == 2026-07-23T19:00:00Z — belongs to the 24th, not the 23rd.
    const run = new Date("2026-07-23T19:00:00.000Z").getTime();
    const end = new Date(istDayEndUtc("2026-07-23")!).getTime();
    expect(run).toBeGreaterThan(end);
  });

  it("includes an early-morning IST run that is still the previous UTC day", () => {
    // 2026-07-23 01:00 IST == 2026-07-22T19:30:00Z — a UTC-day filter would miss this.
    const run = new Date("2026-07-22T19:30:00.000Z").getTime();
    const start = new Date(istDayStartUtc("2026-07-23")!).getTime();
    const end = new Date(istDayEndUtc("2026-07-23")!).getTime();
    expect(run).toBeGreaterThanOrEqual(start);
    expect(run).toBeLessThanOrEqual(end);
  });
});

describe("istDateOf / istToday", () => {
  it("rolls to the next IST date after 18:30Z", () => {
    expect(istDateOf(new Date("2026-07-22T18:29:59.999Z"))).toBe("2026-07-22");
    expect(istDateOf(new Date("2026-07-22T18:30:00.000Z"))).toBe("2026-07-23");
  });

  it("uses the IST date, not the UTC date, for 'today'", () => {
    // 20:00Z on the 22nd is already the 23rd in IST.
    expect(istToday(new Date("2026-07-22T20:00:00.000Z"))).toBe("2026-07-23");
  });
});

describe("istLastNDays", () => {
  it("returns an inclusive IST range ending today", () => {
    const now = new Date("2026-07-23T06:00:00.000Z"); // 11:30 IST on the 23rd
    expect(istLastNDays(7, now)).toEqual({ from: "2026-07-17", to: "2026-07-23" });
  });

  it("treats a 1-day window as today only", () => {
    const now = new Date("2026-07-23T06:00:00.000Z");
    expect(istLastNDays(1, now)).toEqual({ from: "2026-07-23", to: "2026-07-23" });
  });

  it("anchors the range on the IST date when UTC has not rolled over yet", () => {
    const now = new Date("2026-07-22T20:00:00.000Z"); // 01:30 IST on the 23rd
    expect(istLastNDays(30, now).to).toBe("2026-07-23");
  });
});
