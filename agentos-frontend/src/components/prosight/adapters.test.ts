import { describe, expect, it } from "vitest";
import {
  rowBaseline,
  seriesBaseline,
  normDirection,
  hasBaselines,
  isNonReconciling,
  childShareDiff,
  seriesDimOf,
  knownSeriesDims,
  pairDims,
  pairCutsFor,
  pairOtherDim,
  pairChildren,
  cutReconciles,
  BASELINE_MODES,
} from "./adapters";
import type {
  ProsightBaseline,
  ProsightNewsData,
  ProsightTimeseriesPoint,
  ProsightTodayRow,
  DimensionRow,
} from "./types";

// ── Fixture builders ─────────────────────────────────────────────────────────

function makeRow(overrides: Partial<ProsightTodayRow> = {}): ProsightTodayRow {
  return {
    label: "TOTAL",
    level: "L0_total",
    group_id: "total::TOTAL",
    full_id: "L0_total::total::TOTAL",
    dims: {},
    actual: 1450,
    predicted: 1666,
    magnitude: -216,
    pct_change: -13.0,
    direction: "drop",
    score: 3.1,
    streak: 2,
    qualified: true,
    driver: "",
    rca: null,
    longest_streak: 2,
    anomaly_ratio: 0.1,
    total_flagged_days: 5,
    cumulative_magnitude: -800,
    ...overrides,
  };
}

// Real backend schema: `actual` is today's value (constant across modes);
// `anchor_value` is the comparison basis; `delta = actual − anchor_value`.
const realBaselines: Record<string, ProsightBaseline> = {
  forecast: {
    actual: 1450,
    anchor_value: 1666,
    delta: -216,
    pct: -13.0,
    lower: 1280,
    upper: 1620,
    is_anomaly: true,
    direction: "drop",
  },
  vs_prev_day: {
    actual: 1450,
    anchor_value: 1526,
    delta: -76,
    pct: -5.0,
    lower: null,
    upper: null,
    is_outside_band: null,
    direction: "drop",
  },
  vs_prev_week: {
    actual: 1450,
    anchor_value: 1516,
    delta: -66,
    pct: -4.4,
    lower: 1180,
    upper: 1410,
    is_outside_band: false,
    direction: "drop",
  },
};

function makeData(latestRow: ProsightTodayRow, opts: {
  perBu?: boolean;
  defaultBu?: string;
  nonReconciling?: string[];
} = {}): ProsightNewsData {
  const day = opts.perBu
    ? {
        today_count: 1,
        today_drops: 1,
        today_spikes: 0,
        l0_today: true,
        prev_count: 0,
        top_alert: null,
        today_rows: [],
        per_bu: { pharmacy: { today_rows: [latestRow] } },
      }
    : {
        today_count: 1,
        today_drops: 1,
        today_spikes: 0,
        l0_today: true,
        prev_count: 0,
        top_alert: null,
        today_rows: [latestRow],
      };
  return {
    dates: ["2026-06-29", "2026-06-30"],
    bus: opts.perBu ? ["pharmacy", "labs"] : undefined,
    default_bu: opts.defaultBu,
    non_reconciling_cuts: opts.nonReconciling,
    data_by_date: { "2026-06-30": day },
    series_timeseries: {},
    feature_importance: {},
  } as ProsightNewsData;
}

// ── rowBaseline ──────────────────────────────────────────────────────────────

