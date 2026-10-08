import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  buildGlobalStatusSegments,
  buildGrootInsights,
  buildStatusSegments,
  collectHubSequence,
  computeGrootOutcome,
  formatGrootApiTimestamp,
  friendlyStatusLabel,
  groupGrootAttempts,
  grootStatusTone,
  isMissingDisplay,
  normalizeGrootEvents,
  parseGrootApiPayload,
  parseGrootAtDisplay,
  segmentWidthPcts,
  shouldSplitAttemptBucket,
  sortGrootEventsChronological,
  summarizeGrootTimeline,
} from "./grootTimelineUtils";

const FIXTURE_ROOT = path.resolve(__dirname, "../../../agentos-backend/tests/fixtures/order_rca");

function loadFixture(name: string): unknown {
  return JSON.parse(readFileSync(path.join(FIXTURE_ROOT, name), "utf8"));
}

describe("parseGrootAtDisplay", () => {
  it("parses Order RCA IST display strings", () => {
    const ms = parseGrootAtDisplay("14 May, 2026 14:24 IST");
    expect(ms).not.toBeNull();
    expect(new Date(ms!).toISOString()).toBe("2026-05-14T14:24:00.000Z");
  });

  it("returns null for empty, missing sentinel, and garbage", () => {
    expect(parseGrootAtDisplay(undefined)).toBeNull();
    expect(parseGrootAtDisplay("")).toBeNull();
    expect(parseGrootAtDisplay("—")).toBeNull();
    expect(parseGrootAtDisplay("not-a-date")).toBeNull();
    expect(parseGrootAtDisplay("32 Jan, 2026 99:99 IST")).toBeNull();
  });
});

describe("formatGrootApiTimestamp", () => {
  it("formats prod Groot DD-MM-YYYY timestamps like the backend", () => {
    expect(formatGrootApiTimestamp("14-05-2026 14:24:16")).toBe("14 May, 2026 14:24 IST");
    expect(formatGrootApiTimestamp("14-05-2026 14:24")).toBe("14 May, 2026 14:24 IST");
  });

  it("passes through unparseable raw strings", () => {
    expect(formatGrootApiTimestamp("bad")).toBe("bad");
    expect(formatGrootApiTimestamp(null)).toBe("—");
  });
});

describe("normalizeGrootEvents", () => {
  it("returns empty for non-arrays and skips invalid rows", () => {
    expect(normalizeGrootEvents(null)).toEqual([]);
    expect(normalizeGrootEvents(undefined)).toEqual([]);
    expect(normalizeGrootEvents("x")).toEqual([]);
    expect(
      normalizeGrootEvents([null, { at: "14 May, 2026 14:24 IST", status: "completed" }, 42]),
    ).toHaveLength(1);
  });

  it("strips em-dash sentinels from backend _display fields", () => {
    const [row] = normalizeGrootEvents([
      { at: "—", status: "—", sub_status: "—", comments: "—", performed_by: "—" },
    ]);
    expect(row.at).toBeUndefined();
    expect(row.status).toBeUndefined();
    expect(row.comments).toBeUndefined();
  });
});

describe("parseGrootApiPayload + fixtures", () => {
  it("groot_parent.json yields parseable events matching backend expectations", () => {
    const payload = loadFixture("groot_parent.json");
    const events = parseGrootApiPayload(payload);
    expect(events.length).toBe(3);
    const completed = events.find((e) => e.status === "completed");
    expect(completed?.at).toBe("14 May, 2026 14:24 IST");
    expect(completed?.comments).toBe("Order Completed");
    const summary = summarizeGrootTimeline(events);
    expect(summary.eventCount).toBe(3);
    expect(summary.parseableCount).toBe(3);
    expect(summary.unparsedCount).toBe(0);
    expect(summary.attemptCount).toBe(1);
    expect(buildGlobalStatusSegments(events).length).toBeGreaterThan(0);
  });

  it("empty groot fixtures produce zero events without throwing", () => {
    for (const file of ["cases/return_refund/groot.json", "cases/split_mounjaro/groot.json"]) {
      const events = parseGrootApiPayload(loadFixture(file));
      expect(events).toEqual([]);
      expect(summarizeGrootTimeline(events).eventCount).toBe(0);
      expect(groupGrootAttempts(events)).toEqual([]);
    }
  });
});

