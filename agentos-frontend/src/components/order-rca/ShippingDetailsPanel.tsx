"use client";

import { GROOT_CALLOUT } from "@/components/order-rca/grootTimelineStyles";
import { cn, isSafeHttpUrl } from "@/lib/cn";
import type { OrderRcaShippingSummary } from "@/lib/orderRca";

function Row({ label, value }: { label: string; value?: string | null }) {
  if (!value?.trim()) return null;
  return (
    <div className="flex min-w-0 gap-2 py-1">
      <span className="w-28 shrink-0 text-slate-500">{label}</span>
      <span className="min-w-0 break-all font-medium text-slate-900">{value}</span>
    </div>
  );
}

export function ShippingDetailsPanel({
  summary,
}: {
  summary?: OrderRcaShippingSummary | null;
}) {
  if (!summary) return null;
  const safeTrackingUrl = isSafeHttpUrl(summary.tracking_url) ? summary.tracking_url!.trim() : null;
  const hasAny = Boolean(
    summary.delivery_partners_code ||
      summary.waybill ||
      summary.fe_name ||
      summary.fe_phone ||
      safeTrackingUrl,
  );
  if (!hasAny) return null;

  return (
    <details className={cn(GROOT_CALLOUT, "border-slate-200 bg-slate-50 text-slate-800")}>
      <summary className="cursor-pointer select-none font-semibold text-slate-900">
        Shipping details
      </summary>
      <div className="mt-2 border-t border-slate-200 pt-2">
        <Row label="Partner" value={summary.delivery_partners_code} />
        <Row label="Waybill" value={summary.waybill} />
        <Row label="Field exec" value={summary.fe_name} />
        <Row label="FE phone" value={summary.fe_phone} />
        {safeTrackingUrl ? (
          <div className="flex min-w-0 gap-2 py-1">
            <span className="w-28 shrink-0 text-slate-500">Tracking</span>
            <a
              href={safeTrackingUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="min-w-0 break-all text-blue-700 underline"
            >
              Open tracking
            </a>
          </div>
        ) : null}
      </div>
    </details>
  );
}
