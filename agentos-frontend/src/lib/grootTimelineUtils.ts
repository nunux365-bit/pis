export type GrootEvent = {
  at?: string;
  status?: string;
  sub_status?: string;
  comments?: string;
  performed_by?: string;
  day?: string;
};

export type GrootTimelineSegment = {
  status: string;
  subStatus: string;
  startMs: number;
  endMs: number;
  label: string;
  timeUnknown?: boolean;
};

export type GrootFulfillmentAttempt = {
  id: number;
  label: string;
  fulfillmentHint: string;
  hubHint: string;
  riderHint: string;
  startMs: number;
  endMs: number;
  events: GrootEvent[];
  segments: GrootTimelineSegment[];
};

export type GrootTimelineSummary = {
  eventCount: number;
  parseableCount: number;
  unparsedCount: number;
  rangeLabel: string;
  rangeStart: number;
  rangeEnd: number;
  attemptCount: number;
  riders: string[];
};

const MISSING = "—";

const MONTHS: Record<string, number> = {
  jan: 0,
  feb: 1,
  mar: 2,
  apr: 3,
  may: 4,
  jun: 5,
  jul: 6,
  aug: 7,
  sep: 8,
  oct: 9,
  nov: 10,
  dec: 11,
};

const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Backend / UI sentinel for missing scalar fields. */
export function isMissingDisplay(v: unknown): boolean {
  if (v === null || v === undefined) return true;
  const s = String(v).trim();
  return s === "" || s === MISSING;
}

/** Coerce API / facts payload into display-safe Groot rows. */
export function normalizeGrootEvents(input: unknown): GrootEvent[] {
  if (!Array.isArray(input)) return [];
  const out: GrootEvent[] = [];
  for (const row of input) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    out.push({
      at: isMissingDisplay(r.at) ? undefined : String(r.at).trim(),
      status: isMissingDisplay(r.status) ? undefined : String(r.status).trim(),
      sub_status: isMissingDisplay(r.sub_status) ? undefined : String(r.sub_status).trim(),
      comments: isMissingDisplay(r.comments) ? undefined : String(r.comments).trim(),
      performed_by: isMissingDisplay(r.performed_by) ? undefined : String(r.performed_by).trim(),
      day: isMissingDisplay(r.day) ? undefined : String(r.day).trim(),
    });
  }
  return out;
}

/** Parse Order RCA display timestamps: ``14 May, 2026 14:24 IST``. */
export function parseGrootAtDisplay(at: string | undefined): number | null {
  if (!at || isMissingDisplay(at)) return null;
  const m = at.trim().match(/^(\d{1,2})\s+([A-Za-z]+),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s+IST$/);
  if (!m) return null;
  const day = Number(m[1]);
  const monKey = m[2].slice(0, 3).toLowerCase();
  const mon = MONTHS[monKey];
  const year = Number(m[3]);
  const hour = Number(m[4]);
  const minute = Number(m[5]);
  if (
    mon === undefined ||
    Number.isNaN(day) ||
    Number.isNaN(year) ||
    Number.isNaN(hour) ||
    Number.isNaN(minute) ||
    day < 1 ||
    day > 31 ||
    hour < 0 ||
    hour > 23 ||
    minute < 0 ||
    minute > 59
  ) {
    return null;
  }
  const ms = Date.UTC(year, mon, day, hour, minute);
  if (Number.isNaN(ms)) return null;
  return ms;
}

/**
 * Mirror backend ``parse_groot_events`` for fixture tests: flatten Groot API day buckets
 * into the same row shape the Order RCA report exposes.
 */
export function parseGrootApiPayload(payload: unknown): GrootEvent[] {
  if (!payload || typeof payload !== "object") return [];
  const data = (payload as { data?: unknown }).data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return [];

  const out: GrootEvent[] = [];
  const days = Object.keys(data as Record<string, unknown>).sort();
  for (const day of days) {
    const evs = (data as Record<string, unknown>)[day];
    if (!Array.isArray(evs)) continue;
    for (const e of evs) {
      if (!e || typeof e !== "object") continue;
      const row = e as Record<string, unknown>;
      const rawAt = row.performed_at ?? row.created_at;
      out.push({
        day,
        status: displayScalar(row.status),
        sub_status: displayScalar(row.sub_status, ""),
        at: formatGrootApiTimestamp(rawAt),
        comments: displayScalar(redactFixtureComment(row.comments), ""),
        performed_by: displayScalar(row.performed_by, ""),
      });
    }
  }
  return out;
}

function displayScalar(v: unknown, fallback = MISSING): string {
  if (v === null || v === undefined) return fallback;
  const s = String(v).trim();
  return s === "" ? fallback : s;
}

