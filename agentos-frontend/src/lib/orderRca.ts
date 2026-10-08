import { apiFetch, apiJson, formatApiErrorDetail } from "./api";

export type OrderRcaRunStatus = "queued" | "running" | "completed" | "failed";

export type OrderRcaProgress = {
  step?: string;
  label?: string;
  completed?: number;
  total?: number;
};

export type OrderRcaPreflight = {
  placed_at?: string;
  allocation_badge?: string;
  allocation_badge_reason?: string;
  allocation_tier_label?: string;
  allocated_to_preferred?: boolean;
  distance_km?: number;
  pincode?: string;
  city?: string;
  state?: string;
  zone?: string;
  order_status?: string;
  order_status_id?: string | number;
  /** CX-style service tag (e.g. 1 hour, Zero day, Standard); omitted when unknown. */
  service_type?: string | null;
  return_reason?: string;
  actual_vendor?: string;
  promised_delivery?: string;
  promised_delivery_source?: string;
  promised_first?: {
    display?: string;
    source?: string;
    communicated_at_display?: string;
  };
  promised_current?: {
    display?: string;
    source?: string;
    communicated_at_display?: string;
  };
  actual_delivery?: string;
  late_minutes?: number;
  late_minutes_display?: string | null;
  /** Computed vs promised_first — canonical for RCA UI and narrative. */
  is_eta_breached?: boolean | null;
  /** Order-service flag — debug / OMS parity only. */
  is_eta_breached_order_api?: boolean | null;
  /**
   * pending_within_sla | open_past_promise | delivered_late | delivered_on_time | unknown_anchor
   */
  breach_kind?: string;
  /** Minutes late (delivered) or minutes past first promise (open). */
  breach_minutes?: number | null;
  breach_minutes_display?: string | null;
  /** Delivery ETA revision chain: First promised → ETA 1..N → Actual. */
  eta_jumps?: Array<{
    label?: string;
    role?: string;
    display?: string;
    from_display?: string;
    source?: string;
    recorded_at_display?: string;
  }>;
  diagnosed_at?: string;
  ideal_summary?: string;
  preferred_vendors?: Array<{
    rank: number;
    physical_store?: string;
    km?: number;
    vendor_type?: string;
    virtual_store_count?: number;
    nearest_virtual_code?: string;
    /** @deprecated use physical_store */
    label?: string;
    code?: string;
  }>;
};

export type OrderRcaStoreViews = {
  skipped?: boolean;
  skip_reason?: string;
  nearby_stores?: Array<Record<string, unknown>>;
  nearby_warehouses?: Array<Record<string, unknown>>;
  allocated_store?: Record<string, unknown> | null;
  status?: string;
  rows?: Array<Record<string, unknown>>;
  panel_note?: string | null;
  /** @deprecated use panel_note */
  proxy_note?: string | null;
};

export type OrderRcaSkuLine = {
  sku_id?: string;
  name?: string;
  quantity?: number | string;
  /** Order-service sku.priority === 30 (sampling / freebie) — display only */
  is_sampling?: boolean;
};

export type CartAllocationOption = {
  title?: string | null;
  eta_to?: string | null;
  eta_short?: string | null;
  service_name?: string | null;
  vendor_code?: string | null;
  sku_ids?: string[];
  is_fastest?: boolean;
};

export type CartAllocationSkuChange = {
  kind?: "added" | "removed" | "qty" | "changed";
  label?: string;
  qty?: number;
  from_qty?: number;
  to_qty?: number;
};

export type CartAllocationOptionsDelta = {
  added?: string[];
  removed?: string[];
  changed?: string[];
  eta_changed?: string[];
  prev_count?: number | null;
  cur_count?: number | null;
};

export type CartAllocationShipmentGroup = {
  group_index?: number;
  group_status?: "new" | "removed";
  sku_ids?: string[];
  sku_labels?: string;
  option_count?: number;
  options?: CartAllocationOption[];
  fastest_title?: string | null;
  fastest_eta?: string | null;
  service_name?: string | null;
  options_delta?: CartAllocationOptionsDelta;
};

