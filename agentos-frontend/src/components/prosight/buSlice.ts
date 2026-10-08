/**
 * Multi-BU slicing helpers (spec 27).
 *
 * The backend emits a backward-compatible prosight_news.json: the legacy
 * top-level shape (data_by_date[d].today_rows, series_timeseries[full_id],
 * feature_importance[level_tag]) is populated from the DEFAULT_BU, while each
 * BU's full slice lives under data_by_date[d].per_bu[bu], series_timeseries[bu]
 * and feature_importance[bu], plus top-level `bus` / `default_bu`.
 *
 * These helpers return the per-BU slice when present and gracefully fall back
 * to the legacy flat shape otherwise — so the UI renders correctly against both
 * the new (per-BU) JSON and any older JSON still in flight.
 */
import type {
  ProsightNewsData,
  ProsightDayData,
  ProsightTimeseriesPoint,
  ProsightFeatureLevelData,
} from "./types";

export type SeriesMap = Record<string, ProsightTimeseriesPoint[]>;
export type FeatureMap = Record<string, ProsightFeatureLevelData>;

/** Resolve the effective BU: an explicit selection, else the JSON's default. */
export function resolveBu(
  data: ProsightNewsData | null | undefined,
  selected?: string | null,
): string {
  if (selected && selected !== "all") return selected;
  return data?.default_bu ?? data?.bus?.[0] ?? "pharmacy";
}

/** Per-BU DayData slice, falling back to the legacy top-level day payload. */
export function buDay(
  day: ProsightDayData | undefined | null,
  bu: string,
): ProsightDayData | undefined {
  if (!day) return undefined;
  return day.per_bu?.[bu] ?? day;
}

/** Per-BU series map ({full_id: points[]}), falling back to the flat top-level map. */
export function buSeries(
  data: ProsightNewsData | null | undefined,
  bu: string,
): SeriesMap {
  if (data?.bus?.includes(bu)) {
    const slice = (data.series_timeseries as Record<string, unknown>)?.[bu];
    if (slice) return slice as SeriesMap;
  }
  return (data?.series_timeseries ?? {}) as SeriesMap;
}

/** Per-BU feature-importance map ({level_tag: block}), falling back to flat top-level. */
export function buFeatureImportance(
  data: ProsightNewsData | null | undefined,
  bu: string,
): FeatureMap {
  if (data?.bus?.includes(bu)) {
    const slice = (data.feature_importance as Record<string, unknown>)?.[bu];
    if (slice) return slice as FeatureMap;
  }
  return (data?.feature_importance ?? {}) as FeatureMap;
}