describe("rowBaseline", () => {
  it("returns null for a null row", () => {
    expect(rowBaseline(null, "forecast")).toBeNull();
    expect(rowBaseline(undefined, "vs_prev_day")).toBeNull();
  });

  it("synthesizes a forecast baseline from legacy fields when baselines absent", () => {
    // actual=1450 today, predicted (basis)=1666, delta = actual − predicted.
    const b = rowBaseline(makeRow(), "forecast");
    expect(b).not.toBeNull();
    expect(b!.actual).toBe(1450);
    expect(b!.anchor_value).toBe(1666);
    expect(b!.delta).toBe(-216);
    expect(b!.pct).toBeCloseTo((-216 / 1666) * 100, 4);
    expect(b!.lower).toBeNull();
    expect(b!.upper).toBeNull();
    expect(b!.is_anomaly).toBe(true);
    expect(b!.direction).toBe("drop");
  });

  it("returns null for a static mode on a legacy row (no baselines)", () => {
    expect(rowBaseline(makeRow(), "vs_prev_day")).toBeNull();
    expect(rowBaseline(makeRow(), "vs_prev_week")).toBeNull();
  });

  it("returns the real baseline object for every mode when present", () => {
    const row = makeRow({ baselines: realBaselines as ProsightTodayRow["baselines"] });
    // rowBaseline normalizes `direction`, so compare by value not identity.
    expect(rowBaseline(row, "forecast")).toEqual(realBaselines.forecast);
    expect(rowBaseline(row, "vs_prev_day")).toEqual(realBaselines.vs_prev_day);
    expect(rowBaseline(row, "vs_prev_week")).toEqual(realBaselines.vs_prev_week);
  });

  it("prefers the real forecast baseline over the synthesized one", () => {
    const row = makeRow({ baselines: realBaselines as ProsightTodayRow["baselines"] });
    // real forecast has a band; synthesized would have null lower/upper
    expect(rowBaseline(row, "forecast")!.lower).toBe(1280);
  });

  it("maps a backend 'normal' direction to null", () => {
    const baselines = {
      forecast: { ...realBaselines.forecast, direction: "normal" },
    } as unknown as ProsightTodayRow["baselines"];
    const row = makeRow({ baselines });
    expect(rowBaseline(row, "forecast")!.direction).toBeNull();
  });
});

describe("normDirection", () => {
  it("passes through spike / drop / mixed and nulls everything else", () => {
    expect(normDirection("spike")).toBe("spike");
    expect(normDirection("drop")).toBe("drop");
    expect(normDirection("mixed")).toBe("mixed");
    expect(normDirection("normal")).toBeNull();
    expect(normDirection(null)).toBeNull();
    expect(normDirection(undefined)).toBeNull();
  });
});

describe("seriesBaseline", () => {
  // Distinct daily actuals so anchor lookups are unambiguous.
  const series: ProsightTimeseriesPoint[] = [
    { date: "2026-06-23", actual: 1000, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-24", actual: 1010, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-25", actual: 1020, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-26", actual: 1030, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-27", actual: 1040, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-28", actual: 1050, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-29", actual: 1200, predicted: 0, lower: 0, upper: 0 },
    { date: "2026-06-30", actual: 1150, predicted: 0, lower: 0, upper: 0 },
  ];

  it("returns null for forecast mode or empty series", () => {
    expect(seriesBaseline(series, "2026-06-30", "forecast")).toBeNull();
    expect(seriesBaseline([], "2026-06-30", "vs_prev_day")).toBeNull();
    expect(seriesBaseline(undefined, "2026-06-30", "vs_prev_day")).toBeNull();
  });

  it("compares today to yesterday (offset 1)", () => {
    const b = seriesBaseline(series, "2026-06-30", "vs_prev_day")!;
    expect(b.actual).toBe(1150);
    expect(b.anchor_value).toBe(1200); // 06-29
    expect(b.delta).toBe(-50);
    expect(b.pct).toBeCloseTo((-50 / 1200) * 100, 4);
    expect(b.direction).toBe("drop");
    expect(b.lower).toBeNull();
  });

  it("compares today to the same day last week (offset 7)", () => {
    const b = seriesBaseline(series, "2026-06-30", "vs_prev_week")!;
    expect(b.actual).toBe(1150);
    expect(b.anchor_value).toBe(1000); // 06-23
    expect(b.delta).toBe(150);
    expect(b.direction).toBe("spike");
  });

  it("returns null when the compared day is missing", () => {
    // no 06-22 in the series, so vs_prev_week on 06-29 has no basis
    expect(seriesBaseline(series, "2026-06-29", "vs_prev_week")).toBeNull();
  });
});

// ── hasBaselines ─────────────────────────────────────────────────────────────

