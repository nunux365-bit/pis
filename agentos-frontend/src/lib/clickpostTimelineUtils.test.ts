import { describe, expect, it } from "vitest";

import {
  buildClickpostInsights,
  normalizeClickpostEvents,
  sortClickpostEventsChronological,
  summarizeClickpostTimeline,
} from "./clickpostTimelineUtils";
import { isSafeHttpUrl } from "./cn";

describe("clickpostTimelineUtils", () => {
  it("normalizes backend clickpost events", () => {
    const events = normalizeClickpostEvents([
      {
        status: "Delivered",
        location: "GURGAON ETAIL DELIVERY",
        at: "17 May, 2026 14:46 IST",
        timestamp: "2026-05-17T14:46:00",
      },
    ]);
    expect(events).toHaveLength(1);
    expect(events[0].status).toBe("Delivered");
  });

  it("sorts chronologically and summarizes gaps", () => {
    const events = normalizeClickpostEvents([
      { status: "Delivered", location: "A", at: "t2", timestamp: "2026-06-15T16:37:53" },
      { status: "Shipped", location: "B", at: "t1", timestamp: "2026-06-09T21:02:38" },
    ]);
    const chron = sortClickpostEventsChronological(events);
    expect(chron[0].status).toBe("Shipped");
    const summary = summarizeClickpostTimeline(events);
    expect(summary.eventCount).toBe(2);
    expect(summary.longestGapLabel).toContain("Shipped");
  });

  it("builds insight cards for courier span", () => {
    const events = normalizeClickpostEvents([
      { status: "Delivered", location: "A", at: "t3", timestamp: "2026-06-15T16:37:53" },
      { status: "Order placed", location: "B", at: "t1", timestamp: "2026-06-09T08:23:28" },
      { status: "Shipped", location: "C", at: "t2", timestamp: "2026-06-09T21:02:38" },
    ]);
    const insights = buildClickpostInsights(events);
    expect(insights.some((i) => i.headline.includes("Delivered scan"))).toBe(true);
    expect(insights.some((i) => i.headline.includes("Order placed"))).toBe(true);
  });

  it("rejects unsafe tracking URLs", () => {
    expect(isSafeHttpUrl("https://example.com/track")).toBe(true);
    expect(isSafeHttpUrl("javascript:alert(1)")).toBe(false);
  });
});
