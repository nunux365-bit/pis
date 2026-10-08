/**
 * Dimension display vocabulary, shared by the breakdown table and its L2 pair
 * drill-down.
 *
 * Lives in its own module rather than in UnifiedDimensionBreakdown because
 * PairDrilldown needs `DIM_LABEL` too, and importing it from the parent that
 * renders the child would be a cycle.
 */

/** Backend dim name → the label shown in headers and cut pickers. */
export const DIM_LABEL: Record<string, string> = {
  state: "state",
  bu: "bu",
  channel: "channel",
  hour: "hour",
  sku_name: "SKU",
  name: "SKU",
  order_state: "order state",
  payment_method: "payment",
  coupon_flag: "coupon",
};

// Known dimension order; anything not listed is appended (so labs `hour`/`sku_name`
// are never silently dropped — spec 38 §3.1). `bu` is a partition/selector now,
// never an RCA child dim, so it is intentionally excluded here.
export const DIM_ORDER = [
  "state",
  "channel",
  "hour",
  "sku_name",
  "name",
  "order_state",
  "payment_method",
  "coupon_flag",
];