describe("hasBaselines", () => {
  it("is false for null / empty data", () => {
    expect(hasBaselines(null)).toBe(false);
    expect(hasBaselines(undefined)).toBe(false);
  });

  it("is false for legacy L0-only data (no baselines)", () => {
    expect(hasBaselines(makeData(makeRow()))).toBe(false);
  });

  it("is false when only a forecast baseline is present (nothing to toggle to)", () => {
    const row = makeRow({
      baselines: { forecast: realBaselines.forecast } as ProsightTodayRow["baselines"],
    });
    expect(hasBaselines(makeData(row))).toBe(false);
  });

  it("is true when static baselines are present on the flat latest-day L0 row", () => {
    const row = makeRow({ baselines: realBaselines as ProsightTodayRow["baselines"] });
    expect(hasBaselines(makeData(row))).toBe(true);
  });

  it("is true when static baselines live in the default-BU per_bu slice", () => {
    const row = makeRow({ baselines: realBaselines as ProsightTodayRow["baselines"] });
    const data = makeData(row, { perBu: true, defaultBu: "pharmacy" });
    expect(hasBaselines(data)).toBe(true);
  });
});

// ── isNonReconciling ─────────────────────────────────────────────────────────

describe("isNonReconciling", () => {
  it("flags SKU-style cuts via the name fallback", () => {
    expect(isNonReconciling("sku_name")).toBe(true);
    expect(isNonReconciling("sku")).toBe(true);
    expect(isNonReconciling("name")).toBe(true);
  });

  it("does not flag ordinary partition cuts", () => {
    expect(isNonReconciling("channel")).toBe(false);
    expect(isNonReconciling("hour")).toBe(false);
    expect(isNonReconciling("state")).toBe(false);
  });

  it("is false for empty / missing dimension names", () => {
    expect(isNonReconciling(null)).toBe(false);
    expect(isNonReconciling(undefined)).toBe(false);
    expect(isNonReconciling("")).toBe(false);
  });

  it("honors a backend-declared list over the fallback", () => {
    const data = makeData(makeRow(), { nonReconciling: ["basket_item"] });
    expect(isNonReconciling("basket_item", data)).toBe(true);
    // once the backend declares the set, the name fallback no longer applies
    expect(isNonReconciling("sku_name", data)).toBe(false);
  });
});

// ── childShareDiff ───────────────────────────────────────────────────────────

function makeDimRow(overrides: Partial<DimensionRow> = {}): DimensionRow {
  return {
    label: "seg",
    actual: 100,
    forecast: 120,
    lower: 110,
    upper: 130,
    point_diff: -20,
    point_share_pct: 25,
    band_diff: 0,
    band_share_pct: 0,
    ...overrides,
  };
}

describe("childShareDiff", () => {
  it("returns the attributed figure when present", () => {
    expect(childShareDiff(makeDimRow({ attributed_point_diff: -8 }))).toBe(-8);
  });

  it("returns attributed 0 (not the raw point_diff) when attributed is exactly 0", () => {
    expect(childShareDiff(makeDimRow({ attributed_point_diff: 0 }))).toBe(0);
  });

  it("falls back to raw point_diff when attributed is absent", () => {
    expect(childShareDiff(makeDimRow())).toBe(-20);
  });
});

// ── Part C — L2 pair cuts (spec 40) ──────────────────────────────────────────

// Fixture mirrors the real 2026-07-15 labs payload: L1 dims city/channel/name,
// pair series `L2_pair::<cut>::<valA>|<valB>`, and the two cut-level rollup
// keys with no `|` in the value part.
const AS_OF = "2026-07-13";

function pt(
  overrides: Partial<ProsightTimeseriesPoint> = {},
): ProsightTimeseriesPoint {
  return { date: AS_OF, actual: 100, predicted: 90, lower: 80, upper: 100, ...overrides };
}

const pairSeries: Record<string, ProsightTimeseriesPoint[]> = {
  "L1_single::city::delhi": [pt()],
  "L1_single::channel::1mg-app/web": [pt()],
  "L1_single::name::cbc (complete blood count)": [pt()],
  "L2_pair::city_channel::delhi|1mg-app/web": [
    { ...pt({ actual: 89, predicted: 72.8, lower: 60.4, upper: 85.2 }), is_anomaly: true, qualified: true, direction: "spike", score: 3.73 },
    pt({ date: "2026-07-12", actual: 70 }),
    pt({ date: "2026-07-06", actual: 60 }),
  ],
  "L2_pair::city_channel::delhi|telesales": [
    { ...pt({ actual: 40, predicted: 45 }), is_anomaly: true, qualified: false, direction: "drop" },
  ],
  "L2_pair::city_channel::mumbai|telesales": [pt()],
  "L2_pair::city_name::delhi|cbc (complete blood count)": [pt({ actual: 55, predicted: 50 })],
  "L2_pair::name_channel::cbc (complete blood count)|telesales": [pt()],
  // rollup buckets — value part has no `|`, must never parse as a pair
  "L2_pair::city_channel::low order": [pt()],
  "L2_pair::city_channel::unqualified": [pt()],
};

