"use client";

import type { ClientTreeSite } from "./siteUtils";
import { GLOBAL_BUCKET_ID } from "./siteUtils";

type Props = {
  sites: ClientTreeSite[];
  selectedBucketId: string;
  siteQuery: string;
  onSiteQueryChange: (q: string) => void;
  attentionOnly: boolean;
  onAttentionOnlyChange: (v: boolean) => void;
  hasGlobal: boolean;
  globalTermCount: number;
  globalNeedsAttention: boolean;
  onSelectBucket: (bucketId: string) => void;
  totalSiteCount: number;
};

export function SiteListPanel({
  sites,
  selectedBucketId,
  siteQuery,
  onSiteQueryChange,
  attentionOnly,
  onAttentionOnlyChange,
  hasGlobal,
  globalTermCount,
  globalNeedsAttention,
  onSelectBucket,
  totalSiteCount,
}: Props) {
  return (
    <div className="flex flex-col border border-[var(--border)] rounded-xl bg-[var(--bg-card)] overflow-hidden min-h-[280px] max-h-[75vh]">
      <div className="p-2 border-b border-[var(--border)] bg-[var(--bg-elev)] space-y-2 shrink-0">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">Sites</h3>
          <span className="text-[10px] text-[var(--text-muted)] tabular-nums">
            {sites.length}
            {attentionOnly || siteQuery ? ` / ${totalSiteCount}` : ""}
          </span>
        </div>
        <input
          type="search"
          value={siteQuery}
          onChange={(e) => onSiteQueryChange(e.target.value)}
          placeholder="Search sites…"
          className="w-full px-2.5 py-1.5 rounded-lg border border-[var(--border)] text-sm bg-[var(--bg-card)]"
        />
        <label className="flex items-center gap-2 text-[11px] text-[var(--text-secondary)] cursor-pointer">
          <input
            type="checkbox"
            checked={attentionOnly}
            onChange={(e) => onAttentionOnlyChange(e.target.checked)}
          />
          Needs attention only
        </label>
      </div>

      <div className="overflow-y-auto flex-1 p-1">
        {hasGlobal && (
          <button
            type="button"
            onClick={() => onSelectBucket(GLOBAL_BUCKET_ID)}
            className={`w-full text-left px-2.5 py-2 rounded-lg mb-0.5 border transition-colors ${
              selectedBucketId === GLOBAL_BUCKET_ID
                ? "border-slate-400 bg-slate-100 ring-1 ring-slate-400"
                : "border-transparent hover:bg-[var(--bg-elev)]"
            }`}
          >
            <div className="text-sm font-medium">Shared (all sites)</div>
            <div className="text-[10px] text-[var(--text-muted)] mt-0.5">
              {globalTermCount} contract{globalTermCount !== 1 ? "s" : ""} · global rate lines
            </div>
            {globalNeedsAttention && (
              <span className="inline-block mt-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 text-[10px]">
                needs review
              </span>
            )}
          </button>
        )}

        {sites.length === 0 ? (
          <p className="text-xs text-[var(--text-muted)] px-2 py-4 text-center">
            {siteQuery || attentionOnly ? "No sites match filters." : "No sites for this client."}
          </p>
        ) : (
          sites.map((site) => (
            <SiteListRow
              key={site.service_site_id}
              site={site}
              selected={selectedBucketId === site.service_site_id}
              onSelect={() => onSelectBucket(site.service_site_id)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function SiteListRow({
  site,
  selected,
  onSelect,
}: {
  site: ClientTreeSite;
  selected: boolean;
  onSelect: () => void;
}) {
  const termCount = site.terms.length;
  const issues: string[] = [];
  if (site.overlap_conflict) issues.push("overlap");
  if (!site.picker_ctv_id && termCount > 0 && !site.overlap_conflict) issues.push("no MIS pick");
  const dataIssues = site.terms.filter((t) => t.null_rate_count > 0 || t.null_site_count > 0).length;
  if (dataIssues > 0) issues.push(`${dataIssues} data`);

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left px-2.5 py-2 rounded-lg mb-0.5 border transition-colors ${
        selected
          ? "border-[var(--accent-green)] bg-[rgba(21,163,115,0.08)] ring-1 ring-[var(--accent-green)]"
          : "border-transparent hover:bg-[var(--bg-elev)]"
      }`}
    >
      <div className="text-sm font-medium leading-snug line-clamp-2">{site.site_name}</div>
      <div className="text-[10px] text-[var(--text-muted)] mt-0.5">
        {termCount} contract{termCount !== 1 ? "s" : ""}
        {site.picker_ctv_id && !site.overlap_conflict && (
          <span className="text-emerald-700"> · MIS ready</span>
        )}
      </div>
      {issues.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {issues.map((tag) => (
            <span
              key={tag}
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${
                tag === "overlap"
                  ? "bg-red-50 text-red-800 border-red-200"
                  : "bg-amber-50 text-amber-800 border-amber-200"
              }`}
            >
              {tag}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}
