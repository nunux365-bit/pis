"use client";

import { useEffect, useMemo, useState } from "react";

import { setO2CContractReviewStatus } from "@/lib/api";

import { ConfirmDialog } from "./ConfirmDialog";
import { formatDateRange } from "./statusUi";
import type { ClientTreeSite } from "./siteUtils";
import { shortCtvTitle, suggestOverlapResolution, type OverlapSuggestion } from "./overlapUtils";

type Props = {
  site: ClientTreeSite;
  periodStart: string;
  periodEnd: string;
  selectedCtvId: string;
  onSelectCtv: (id: string) => void;
  onResolved: (finishedSiteId: string) => void;
  nextOverlapSiteName?: string | null;
};

function CompareCard({
  label,
  tone,
  ctv,
  reasons,
  selected,
  onSelect,
}: {
  label: string;
  tone: "keep" | "expire";
  ctv: OverlapSuggestion["keep"];
  reasons: string[];
  selected: boolean;
  onSelect: () => void;
}) {
  const border =
    tone === "keep"
      ? selected
        ? "border-emerald-500 ring-1 ring-emerald-500 bg-emerald-50/80"
        : "border-emerald-200 hover:bg-emerald-50/50"
      : selected
        ? "border-red-400 ring-1 ring-red-400 bg-red-50/50"
        : "border-red-200 hover:bg-red-50/30";

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left p-2.5 rounded-lg border transition-colors ${border}`}
    >
      <div className="text-[10px] font-bold uppercase tracking-wide mb-1">
        {tone === "keep" ? "✓ Keep for MIS" : "✕ Expire duplicate"}
      </div>
      <div className="text-xs font-semibold leading-snug">{shortCtvTitle(ctv)}</div>
      <div className="text-[10px] text-[var(--text-muted)] mt-1">
        {formatDateRange(ctv.effective_from, ctv.effective_to)}
      </div>
      {reasons.length > 0 && (
        <p className="text-[10px] text-[var(--text-secondary)] mt-1">{reasons.join(" · ")}</p>
      )}
    </button>
  );
}

export function OverlapResolverPanel({
  site,
  periodStart,
  periodEnd,
  selectedCtvId,
  onSelectCtv,
  onResolved,
  nextOverlapSiteName,
}: Props) {
  const suggestion = useMemo(() => suggestOverlapResolution(site), [site]);
  const [swapped, setSwapped] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const keep = suggestion
    ? swapped
      ? suggestion.expire
      : suggestion.keep
    : null;
  const expire = suggestion
    ? swapped
      ? suggestion.keep
      : suggestion.expire
    : null;

  useEffect(() => {
    setSwapped(false);
  }, [site.service_site_id]);

  useEffect(() => {
    if (keep && selectedCtvId !== keep.contract_terms_version_id) {
      onSelectCtv(keep.contract_terms_version_id);
    }
  }, [keep?.contract_terms_version_id, site.service_site_id]);

  if (!suggestion || !keep || !expire) {
    return (
      <div className="border border-[var(--border)] rounded-xl p-4 text-sm text-[var(--text-muted)]">
        Overlap detected but could not suggest keep/expire. Pick a contract manually.
      </div>
    );
  }

  const keepReasons = swapped ? suggestion.expireReasons : suggestion.keepReasons;
  const expireReasons = swapped ? suggestion.keepReasons : suggestion.expireReasons;

  async function doExpire() {
    setBusy(true);
    setError("");
    try {
      await setO2CContractReviewStatus({
        contract_terms_version_id: expire!.contract_terms_version_id,
        status: "expired",
        period_start: periodStart,
        period_end: periodEnd,
      });
      setConfirmOpen(false);
      onResolved(site.service_site_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Expire failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col border border-red-200 rounded-xl bg-[var(--bg-card)] overflow-hidden min-h-[280px] max-h-[75vh]">
      <ConfirmDialog
        open={confirmOpen}
        title="Expire duplicate contract?"
        message={`Expire:\n${expire.title ?? "Untitled"}\n\nKeep for billing:\n${keep.title ?? "Untitled"}\n\nMIS for ${periodStart}–${periodEnd} will use the kept contract. This cannot be undone in the UI.`}
        confirmLabel="Expire duplicate"
        variant="warning"
        busy={busy}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() => void doExpire()}
      />

      <div className="px-3 py-2.5 border-b border-red-100 bg-red-50 shrink-0">
        <h3 className="text-sm font-semibold text-red-950 leading-snug">{site.site_name}</h3>
        <p className="text-[11px] text-red-800 mt-1 leading-relaxed">
          Two approved contracts cover this month — MIS is blocked. Review the suggestion below, then one click
          to expire the duplicate.
        </p>
        {suggestion.confidence === "low" && (
          <p className="text-[11px] text-amber-900 mt-1 font-medium">
            Low confidence — confirm with commercial before expiring.
          </p>
        )}
      </div>

      <div className="overflow-y-auto flex-1 p-2 space-y-2">
        {error && <p className="text-xs text-red-600 px-1">{error}</p>}

        <CompareCard
          label="keep"
          tone="keep"
          ctv={keep}
          reasons={keepReasons}
          selected={selectedCtvId === keep.contract_terms_version_id}
          onSelect={() => onSelectCtv(keep.contract_terms_version_id)}
        />
        <CompareCard
          label="expire"
          tone="expire"
          ctv={expire}
          reasons={expireReasons}
          selected={selectedCtvId === expire.contract_terms_version_id}
          onSelect={() => onSelectCtv(expire.contract_terms_version_id)}
        />

        <div className="flex flex-col gap-2 pt-1 px-1">
          <button
            type="button"
            disabled={busy}
            onClick={() => setConfirmOpen(true)}
            className="w-full py-2.5 rounded-lg bg-amber-600 text-white text-sm font-semibold hover:bg-amber-700 disabled:opacity-50"
          >
            Expire duplicate &amp; {nextOverlapSiteName ? "next site" : "done"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setSwapped((s) => !s)}
            className="w-full py-1.5 rounded-lg border border-[var(--border)] text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-elev)]"
          >
            Swap keep / expire
          </button>
        </div>

        {suggestion.remainingApproved > 1 && (
          <p className="text-[10px] text-[var(--text-muted)] px-1">
            After this, {suggestion.remainingApproved - 1} more approved overlap
            {suggestion.remainingApproved - 1 !== 1 ? "s" : ""} may remain on this site — repeat if needed.
          </p>
        )}
      </div>
    </div>
  );
}