describe("groupGrootAttempts", () => {
  it("does not split fulfillment line immediately after inhouse in same burst", () => {
    const events = [
      { at: "20 May, 2026 12:48 IST", status: "placed", comments: "Order created on inhouse" },
      {
        at: "20 May, 2026 12:48 IST",
        status: "new",
        comments: "Order created with fulfillment status request_for_return_and_refund",
      },
    ];
    expect(groupGrootAttempts(events)).toHaveLength(1);
  });

  it("splits on inhouse creation anchors", () => {
    const events = [
      { at: "20 May, 2026 12:48 IST", status: "placed", comments: "Order created on inhouse" },
      {
        at: "20 May, 2026 12:48 IST",
        status: "new",
        comments: "Order created with fulfillment status request_for_return_and_refund",
      },
      { at: "21 May, 2026 08:07 IST", status: "placed", comments: "Order created on inhouse" },
      {
        at: "21 May, 2026 08:07 IST",
        status: "new",
        comments: "Order created with fulfillment status request_for_return_and_refund",
      },
    ];
    const attempts = groupGrootAttempts(events);
    expect(attempts).toHaveLength(2);
    expect(attempts[0].events).toHaveLength(2);
    expect(attempts[0].fulfillmentHint).toBe("request_for_return_and_refund");
  });

  it("returns one attempt when no inhouse anchor exists", () => {
    const events = parseGrootApiPayload(loadFixture("groot_parent.json"));
    expect(groupGrootAttempts(events)).toHaveLength(1);
  });
});

describe("buildStatusSegments", () => {
  it("merges consecutive identical statuses", () => {
    const segments = buildStatusSegments([
      { at: "14 May, 2026 13:42 IST", status: "new", sub_status: "" },
      { at: "14 May, 2026 13:43 IST", status: "new", sub_status: "" },
      { at: "14 May, 2026 14:24 IST", status: "completed", sub_status: "completed" },
    ]);
    expect(segments).toHaveLength(2);
    expect(segments[0].status).toBe("new");
    expect(segments[1].status).toBe("completed");
  });

  it("still emits segments for events without parseable timestamps", () => {
    const segments = buildStatusSegments([
      { at: "—", status: "parked", comments: "Order marked on hold" },
      { at: "14 May, 2026 14:24 IST", status: "completed", sub_status: "completed" },
    ]);
    expect(segments.length).toBeGreaterThanOrEqual(2);
    expect(segments.some((s) => s.timeUnknown)).toBe(true);
  });

  it("handles same-timestamp next event with positive duration fallback", () => {
    const segments = buildStatusSegments([
      { at: "14 May, 2026 14:24 IST", status: "new" },
      { at: "14 May, 2026 14:24 IST", status: "completed" },
    ]);
    expect(segments).toHaveLength(2);
    expect(segments[0].endMs).toBeGreaterThan(segments[0].startMs);
  });
});

describe("segmentWidthPcts", () => {
  it("returns widths that sum to approximately 100", () => {
    const events = parseGrootApiPayload(loadFixture("groot_parent.json"));
    const segments = buildGlobalStatusSegments(events);
    const widths = segmentWidthPcts(segments, 0, Date.UTC(2026, 4, 14, 20, 0));
    const sum = widths.reduce((a, b) => a + b, 0);
    expect(widths.length).toBe(segments.length);
    expect(sum).toBeGreaterThan(99);
    expect(sum).toBeLessThan(101);
  });

  it("returns empty array for no segments", () => {
    expect(segmentWidthPcts([], 0, 1)).toEqual([]);
  });
});