describe("seriesDimOf", () => {
  it("maps RCA sku dims to the series vocabulary and passes others through", () => {
    expect(seriesDimOf("sku_name")).toBe("name");
    expect(seriesDimOf("sku")).toBe("name");
    expect(seriesDimOf("city")).toBe("city");
  });
});

describe("knownSeriesDims", () => {
  it("collects L1 dims from series keys", () => {
    expect(knownSeriesDims(pairSeries).sort()).toEqual(["channel", "city", "name"]);
  });

  it("is empty for missing maps", () => {
    expect(knownSeriesDims(undefined)).toEqual([]);
    expect(knownSeriesDims({})).toEqual([]);
  });
});

describe("pairDims", () => {
  const known = ["city", "channel", "name", "coupon_flag", "payment_method"];

  it("splits simple pair keys", () => {
    expect(pairDims("city_channel", known)).toEqual(["city", "channel"]);
    expect(pairDims("name_channel", known)).toEqual(["name", "channel"]);
  });

  it("handles dims that themselves contain underscores", () => {
    expect(pairDims("coupon_flag_payment_method", known)).toEqual([
      "coupon_flag",
      "payment_method",
    ]);
  });

  it("returns null when no boundary yields two known dims", () => {
    expect(pairDims("city_bogus", known)).toBeNull();
    expect(pairDims("channel", known)).toBeNull();
  });
});

describe("pairCutsFor", () => {
  it("finds every cut containing the dim (via real pair series only)", () => {
    expect(pairCutsFor(pairSeries, "city").sort()).toEqual([
      "city_channel",
      "city_name",
    ]);
    expect(pairCutsFor(pairSeries, "channel").sort()).toEqual([
      "city_channel",
      "name_channel",
    ]);
  });

  it("normalizes the RCA sku dim to the series vocabulary", () => {
    expect(pairCutsFor(pairSeries, "sku_name").sort()).toEqual([
      "city_name",
      "name_channel",
    ]);
  });

  it("is empty for non-pair dims and payloads without L2 series", () => {
    expect(pairCutsFor(pairSeries, "hour")).toEqual([]);
    expect(pairCutsFor({ "L1_single::city::delhi": [pt()] }, "city")).toEqual([]);
    expect(pairCutsFor(undefined, "city")).toEqual([]);
  });
});

describe("pairOtherDim", () => {
  const known = ["city", "channel", "name"];

  it("returns the other side, normalizing sku_name", () => {
    expect(pairOtherDim("city_channel", "city", known)).toBe("channel");
    expect(pairOtherDim("city_channel", "channel", known)).toBe("city");
    expect(pairOtherDim("city_name", "sku_name", known)).toBe("city");
  });

  it("returns null when the dim is not a side", () => {
    expect(pairOtherDim("city_channel", "hour", known)).toBeNull();
    expect(pairOtherDim("bogus", "city", known)).toBeNull();
  });
});