function redactFixtureComment(v: unknown): unknown {
  if (typeof v !== "string") return v;
  return v.replace(/\b\d{10}\b/g, "[phone]");
}

/** Prod Groot API: ``DD-MM-YYYY HH:MM:SS`` naive IST → report display string. */
export function formatGrootApiTimestamp(value: unknown): string {
  const s = value == null ? "" : String(value).trim();
  if (!s) return MISSING;
  const m = s.match(/^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})(?::\d{2})?/);
  if (!m) return s;
  const day = Number(m[1]);
  const month = Number(m[2]) - 1;
  const year = Number(m[3]);
  const hour = m[4];
  const minute = m[5];
  if (month < 0 || month > 11 || Number.isNaN(day) || Number.isNaN(year)) return s;
  return `${day} ${MONTH_SHORT[month]}, ${year} ${hour}:${minute} IST`;
}

export function sortGrootEventsChronological(events: GrootEvent[]): GrootEvent[] {
  return [...events].sort((a, b) => {
    const ta = parseGrootAtDisplay(a.at);
    const tb = parseGrootAtDisplay(b.at);
    if (ta == null && tb == null) return 0;
    if (ta == null) return 1;
    if (tb == null) return -1;
    return ta - tb;
  });
}

/** Anchor comment for a new hyperlocal fulfillment try. */
export function isGrootAttemptStart(event: GrootEvent): boolean {
  const c = String(event.comments ?? "").toLowerCase();
  return (
    c.includes("order created on inhouse") ||
    c.includes("order created with fulfillment status")
  );
}

/** Split bucket on anchor unless fulfillment pairs with inhouse in the same burst. */
export function shouldSplitAttemptBucket(event: GrootEvent, current: GrootEvent[]): boolean {
  if (!current.length || !isGrootAttemptStart(event)) return false;
  const c = String(event.comments ?? "").toLowerCase();
  if (c.includes("fulfillment status") && !c.includes("inhouse")) {
    const pairedInhouse = current
      .slice(-3)
      .some((e) => String(e.comments ?? "").toLowerCase().includes("order created on inhouse"));
    if (pairedInhouse) return false;
    // Retail HL often logs wfdp pings before packaging create — same journey, not a retry.
    if (c.includes("fulfillment status packaging")) return false;
  }
  return true;
}

function fulfillmentFromComments(comments: string): string {
  const m = comments.match(/fulfillment status\s+([a-z0-9_]+)/i);
  return m?.[1] ?? "";
}

function hubFromComments(comments: string): string {
  const m = comments.match(/transferred to hub\s+([^\n]+)/i);
  return m?.[1]?.trim() ?? "";
}

function riderFromComments(comments: string): string {
  const m = comments.match(/assigned to\s+(DE\d+)/i);
  if (m?.[1]) return m[1].toUpperCase();
  const loose = comments.match(/DE\d+/i);
  return loose ? loose[0].toUpperCase() : "";
}

function riderUnassignFromComments(comments: string): boolean {
  const c = comments.toLowerCase();
  return c.includes("unassigned from rider") || c.includes("unassigned from batch");
}

export function collectGrootRiders(events: GrootEvent[]): string[] {
  const set = new Set<string>();
  for (const e of events) {
    const fromComment = riderFromComments(String(e.comments ?? ""));
    if (fromComment) set.add(fromComment.toUpperCase());
    const inline = String(e.comments ?? "").match(/DE\d+/gi);
    inline?.forEach((r) => set.add(r.toUpperCase()));
  }
  return [...set].sort();
}

export function groupGrootAttempts(events: GrootEvent[]): GrootFulfillmentAttempt[] {
  const chron = sortGrootEventsChronological(events);
  if (!chron.length) return [];

  const buckets: GrootEvent[][] = [];
  let current: GrootEvent[] = [];

  for (const ev of chron) {
    if (shouldSplitAttemptBucket(ev, current)) {
      buckets.push(current);
      current = [ev];
    } else {
      current.push(ev);
    }
  }
  if (current.length) buckets.push(current);

  const attempts = buckets.map((attemptEvents, idx) => {
    const times = attemptEvents
      .map((e) => parseGrootAtDisplay(e.at))
      .filter((t): t is number => t != null);
    const startMs = times.length ? Math.min(...times) : 0;
    const endMs = times.length ? Math.max(...times) : 0;

    const commentsJoined = attemptEvents.map((e) => String(e.comments ?? "")).join("\n");
    const fulfillmentHint = fulfillmentFromComments(commentsJoined);
    const hubHint = hubFromComments(commentsJoined);
    const riders = new Set<string>();
    for (const e of attemptEvents) {
      const r = riderFromComments(String(e.comments ?? ""));
      if (r) riders.add(r);
    }

    const labelParts: string[] = [];
    if (fulfillmentHint) labelParts.push(fulfillmentHint.replaceAll("_", " "));
    if (hubHint) labelParts.push(`hub ${hubHint}`);
    if (riders.size) labelParts.push([...riders].slice(0, 2).join(", "));

    return {
      id: idx + 1,
      label: labelParts.length ? labelParts.join(" · ") : `Attempt ${idx + 1}`,
      fulfillmentHint,
      hubHint,
      riderHint: [...riders].join(", "),
      startMs,
      endMs,
      events: [...attemptEvents].reverse(),
      segments: buildStatusSegments(attemptEvents),
    } satisfies GrootFulfillmentAttempt;
  });

  return attempts.reverse();
}