export type CartAllocationJourneyStep = {
  at?: string;
  step_kind?: string;
  step_label?: string;
  layout?: string;
  layout_label?: string;
  sku_summary?: string | null;
  sku_delta?: string | null;
  sku_changes?: CartAllocationSkuChange[] | null;
  shipment_delta?: string | null;
  shipments?: CartAllocationShipmentGroup[];
  show_all_options?: boolean;
  changes?: string[];
};

export type CartAllocationCartVsOrder = {
  match?: boolean;
  detail?: string;
  placed_summary?: string | null;
  cart_summary?: string | null;
};

export type CartAllocationJourney = {
  available?: boolean;
  empty?: boolean;
  empty_reason?: string | null;
  placed_at?: string;
  cart_id?: string | null;
  headline?: string;
  insights?: string[];
  meaningful_changes?: number;
  steps?: CartAllocationJourneyStep[];
  cart_vs_order?: CartAllocationCartVsOrder | null;
  note?: string;
};

export type OrderRcaStatusMilestone = {
  status_id?: string;
  label?: string;
  at?: string;
  duration_min?: number | null;
};

export type OrderRcaStatusTransition = {
  label?: string;
  duration_min?: number | null;
  duration_display?: string;
  default_sla?: string;
  default_sla_min?: number | null;
  sla_status?: string;
  at?: string;
  hover?: string;
  from_status_id?: string;
  to_status_id?: string;
};

export type OrderRcaShippingSummary = {
  delivery_partners_code?: string;
  waybill?: string;
  fe_name?: string;
  fe_phone?: string;
  tracking_url?: string;
};

export type OrderRcaOperations = {
  skipped?: boolean;
  skip_reason?: string;
  status_chronology?: Array<Record<string, unknown>>;
  status_transitions?: OrderRcaStatusTransition[];
  ops_sla?: {
    is_active_service?: boolean;
    early_cutoff_min?: number;
    packaging_cutoff_min?: number | null;
  };
  segments?: Array<Record<string, unknown>>;
  groot_events?: Array<Record<string, unknown>>;
  groot_empty?: boolean;
  clickpost_events?: Array<Record<string, unknown>>;
  clickpost_empty?: boolean;
  last_mile_mode?: "groot" | "clickpost" | "none";
  shipping_summary?: OrderRcaShippingSummary;
  split_child?: boolean;
  timeline_note?: string;
  return_note?: string;
  return_followed?: boolean;
  borrowed_status_ids?: string[];
  boundary_status_id?: string;
  parent_id?: string | null;
  order_id?: string;
};

export type OrderRcaSplitInsights = {
  parent_id?: string;
  child_id?: string;
  insights?: Array<{ headline: string; detail: string }>;
};

export type MsnAdherenceSkuLine = {
  sku_id?: string;
  name?: string;
  sku_sub_grade?: string | null;
  effective_msn?: number | null;
  effective_msn_display?: string | null;
  on_shelf_qty?: number | null;
  on_shelf_display?: string | null;
  asked_qty?: number | null;
  asked_display?: string | null;
  status_code?: string;
  status_label?: string;
  status_tone?: "ok" | "warn" | "bad" | "slate";
};

export type MsnAdherenceStore = {
  physical_store?: string;
  distance_km?: number;
  vendor_type?: string;
  store_kind?: string;
  rollup_status_code?: string;
  rollup_label?: string;
  rollup_tone?: "ok" | "warn" | "bad" | "slate";
  skus?: MsnAdherenceSkuLine[];
};

export type OrderRcaMsnAdherence = {
  status?: string;
  panel_note?: string;
  p1_api_configured?: boolean;
  stores?: MsnAdherenceStore[];
};

export type OrderRcaP1 = OrderRcaStoreViews & {
  status?: string;
  msn_adherence?: OrderRcaMsnAdherence;
  rows?: Array<Record<string, unknown>>;
};

