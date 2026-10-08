import { describe, expect, it } from "vitest";
import {
  RESPONDER_EVAL_SEGMENTS,
  responderEvalReasonTagValue,
} from "./responderEval";

describe("responderEval segments", () => {
  it("defines composite bot cohorts and human in order", () => {
    expect(RESPONDER_EVAL_SEGMENTS.map((s) => s.id)).toEqual([
      "composite",
      "bot_closed",
      "bot_pre_handoff",
      "human",
    ]);
  });
});

describe("responderEval reason filter helpers", () => {
  it("maps attribute ids to stored issue tags", () => {
    expect(responderEvalReasonTagValue("traceability_failure", "unsupported_refund")).toBe(
      "traceability:unsupported_refund"
    );
    expect(responderEvalReasonTagValue("hallucination", "severity_P0")).toBe(
      "hallucination_severity:P0"
    );
    expect(responderEvalReasonTagValue("resolution:partial", "delivered")).toBe(
      "resolution_lifecycle:delivered"
    );
  });
});
describe("responderEval timeseries segment volume keys", () => {
  const point = {
    bucket_start: "2026-05-01T00:00:00+00:00",
    total: 10,
    total_bot: 6,
    total_bot_closed: 4,
    total_bot_pre_handoff: 2,
    total_human: 4,
    avg_score: 80,
    avg_bot_score: 75,
    avg_bot_closed_score: 78,
    avg_bot_pre_handoff_score: 70,
    avg_human_score: 85,
  };

  it("maps segment volume fields including bot cohorts", () => {
    expect(point.total).toBe(10);
    expect(point.total_bot).toBe(6);
    expect(point.total_bot_closed + point.total_bot_pre_handoff).toBe(point.total_bot);
    expect(point.total_human).toBe(4);
  });
});
