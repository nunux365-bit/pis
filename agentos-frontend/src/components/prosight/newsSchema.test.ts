/**
 * Tests for the `/api/prosight/news` validation boundary.
 *
 * Two halves, and the second matters as much as the first: the schema must
 * reject malformed payloads *and* must not reject the shapes that render
 * correctly today. A false rejection here blanks the whole dashboard, which is
 * strictly worse than the NaN it was written to prevent.
 */
import { describe, expect, it } from "vitest";

import { ProsightNewsSchemaError, parseProsightNews } from "./newsSchema";

const minimal = {
  dates: ["2026-08-17", "2026-08-18"],
  data_by_date: {},
  series_timeseries: {},
  feature_importance: {},
};

describe("parseProsightNews — rejects", () => {
  it("a non-object response", () => {
    // An HTML error page served with 200 is the realistic case.
    expect(() => parseProsightNews("<html>502</html>")).toThrow(
      ProsightNewsSchemaError
    );
    expect(() => parseProsightNews(null)).toThrow(ProsightNewsSchemaError);
    expect(() => parseProsightNews([])).toThrow(ProsightNewsSchemaError);
  });

  it("a missing `dates` — the field both call sites index immediately", () => {
    expect(() => parseProsightNews({ data_by_date: {} })).toThrow(
      ProsightNewsSchemaError
    );
  });

  it("`dates` holding non-strings", () => {
    expect(() => parseProsightNews({ ...minimal, dates: [20260818] })).toThrow(
      ProsightNewsSchemaError
    );
  });

  it("names the offending path in the message", () => {
    try {
      parseProsightNews({ ...minimal, dates: [20260818] });
      throw new Error("should have thrown");
    } catch (e) {
      const err = e as ProsightNewsSchemaError;
      expect(err).toBeInstanceOf(ProsightNewsSchemaError);
      // Without the path the error is unactionable — this is the whole point of
      // failing loudly rather than producing NaN downstream.
      expect(err.message).toContain("dates[0]");
      expect(err.message).toContain("expected string");
    }
  });
});

describe("parseProsightNews — accepts what renders today", () => {
  it("the minimal payload", () => {
    expect(parseProsightNews(minimal).dates).toEqual([
      "2026-08-17",
      "2026-08-18",
    ]);
  });

  it("an empty `dates` — legitimate before the first sync", () => {
    expect(parseProsightNews({ ...minimal, dates: [] }).dates).toEqual([]);
  });

  it("null numeric leaves, which live payloads genuinely carry", () => {
    // `adapters.ts` screens these with `finite()`. A schema demanding
    // z.number() here would reject data that renders fine.
    const withNulls = {
      ...minimal,
      series_timeseries: {
        "L0_total::total::TOTAL": [
          { date: "2026-08-18", actual: 10, predicted: null, lower: null, upper: null },
        ],
      },
    };
    expect(() => parseProsightNews(withNulls)).not.toThrow();
  });

  it("both polymorphic shapes of series_timeseries", () => {
    // Per the note on ProsightNewsData these maps carry BOTH legacy flat keys
    // (array values) AND per-BU sub-objects (object values). Asserting either
    // one would reject the other.
    const legacyFlat = {
      ...minimal,
      series_timeseries: { "L0_total::total::TOTAL": [{ date: "2026-08-18" }] },
    };
    const perBu = {
      ...minimal,
      series_timeseries: {
        pharma: { "L0_total::total::TOTAL": [{ date: "2026-08-18" }] },
      },
    };
    expect(() => parseProsightNews(legacyFlat)).not.toThrow();
    expect(() => parseProsightNews(perBu)).not.toThrow();
  });

  it("unknown extra fields, and passes them through intact", () => {
    // Live snapshots carry `generated_at`, which this schema does not declare.
    // zod strips unknown keys by default, so asserting only "does not throw"
    // would have missed the schema silently deleting them.
    const withExtra = {
      ...minimal,
      generated_at: "2026-08-19T05:00:00Z",
      some_new_spec_41_field: { a: 1 },
    };
    const out = parseProsightNews(withExtra) as unknown as Record<
      string,
      unknown
    >;
    expect(out.generated_at).toBe("2026-08-19T05:00:00Z");
    expect(out.some_new_spec_41_field).toEqual({ a: 1 });
  });

  it("the optional multi-BU fields when present, and when absent", () => {
    const full = {
      ...minimal,
      bus: ["pharma", "labs"],
      default_bu: "pharma",
      non_reconciling_cuts: ["sku_name"],
    };
    expect(parseProsightNews(full).bus).toEqual(["pharma", "labs"]);
    expect(parseProsightNews(minimal).bus).toBeUndefined();
  });
});

describe("parseProsightNews — defaults", () => {
  it("substitutes empty maps so indexing cannot throw", () => {
    // `undefined` here would be a TypeError on the first `data.data_by_date[d]`.
    const out = parseProsightNews({ dates: [] });
    expect(out.data_by_date).toEqual({});
    expect(out.series_timeseries).toEqual({});
    expect(out.feature_importance).toEqual({});
  });
});
