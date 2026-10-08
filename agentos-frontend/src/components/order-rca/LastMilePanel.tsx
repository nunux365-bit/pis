"use client";

import { ClickpostTimelineView } from "@/components/order-rca/ClickpostTimelineView";
import { GrootTimelineView } from "@/components/order-rca/GrootTimelineView";
import { GROOT_CALLOUT } from "@/components/order-rca/grootTimelineStyles";
import { ShippingDetailsPanel } from "@/components/order-rca/ShippingDetailsPanel";
import { cn } from "@/lib/cn";
import type { OrderRcaOperations } from "@/lib/orderRca";

function resolveLastMileMode(operations?: OrderRcaOperations | null): "groot" | "clickpost" | "none" {
  const mode = operations?.last_mile_mode;
  if (mode === "groot" || mode === "clickpost" || mode === "none") return mode;
  const grootEvents = operations?.groot_events ?? [];
  const clickpostEvents = operations?.clickpost_events ?? [];
  if (operations?.groot_empty === false && grootEvents.length > 0) return "groot";
  if (operations?.clickpost_empty === false && clickpostEvents.length > 0) return "clickpost";
  return "none";
}

export function LastMilePanel({ operations }: { operations?: OrderRcaOperations | null }) {
  const mode = resolveLastMileMode(operations);

  return (
    <div className="space-y-2">
      <ShippingDetailsPanel summary={operations?.shipping_summary} />
      {mode === "clickpost" ? (
        <>
          <p className="text-[13px] font-bold text-slate-900">Courier (ClickPost)</p>
          <ClickpostTimelineView
            events={(operations?.clickpost_events ?? []) as Array<Record<string, unknown>>}
          />
        </>
      ) : null}
      {mode === "groot" ? (
        <>
          <p className="text-[13px] font-bold text-slate-900">Hyperlocal (Groot)</p>
          <GrootTimelineView
            events={(operations?.groot_events ?? []) as Array<Record<string, unknown>>}
          />
        </>
      ) : null}
      {mode === "none" ? (
        <p className={cn(GROOT_CALLOUT, "border-slate-200 bg-slate-50 text-slate-700")}>
          Last-mile tracking unavailable — no hyperlocal Groot timeline and no courier scan data from
          ClickPost (partner may be unsupported or not yet synced). Shipping details above may still
          apply for warehouse / 3PL orders.
        </p>
      ) : null}
    </div>
  );
}