export type OrderRcaAllocationUnavailable = {
  reason: "unavailable";
  days_placed?: number | null;
  message: string;
};

export type OrderRcaPerfectOrderPillar = {
  id: string;
  pass?: boolean | null;
  status: "pass" | "fail" | "unknown";
  label: string;
  detail?: string;
  counts_as_perfect?: boolean;
};

export type OrderRcaPerfectOrder = {
  overall: "perfect" | "imperfect";
  overall_pass: boolean;
  hint?: string;
  total_mrp_increase?: number;
  mrp_increase_limit?: number;
  mrp_increase_source?: string;
  pillars: OrderRcaPerfectOrderPillar[];
};

export type OrderRcaFacts = {
  order_id: string;
  parent_id?: string | null;
  analysis_skipped?: boolean;
  analysis_mode?: string;
  allocation_unavailable?: OrderRcaAllocationUnavailable | null;
  warnings?: string[];
  preflight: OrderRcaPreflight;
  p1: OrderRcaP1;
  p2: { status: string; rows: unknown[]; status_message?: string };
  p3: OrderRcaStoreViews;
  p4: OrderRcaStoreViews;
  operations: OrderRcaOperations;
  skus?: OrderRcaSkuLine[];
  cart_allocation_journey?: CartAllocationJourney;
  signals?: string[];
  split_insights?: OrderRcaSplitInsights | null;
  vendor_types_in_allocation?: string[];
  perfect_order?: OrderRcaPerfectOrder;
};

export type OrderRcaSynthesis = {
  verdict: string;
  verdict_subline?: string;
  primary_cause: string;
  contributing_factors: string[];
  recommended_action: string;
  segment_notes?: Array<{ label: string; note: string }>;
  data_gaps?: string[];
  hypotheses: Array<{ finding: string; hypothesis: string; alignment: string }>;
};

export type OrderRcaReport = {
  order_id: string;
  schema_version: number;
  facts: OrderRcaFacts;
  synthesis: OrderRcaSynthesis;
  synthesis_source?: string;
};

export type OrderRcaRun = {
  run_id: string;
  order_id: string;
  status: OrderRcaRunStatus;
  progress?: OrderRcaProgress;
  report?: OrderRcaReport | null;
  error?: { code?: string; message?: string } | null;
};

export async function startOrderRcaDiagnose(orderId: string): Promise<{ run_id: string; order_id: string; status: string }> {
  const res = await apiFetch("/api/order-rca/diagnose", {
    method: "POST",
    body: JSON.stringify({ order_id: orderId.trim() }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiErrorDetail((body as { detail?: unknown }).detail) || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function getOrderRcaRun(runId: string): Promise<OrderRcaRun> {
  return apiJson<OrderRcaRun>(`/api/order-rca/runs/${encodeURIComponent(runId)}`);
}

export function displayVal(v: unknown, fallback = "—"): string {
  if (v === null || v === undefined || v === "") return fallback;
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

/** Human-readable delay for RCA UI (matches backend time_utils.format_duration_minutes). */
export function formatDurationMinutes(minutes: number | null | undefined, suffix = ""): string | null {
  if (minutes == null || !Number.isFinite(minutes)) return null;
  const total = Math.max(0, Math.floor(minutes));
  if (total <= 0) return null;
  if (total <= 60) return `${total} min${suffix}`;
  if (total <= 24 * 60) {
    const hours = Math.floor(total / 60);
    const mins = total % 60;
    if (mins === 0) return `${hours} h${suffix}`;
    return `${hours} h ${mins} min${suffix}`;
  }
  const days = Math.floor(total / (24 * 60));
  const rem = total % (24 * 60);
  const hours = Math.floor(rem / 60);
  const mins = rem % 60;
  const parts = [`${days} d`];
  if (hours > 0) parts.push(`${hours} h`);
  if (mins > 0) parts.push(`${mins} min`);
  return parts.join(" ") + suffix;
}