describe("sortGrootEventsChronological", () => {
  it("orders by parsed timestamp and pushes unknown times last", () => {
    const sorted = sortGrootEventsChronological([
      { at: "22 May, 2026 18:41 IST" },
      { at: "20 May, 2026 12:48 IST" },
      { at: "—" },
    ]);
    expect(sorted[0].at).toContain("20 May");
    expect(sorted[sorted.length - 1].at).toBe("—");
  });
});

describe("grootStatusTone", () => {
  it("handles empty and unknown safely", () => {
    expect(grootStatusTone(undefined)).toBe("neutral");
    expect(grootStatusTone("")).toBe("neutral");
    expect(grootStatusTone("completed")).toBe("success");
    expect(grootStatusTone("pickup_rescheduled")).toBe("warning");
  });
});

describe("isMissingDisplay", () => {
  it("treats em-dash and blanks as missing", () => {
    expect(isMissingDisplay("—")).toBe(true);
    expect(isMissingDisplay("  ")).toBe(true);
    expect(isMissingDisplay("completed")).toBe(false);
  });
});

describe("summarizeGrootTimeline", () => {
  it("reports unparsed count when timestamps are missing", () => {
    const summary = summarizeGrootTimeline([
      { at: "—", status: "parked" },
      { at: "14 May, 2026 14:24 IST", status: "completed" },
    ]);
    expect(summary.eventCount).toBe(2);
    expect(summary.unparsedCount).toBe(1);
    expect(summary.parseableCount).toBe(1);
  });
});

describe("computeGrootOutcome", () => {
  it("reports completed earlier when latest event is not completed (E15)", () => {
    const events = [
      { at: "28 Apr, 2026 13:31 IST", status: "completed", comments: "Order Completed" },
      { at: "22 May, 2026 18:41 IST", status: "parked", comments: "Order marked on hold" },
    ];
    const outcome = computeGrootOutcome(events, groupGrootAttempts(events));
    expect(outcome.everCompleted).toBe(true);
    expect(outcome.latestIsCompleted).toBe(false);
    expect(outcome.outcomeTitle).toContain("earlier");
  });
});

describe("friendlyStatusLabel", () => {
  it("maps raw API statuses to readable labels", () => {
    expect(friendlyStatusLabel("pickup_rescheduled")).toBe("Pickup rescheduled");
    expect(friendlyStatusLabel("out_for_delivery")).toBe("Out for delivery");
  });
});

describe("collectHubSequence", () => {
  it("returns ordered unique hubs", () => {
    const hubs = collectHubSequence([
      { at: "20 May, 2026 12:48 IST", comments: "transferred to hub makali fc" },
      { at: "21 May, 2026 08:07 IST", comments: "transferred to hub hsr dc" },
      { at: "21 May, 2026 08:44 IST", comments: "transferred to hub hsr dc" },
    ]);
    expect(hubs).toEqual(["makali fc", "hsr dc"]);
  });
});