const FRIENDLY_STATUS: Record<string, string> = {
  new: "Created (new)",
  placed: "Placed",
  parked: "Parked / on hold",
  assigned: "Assigned to rider",
  picked: "Picked",
  out_for_delivery: "Out for delivery",
  out_for_pickup: "Out for pickup",
  on_the_way: "On the way (delivery)",
  pickup_on_the_way: "On the way (pickup)",
  pickup_rescheduled: "Pickup rescheduled",
  rescheduled: "Rescheduled",
  cancelled: "Cancelled",
  completed: "Completed",
  reached: "Reached customer",
};

export function friendlyStatusLabel(status: string, subStatus?: string): string {
  const s = status.toLowerCase();
  const base = FRIENDLY_STATUS[s] ?? s.replaceAll("_", " ");
  const sub = subStatus && !isMissingDisplay(subStatus) ? subStatus.toLowerCase() : "";
  if (!sub || sub === s) return base;
  const subFriendly = FRIENDLY_STATUS[sub] ?? sub.replaceAll("_", " ");
  return `${base} · ${subFriendly}`;
}

function eventStatusLabel(ev: GrootEvent): { status: string; subStatus: string; label: string } {
  const status = String(ev.status ?? "unknown").toLowerCase();
  const subStatus = isMissingDisplay(ev.sub_status) ? "" : String(ev.sub_status).toLowerCase();
  const label = friendlyStatusLabel(status, subStatus);
  return { status, subStatus, label };
}

export function isCompletedGrootEvent(event: GrootEvent): boolean {
  const s = String(event.status ?? "").toLowerCase();
  const c = String(event.comments ?? "").toLowerCase();
  return s === "completed" || s === "delivered" || c.includes("order completed");
}

export function getLatestChronologicalEvent(events: GrootEvent[]): GrootEvent | null {
  const chron = sortGrootEventsChronological(events);
  if (!chron.length) return null;
  const parseable = chron.filter((e) => parseGrootAtDisplay(e.at) != null);
  return parseable.length ? parseable[parseable.length - 1]! : chron[chron.length - 1]!;
}

export function collectHubSequence(events: GrootEvent[]): string[] {
  const hubs: string[] = [];
  for (const e of sortGrootEventsChronological(events)) {
    const h = hubFromComments(String(e.comments ?? ""));
    if (h && hubs[hubs.length - 1] !== h) hubs.push(h);
  }
  return hubs;
}

export type GrootRiderChurn = {
  assignCount: number;
  unassignCount: number;
  distinctRiders: string[];
  perAttemptRiders: Array<{ attemptId: number; riders: string[] }>;
};

export function computeRiderChurn(
  events: GrootEvent[],
  attempts: GrootFulfillmentAttempt[],
): GrootRiderChurn {
  let assignCount = 0;
  let unassignCount = 0;
  const distinct = new Set<string>();
  for (const e of events) {
    const c = String(e.comments ?? "");
    const cl = c.toLowerCase();
    if (cl.includes("assigned to de") || /order assigned to de/i.test(c)) assignCount++;
    if (riderUnassignFromComments(c)) unassignCount++;
    const r = riderFromComments(c);
    if (r) distinct.add(r);
  }
  const perAttemptRiders = attempts.map((a) => {
    const riders = new Set<string>();
    for (const e of a.events) {
      const r = riderFromComments(String(e.comments ?? ""));
      if (r) riders.add(r);
    }
    if (a.riderHint) a.riderHint.split(",").map((x) => x.trim()).forEach((r) => riders.add(r));
    return { attemptId: a.id, riders: [...riders] };
  });
  return {
    assignCount,
    unassignCount,
    distinctRiders: [...distinct].sort(),
    perAttemptRiders,
  };
}

export type GrootFulfillmentLegSummary = {
  leg: "pickup" | "delivery";
  label: string;
  eventCount: number;
  dwellMin: number;
  dwellDisplay: string;
};

