/**
 * Constants for NewsPage - Prosight N Dashboard
 */

// Chart color palette for multiple series
export const COLORS = [
  "#2563eb",
  "#dc2626",
  "#16a34a",
  "#d97706",
  "#7c3aed",
  "#0891b2",
  "#be185d",
  "#84cc16",
  "#9a3412",
  "#0e7490",
];

// Hierarchy levels
export const LEVELS = ["L0_total", "L1_single", "L2_pair", "L3_full"] as const;
export type LevelType = (typeof LEVELS)[number];

// Feature patterns for driver analysis
export const FEAT_PATTERNS: [string, string][] = [
  ["rr_gmv", "Returns / refund GMV"],
  ["delivered_gmv", "Delivered GMV"],
  ["delivered_order", "Delivered orders"],
  ["delivered_units_sold", "Units sold"],
  ["delivered_coupon_discount", "Coupon discount"],
  ["cancelled_order", "Cancellations"],
];

// Feature explanations for up/down movements
export const FEAT_EXP: Record<string, { up: string; down: string }> = {
  "Returns/refund GMV": {
    up: "Higher returns eat into fulfilled GMV — a key drop trigger.",
    down: "Fewer returns improved net fulfilled GMV.",
  },
  "Delivered GMV": {
    up: "Core GMV strong — directly supports the metric.",
    down: "Core delivered GMV fell, dragging the metric down.",
  },
  "Delivered orders": {
    up: "More orders fulfilled — direct positive driver.",
    down: "Fewer delivered orders — main output metric fell.",
  },
  "Units sold": {
    up: "Higher unit sales boosted output.",
    down: "Fewer units sold — reduced fulfilment volume.",
  },
  "Coupon discount": {
    up: "Heavier discounting — indicates promotional demand.",
    down: "Lower coupon utilisation — reduced order incentives.",
  },
  Cancellations: {
    up: "Cancellations surged — net fulfilment dropped.",
    down: "Fewer cancellations improved net delivered orders.",
  },
};