describe("buildGrootInsights", () => {
  it("ranks dwell and repeats on groot_parent fixture", () => {
    const events = parseGrootApiPayload(loadFixture("groot_parent.json"));
    const insights = buildGrootInsights(events);
    expect(insights.outcome.everCompleted).toBe(true);
    expect(insights.stagesByDwell.length).toBeGreaterThan(0);
    expect(insights.longestBlocker?.status).toBeTruthy();
    expect(insights.hubSequence).toEqual([]);
    const completed = insights.stagesByDwell.find((s) => s.status === "completed");
    expect(completed?.eventCount).toBe(1);
  });

  it("flags cancellations, reschedules, and retries on dense flow", () => {
    const events = [
      { at: "20 May, 2026 12:48 IST", status: "placed", comments: "Order created on inhouse" },
      { at: "20 May, 2026 12:48 IST", status: "parked", comments: "Order Cancelled" },
      { at: "20 May, 2026 13:00 IST", status: "parked", comments: "Order marked on hold" },
      { at: "21 May, 2026 08:07 IST", status: "placed", comments: "Order created on inhouse" },
      { at: "21 May, 2026 08:10 IST", status: "pickup_rescheduled", comments: "Order Rescheduled" },
      { at: "21 May, 2026 08:15 IST", status: "parked", comments: "Order Unassigned from Batch 123" },
    ];
    const insights = buildGrootInsights(events);
    expect(insights.stagesRepeated.some((s) => s.status === "parked")).toBe(true);
    const cancel = insights.failures.find((f) => f.id === "cancel");
    expect(cancel).toBeTruthy();
    expect(cancel?.title).toBe("Cancel / retry events");
    expect(cancel?.tone).toBe("danger");
    expect(insights.failures.some((f) => f.id === "reschedule")).toBe(true);
    expect(insights.failures.some((f) => f.id === "unassign")).toBe(true);
    expect(insights.failures.some((f) => f.id === "retries")).toBe(true);
    expect(insights.outcome.everCompleted).toBe(false);
    expect(insights.failures.some((f) => f.id === "no_complete")).toBe(true);
  });

  it("uses warning tone for allocation-cancel only on completed PO (one_hour fixture)", () => {
    const events = parseGrootApiPayload(
      loadFixture("cases/one_hour_groot_3p_wait/groot.json"),
    );
    const insights = buildGrootInsights(events);
    expect(groupGrootAttempts(events)).toHaveLength(1);
    expect(insights.failures.some((f) => f.id === "retries")).toBe(false);
    const cancel = insights.failures.find((f) => f.id === "cancel");
    expect(cancel?.count).toBe(1);
    expect(cancel?.title).toBe("Cancel / retry events");
    expect(cancel?.tone).toBe("warning");
    expect(insights.headlines.some((h) => h.includes("cancel/retry"))).toBe(true);
    expect(insights.headlines.some((h) => h.includes("fulfillment attempts"))).toBe(false);
  });

  it("includes hub names on hub failure card", () => {
    const insights = buildGrootInsights([
      { at: "20 May, 2026 12:48 IST", comments: "transferred to hub makali fc" },
      { at: "21 May, 2026 08:07 IST", comments: "transferred to hub hsr dc" },
    ]);
    const hub = insights.failures.find((f) => f.id === "hub");
    expect(hub?.detail).toContain("makali fc");
  });

  it("returns empty-safe insights for empty input", () => {
    const insights = buildGrootInsights([]);
    expect(insights.stagesByDwell).toEqual([]);
    expect(insights.failures).toEqual([]);
    expect(insights.headlines).toEqual([]);
  });
});

describe("edge cases E6 E7", () => {
  it("E6: all unparseable timestamps still returns insights shell", () => {
    const insights = buildGrootInsights([{ at: "—", status: "parked" }]);
    expect(insights.showDwellDisclaimer).toBe(true);
    expect(insights.stagesByDwell.every((s) => s.dwellMin === 0)).toBe(true);
  });

  it("E7: single event timeline", () => {
    const events = [{ at: "14 May, 2026 14:24 IST", status: "completed" }];
    const insights = buildGrootInsights(events);
    expect(insights.stagesByDwell.length).toBeGreaterThan(0);
    expect(groupGrootAttempts(events)).toHaveLength(1);
  });
});

describe("shouldSplitAttemptBucket", () => {
  it("returns false for paired fulfillment after inhouse", () => {
    const current = [{ comments: "Order created on inhouse" } as const];
    const next = { comments: "Order created with fulfillment status x" };
    expect(shouldSplitAttemptBucket(next, current)).toBe(false);
  });

  it("does not split packaging create after early wfdp pings", () => {
    const current = [
      { comments: "Batching Disabled" },
      { comments: "Order allocation attempt started for inhouse" },
    ];
    const next = { comments: "Order created with fulfillment status packaging" };
    expect(shouldSplitAttemptBucket(next, current)).toBe(false);
  });
});