describe("pairChildren", () => {
  it("builds rows for the clicked value, labelled by the other side", () => {
    const rows = pairChildren(pairSeries, "city_channel", "city", "delhi", AS_OF);
    expect(rows.map((r) => r.label).sort()).toEqual(["1mg-app/web", "telesales"]);
    const app = rows.find((r) => r.label === "1mg-app/web")!;
    expect(app.group_id).toBe("city_channel::delhi|1mg-app/web");
    expect(app.actual).toBe(89);
    expect(app.forecast).toBe(72.8);
    expect(app.point_diff).toBeCloseTo(16.2, 6);
    expect(app.band_diff).toBeCloseTo(89 - 85.2, 6);
    expect(app.flagged).toBe(true);
    expect(app.anom).toBe("spike");
  });

  it("matches the correct side (channel view of the same cut)", () => {
    const rows = pairChildren(pairSeries, "city_channel", "channel", "telesales", AS_OF);
    expect(rows.map((r) => r.label).sort()).toEqual(["delhi", "mumbai"]);
  });

  it("gates the anomaly pill on qualification", () => {
    const rows = pairChildren(pairSeries, "city_channel", "city", "delhi", AS_OF);
    const tele = rows.find((r) => r.label === "telesales")!;
    expect(tele.flagged).toBe(false); // is_anomaly but qualified:false
    expect(tele.anom).toBeNull();
  });

  it("ignores rollup keys and pairs with no as-of point", () => {
    // rollup buckets share the cut prefix but are not (city, channel) pairs
    const low = pairChildren(pairSeries, "city_channel", "city", "low order", AS_OF);
    expect(low).toEqual([]);
    // delhi|telesales has no point on 07-12
    const rows = pairChildren(pairSeries, "city_channel", "city", "delhi", "2026-07-12");
    expect(rows.map((r) => r.label)).toEqual(["1mg-app/web"]);
  });

  it("enriches the score from a matching flagged today_row", () => {
    const l2Row = makeRow({
      level: "L2_pair",
      group_id: "city_channel::delhi|1mg-app/web",
      score: 4.49,
    });
    const rows = pairChildren(pairSeries, "city_channel", "city", "delhi", AS_OF, [l2Row]);
    expect(rows.find((r) => r.label === "1mg-app/web")!.self_z).toBe(4.49);
    // without a match, falls back to the point's own score
    const bare = pairChildren(pairSeries, "city_channel", "city", "delhi", AS_OF);
    expect(bare.find((r) => r.label === "1mg-app/web")!.self_z).toBe(3.73);
  });

  it("returns [] for unknown cuts, sides, or missing inputs", () => {
    expect(pairChildren(pairSeries, "bogus_cut", "city", "delhi", AS_OF)).toEqual([]);
    expect(pairChildren(pairSeries, "city_channel", "hour", "9", AS_OF)).toEqual([]);
    expect(pairChildren(undefined, "city_channel", "city", "delhi", AS_OF)).toEqual([]);
    expect(pairChildren(pairSeries, "city_channel", "city", "delhi", "")).toEqual([]);
  });

  it("static baselines work off the returned group_id (integration with seriesBaseline)", () => {
    const rows = pairChildren(pairSeries, "city_channel", "city", "delhi", AS_OF);
    const app = rows.find((r) => r.label === "1mg-app/web")!;
    const b = seriesBaseline(
      pairSeries[`L2_pair::${app.group_id}`],
      AS_OF,
      "vs_prev_day",
    )!;
    expect(b.anchor_value).toBe(70);
    expect(b.delta).toBe(19);
  });
});

describe("cutReconciles", () => {
  const known = ["city", "channel", "name"];

  function dataWithContract(): ProsightNewsData {
    const d = makeData(makeRow());
    d.display_contract = {
      by_bu: {
        labs: {
          city_channel: { level: "L2_pair", reconciled: true, non_partitioning: false },
          city_name: { level: "L2_pair", reconciled: false, non_partitioning: true },
          name_channel: { level: "L2_pair", reconciled: false, non_partitioning: true },
        },
      },
    };
    return d;
  }

  it("prefers display_contract flags", () => {
    const d = dataWithContract();
    expect(cutReconciles(d, "labs", "city_channel", known)).toBe(true);
    expect(cutReconciles(d, "labs", "city_name", known)).toBe(false);
    expect(cutReconciles(d, "labs", "name_channel", known)).toBe(false);
  });

  it("falls back to per-side isNonReconciling when undeclared", () => {
    const d = makeData(makeRow()); // no display_contract
    expect(cutReconciles(d, "labs", "city_channel", known)).toBe(true);
    expect(cutReconciles(d, "labs", "city_name", known)).toBe(false); // name side overlaps
    expect(cutReconciles(null, undefined, "name_channel", known)).toBe(false);
  });

  it("treats plain L1 dims by their own reconciliation", () => {
    expect(cutReconciles(null, undefined, "channel", known)).toBe(true);
    expect(cutReconciles(null, undefined, "sku_name", known)).toBe(false);
  });
});

// ── module invariants ────────────────────────────────────────────────────────