function legForStatus(status: string): "pickup" | "delivery" | "other" {
  const s = status.toLowerCase();
  if (s.includes("pickup") || s.includes("pickup_rescheduled")) return "pickup";
  if (
    s.includes("out_for_delivery") ||
    s.includes("on_the_way") ||
    s === "reached" ||
    s === "completed" ||
    s === "delivered"
  ) {
    return "delivery";
  }
  return "other";
}

export function computeFulfillmentLegs(events: GrootEvent[]): GrootFulfillmentLegSummary[] {
  const segments = buildGlobalStatusSegments(events).filter((s) => !s.timeUnknown);
  const eventCounts = { pickup: 0, delivery: 0 };
  for (const e of events) {
    const leg = legForStatus(String(e.status ?? ""));
    if (leg === "pickup") eventCounts.pickup++;
    if (leg === "delivery") eventCounts.delivery++;
  }
  const dwellMs = { pickup: 0, delivery: 0 };
  for (const seg of segments) {
    const leg = legForStatus(seg.status);
    if (leg === "pickup" || leg === "delivery") {
      dwellMs[leg] += Math.max(seg.endMs - seg.startMs, 0);
    }
  }
  const out: GrootFulfillmentLegSummary[] = [];
  if (eventCounts.pickup > 0 || dwellMs.pickup > 0) {
    const dwellMin = dwellMs.pickup / 60_000;
    out.push({
      leg: "pickup",
      label: "Return / pickup leg",
      eventCount: eventCounts.pickup,
      dwellMin,
      dwellDisplay: formatDwellMinutes(dwellMin),
    });
  }
  if (eventCounts.delivery > 0 || dwellMs.delivery > 0) {
    const dwellMin = dwellMs.delivery / 60_000;
    out.push({
      leg: "delivery",
      label: "Forward delivery leg",
      eventCount: eventCounts.delivery,
      dwellMin,
      dwellDisplay: formatDwellMinutes(dwellMin),
    });
  }
  return out;
}

export type GrootOutcomeSummary = {
  latestStatusLabel: string;
  latestAt: string;
  latestTone: GrootStatusTone;
  everCompleted: boolean;
  latestIsCompleted: boolean;
  outcomeTitle: string;
  outcomeDetail: string;
  outcomeTone: "emerald" | "rose" | "amber" | "slate";
};

export function buildStatusSegments(events: GrootEvent[]): GrootTimelineSegment[] {
  const chron = sortGrootEventsChronological(events);
  if (!chron.length) return [];

  const segments: GrootTimelineSegment[] = [];
  const unparsed: GrootEvent[] = [];

  for (let i = 0; i < chron.length; i++) {
    const ev = chron[i];
    const startMs = parseGrootAtDisplay(ev.at);
    if (startMs == null) {
      unparsed.push(ev);
      continue;
    }
    const next = chron[i + 1];
    const nextMs = next ? parseGrootAtDisplay(next.at) : null;
    const endMs = nextMs != null && nextMs > startMs ? nextMs : startMs + 60_000;
    const { status, subStatus, label } = eventStatusLabel(ev);
    const prev = segments[segments.length - 1];
    if (prev && !prev.timeUnknown && prev.status === status && prev.subStatus === subStatus && prev.endMs === startMs) {
      prev.endMs = endMs;
      continue;
    }
    segments.push({ status, subStatus, startMs, endMs, label });
  }

  if (unparsed.length) {
    const anchor =
      segments.length > 0
        ? segments[segments.length - 1].endMs
        : Date.UTC(2026, 0, 1);
    unparsed.forEach((ev, i) => {
      const { status, subStatus, label } = eventStatusLabel(ev);
      const startMs = anchor + i * 60_000;
      const endMs = startMs + 60_000;
      segments.push({
        status,
        subStatus,
        startMs,
        endMs,
        label: `${label} (time unknown)`,
        timeUnknown: true,
      });
    });
  }

  return segments;
}

export function buildGlobalStatusSegments(events: GrootEvent[]): GrootTimelineSegment[] {
  return buildStatusSegments(events);
}

