/**
 * Local types for NewsPage - Prosight N Dashboard
 */

import type { TodayRow, SeriesTimeseries } from "../types";
import type { LEVELS } from "./constants";

// Re-export LevelType
export type LevelType = (typeof LEVELS)[number];

// ══════════════════════════════════════════════════════════════════════════════
// Filter State
// ══════════════════════════════════════════════════════════════════════════════

export interface FilterState {
  dir: "all" | "drop" | "spike";
  minScore: number;
  levels: Set<string>;
  bu: string;
  qualifiedOnly: boolean;
}

// ══════════════════════════════════════════════════════════════════════════════
// Series Types
// ══════════════════════════════════════════════════════════════════════════════

export interface SeriesInfo {
  fid: string;
  level: string;
  dims: Record<string, string | null>;
  label: string;
}

export interface SeriesCardData extends TodayRow {
  dates: string[];
  dateDir: Record<string, string>;
  scores: number[];
  magnitudes: number[];
  drops: number;
  spikes: number;
  recentRow: TodayRow & { _date: string };
  flaggedDays: number;
  maxScore: number;
  avgMagnitude: number;
  todayMagnitude?: number | null;
  todayBandDiff?: number | null;
  todayDirection?: string | null;
  todayAlertDir?: string | null;
  todayAlertMag?: number | null;
  pinnedByChart?: boolean;
  _date?: string;
}

// ══════════════════════════════════════════════════════════════════════════════
// Chart Types
// ══════════════════════════════════════════════════════════════════════════════

export interface ChartDataPoint {
  date: string;
  [key: string]: string | number | null | undefined;
}

// ══════════════════════════════════════════════════════════════════════════════
// Selection & Props
// ══════════════════════════════════════════════════════════════════════════════

export interface DetailSelection {
  full_id: string;
  label: string;
  startDate: string;
  endDate: string;
  detailStartDate?: string;
  detailEndDate?: string;
  clickedDate?: string;
  summaryStartDate?: string;
  summaryEndDate?: string;
  nonce?: string;
}

export interface NewsPageProps {
  initialSelection?: DetailSelection | null;
  detailSelectionStorageKey?: string;
  disableAiExplain?: boolean;
}

// ══════════════════════════════════════════════════════════════════════════════
// Component-specific Types
// ══════════════════════════════════════════════════════════════════════════════

export interface ChildDelta {
  label: string;
  level: string;
  delta: number;
  pct: number;
  isAnom: boolean;
  direction: string;
}

export interface AttentionData {
  feature: string;
  cleanName: string;
  mentions: number;
  avgPct: number;
  barPct: number;
  drops: number;
  spikes: number;
}

export interface TFTImportance {
  encoder?: Record<string, number>;
  decoder?: Record<string, number>;
  attention_lags?: number[];
}

// ══════════════════════════════════════════════════════════════════════════════
// Chart Tooltip Types
// ══════════════════════════════════════════════════════════════════════════════

export interface ChartTooltipProps {
  active?: boolean;
  payload?: Array<{ dataKey?: string | number; value?: number | string }>;
  label?: string;
  selIds: string[];
  ts: Record<string, SeriesTimeseries[]>;
  infoOf: (fid: string) => SeriesInfo;
}

// ══════════════════════════════════════════════════════════════════════════════
// SeriesCard Props
// ══════════════════════════════════════════════════════════════════════════════

export interface SeriesCardProps {
  card: SeriesCardData;
  allSeries: SeriesInfo[];
  allDatesInRange: string[];
  ts: Record<string, SeriesTimeseries[]>;
  selIds: string[];
  onAddToChart: () => void;
  isFav: boolean;
  onToggleFav: () => void;
  focused: boolean;
  pinned: boolean;
  rEnd: string;
}