describe("BASELINE_MODES", () => {
  it("lists forecast first (the default)", () => {
    expect(BASELINE_MODES[0]).toBe("forecast");
    expect(BASELINE_MODES).toEqual(["forecast", "vs_prev_day", "vs_prev_week"]);
  });
});

// ── malformed-payload handling ───────────────────────────────────────────────
//
// The `./types` declarations are an unenforced compile-time claim about a
// still-evolving backend (see the note atop adapters.ts). These cast through
// `unknown` on purpose: they assert what happens when the payload violates the
// declared types at runtime, which is precisely what TypeScript cannot catch.

describe("malformed numeric fields", () => {
  const bad = (v: unknown) => v as number;

  it("rowBaseline yields no forecast rather than a NaN one", () => {
    for (const v of [null, undefined, "1450", NaN, Infinity, {}]) {
      expect(rowBaseline(makeRow({ actual: bad(v) }), "forecast")).toBeNull();
      expect(rowBaseline(makeRow({ predicted: bad(v) }), "forecast")).toBeNull();
    }
  });

  it("rowBaseline treats a malformed provided block as absent", () => {
    const row = makeRow({
      baselines: {
        vs_prev_day: { ...realBaselines.vs_prev_day, anchor_value: bad("1526") },
      } as ProsightTodayRow["baselines"],
    });
    expect(rowBaseline(row, "vs_prev_day")).toBeNull();
  });

  it("rowBaseline recomputes a malformed delta/pct from sound core numbers", () => {
    const row = makeRow({
      baselines: {
        vs_prev_day: {
          ...realBaselines.vs_prev_day,
          delta: bad(null),
          pct: bad("-5.0"),
        },
      } as ProsightTodayRow["baselines"],
    });
    const b = rowBaseline(row, "vs_prev_day")!;
    expect(b.delta).toBe(1450 - 1526);
    expect(b.pct).toBeCloseTo((-76 / 1526) * 100, 6);
  });

  it("seriesBaseline rejects a numeric string (the old NaN check let it pass)", () => {
    const series: ProsightTimeseriesPoint[] = [
      { date: "2026-06-29", actual: 1200, predicted: 0, lower: 0, upper: 0 },
      { date: "2026-06-30", actual: bad("1150"), predicted: 0, lower: 0, upper: 0 },
    ];
    expect(seriesBaseline(series, "2026-06-30", "vs_prev_day")).toBeNull();
  });

  it("seriesBaseline survives an unparseable as-of date instead of throwing", () => {
    const series: ProsightTimeseriesPoint[] = [
      { date: "not-a-date", actual: 1150, predicted: 0, lower: 0, upper: 0 },
    ];
    expect(() => seriesBaseline(series, "not-a-date", "vs_prev_day")).not.toThrow();
    expect(seriesBaseline(series, "not-a-date", "vs_prev_day")).toBeNull();
  });

  it("childShareDiff never returns a non-number", () => {
    // A string attributed figure is not trusted — falls back to point_diff.
    expect(childShareDiff(makeDimRow({ attributed_point_diff: bad("-12") }))).toBe(-20);
    expect(
      childShareDiff(makeDimRow({ attributed_point_diff: bad(null), point_diff: bad("x") })),
    ).toBeNull();
  });

  it("pairChildren skips a point whose actual is malformed", () => {
    const series = {
      ...pairSeries,
      "L2_pair::city_channel::delhi|telesales": [
        { ...pt({ actual: bad("40"), predicted: 45 }) },
      ],
    };
    const rows = pairChildren(series, "city_channel", "city", "delhi", AS_OF);
    expect(rows.map((r) => r.label)).toEqual(["1mg-app/web"]);
  });

  it("pairChildren reports no band excursion when a bound is missing", () => {
    const series = {
      ...pairSeries,
      "L2_pair::city_channel::delhi|telesales": [
        pt({ actual: 40, predicted: 45, lower: bad(null), upper: bad(null) }),
      ],
    };
    const row = pairChildren(series, "city_channel", "city", "delhi", AS_OF).find(
      (r) => r.label === "telesales",
    )!;
    expect(row.band_diff).toBe(0);
    expect(Number.isNaN(row.point_diff)).toBe(false);
    expect(row.point_diff).toBeCloseTo(-5, 6);
  });
});
