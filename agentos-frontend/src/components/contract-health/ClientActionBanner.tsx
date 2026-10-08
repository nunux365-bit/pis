"use client";

import Link from "next/link";

import type { ContractHealthClientTree, ContractHealthMisReadiness } from "@/lib/api";

type Props = {
  tree: ContractHealthClientTree;
  periodStart: string;
  periodEnd: string;
  onFixOverlaps: () => void;
  onFixDataIssues: () => void;
};

function readinessFromTree(tree: ContractHealthClientTree): ContractHealthMisReadiness {
  if (tree.mis_readiness) return tree.mis_readiness;
  const overlapSites = tree.sites.filter((s) => s.overlap_conflict);
  return {
    ready: overlapSites.length === 0,
    billable_site_count: tree.sites.filter((s) => s.terms.length > 0).length,
    blocker_count: overlapSites.length,
    warning_count: 0,
    blockers: overlapSites.map((s) => ({
      code: "overlap",
      service_site_id: s.service_site_id,
      site_name: s.site_name,
      message: "Overlap",
    })),
    warnings: [],
  };
}

export function ClientActionBanner({
  tree,
  periodStart,
  periodEnd,
  onFixOverlaps,
  onFixDataIssues,
}: Props) {
  const readiness = readinessFromTree(tree);
  const overlapBlockers = readiness.blockers.filter((b) => b.code === "overlap");
  const otherBlockers = readiness.blockers.filter((b) => b.code !== "overlap");
  const warnings = readiness.warnings ?? [];

  if (readiness.ready && warnings.length === 0) {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2.5 flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-emerald-900">
          <strong>Ready for MIS</strong> — {readiness.billable_site_count} billing site
          {readiness.billable_site_count !== 1 ? "s" : ""} have a clear contract for {periodStart}–{periodEnd}.
        </p>
        <Link
          href="/o2c/ohc-mis"
          className="shrink-0 px-3 py-1.5 rounded-lg bg-emerald-700 text-white text-xs font-semibold hover:bg-emerald-800"
        >
          Open MIS →
        </Link>
      </div>
    );
  }

  if (readiness.ready && warnings.length > 0) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-amber-950">
            <strong>Can run MIS</strong>, but {warnings.length} site
            {warnings.length !== 1 ? "s" : ""} have rate/mapping gaps — fix to avoid billing errors.
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onFixDataIssues}
              className="px-3 py-1.5 rounded-lg border border-amber-700 text-amber-900 text-xs font-semibold hover:bg-amber-100"
            >
              Review data →
            </button>
            <Link
              href="/o2c/ohc-mis"
              className="px-3 py-1.5 rounded-lg bg-emerald-700 text-white text-xs font-semibold hover:bg-emerald-800"
            >
              Open MIS →
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 space-y-2">
      <p className="text-sm text-red-950 font-medium">
        MIS will fail or skip sites — {readiness.blocker_count} blocker
        {readiness.blocker_count !== 1 ? "s" : ""} for {periodStart}–{periodEnd}
      </p>
      {overlapBlockers.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-red-900">
            {overlapBlockers.length} site{overlapBlockers.length !== 1 ? "s" : ""}: duplicate approved contracts
            (expire one per site).
          </p>
          <button
            type="button"
            onClick={onFixOverlaps}
            className="shrink-0 px-3 py-1.5 rounded-lg bg-red-700 text-white text-xs font-semibold hover:bg-red-800"
          >
            Fix overlaps ({overlapBlockers.length}) →
          </button>
        </div>
      )}
      {otherBlockers.length > 0 && (
        <ul className="text-xs text-red-900 list-disc pl-4">
          {otherBlockers.slice(0, 5).map((b) => (
            <li key={b.service_site_id}>{b.site_name}: {b.message}</li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && overlapBlockers.length === 0 && (
        <button
          type="button"
          onClick={onFixDataIssues}
          className="text-xs font-semibold text-amber-900 underline"
        >
          Show sites with data issues →
        </button>
      )}
    </div>
  );
}
