export type ClickpostEvent = {
  status: string;
  location: string;
  at: string;
  timestamp?: string;
};

function parseClickpostTs(value: string | undefined): number | null {
  if (!value?.trim()) return null;
  const raw = value.trim();
  const ms = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T"));
  return Number.isFinite(ms) ? ms : null;
}

export function normalizeClickpostEvents(
  events?: Array<Record<string, unknown>> | null,
): ClickpostEvent[] {
  if (!events?.length) return [];
  return events
    .filter((e) => e && typeof e === "object")
    .map((e) => ({
      status: String(e.status ?? "").trim() || "—",
      location: String(e.location ?? "").trim(),
      at: String(e.at ?? "").trim() || "—",
      timestamp: String(e.timestamp ?? "").trim() || undefined,
    }));
}

export function sortClickpostEventsChronological(events: ClickpostEvent[]): ClickpostEvent[] {
  return [...events].sort((a, b) => {
    const ta = parseClickpostTs(a.timestamp) ?? parseClickpostTs(a.at);
    const tb = parseClickpostTs(b.timestamp) ?? parseClickpostTs(b.at);
    if (ta == null && tb == null) return 0;
    if (ta == null) return 1;
    if (tb == null) return -1;
    return ta - tb;
  });
}

export type ClickpostTimelineSummary = {
  eventCount: number;
  rangeLabel: string | null;
  longestGapLabel: string | null;
};

export type ClickpostInsight = {
  headline: string;
  detail: string;
  tone?: "ok" | "warn" | "slate";
};

export function buildClickpostInsights(events: ClickpostEvent[]): ClickpostInsight[] {
  if (!events.length) return [];
  const chron = sortClickpostEventsChronological(events);
  const insights: ClickpostInsight[] = [];
  const first = chron[0];
  const last = chron[chron.length - 1];

  if (first?.status && last?.status && first !== last) {
    insights.push({
      headline: `${first.status} → ${last.status}`,
      detail: first.at && last.at ? `${first.at} through ${last.at}` : "Courier scan span",
      tone: "slate",
    });
  }

  const summary = summarizeClickpostTimeline(events);
  if (summary.longestGapLabel) {
    insights.push({
      headline: "Longest gap between scans",
      detail: summary.longestGapLabel,
      tone: "warn",
    });
  }

  const statuses = chron.map((e) => e.status.toLowerCase());
  if (statuses.some((s) => s.includes("out for delivery"))) {
    const ofd = [...chron].reverse().find((e) => e.status.toLowerCase().includes("out for delivery"));
    if (ofd) {
      insights.push({
        headline: "Out for delivery scan",
        detail: ofd.location ? `${ofd.at} — ${ofd.location}` : ofd.at,
        tone: "slate",
      });
    }
  }

  if (statuses.some((s) => s.includes("delivered"))) {
    const delivered = [...chron].reverse().find((e) => e.status.toLowerCase().includes("delivered"));
    if (delivered) {
      insights.push({
        headline: "Delivered scan",
        detail: delivered.location
          ? `${delivered.at} — ${delivered.location}`
          : delivered.at,
        tone: "ok",
      });
    }
  }

  const placedIdx = statuses.findIndex((s) => s.includes("order placed") || s.includes("placed"));
  const shippedIdx = statuses.findIndex((s) => s.includes("shipped"));
  if (placedIdx >= 0 && shippedIdx > placedIdx) {
    const placed = chron[placedIdx];
    const shipped = chron[shippedIdx];
    const t0 = parseClickpostTs(placed.timestamp) ?? parseClickpostTs(placed.at);
    const t1 = parseClickpostTs(shipped.timestamp) ?? parseClickpostTs(shipped.at);
    if (t0 != null && t1 != null && t1 > t0) {
      const gapMin = Math.round((t1 - t0) / 60_000);
      insights.push({
        headline: "Order placed → shipped",
        detail: formatGapMinutes(gapMin),
        tone: gapMin > 24 * 60 ? "warn" : "slate",
      });
    }
  }

  return insights;
}

function formatGapMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

export function summarizeClickpostTimeline(events: ClickpostEvent[]): ClickpostTimelineSummary {
  if (!events.length) {
    return { eventCount: 0, rangeLabel: null, longestGapLabel: null };
  }
  const chron = sortClickpostEventsChronological(events);
  const first = chron[0];
  const last = chron[chron.length - 1];
  const rangeLabel =
    first?.at && last?.at && first.at !== last.at ? `${first.at} → ${last.at}` : first?.at ?? null;

  let longestGap = 0;
  let longestLabel: string | null = null;
  for (let i = 1; i < chron.length; i += 1) {
    const prev = parseClickpostTs(chron[i - 1].timestamp) ?? parseClickpostTs(chron[i - 1].at);
    const cur = parseClickpostTs(chron[i].timestamp) ?? parseClickpostTs(chron[i].at);
    if (prev == null || cur == null) continue;
    const gapMin = Math.round((cur - prev) / 60_000);
    if (gapMin > longestGap) {
      longestGap = gapMin;
      longestLabel = `${chron[i - 1].status} → ${chron[i].status}: ${formatGapMinutes(gapMin)}`;
    }
  }

  return {
    eventCount: events.length,
    rangeLabel,
    longestGapLabel: longestLabel,
  };
}
