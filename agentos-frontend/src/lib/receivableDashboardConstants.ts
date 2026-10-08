/**
 * Receivable dashboard tokens shared by `lib` (mocks, tests) and UI components.
 * `RECEIVABLE_REMINDER_GRID_FORMAT` must match `REMINDER_GRID_FORMAT` in
 * `receivable_dashboard.py`.
 */
export const RECEIVABLE_REMINDER_GRID_FORMAT = "excel_5x6_v1" as const;

/** Empty / muted matrix cell background (collection grid heat + filter-out cells). */
export const HEAT_EMPTY = "hsl(210 18% 96%)";

export const HEAT_TEXT_HEX = "#0f172a";

export const FILTER_MUTED = "rgb(100, 116, 139)";
