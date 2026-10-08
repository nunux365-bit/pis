"use client";

import type { YserPoGroupTaxHint } from "@/lib/procurementYserPoGroupTax";
import { yserPoGroupTaxBannerContent } from "@/lib/procurementYserPoGroupTax";

type Props = {
  hint: YserPoGroupTaxHint;
};

/** Info banner for grouped YSER PO lines — which tax SAP will receive. */
export function YserPoGroupTaxBanner({ hint }: Props) {
  const { title, body } = yserPoGroupTaxBannerContent(hint);
  const tone = hint.hasConflict
    ? "border-amber-500/40 bg-amber-500/10"
    : "border-[var(--accent-blue)]/30 bg-[var(--accent-blue)]/5";

  return (
    <div
      role="note"
      className={`mb-3 rounded-lg border px-3 py-2.5 ${tone}`}
    >
      <p className="text-[12px] font-semibold leading-snug text-[var(--text-primary)]">{title}</p>
      <p className="mt-1 text-[11px] leading-relaxed text-[var(--text-secondary)]">{body}</p>
    </div>
  );
}
