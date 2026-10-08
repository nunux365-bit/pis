"use client";

/**
 * NewsPage - Detailed graph view for Prosight N Dashboard
 * Shows time series charts with anomaly markers, series picker, and detailed cards
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  CartesianGrid,
  ResponsiveContainer,
} from "recharts";
import type { ProsightNewsData, TodayRow, SeriesTimeseries } from "../types";
import { COLORS, FEAT_PATTERNS } from "./constants";
import { abbr, lvlS, mmdd, seriesLabel, matchRow } from "./utils";
import { resolveBu, buDay, buSeries, buFeatureImportance } from "../buSlice";
import { parseProsightNews } from "../newsSchema";
import { FS, L, Sel, Rad, Chk } from "./atoms";
import { ChartTooltip } from "./ChartTooltip";
import { SeriesCard, AttentionView } from "./SeriesCard";
import type {
  FilterState,
  SeriesInfo,
  SeriesCardData,
  ChartDataPoint,
  NewsPageProps,
  AttentionData,
  ChartTooltipProps,
} from "./types";

// ══════════════════════════════════════════════════════════════════════════════
// Main NewsPage Component
// ══════════════════════════════════════════════════════════════════════════════

export default function NewsPage({
  initialSelection = null,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  disableAiExplain = true,
}: NewsPageProps) {
  const [data, setData] = useState<ProsightNewsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selIds, setSel] = useState<string[]>([]);
  const [picker, setPick] = useState(false);
  const [search, setSrch] = useState("");
  const [sortBy, setSort] = useState("magnitude");
  const [rStart, setRS] = useState("");
  const [rEnd, setRE] = useState("");
  const [view, setView] = useState<"cards" | "attention">("cards");
  const [focusedId, setFocused] = useState<string | null>(null);
  const [chartSplit, setChartSplit] = useState(50);
  const [isDraggingSplit, setIsDraggingSplit] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [cardSearch, setCardSrch] = useState("");
  const picRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const autoSel = useRef(false);
  const appliedSelectionKey = useRef<string | null>(null);

  const [favIds, setFavIds] = useState<Set<string>>(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem("prosight_favs") || "[]"));
    } catch {
      return new Set();
    }
  });
  const [showTop5, setShowTop5] = useState(false);
  const [activeTab, setActiveTab] = useState<"all" | "favs">("all");

  const [F, setF] = useState<FilterState>({
    dir: "all",
    minScore: 0,
    levels: new Set(["L0_total", "L1_single", "L2_pair", "L3_full"]),
    bu: "",
    qualifiedOnly: true,
  });

  const sf = (k: keyof FilterState, v: FilterState[keyof FilterState]) =>
    setF((f) => ({ ...f, [k]: v }));
  const tlv = (lv: string) =>
    setF((f) => {
      const s = new Set(f.levels);
      if (s.has(lv)) {
        s.delete(lv);
      } else {
        s.add(lv);
      }
      return { ...f, levels: s };
    });

  // Load data
  useEffect(() => {
    fetch("/api/prosight/news")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((raw) => {
        // Same boundary as ProsightSummaryView. This site is the more fragile
        // of the two: `d.dates[0]` below has no guard, so an absent `dates`
        // was a TypeError mid-render rather than a caught error.
        const d = parseProsightNews(raw);
        setData(d);
        setRS(d.dates[0]);
        setRE(d.dates[d.dates.length - 1]);
      })
      .catch((e) => setError(e.message));
  }, []);

  // Close picker on outside click
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (picRef.current && !picRef.current.contains(e.target as Node))
        setPick(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  // Drag split handler
  useEffect(() => {
    if (!isDraggingSplit) return;

    const onMove = (e: MouseEvent) => {
      if (!mainRef.current) return;
      const rect = mainRef.current.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const pct = Math.round((y / rect.height) * 100);
      setChartSplit(Math.min(80, Math.max(25, pct)));
    };

    const onUp = () => setIsDraggingSplit(false);

    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);

    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [isDraggingSplit]);

  // Effective BU (explicit radio selection, else the JSON default). All data
  // reads below are sliced to this BU via the buDay / buSeries helpers (spec 27).
  const bu = resolveBu(data, F.bu);

  const ts = useMemo(
    () => buSeries(data, bu) as Record<string, SeriesTimeseries[]>,
    [data, bu]
  );

  // All series with dims
  const allSeries = useMemo<SeriesInfo[]>(() => {
    if (!data) return [];
    const seriesMap: Record<string, SeriesInfo> = {};
    Object.values(data.data_by_date).forEach((rawDay) => {
      const day = buDay(rawDay, bu);
      if (!day) return;
      day.today_rows.forEach((r) => {
        if (!seriesMap[r.full_id]) {
          seriesMap[r.full_id] = {
            fid: r.full_id,
            level: r.level,
            dims: r.dims ?? {},
            label: seriesLabel(r.dims, r.level),
          };
        }
      });
    });
    const l0key = "L0_total::total::TOTAL";
    if (ts[l0key] && !seriesMap[l0key]) {
      seriesMap[l0key] = {
        fid: l0key,
        level: "L0_total",
        dims: {},
        label: seriesLabel({}, "L0_total"),
      };
    }
    return Object.values(seriesMap);
  }, [data, ts, bu]);

  // Period rows
  const periodRows = useMemo(() => {
    if (!data || !rStart || !rEnd) return [];
    const rows: (TodayRow & { _date: string })[] = [];
    data.dates
      .filter((d) => d >= rStart && d <= rEnd)
      .forEach((d) => {
        (buDay(data.data_by_date[d], bu)?.today_rows ?? []).forEach((r) => {
          if (matchRow(r, F)) rows.push({ ...r, _date: d });
        });
      });
    const cmp: Record<string, (a: TodayRow, b: TodayRow) => number> = {
      magnitude: (a, b) => Math.abs(b.magnitude ?? 0) - Math.abs(a.magnitude ?? 0),
      score: (a, b) => b.score - a.score,
      long_term: (a, b) => (b.cumulative_magnitude ?? 0) - (a.cumulative_magnitude ?? 0),
    };
    return rows.sort(cmp[sortBy] || cmp.magnitude);
  }, [data, rStart, rEnd, F, sortBy]);

  // All dates in range
  const allDatesInRange = useMemo(() => {
    if (!data) return [];
    return data.dates.filter((d) => d >= rStart && d <= rEnd);
  }, [data, rStart, rEnd]);

  // All period rows unfiltered
  const allPeriodRows = useMemo(() => {
    if (!data || !rStart || !rEnd) return [];
    const rows: (TodayRow & { _date: string })[] = [];
    data.dates
      .filter((d) => d >= rStart && d <= rEnd)
      .forEach((d) => {
        (buDay(data.data_by_date[d], bu)?.today_rows ?? []).forEach((r) => {
          rows.push({ ...r, _date: d });
        });
      });
    return rows;
  }, [data, rStart, rEnd, bu]);

  const allSeriesCards = useMemo<SeriesCardData[]>(() => {
    const byId: Record<string, SeriesCardData> = {};
    allPeriodRows.forEach((r) => {
      if (!byId[r.full_id]) {
        byId[r.full_id] = {
          ...r,
          dates: [],
          dateDir: {},
          scores: [],
          magnitudes: [],
          drops: 0,
          spikes: 0,
          recentRow: r,
          flaggedDays: 0,
          maxScore: 0,
          avgMagnitude: 0,
        };
      }
      const c = byId[r.full_id];
      c.dates.push(r._date);
      c.dateDir[r._date] = r.direction;
      c.scores.push(r.score);
      c.magnitudes.push(r.magnitude);
      if (r.direction === "drop") c.drops++;
      else if (r.direction === "spike") c.spikes++;
      if (r._date > c.recentRow._date) c.recentRow = r;
    });
    return Object.values(byId).map((c) => ({
      ...c,
      flaggedDays: c.dates.length,
      maxScore: Math.max(...c.scores),
      avgMagnitude: c.magnitudes.reduce((a, b) => a + b, 0) / c.magnitudes.length,
    }));
  }, [allPeriodRows]);

  // Today alerts
  const todayAlerts = useMemo(() => {
    if (!data || !rEnd) return {};
    const rows = buDay(data.data_by_date[rEnd], bu)?.today_rows ?? [];
    return Object.fromEntries(rows.map((r) => [r.full_id, r]));
  }, [data, rEnd, bu]);

  // Series cards (filtered)
  const seriesCards = useMemo<SeriesCardData[]>(() => {
    const byId: Record<string, SeriesCardData> = {};
    periodRows.forEach((r) => {
      if (!byId[r.full_id]) {
        byId[r.full_id] = {
          ...r,
          dates: [],
          dateDir: {},
          scores: [],
          magnitudes: [],
          drops: 0,
          spikes: 0,
          recentRow: r,
          flaggedDays: 0,
          maxScore: 0,
          avgMagnitude: 0,
        };
      }
      if (r._date > byId[r.full_id].recentRow._date) byId[r.full_id].recentRow = r;
      const c = byId[r.full_id];
      c.dates.push(r._date);
      c.dateDir[r._date] = r.direction;
      c.scores.push(r.score);
      c.magnitudes.push(r.magnitude);
      if (r.direction === "drop") c.drops++;
      else if (r.direction === "spike") c.spikes++;
    });

    return Object.values(byId)
      .map((c) => {
        const todayPt = (ts[c.full_id] ?? []).find((p) => p.date === rEnd);
        const todayMag = todayPt ? todayPt.actual - todayPt.predicted : null;
        const todayBandDiff = todayPt
          ? todayPt.actual < todayPt.lower
            ? todayPt.actual - todayPt.lower
            : todayPt.actual > todayPt.upper
              ? todayPt.actual - todayPt.upper
              : 0
          : null;
        const todayDirection = todayPt
          ? todayPt.actual < todayPt.lower
            ? "drop"
            : todayPt.actual > todayPt.upper
              ? "spike"
              : "normal"
          : null;
        const alertRow = todayAlerts[c.full_id];
        return {
          ...c,
          flaggedDays: c.dates.length,
          maxScore: Math.max(...c.scores),
          avgMagnitude: c.magnitudes.reduce((a, b) => a + b, 0) / c.magnitudes.length,
          todayMagnitude: todayMag,
          todayBandDiff,
          todayDirection,
          todayAlertDir: alertRow?.direction ?? null,
          todayAlertMag: alertRow?.magnitude ?? null,
        };
      })
      .sort((a, b) => {
        const cmp: Record<string, (a: SeriesCardData, b: SeriesCardData) => number> = {
          today: (a, b) =>
            (a.todayAlertMag ?? a.todayBandDiff ?? 0) -
            (b.todayAlertMag ?? b.todayBandDiff ?? 0),
          magnitude: (a, b) =>
            Math.abs(b.recentRow?.magnitude ?? 0) - Math.abs(a.recentRow?.magnitude ?? 0),
          score: (a, b) => b.maxScore - a.maxScore,
          long_term: (a, b) => (b.flaggedDays ?? 0) - (a.flaggedDays ?? 0),
        };
        return (cmp[sortBy] || cmp.today)(a, b);
      });
  }, [periodRows, sortBy, ts, rEnd, todayAlerts]);

  // Types for analyses data
  interface ThemeItem {
    feature: string;
    mentions: number;
    avg_pct?: number;
  }

  interface AnalysisBucket {
    themes?: ThemeItem[];
  }

  const attentionData = useMemo<AttentionData[]>(() => {
    if (!data) return [];
    const feats: Record<string, AttentionData> = {};

    data.dates
      .filter((d) => d >= rStart && d <= rEnd)
      .forEach((d) => {
        const day = buDay(data.data_by_date[d], bu) as
          | { analyses?: Record<string, AnalysisBucket> }
          | undefined;
        if (!day) return;
        Object.values(day.analyses ?? {}).forEach((bkt) => {
          (bkt.themes ?? []).forEach((t) => {
            if (!feats[t.feature]) {
              feats[t.feature] = {
                feature: t.feature,
                cleanName: "",
                mentions: 0,
                avgPct: 0,
                barPct: 0,
                drops: 0,
                spikes: 0,
              };
            }
            feats[t.feature].mentions += t.mentions;
          });
        });
      });

    periodRows.forEach((r) => {
      if (!r.driver || r.driver === "—") return;
      r.driver.split(" · ").forEach((part) => {
        for (const [k] of FEAT_PATTERNS) {
          const label = FEAT_PATTERNS.find(([key]) => key === k)?.[1] || "";
          if (part.includes(label) && feats[k]) {
            if (r.direction === "drop") feats[k].drops++;
            else if (r.direction === "spike") feats[k].spikes++;
          }
        }
      });
    });

    const nameMap = Object.fromEntries(FEAT_PATTERNS);
    Object.values(feats).forEach((f) => {
      f.cleanName = nameMap[f.feature] || f.feature;
    });

    const sorted = Object.values(feats)
      .sort((a, b) => b.mentions - a.mentions)
      .slice(0, 8);
    const maxM = sorted[0]?.mentions || 1;

    return sorted.map((f) => ({
      ...f,
      avgPct: 0,
      barPct: (f.mentions / maxM) * 100,
    }));
  }, [data, rStart, rEnd, periodRows, bu]);

  // Auto-select series on load (valid one-time initialization pattern)
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => {
    if (!seriesCards.length) return;

    const requestedId = initialSelection?.full_id;
    const requestKey = initialSelection
      ? `${initialSelection.full_id || ""}@${initialSelection.startDate || ""}@${initialSelection.endDate || ""}@${initialSelection.nonce || ""}`
      : null;

    if (requestedId && appliedSelectionKey.current !== requestKey) {
      const exists =
        seriesCards.some((c) => c.full_id === requestedId) ||
        allSeries.some((s) => s.fid === requestedId) ||
        Boolean(ts?.[requestedId]);

      if (exists) {
        setSel([requestedId]);
        setFocused(requestedId);
        if (initialSelection?.startDate) setRS(initialSelection.startDate);
        if (initialSelection?.endDate) setRE(initialSelection.endDate);
        appliedSelectionKey.current = requestKey;
        autoSel.current = true;
      }
      return;
    }

    if (!requestedId && !autoSel.current) {
      const top = seriesCards[0];
      setSel([top.full_id]);
      setFocused(top.full_id);
      autoSel.current = true;
    }
  }, [seriesCards, allSeries, ts, initialSelection]);

  // Picker options
  const options = useMemo(() => {
    const filtered = allSeries.filter((s) => {
      if (!F.levels.has(s.level)) return false;
      // BU is selected upstream by slicing the data (spec 27), not by filtering
      // rows on a `bu` dim — which no longer exists in per-BU series.
      return true;
    });
    const l0 = filtered.filter((s) => s.level === "L0_total");
    const rest = filtered
      .filter((s) => s.level !== "L0_total")
      .sort((a, b) => a.label.localeCompare(b.label));
    return [...l0, ...rest];
  }, [allSeries, F]);

  const searched = useMemo(() => {
    const q = search.toLowerCase();
    return q
      ? options.filter(
          (s) => s.label.toLowerCase().includes(q) || lvlS(s.level).includes(q)
        )
      : options;
  }, [options, search]);

  // Chart data
  const chartData = useMemo<ChartDataPoint[]>(() => {
    if (!selIds.length || !rStart || !rEnd) return [];
    const dateSet = new Set<string>();
    selIds.forEach((fid) =>
      (ts[fid] ?? []).forEach((p) => {
        if (p.date >= rStart && p.date <= rEnd) dateSet.add(p.date);
      })
    );
    const dates = [...dateSet].sort();
    return dates.map((dt) => {
      const pt: ChartDataPoint = { date: mmdd(dt) };
      selIds.forEach((fid, i) => {
        const p = (ts[fid] ?? []).find((x) => x.date === dt);
        if (!p || !p.predicted) return;
        pt[`a${i}`] = p.actual;
        pt[`p${i}`] = p.predicted;
        pt[`lo${i}`] = p.lower;
        pt[`bw${i}`] = p.upper - p.lower;
        pt[`dr${i}`] = p.is_anomaly ? p.direction : null;
      });
      return pt;
    });
  }, [selIds, ts, rStart, rEnd]);

  // Y-axis domain
  const yDomain = useMemo<[number | "auto", number | "auto"]>(() => {
    if (!chartData.length) return ["auto", "auto"];
    const vals = chartData.flatMap((pt) =>
      selIds.map((_, i) => pt[`a${i}`] as number | undefined).filter((v): v is number => v != null)
    );
    if (!vals.length) return ["auto", "auto"];
    const sorted = [...vals].sort((a, b) => a - b);
    const lo = sorted[Math.floor(sorted.length * 0.02)];
    const hi = sorted[Math.floor(sorted.length * 0.98)];
    const pad = (hi - lo) * 0.1 || 5;
    return [Math.floor(lo - pad), Math.ceil(hi + pad)];
  }, [chartData, selIds]);

  // Helpers
  const toggleFav = (fid: string) =>
    setFavIds((prev) => {
      const next = new Set(prev);
      next.has(fid) ? next.delete(fid) : next.add(fid);
      try {
        localStorage.setItem("prosight_favs", JSON.stringify([...next]));
      } catch {
        // ignore localStorage errors
      }
      return next;
    });

  const displayCards = useMemo(() => {
    let cards =
      activeTab === "favs"
        ? seriesCards.filter((c) => favIds.has(c.full_id))
        : seriesCards;
    if (F.dir !== "all") {
      cards = cards.filter((c) => c.todayAlertDir === F.dir);
    }
    if (showTop5) cards = cards.slice(0, 5);
    if (focusedId) {
      const visible = new Set(cards.map((c) => c.full_id));
      if (!visible.has(focusedId)) {
        const pinned = allSeriesCards.find((c) => c.full_id === focusedId);
        if (pinned) return [{ ...pinned, pinnedByChart: true }, ...cards];
      }
    }
    return cards;
  }, [seriesCards, activeTab, favIds, showTop5, focusedId, allSeriesCards, F.dir]);

  const visibleCards = useMemo(() => {
    if (!cardSearch) return displayCards;
    const q = cardSearch.toLowerCase();
    return displayCards.filter(
      (c) =>
        seriesLabel(c.dims, c.level).toLowerCase().includes(q) ||
        lvlS(c.level).toLowerCase().includes(q)
    );
  }, [displayCards, cardSearch]);

  const colorOf = (fid: string) => COLORS[selIds.indexOf(fid) % COLORS.length];
  const infoOf = (fid: string): SeriesInfo =>
    allSeries.find((x) => x.fid === fid) ?? { fid, label: fid, level: "", dims: {} };
  const togSeries = (fid: string) =>
    setSel((ids) => (ids.includes(fid) ? ids.filter((x) => x !== fid) : [...ids, fid]));

  useEffect(() => {
    if (!focusedId) return;
    const el = document.getElementById(
      "card-" + focusedId.replace(/[^a-zA-Z0-9_-]/g, "_")
    );
    if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const t = setTimeout(() => setFocused(null), 2500);
    return () => clearTimeout(t);
  }, [focusedId]);

  const addFiltered = () => {
    const ids = [...new Set(periodRows.map((r) => r.full_id))].slice(0, 10);
    setSel((prev) => [...new Set([...prev, ...ids])].slice(0, 10));
  };

  const preset = (days: number) => {
    if (!data) return;
    const all = data.dates;
    setRE(all[all.length - 1]);
    setRS(days === 0 ? all[0] : all[Math.max(0, all.length - days)]);
  };

  // Anomaly dot renderer
  interface DotProps {
    key?: string;
    cx?: number;
    cy?: number;
    payload?: ChartDataPoint;
  }

  const makeDot = (i: number) => {
    const AnomalyDot = (props: DotProps) => {
      const { key, cx, cy, payload } = props;
      if (cx === undefined || cy === undefined || !payload) return <g key={key} />;
      const dir = payload[`dr${i}`];
      if (!dir) return <g key={key} />;
      const fill = dir === "drop" ? "#dc2626" : "#f59e0b";
      const outer = dir === "drop" ? "#7f1d1d" : "#92400e";
      const s = 6;
      if (dir === "drop")
        return (
          <polygon
            key={key}
            points={`${cx},${cy + s} ${cx - s},${cy - s + 2} ${cx + s},${cy - s + 2}`}
            fill={fill}
            stroke={outer}
            strokeWidth={0.8}
          />
        );
      return (
        <polygon
          key={key}
          points={`${cx},${cy - s} ${cx - s},${cy + s - 2} ${cx + s},${cy + s - 2}`}
          fill={fill}
          stroke={outer}
          strokeWidth={0.8}
        />
      );
    };
    AnomalyDot.displayName = `AnomalyDot${i}`;
    return AnomalyDot;
  };

  // Early states
  if (error)
    return (
      <div className="h-full flex items-center justify-center text-center p-8">
        <div>
          <p className="text-[13px] text-[#374151] mb-1.5">Could not load data</p>
          <p className="font-mono text-[11px] text-[#6b7280] mb-2">{error}</p>
        </div>
      </div>
    );

  if (!data)
    return (
      <div className="h-full flex items-center justify-center text-[13px] text-[#9ca3af]">
        Loading…
      </div>
    );

  return (
    <div className="flex h-full overflow-hidden bg-[#f3f4f6] text-[11px] font-sans">
      {/* SIDEBAR */}
      <aside
        className="flex-shrink-0 bg-white border-r-2 border-[#d1d5db] flex flex-col transition-[width] duration-150"
        style={{
          width: sidebarCollapsed ? 34 : 188,
          overflowY: sidebarCollapsed ? "hidden" : "auto",
        }}
      >
        <div
          className="bg-[#111827] flex items-center"
          style={{
            padding: sidebarCollapsed ? "9px 5px" : "9px 12px",
            justifyContent: sidebarCollapsed ? "center" : "space-between",
            gap: 6,
          }}
        >
          {!sidebarCollapsed && (
            <span className="text-white font-bold text-xs tracking-[2px]">PROSIGHT</span>
          )}
          <button
            type="button"
            onClick={() => setSidebarCollapsed((v) => !v)}
            title={sidebarCollapsed ? "Expand filters" : "Collapse filters"}
            className="w-[22px] h-[22px] border border-white/25 rounded bg-white/10 text-white cursor-pointer text-xs leading-[18px]"
          >
            {sidebarCollapsed ? "›" : "‹"}
          </button>
        </div>

        {sidebarCollapsed ? (
          <button
            type="button"
            onClick={() => setSidebarCollapsed(false)}
            title="Expand filters"
            className="mx-auto my-2.5 text-[10px] tracking-[1.5px] text-[#6b7280] bg-transparent border-none cursor-pointer uppercase"
            style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
          >
            Filters
          </button>
        ) : (
          <>
            <FS title="Period">
              <div className="flex gap-1 mb-2 flex-wrap">
                {(
                  [
                    ["All", 0],
                    ["30d", 30],
                    ["14d", 14],
                    ["7d", 7],
                  ] as [string, number][]
                ).map(([l, d]) => (
                  <button
                    key={l}
                    onClick={() => preset(d)}
                    className="text-[10px] px-2 py-0.5 border border-[#d1d5db] rounded bg-[#f9fafb] cursor-pointer"
                  >
                    {l}
                  </button>
                ))}
              </div>
              <L>From</L>
              <Sel value={rStart} opts={data.dates} onChange={setRS} />
              <L>To</L>
              <Sel value={rEnd} opts={data.dates} onChange={setRE} />
              <p className="text-[9px] text-[#9ca3af] mt-1">
                {data.dates.filter((d) => d >= rStart && d <= rEnd).length} days selected
              </p>
            </FS>

            <FS title="Direction">
              {(
                [
                  ["all", "All on " + rEnd],
                  ["drop", "↓  Drops today"],
                  ["spike", "↑  Spikes today"],
                ] as [FilterState["dir"], string][]
              ).map(([v, l]) => (
                <Rad key={v} label={l} checked={F.dir === v} onChange={() => sf("dir", v)} />
              ))}
            </FS>

            <FS title="Score">
              {(
                [
                  [0, "All"],
                  [1.5, "≥ 1.5"],
                  [2, "≥ 2.0"],
                  [2.5, "≥ 2.5"],
                ] as [number, string][]
              ).map(([v, l]) => (
                <Rad
                  key={v}
                  label={l}
                  checked={F.minScore === v}
                  onChange={() => sf("minScore", v)}
                />
              ))}
            </FS>

            <FS title="Level">
              {(
                [
                  ["L0_total", "L0 — Total"],
                  ["L1_single", "L1 — Single"],
                  ["L2_pair", "L2 — Pair"],
                  ["L3_full", "L3 — Full"],
                ] as [string, string][]
              ).map(([lv, l]) => (
                <Chk key={lv} label={l} checked={F.levels.has(lv)} onChange={() => tlv(lv)} />
              ))}
            </FS>

            <FS title="Business unit">
              {(data?.bus ?? [bu]).map((v) => (
                <Rad
                  key={v}
                  label={v.charAt(0).toUpperCase() + v.slice(1)}
                  checked={bu === v}
                  onChange={() => sf("bu", v)}
                />
              ))}
            </FS>

            <FS title="Quality">
              <Chk
                label="Qualified only"
                checked={F.qualifiedOnly}
                onChange={() => sf("qualifiedOnly", !F.qualifiedOnly)}
              />
              <p className="text-[9px] text-[#9ca3af] mt-0.5 leading-tight">
                Uncheck to show series with high prediction error
              </p>
            </FS>
          </>
        )}
      </aside>

      {/* MAIN */}
      <div
        ref={mainRef}
        className="flex-1 min-w-0 flex flex-col h-full overflow-hidden"
      >
        {/* CHART PANEL */}
        <div
          className="flex flex-col bg-white overflow-hidden"
          style={{
            flex: `0 0 ${chartSplit}%`,
            transition: isDraggingSplit ? "none" : "flex 0.2s",
          }}
        >
          {/* chart header */}
          <div className="flex-shrink-0 px-3.5 py-1.5 border-b border-[#e5e7eb] bg-[#f9fafb] flex items-center gap-2.5">
            <span className="text-[10px] font-bold uppercase tracking-[2px] text-[#374151]">
              Chart view
            </span>
            <span className="text-[10px] text-[#9ca3af]">
              {mmdd(rStart)} → {mmdd(rEnd)} · {selIds.length} series
            </span>
            <div className="flex gap-2 items-center text-[9px] text-[#6b7280] ml-auto border-l border-[#e5e7eb] pl-2">
              <span className="text-[#dc2626] font-bold">▼ drop</span>
              <span className="text-[#f59e0b] font-bold">▲ spike</span>
              <span className="text-[#93c5fd]">━ band</span>
            </div>
          </div>

          {/* series selector */}
          <div
            ref={picRef}
            className="flex-shrink-0 px-3.5 py-1.5 border-b border-[#f3f4f6] flex items-center gap-2 flex-wrap"
          >
            <div className="relative">
              <button
                onClick={() => {
                  setPick((o) => !o);
                  setSrch("");
                }}
                className="text-[11px] px-2.5 py-0.5 border border-[#d1d5db] rounded bg-white cursor-pointer flex items-center gap-1"
              >
                ＋ Select {picker ? "▲" : "▼"}
              </button>
              {picker && (
                <div className="absolute top-full left-0 z-50 mt-1 bg-white border border-[#d1d5db] rounded-lg shadow-lg w-[300px]">
                  <div className="p-2 border-b border-[#f3f4f6]">
                    <input
                      autoFocus
                      value={search}
                      onChange={(e) => setSrch(e.target.value)}
                      placeholder="Search series…"
                      className="w-full text-[11px] px-2 py-1 border border-[#d1d5db] rounded outline-none"
                    />
                  </div>
                  <div className="max-h-60 overflow-y-auto">
                    {searched.map((s) => (
                      <label
                        key={s.fid}
                        className="flex items-start gap-2 px-3 py-1.5 cursor-pointer border-b border-[#f9fafb]"
                        style={{
                          background: selIds.includes(s.fid) ? "#eff6ff" : "transparent",
                        }}
                      >
                        <input
                          type="checkbox"
                          className="mt-0.5 w-3 h-3 accent-[#2563eb] flex-shrink-0"
                          checked={selIds.includes(s.fid)}
                          onChange={() => togSeries(s.fid)}
                        />
                        <div className="min-w-0">
                          <span className="text-[9px] bg-[#e5e7eb] text-[#374151] px-1 rounded font-mono mr-1">
                            {lvlS(s.level)}
                          </span>
                          {selIds.includes(s.fid) && (
                            <span
                              className="w-2 h-2 rounded-full inline-block mr-1"
                              style={{ background: colorOf(s.fid) }}
                            />
                          )}
                          <span className="text-[11px] text-[#374151] break-words">
                            {s.label}
                          </span>
                        </div>
                      </label>
                    ))}
                  </div>
                  <div className="p-2 border-t border-[#f3f4f6] flex gap-1.5">
                    <button
                      onClick={addFiltered}
                      className="flex-1 text-[10px] p-1 border border-[#d1d5db] rounded cursor-pointer bg-[#f9fafb]"
                    >
                      Add filtered ({new Set(periodRows.map((r) => r.full_id)).size})
                    </button>
                    <button
                      onClick={() => setSel([])}
                      className="flex-1 text-[10px] p-1 border border-[#fca5a5] rounded cursor-pointer bg-[#fff7f7] text-[#dc2626]"
                    >
                      Clear all
                    </button>
                  </div>
                </div>
              )}
            </div>

            {selIds.length === 0 ? (
              <span className="text-[11px] text-[#9ca3af] italic">
                No series selected — use the picker or &quot;Add filtered&quot;
              </span>
            ) : (
              <>
                {selIds.map((fid) => {
                  const { label, level } = infoOf(fid);
                  const c = colorOf(fid);
                  return (
                    <span
                      key={fid}
                      className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full max-w-60"
                      style={{
                        border: `1px solid ${c}`,
                        color: c,
                        background: `${c}15`,
                      }}
                    >
                      <span
                        className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                        style={{ background: c }}
                      />
                      <span className="text-[9px] opacity-70 font-mono">
                        {lvlS(level)}
                      </span>
                      <span className="overflow-hidden text-ellipsis whitespace-nowrap">
                        {label}
                      </span>
                      <button
                        title="View card"
                        onClick={() => {
                          setFocused(fid);
                          setTimeout(() => {
                            const id =
                              "card-" + fid.replace(/[^a-zA-Z0-9_-]/g, "_");
                            document
                              .getElementById(id)
                              ?.scrollIntoView({ behavior: "smooth", block: "start" });
                          }, 60);
                        }}
                        className="text-[9px] px-1 rounded cursor-pointer bg-transparent font-semibold opacity-80"
                        style={{ border: `1px solid ${c}`, color: c }}
                      >
                        card ↓
                      </button>
                      <button
                        onClick={() => togSeries(fid)}
                        className="border-none bg-none cursor-pointer p-0 text-[13px] opacity-50"
                        style={{ color: c }}
                      >
                        ×
                      </button>
                    </span>
                  );
                })}
                <button
                  onClick={() => setSel([])}
                  className="ml-auto text-[10px] px-2 py-0.5 border border-[#fca5a5] rounded bg-[#fff7f7] text-[#dc2626] cursor-pointer"
                >
                  Clear all
                </button>
              </>
            )}
          </div>

          {/* chart */}
          <div className="flex-1 min-h-0 p-2 px-3.5">
            {selIds.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center gap-2 text-[#9ca3af]">
                <span className="text-[28px]">📈</span>
                <span className="text-[13px]">Select series above</span>
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={chartData}
                  margin={{ top: 4, right: 14, left: 0, bottom: 0 }}
                >
                  <CartesianGrid
                    stroke="#f0eee9"
                    strokeDasharray="2 4"
                    vertical={false}
                  />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 9, fill: "#9ca3af" }}
                    stroke="none"
                    interval="preserveStartEnd"
                    minTickGap={24}
                  />
                  <YAxis
                    tick={{ fontSize: 9, fill: "#9ca3af" }}
                    stroke="none"
                    width={38}
                    domain={yDomain}
                    tickFormatter={abbr}
                  />
                  {rEnd && chartData.some((pt) => pt.date === mmdd(rEnd)) && (
                    <ReferenceLine
                      x={mmdd(rEnd)}
                      stroke="#111827"
                      strokeWidth={1.5}
                      label={{
                        value: "today",
                        position: "insideTopRight",
                        fontSize: 8,
                        fill: "#111827",
                        fontWeight: 600,
                      }}
                    />
                  )}
                  {/* bands */}
                  {selIds.map((fid, i) => [
                    <Area
                      key={`lo${i}`}
                      type="monotone"
                      dataKey={`lo${i}`}
                      fill="transparent"
                      stroke="none"
                      stackId={`b${i}`}
                      isAnimationActive={false}
                      legendType="none"
                    />,
                    <Area
                      key={`bw${i}`}
                      type="monotone"
                      dataKey={`bw${i}`}
                      fill={COLORS[i % COLORS.length]}
                      fillOpacity={0.12}
                      stroke={COLORS[i % COLORS.length]}
                      strokeWidth={0.5}
                      strokeOpacity={0.35}
                      strokeDasharray="3 2"
                      stackId={`b${i}`}
                      isAnimationActive={false}
                      legendType="none"
                    />,
                  ])}
                  {/* predicted lines */}
                  {selIds.map((fid, i) => (
                    <Line
                      key={`p${i}`}
                      type="monotone"
                      dataKey={`p${i}`}
                      stroke={COLORS[i % COLORS.length]}
                      strokeWidth={1.5}
                      strokeDasharray="5 3"
                      strokeOpacity={0.6}
                      dot={false}
                      legendType="none"
                      isAnimationActive={false}
                    />
                  ))}
                  {/* actual lines with anomaly dots */}
                  {selIds.map((fid, i) => (
                    <Line
                      key={`a${i}`}
                      type="monotone"
                      dataKey={`a${i}`}
                      stroke={COLORS[i % COLORS.length]}
                      strokeWidth={1.8}
                      dot={makeDot(i)}
                      activeDot={{ r: 4, stroke: "#fff", strokeWidth: 1 }}
                      name={infoOf(fid).label}
                      isAnimationActive={false}
                    />
                  ))}
                  <Tooltip
                    content={(props) => (
                      <ChartTooltip
                        active={props.active}
                        payload={props.payload as ChartTooltipProps["payload"]}
                        label={props.label as string}
                        selIds={selIds}
                        ts={ts}
                        infoOf={infoOf}
                      />
                    )}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* DIVIDER */}
        <div
          onMouseDown={() => setIsDraggingSplit(true)}
          title="Drag to resize chart and cards"
          className="h-2 flex-shrink-0 border-t border-b border-[#374151] cursor-row-resize relative"
          style={{ background: isDraggingSplit ? "#2563eb" : "#4b5563" }}
        >
          <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-14 h-0.5 rounded-full bg-white/65" />
        </div>

        {/* BOTTOM PANEL */}
        <div
          className="flex flex-col bg-[#f9fafb] min-h-0"
          style={{ flex: `0 0 ${100 - chartSplit}%` }}
        >
          {/* header */}
          <div className="flex-shrink-0 px-3.5 py-1.5 bg-[#e5e7eb] border-b border-[#d1d5db] flex items-center gap-2.5 flex-wrap">
            <div className="flex gap-0 border border-[#d1d5db] rounded overflow-hidden">
              {(
                [
                  ["all", `All (${displayCards.length})`],
                  ["favs", `★ Favs (${favIds.size})`],
                ] as ["all" | "favs", string][]
              ).map(([v, l]) => (
                <button
                  key={v}
                  onClick={() => setActiveTab(v)}
                  className="text-[10px] px-2.5 py-0.5 border-none cursor-pointer"
                  style={{
                    background: activeTab === v ? "#1d4ed8" : "#fff",
                    color: activeTab === v ? "#fff" : "#374151",
                    fontWeight: activeTab === v ? 600 : 400,
                  }}
                >
                  {l}
                </button>
              ))}
            </div>

            <div className="flex gap-0 border border-[#d1d5db] rounded overflow-hidden">
              {(
                [
                  ["cards", "Anomalies"],
                  ["attention", "Drivers"],
                ] as ["cards" | "attention", string][]
              ).map(([v, l]) => (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  className="text-[10px] px-2.5 py-0.5 border-none cursor-pointer"
                  style={{
                    background: view === v ? "#374151" : "#fff",
                    color: view === v ? "#fff" : "#374151",
                    fontWeight: view === v ? 600 : 400,
                  }}
                >
                  {l}
                </button>
              ))}
            </div>

            <button
              onClick={() => setShowTop5((t) => !t)}
              className="text-[10px] px-2.5 py-0.5 border border-[#d1d5db] rounded cursor-pointer"
              style={{
                background: showTop5 ? "#fef3c7" : "#fff",
                color: showTop5 ? "#92400e" : "#374151",
                fontWeight: showTop5 ? 600 : 400,
              }}
            >
              Top 5
            </button>

            <span className="text-[10px] text-[#6b7280]">
              {visibleCards.length}
              {cardSearch ? ` of ${displayCards.length}` : ""} series
            </span>

            {view === "cards" && (
              <select
                value={sortBy}
                onChange={(e) => setSort(e.target.value)}
                className="text-[10px] px-1.5 py-0.5 border border-[#d1d5db] rounded bg-white"
              >
                <option value="today">Sort: Today&apos;s drop</option>
                <option value="magnitude">Sort: Latest alert</option>
                <option value="score">Sort: Score</option>
                <option value="long_term">Sort: Long-term</option>
              </select>
            )}

            <div className="flex items-center gap-1 ml-auto border border-[#d1d5db] rounded bg-white px-1.5 py-0.5">
              <span className="text-[10px] text-[#9ca3af]">🔍</span>
              <input
                value={cardSearch}
                onChange={(e) => setCardSrch(e.target.value)}
                placeholder="Search cards…"
                className="text-[10px] border-none outline-none w-28 bg-transparent text-[#374151]"
              />
              {cardSearch && (
                <button
                  onClick={() => setCardSrch("")}
                  className="text-[11px] border-none bg-none cursor-pointer text-[#9ca3af] p-0"
                >
                  ✕
                </button>
              )}
            </div>
          </div>

          {/* scrollable content */}
          <div className="flex-1 min-h-0 overflow-y-auto p-3 px-3.5">
            {view === "attention" ? (
              <AttentionView
                data={buFeatureImportance(data, bu)}
                attentionData={attentionData}
              />
            ) : periodRows.length === 0 ? (
              <div className="text-center text-[#9ca3af] text-[13px] pt-12">
                No anomalies match filters.
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {visibleCards.map((card) => (
                  <SeriesCard
                    key={card.full_id}
                    card={card}
                    allSeries={allSeries}
                    allDatesInRange={allDatesInRange}
                    ts={ts}
                    selIds={selIds}
                    onAddToChart={() => togSeries(card.full_id)}
                    isFav={favIds.has(card.full_id)}
                    onToggleFav={() => toggleFav(card.full_id)}
                    focused={focusedId === card.full_id}
                    pinned={!!card.pinnedByChart}
                    rEnd={rEnd}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
