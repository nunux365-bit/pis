/**
 * Dev-only rich mock for the receivables dashboard when
 * `?receivableMock=full` is used on the Email Agent page (development build).
 * Aligned with Excel-style “Total due” + “Parties” reference (5 ageing rows, no “Not due”).
 */

import type { ReceivableDashboardSnapshot } from "@/lib/api";
import { RECEIVABLE_REMINDER_GRID_FORMAT } from "@/lib/receivableDashboardConstants";

const AGE = [
  "0-1 Months",
  "1-3 Months",
  "3-6 Months",
  "6-12 Months",
  "1Y+",
];

/** Dev mock (₹ Lakh): 5 ageing rows × 6 reminder columns ``"0-1"``, ``"2"``…``"6+"``. */
const VALUES: number[][] = [
  [5.1, 2.7, 4.6, 25.4, 0, 6.7],
  [12.0, 17.3, 21.6, 31.3, 169.3, 3.8],
  [0, 7.6, 4.8, 0, 1.4, 7.5],
  [2.0, 1.3, 22.8, 40.1, 0, 3.4],
  [0, 0, 3.3, 1.8, 0, 0],
];

/** Pre-formatted lines like the workbook “Parties” column (name + ₹ L + wtd m). */
const LINES: string[][][] = VALUES.map((row, ri) =>
  row.map((v, ci) => {
    if (v <= 0) return [];
    if (ri === 0 && ci === 1) {
      return [
        "EMMVEE ENERGY PRIVATE LIMITED (₹2.7 L, 0.8m)",
        "Sample Corp (₹0.0 L, 0.4m)",
      ];
    }
    if (ri === 0 && ci === 0) {
      return ["Zero-reminder bucket party (₹5.1 L, 1.1m)"];
    }
    if (ri === 1 && ci === 5) {
      return ["Party in cell R1C5 (₹3.8 L, 4.2m)"];
    }
    if (v > 20) {
      return [`TATA AUTOCOMP SYSTEMS LIMITED (₹${v.toFixed(1)} L, 6.9m)`];
    }
    return [`Party R${ri}C${ci} (₹${v.toFixed(1)} L, ${(1.2 + ci * 0.3).toFixed(1)}m)`];
  })
);

export function getReceivableDashboardFullMock(): ReceivableDashboardSnapshot {
  const kpiAll: Record<string, number> = {
    "Not Due": 6665.72,
    "Net Due": 4992.82,
    RECEIVABLES: 11872.31,
    "0-1 Months": 2484.44,
    "1-3 Months": 2036.71,
    "3-6 Months": 392.24,
    "6-9 Months": 115.69,
    "6-12 Months": 200.0,
    CHECK: 0,
    "DUE TDS": 213.77,
    "+1YRS": 98.01,
  };

  return {
    id: "00000000-0000-4000-8000-00000000mock",
    created_at: new Date().toISOString(),
    source_message_id: null,
    payload: {
      meta: {
        parser_version: "1-mock",
        reminder_grid_format: RECEIVABLE_REMINDER_GRID_FORMAT,
        workbook: "Receivables-REFERENCE.xlsx",
        receivable_sheet: "Receivable as on (mock)",
        unbilled_sheet: "Partywise Unbilled",
        reminder_sends: {
          table: "email_automation_sends",
          filters: {
            status_in: ["approved", "rendered", "sent"],
            workflow_type: "PAYMENT_REMINDER_WEEKLY",
            test_mode: false,
          },
          aggregation: "per business_key: count(distinct period_key) (example)",
          row_count: 400,
          distinct_business_keys: 200,
          distinct_period_keys: 4,
        },
      },
      business_units: ["All", "Corporate Wellness", "e-Pharmacy (Platform Aggregator)"],
      kpi_lakh: {
        all: kpiAll,
        by_business_unit: {
          "Corporate Wellness": { ...kpiAll, "Net Due": 2100.0, "0-1 Months": 900.0 },
          "e-Pharmacy (Platform Aggregator)": {
            ...kpiAll,
            "Net Due": 1800.0,
            "1-3 Months": 600.0,
          },
        },
      },
      unbilled_lakh: {
        all: 900.5,
        by_business_unit: {
          "Corporate Wellness": 500.0,
          "e-Pharmacy (Platform Aggregator)": 400.0,
        },
      },
      top_parties: {
        all: [
          {
            code: "C001",
            name: "TATA STEEL LIMITED",
            business_unit: "Corporate Wellness",
            net_due_lakh: 139.9,
            wtd_age_months: 6.9,
          },
          {
            code: "C002",
            name: "Tata Power Company Ltd",
            business_unit: "Corporate Wellness",
            net_due_lakh: 28.7,
            wtd_age_months: 4.2,
          },
        ],
        by_business_unit: {
          "Corporate Wellness": [
            {
              code: "C001",
              name: "TATA STEEL LIMITED",
              business_unit: "Corporate Wellness",
              net_due_lakh: 139.9,
              wtd_age_months: 6.9,
            },
          ],
        },
      },
      collection_grids: {
        all: {
          reminder_bands: ["0-1", "2", "3", "4", "5", "6+"],
          ageing_buckets: AGE,
          values_lakh: VALUES,
          client_names: LINES,
        },
        by_business_unit: {
          "Corporate Wellness": {
            reminder_bands: ["0-1", "2", "3", "4", "5", "6+"],
            ageing_buckets: AGE,
            values_lakh: VALUES.map((r) => r.map((v) => v * 0.6)),
            client_names: LINES,
          },
          "e-Pharmacy (Platform Aggregator)": {
            reminder_bands: ["0-1", "2", "3", "4", "5", "6+"],
            ageing_buckets: AGE,
            values_lakh: VALUES.map((r) => r.map((v) => v * 0.45)),
            client_names: LINES,
          },
        },
      },
    },
  };
}