export function summarizeGrootTimeline(events: GrootEvent[]): GrootTimelineSummary {
  const normalized = normalizeGrootEvents(events);
  const chron = sortGrootEventsChronological(normalized);
  const parseableCount = chron.filter((e) => parseGrootAtDisplay(e.at) != null).length;
  const times = chron
    .map((e) => parseGrootAtDisplay(e.at))
    .filter((t): t is number => t != null);
  const rangeStart = times.length ? Math.min(...times) : 0;
  const rangeEnd = times.length ? Math.max(...times) : 1;
  const oldest = chron.find((e) => parseGrootAtDisplay(e.at) === rangeStart);
  const newest = [...chron].reverse().find((e) => parseGrootAtDisplay(e.at) === rangeEnd);

  let rangeLabel = "";
  if (parseableCount === 0 && normalized.length > 0) {
    rangeLabel = "Timestamps unavailable";
  } else if (oldest?.at && newest?.at && oldest.at !== newest.at) {
    rangeLabel = `${oldest.at} → ${newest.at}`;
  } else {
    rangeLabel = newest?.at ?? oldest?.at ?? "";
  }

  return {
    eventCount: normalized.length,
    parseableCount,
    unparsedCount: normalized.length - parseableCount,
    rangeLabel,
    rangeStart,
    rangeEnd: Math.max(rangeEnd, rangeStart + 60_000),
    attemptCount: groupGrootAttempts(normalized).length,
    riders: collectGrootRiders(normalized),
  };
}

export type GrootStatusTone = "neutral" | "progress" | "delivery" | "success" | "warning" | "danger";

export function grootStatusTone(status: string | undefined): GrootStatusTone {
  const s = String(status ?? "").toLowerCase();
  if (!s || s === "unknown") return "neutral";
  if (s === "completed" || s === "delivered") return "success";
  if (s.includes("cancel")) return "danger";
  if (s.includes("reschedul")) return "warning";
  if (
    s.includes("out_for") ||
    s.includes("on_the_way") ||
    s.includes("pickup_on_the_way") ||
    s === "reached"
  ) {
    return "delivery";
  }
  if (s === "assigned" || s === "picked" || s === "picked_up") return "progress";
  return "neutral";
}

/** Bar / dot fills — same hues as Order RCA ``Tag`` (ok=emerald, warn=amber, bad=rose, info=sky). */
export const GROOT_STATUS_TONE_CLASS: Record<GrootStatusTone, string> = {
  neutral: "bg-slate-400",
  progress: "bg-blue-500",
  delivery: "bg-sky-500",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-rose-500",
};

/** Pill / chip surfaces aligned with status tone (legend, repeat row). */
export const GROOT_STATUS_PILL_CLASS: Record<GrootStatusTone, string> = {
  neutral: "border-slate-200 bg-slate-50 text-slate-800",
  progress: "border-sky-200 bg-sky-50 text-sky-900",
  delivery: "border-sky-200 bg-sky-50 text-sky-900",
  success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  danger: "border-rose-200 bg-rose-50 text-rose-900",
};

export type GrootInsightCardTone = "slate" | "amber" | "rose" | "emerald" | "sky";

/** Map Groot status semantics → stat-card palette on the Order RCA page. */
export function grootToneToInsightCard(tone: GrootStatusTone): GrootInsightCardTone {
  switch (tone) {
    case "success":
      return "emerald";
    case "danger":
      return "rose";
    case "warning":
      return "amber";
    case "progress":
    case "delivery":
      return "sky";
    default:
      return "slate";
  }
}

export function failureSignalsCardTone(failures: GrootFailureInsight[]): GrootInsightCardTone {
  if (failures.some((f) => f.tone === "danger")) return "rose";
  if (failures.some((f) => f.tone === "warning")) return "amber";
  if (failures.some((f) => f.tone === "info")) return "sky";
  return "slate";
}

export function outcomeToneToInsightCard(
  tone: GrootOutcomeSummary["outcomeTone"],
): GrootInsightCardTone {
  if (tone === "emerald") return "emerald";
  if (tone === "rose") return "rose";
  if (tone === "amber") return "amber";
  return "slate";
}

/** Width percentages that sum to 100 (avoids min-width blowout on dense timelines). */
export function segmentWidthPcts(
  segments: GrootTimelineSegment[],
  rangeStart: number,
  rangeEnd: number,
): number[] {
  if (!segments.length) return [];
  const span = Math.max(rangeEnd - rangeStart, 60_000);
  const raw = segments.map((seg) => {
    const dur = Math.max(seg.endMs - seg.startMs, seg.timeUnknown ? 60_000 : 1_000);
    return (dur / span) * 100;
  });
  const sum = raw.reduce((a, b) => a + b, 0) || 1;
  return raw.map((w) => Math.max(0.25, (w / sum) * 100));
}

/** @deprecated Use segmentWidthPcts; kept for tests. */
export function segmentWidthPct(
  segment: GrootTimelineSegment,
  rangeStart: number,
  rangeEnd: number,
): number {
  const pcts = segmentWidthPcts([segment], rangeStart, rangeEnd);
  return pcts[0] ?? 0.35;
}

export function formatEventRangeLabel(events: GrootEvent[]): string {
  if (!events.length) return MISSING;
  const chron = sortGrootEventsChronological(events);
  const oldest = chron[0]?.at;
  const newest = chron[chron.length - 1]?.at;
  if (oldest && newest && oldest !== newest && !isMissingDisplay(oldest) && !isMissingDisplay(newest)) {
    return `${oldest} → ${newest}`;
  }
  return newest ?? oldest ?? MISSING;
}

