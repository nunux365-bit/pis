"use client";

import ProsightNewsWithSummary from "@/components/prosight/ProsightNewsWithSummary";

/**
 * Prosight N Dashboard - Daily Anomaly Report
 *
 * This page displays the Prosight anomaly dashboard with two views:
 * - Summary view: Flock-style daily anomaly report with date picker
 * - Detailed graph view: Time series charts with filtering and series cards
 *
 * Use the tab toggle to switch between views.
 */
export default function ProsightPage() {
  return <ProsightNewsWithSummary />;
}