export function filterGrootMilestones(events: GrootEvent[]): GrootEvent[] {
  const list = [...sortGrootEventsChronological(events)].reverse();
  const seen = new Set<string>();
  return list.filter((e) => {
    const key = `${e.status}|${e.sub_status}|${String(e.comments ?? "").slice(0, 40)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    const c = String(e.comments ?? "").toLowerCase();
    const s = String(e.status ?? "").toLowerCase();
    return (
      isMilestoneComment(c) ||
      ["completed", "cancelled", "placed", "assigned", "out_for_delivery", "out_for_pickup"].some((x) =>
        s.includes(x),
      )
    );
  });
}

export function isMilestoneComment(c: string): boolean {
  return (
    c.includes("order completed") ||
    c.includes("order cancelled") ||
    c.includes("order created") ||
    c.includes("assigned to de") ||
    c.includes("out for delivery") ||
    c.includes("out for pickup") ||
    c.includes("transferred to hub") ||
    c.includes("rescheduled")
  );
}

export type GrootStatusAggregate = {
  status: string;
  label: string;
  eventCount: number;
  segmentVisits: number;
  dwellMin: number;
  dwellDisplay: string;
  pctOfJourney: number;
  tone: GrootStatusTone;
};

export type GrootFailureInsight = {
  id: string;
  tone: "danger" | "warning" | "info";
  title: string;
  detail: string;
  count: number;
};

export type GrootTimelineInsights = {
  stagesByDwell: GrootStatusAggregate[];
  stagesRepeated: GrootStatusAggregate[];
  longestBlocker: GrootStatusAggregate | null;
  failures: GrootFailureInsight[];
  headlines: string[];
  /** @deprecated use outcome.everCompleted */
  completedInTimeline: boolean;
  totalDwellMin: number;
  outcome: GrootOutcomeSummary;
  lastStatus: { label: string; at: string; tone: GrootStatusTone };
  riderChurn: GrootRiderChurn;
  hubSequence: string[];
  fulfillmentLegs: GrootFulfillmentLegSummary[];
  showDwellDisclaimer: boolean;
};

export function computeGrootOutcome(
  events: GrootEvent[],
  attempts: GrootFulfillmentAttempt[],
): GrootOutcomeSummary {
  const chron = sortGrootEventsChronological(events);
  const everCompleted = chron.some(isCompletedGrootEvent);
  const latestGlobal = getLatestChronologicalEvent(events);
  const newestAttempt = attempts[0];
  const latestInNewestAttempt = newestAttempt?.events[0] ?? latestGlobal;
  const latest = latestInNewestAttempt ?? latestGlobal;
  const latestIsCompleted = latest ? isCompletedGrootEvent(latest) : false;
  const latestStatus = latest ? String(latest.status ?? "unknown").toLowerCase() : "unknown";
  const latestLabel = latest ? friendlyStatusLabel(latestStatus, latest.sub_status) : MISSING;
  const latestAt = latest?.at ?? MISSING;
  const latestTone = grootStatusTone(latestStatus);

  let outcomeTitle = "Not completed";
  let outcomeDetail = "Latest Groot state is not completed.";
  let outcomeTone: GrootOutcomeSummary["outcomeTone"] = "rose";

  if (latestIsCompleted) {
    outcomeTitle = "Completed (latest)";
    outcomeDetail = `Latest event is completed at ${latestAt}.`;
    outcomeTone = "emerald";
  } else if (everCompleted) {
    outcomeTitle = "Completed earlier · latest failed";
    outcomeDetail = `Had completed in timeline; latest state is ${latestLabel} (${latestAt}).`;
    outcomeTone = "amber";
  } else if (latestStatus.includes("cancel")) {
    outcomeTitle = "Cancelled (latest)";
    outcomeDetail = `Latest state is ${latestLabel} at ${latestAt}.`;
    outcomeTone = "rose";
  } else if (latest) {
    outcomeTitle = `Open · ${latestLabel}`;
    outcomeDetail = `Latest Groot event at ${latestAt}.`;
    outcomeTone = "slate";
  }

  return {
    latestStatusLabel: latestLabel,
    latestAt,
    latestTone,
    everCompleted,
    latestIsCompleted,
    outcomeTitle,
    outcomeDetail,
    outcomeTone,
  };
}

export function formatDwellMinutes(min: number): string {
  if (!Number.isFinite(min) || min < 1) return "< 1 min";
  if (min < 60) return `${Math.round(min)} min`;
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function friendlyStatus(status: string): string {
  return friendlyStatusLabel(status);
}

function countPattern(events: GrootEvent[], predicate: (e: GrootEvent) => boolean): number {
  return events.reduce((n, e) => (predicate(e) ? n + 1 : n), 0);
}

function isGrootCancelEvent(event: GrootEvent): boolean {
  const status = String(event.status ?? "").toLowerCase();
  const comment = String(event.comments ?? "").toLowerCase();
  return status.includes("cancel") || comment.includes("cancel");
}

function isAllocationCancelComment(comment: string): boolean {
  return /allocation.*cancel/i.test(comment);
}

function isOrderLevelCancelEvent(event: GrootEvent): boolean {
  const status = String(event.status ?? "").toLowerCase();
  const comment = String(event.comments ?? "").toLowerCase();
  if (status.includes("cancel")) return true;
  if (/order cancel(?:l)?ed/.test(comment)) return true;
  return isGrootCancelEvent(event) && !isAllocationCancelComment(comment);
}

function cancelFailureTone(events: GrootEvent[], everCompleted: boolean): GrootFailureInsight["tone"] {
  const cancelEvents = events.filter(isGrootCancelEvent);
  if (!cancelEvents.length) return "danger";
  if (cancelEvents.some(isOrderLevelCancelEvent)) return "danger";
  const allocationOnly = cancelEvents.every((e) =>
    isAllocationCancelComment(String(e.comments ?? "")),
  );
  if (everCompleted && allocationOnly) return "warning";
  return "danger";
}

const CANCEL_RETRY_FAILURE_TITLE = "Cancel / retry events";
const CANCEL_RETRY_FAILURE_DETAIL =
  "Delivery-partner or inhouse allocation was cancelled, or hyperlocal fulfilment was aborted — usually a retry before reassignment.";

export function buildGrootInsights(events: GrootEvent[]): GrootTimelineInsights {
  const normalized = normalizeGrootEvents(events);
  const chron = sortGrootEventsChronological(normalized);
  const segments = buildGlobalStatusSegments(normalized).filter((s) => !s.timeUnknown);
  const attempts = groupGrootAttempts(normalized);
  const summary = summarizeGrootTimeline(normalized);

  const eventCountByStatus = new Map<string, number>();
  for (const e of chron) {
    const status = String(e.status ?? "unknown").toLowerCase();
    eventCountByStatus.set(status, (eventCountByStatus.get(status) ?? 0) + 1);
  }

  const dwellByStatus = new Map<string, { dwellMs: number; segmentVisits: number }>();
  for (const seg of segments) {
    const cur = dwellByStatus.get(seg.status) ?? { dwellMs: 0, segmentVisits: 0 };
    cur.dwellMs += Math.max(seg.endMs - seg.startMs, 0);
    cur.segmentVisits += 1;
    dwellByStatus.set(seg.status, cur);
  }

  const statusKeys = new Set([...eventCountByStatus.keys(), ...dwellByStatus.keys()]);
  const totalDwellMs = [...dwellByStatus.values()].reduce((a, b) => a + b.dwellMs, 0) || 1;

  const stagesByDwell: GrootStatusAggregate[] = [...statusKeys]
    .map((status) => {
      const dwell = dwellByStatus.get(status);
      const dwellMin = (dwell?.dwellMs ?? 0) / 60_000;
      return {
        status,
        label: friendlyStatus(status),
        eventCount: eventCountByStatus.get(status) ?? 0,
        segmentVisits: dwell?.segmentVisits ?? 0,
        dwellMin,
        dwellDisplay: formatDwellMinutes(dwellMin),
        pctOfJourney: Math.round(((dwell?.dwellMs ?? 0) / totalDwellMs) * 100),
        tone: grootStatusTone(status),
      };
    })
    .filter((s) => s.eventCount > 0 || s.dwellMin > 0)
    .sort((a, b) => b.dwellMin - a.dwellMin || b.eventCount - a.eventCount);

  const stagesRepeated = stagesByDwell
    .filter((s) => s.eventCount >= 2)
    .sort((a, b) => b.eventCount - a.eventCount || b.dwellMin - a.dwellMin);

  const longestBlocker = stagesByDwell.find((s) => s.dwellMin >= 1) ?? null;

  const failures: GrootFailureInsight[] = [];
  const cancelCount = countPattern(chron, isGrootCancelEvent);
  const rescheduleCount = countPattern(
    chron,
    (e) =>
      String(e.status ?? "").toLowerCase().includes("reschedul") ||
      String(e.comments ?? "").toLowerCase().includes("rescheduled"),
  );
  const unassignCount = countPattern(chron, (e) =>
    String(e.comments ?? "").toLowerCase().includes("unassigned"),
  );
  const hubTransferCount = countPattern(chron, (e) =>
    String(e.comments ?? "").toLowerCase().includes("transferred to hub"),
  );
  const onHoldCount = countPattern(chron, (e) =>
    String(e.comments ?? "").toLowerCase().includes("on hold"),
  );
  const inhouseCreates = countPattern(chron, (e) => isGrootAttemptStart(e));
  const outcome = computeGrootOutcome(normalized, attempts);

  if (cancelCount > 0) {
    failures.push({
      id: "cancel",
      tone: cancelFailureTone(chron, outcome.everCompleted),
      title: CANCEL_RETRY_FAILURE_TITLE,
      detail: CANCEL_RETRY_FAILURE_DETAIL,
      count: cancelCount,
    });
  }
  if (rescheduleCount > 0) {
    failures.push({
      id: "reschedule",
      tone: "warning",
      title: "Reschedules",
      detail: "Pickup or delivery window moved — adds waiting time before rider action.",
      count: rescheduleCount,
    });
  }
  if (unassignCount > 0) {
    failures.push({
      id: "unassign",
      tone: "warning",
      title: "Rider / batch unassigns",
      detail: "Rider or batch was dropped — usually blocks progress until reassigned.",
      count: unassignCount,
    });
  }
  const hubSequence = collectHubSequence(normalized);
  if (hubTransferCount > 0) {
    const hubList =
      hubSequence.length > 0
        ? hubSequence.slice(0, 6).join(" → ") + (hubSequence.length > 6 ? " → …" : "")
        : "see events";
    failures.push({
      id: "hub",
      tone: "info",
      title: "Hub transfers",
      detail: `Route: ${hubList}. Each transfer can add parked / on-hold time.`,
      count: hubTransferCount,
    });
  }
  if (onHoldCount > 0) {
    failures.push({
      id: "hold",
      tone: "warning",
      title: "On hold",
      detail: "Order parked on hold — waiting for slot, inventory, or ops action.",
      count: onHoldCount,
    });
  }
  if (attempts.length > 1 || inhouseCreates > 1) {
    failures.push({
      id: "retries",
      tone: "warning",
      title: "Fulfillment retries",
      detail: "Multiple inhouse re-creates — same PO restarted hyperlocal fulfillment.",
      count: Math.max(attempts.length, inhouseCreates),
    });
  }

  const riderChurn = computeRiderChurn(normalized, attempts);
  const fulfillmentLegs = computeFulfillmentLegs(normalized);
  const latest = getLatestChronologicalEvent(normalized);

  if (!outcome.everCompleted && chron.length > 0) {
    failures.push({
      id: "no_complete",
      tone: "danger",
      title: "No completion in Groot",
      detail: "Timeline never reached completed — journey may still be open or failed out of hyperlocal.",
      count: 1,
    });
  }

  const headlines: string[] = [];
  if (longestBlocker && longestBlocker.dwellMin >= 5) {
    headlines.push(
      `Longest wait: ${longestBlocker.label} (~${longestBlocker.dwellDisplay}, ${longestBlocker.pctOfJourney}% of tracked time between events)`,
    );
  }
  if (stagesRepeated.length > 0) {
    const top = stagesRepeated.slice(0, 2).map((s) => `${s.label} ×${s.eventCount}`);
    headlines.push(`Most repeated: ${top.join(", ")}`);
  }
  if (summary.attemptCount > 1) {
    headlines.push(`${summary.attemptCount} fulfillment attempts`);
  }
  if (cancelCount > 0) {
    headlines.push(`${cancelCount} cancel/retry events`);
  }
  if (chron.length > 0) {
    headlines.push(`${outcome.outcomeTitle} — ${outcome.latestStatusLabel} (${outcome.latestAt})`);
  }
  if (hubSequence.length > 1) {
    headlines.push(`Hub path: ${hubSequence.slice(0, 4).join(" → ")}`);
  }

  const showDwellDisclaimer =
    summary.eventCount >= 50 || summary.attemptCount >= 3 || totalDwellMs / 60_000 < 60;

  return {
    stagesByDwell,
    stagesRepeated,
    longestBlocker: longestBlocker ?? null,
    failures: failures.sort((a, b) => b.count - a.count),
    headlines: headlines.slice(0, 5),
    completedInTimeline: outcome.everCompleted,
    totalDwellMin: totalDwellMs / 60_000,
    outcome,
    lastStatus: {
      label: latest ? friendlyStatusLabel(String(latest.status ?? ""), latest.sub_status) : MISSING,
      at: latest?.at ?? MISSING,
      tone: grootStatusTone(String(latest?.status ?? "")),
    },
    riderChurn,
    hubSequence,
    fulfillmentLegs,
    showDwellDisclaimer,
  };
}
