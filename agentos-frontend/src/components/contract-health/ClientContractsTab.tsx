"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getO2CContractHealthClientTree,
  listBillingClients,
  type ContractHealthClientTree,
} from "@/lib/api";

import { ClientActionBanner } from "./ClientActionBanner";
import { CtvDetailPanel } from "./CtvDetailPanel";
import { GlobalContractsPanel } from "./GlobalContractsPanel";
import { OverlapResolverPanel } from "./OverlapResolverPanel";
import { SiteContractsPanel } from "./SiteContractsPanel";
import { SiteListPanel } from "./SiteListPanel";
import {
  defaultCtvForBucket,
  findBucketForCtv,
  GLOBAL_BUCKET_ID,
  globalNeedsAttention,
  nextOverlapSiteId,
  siteNeedsAttention,
  sortSitesForList,
} from "./siteUtils";
import { collectCtvIdsFromTree, resolveCtvSelection } from "./treeUtils";

type Props = {
  periodStart: string;
  periodEnd: string;
  initialClientId?: string;
  initialCtvId?: string;
};

function treeContextForCtv(tree: ContractHealthClientTree, ctvId: string) {
  if (tree.global_terms.some((t) => t.contract_terms_version_id === ctvId)) {
    return {
      isGlobalBucket: true,
      siteName: null as string | null,
      siteOverlap: false,
      pickerCtvId: null as string | null,
    };
  }
  for (const site of tree.sites) {
    if (site.terms.some((t) => t.contract_terms_version_id === ctvId)) {
      return {
        isGlobalBucket: false,
        siteName: site.site_name,
        siteOverlap: site.overlap_conflict,
        pickerCtvId: site.picker_ctv_id,
      };
    }
  }
  return { isGlobalBucket: false, siteName: null, siteOverlap: false, pickerCtvId: null };
}

function resolveBucketSelection(
  tree: ContractHealthClientTree,
  preferredBucket: string | undefined,
  preferredCtv: string | undefined
): string {
  const fromCtv = preferredCtv ? findBucketForCtv(tree, preferredCtv) : null;
  if (fromCtv) return fromCtv;
  if (preferredBucket) {
    if (preferredBucket === GLOBAL_BUCKET_ID && tree.global_terms.length > 0) {
      return GLOBAL_BUCKET_ID;
    }
    if (tree.sites.some((s) => s.service_site_id === preferredBucket)) {
      return preferredBucket;
    }
  }
  const overlapSite = tree.sites.find((s) => s.overlap_conflict);
  if (overlapSite) return overlapSite.service_site_id;
  if (tree.sites[0]) return tree.sites[0].service_site_id;
  if (tree.global_terms.length > 0) return GLOBAL_BUCKET_ID;
  return "";
}

export function ClientContractsTab({ periodStart, periodEnd, initialClientId, initialCtvId }: Props) {
  const [clientQuery, setClientQuery] = useState("");
  const [clients, setClients] = useState<{ id: string; name: string }[]>([]);
  const [clientId, setClientId] = useState(initialClientId ?? "");
  const [tree, setTree] = useState<ContractHealthClientTree | null>(null);
  const [selectedBucketId, setSelectedBucketId] = useState("");
  const [selectedCtvId, setSelectedCtvId] = useState(initialCtvId ?? "");
  const [siteQuery, setSiteQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [clientLoadError, setClientLoadError] = useState("");
  const [treeError, setTreeError] = useState("");
  const [attentionOnly, setAttentionOnly] = useState(false);
  const pendingCtvRef = useRef(initialCtvId ?? "");
  const pendingBucketRef = useRef("");
  const selectedCtvRef = useRef(selectedCtvId);
  const selectedBucketRef = useRef(selectedBucketId);
  selectedCtvRef.current = selectedCtvId;
  selectedBucketRef.current = selectedBucketId;

  useEffect(() => {
    if (initialClientId) setClientId(initialClientId);
    if (initialCtvId) {
      setSelectedCtvId(initialCtvId);
      pendingCtvRef.current = initialCtvId;
    }
  }, [initialClientId, initialCtvId]);

  const clientOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of clients) map.set(c.id, c.name);
    if (clientId && tree?.client_name) map.set(clientId, tree.client_name);
    else if (clientId && !map.has(clientId)) map.set(clientId, "Selected client");
    return Array.from(map.entries())
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [clients, clientId, tree?.client_name]);

  const loadClients = useCallback(async () => {
    setClientLoadError("");
    try {
      const out = await listBillingClients(clientQuery, 80);
      setClients(out.items ?? []);
    } catch (e) {
      setClients([]);
      setClientLoadError(e instanceof Error ? e.message : "Failed to load clients");
    }
  }, [clientQuery]);

  useEffect(() => {
    void loadClients();
  }, [loadClients]);

  const loadTree = useCallback(async () => {
    if (!clientId) {
      setTree(null);
      return;
    }
    if (!periodStart || !periodEnd || periodStart > periodEnd) {
      setTreeError("Invalid billing period");
      return;
    }
    setLoading(true);
    setTreeError("");
    try {
      const t = await getO2CContractHealthClientTree(clientId, periodStart, periodEnd);
      setTree(t);
      const preferCtv = pendingCtvRef.current || initialCtvId;
      const nextCtv = resolveCtvSelection(t, preferCtv);
      const nextBucket = resolveBucketSelection(t, pendingBucketRef.current, nextCtv);
      setSelectedBucketId(nextBucket);
      setSelectedCtvId(nextCtv);
      pendingCtvRef.current = "";
      pendingBucketRef.current = "";
    } catch (e) {
      setTreeError(e instanceof Error ? e.message : "Failed to load client tree");
      setTree(null);
    } finally {
      setLoading(false);
    }
  }, [clientId, periodStart, periodEnd, initialCtvId]);

  useEffect(() => {
    void loadTree();
  }, [loadTree]);

  const sortedSites = useMemo(() => (tree ? sortSitesForList(tree.sites) : []), [tree]);

  const filteredSites = useMemo(() => {
    const q = siteQuery.trim().toLowerCase();
    return sortedSites.filter((site) => {
      if (attentionOnly && !siteNeedsAttention(site)) return false;
      if (q && !site.site_name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [sortedSites, siteQuery, attentionOnly]);

  const showGlobalInList =
    tree &&
    tree.global_terms.length > 0 &&
    (!attentionOnly || globalNeedsAttention(tree));

  const selectedSite = useMemo(() => {
    if (!tree || selectedBucketId === GLOBAL_BUCKET_ID) return null;
    return tree.sites.find((s) => s.service_site_id === selectedBucketId) ?? null;
  }, [tree, selectedBucketId]);

  useEffect(() => {
    if (!tree || loading) return;
    const visibleBuckets = new Set(filteredSites.map((s) => s.service_site_id));
    if (showGlobalInList) visibleBuckets.add(GLOBAL_BUCKET_ID);
    if (selectedBucketId && visibleBuckets.has(selectedBucketId)) return;
    const next =
      filteredSites[0]?.service_site_id ??
      (showGlobalInList ? GLOBAL_BUCKET_ID : "");
    if (next && next !== selectedBucketId) {
      setSelectedBucketId(next);
      const ctv = defaultCtvForBucket(tree, next);
      if (ctv) setSelectedCtvId(ctv);
    }
  }, [tree, loading, filteredSites, showGlobalInList, selectedBucketId, attentionOnly, siteQuery]);

  const ctx = tree && selectedCtvId ? treeContextForCtv(tree, selectedCtvId) : null;
  const allCtvIds = useMemo(() => collectCtvIdsFromTree(tree), [tree]);

  const selectBucket = useCallback(
    (bucketId: string) => {
      if (!tree) return;
      setSelectedBucketId(bucketId);
      const currentBucket = findBucketForCtv(tree, selectedCtvId);
      if (currentBucket === bucketId && selectedCtvId) return;
      const next = defaultCtvForBucket(tree, bucketId);
      if (next) setSelectedCtvId(next);
    },
    [tree, selectedCtvId]
  );

  const selectCtv = useCallback(
    (ctvId: string) => {
      if (!tree) return;
      setSelectedCtvId(ctvId);
      const bucket = findBucketForCtv(tree, ctvId);
      if (bucket) setSelectedBucketId(bucket);
    },
    [tree]
  );

  const overlapSiteCount = useMemo(
    () => (tree ? tree.sites.filter((s) => s.overlap_conflict).length : 0),
    [tree]
  );

  const nextOverlapSiteName = useMemo(() => {
    if (!tree || !selectedSite?.overlap_conflict) return null;
    const nextId = nextOverlapSiteId(tree.sites, selectedSite.service_site_id);
    return tree.sites.find((s) => s.service_site_id === nextId)?.site_name ?? null;
  }, [tree, selectedSite]);

  const handleOverlapResolved = useCallback(
    (finishedSiteId: string) => {
      if (!tree) return;
      const nextId = nextOverlapSiteId(tree.sites, finishedSiteId);
      pendingBucketRef.current = nextId ?? selectedBucketRef.current;
      pendingCtvRef.current = "";
      void loadTree();
    },
    [tree, loadTree]
  );

  const startFixOverlaps = useCallback(() => {
    if (!tree) return;
    setAttentionOnly(true);
    const first = sortSitesForList(tree.sites).find((s) => s.overlap_conflict);
    if (first) selectBucket(first.service_site_id);
  }, [tree, selectBucket]);

  const startFixDataIssues = useCallback(() => {
    setAttentionOnly(true);
  }, []);

  return (
    <div className="space-y-3">

      <div className="flex gap-2 flex-wrap items-center">
        <input
          value={clientQuery}
          onChange={(e) => setClientQuery(e.target.value)}
          placeholder="Search clients"
          className="flex-1 min-w-[160px] px-3 py-2 rounded-lg border border-[var(--border)] text-sm bg-[var(--bg-elev)]"
        />
        <select
          value={clientId}
          onChange={(e) => {
            setClientId(e.target.value);
            setSelectedCtvId("");
            setSelectedBucketId("");
            setSiteQuery("");
            pendingCtvRef.current = "";
            pendingBucketRef.current = "";
          }}
          className="min-w-[220px] px-2 py-2 rounded-lg border border-[var(--border)] text-sm bg-[var(--bg-elev)]"
        >
          <option value="">Select client</option>
          {clientOptions.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>

      {tree?.client_name && (
        <>
          <p className="text-sm font-semibold">
            {tree.client_name}
            <span className="text-[var(--text-muted)] font-normal text-xs ml-2">
              Billing month: {periodStart} → {periodEnd}
              {tree.sites.length > 0 && (
                <span className="ml-2">
                  · {tree.sites.length} site{tree.sites.length !== 1 ? "s" : ""}
                  {overlapSiteCount > 0 && (
                    <span className="text-red-600"> · {overlapSiteCount} blocked</span>
                  )}
                </span>
              )}
            </span>
          </p>
          <ClientActionBanner
            tree={tree}
            periodStart={periodStart}
            periodEnd={periodEnd}
            onFixOverlaps={startFixOverlaps}
            onFixDataIssues={startFixDataIssues}
          />
        </>
      )}

      {clientLoadError && <p className="text-sm text-amber-600">{clientLoadError}</p>}
      {treeError && <p className="text-sm text-red-500">{treeError}</p>}
      {loading && <p className="text-sm text-[var(--text-muted)]">Loading client…</p>}

      {tree && !loading && (
        <div className="grid grid-cols-1 xl:grid-cols-[minmax(200px,240px)_minmax(260px,320px)_minmax(0,1fr)] gap-3">
          <div className="space-y-1">
            <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] font-semibold xl:hidden">
              1 · Sites
            </p>
            <SiteListPanel
              sites={filteredSites}
              selectedBucketId={selectedBucketId}
              siteQuery={siteQuery}
              onSiteQueryChange={setSiteQuery}
              attentionOnly={attentionOnly}
              onAttentionOnlyChange={setAttentionOnly}
              hasGlobal={tree.global_terms.length > 0}
              globalTermCount={tree.global_terms.length}
              globalNeedsAttention={globalNeedsAttention(tree)}
              onSelectBucket={selectBucket}
              totalSiteCount={tree.sites.length}
            />
          </div>

          <div className="space-y-1">
            <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] font-semibold xl:hidden">
              2 · Contracts
            </p>
            {selectedBucketId === GLOBAL_BUCKET_ID ? (
              <GlobalContractsPanel
                terms={tree.global_terms}
                selectedCtvId={selectedCtvId}
                onSelectCtv={selectCtv}
              />
            ) : selectedSite?.overlap_conflict ? (
              <OverlapResolverPanel
                site={selectedSite}
                periodStart={periodStart}
                periodEnd={periodEnd}
                selectedCtvId={selectedCtvId}
                onSelectCtv={selectCtv}
                onResolved={handleOverlapResolved}
                nextOverlapSiteName={nextOverlapSiteName}
              />
            ) : selectedSite ? (
              <SiteContractsPanel
                site={selectedSite}
                periodStart={periodStart}
                periodEnd={periodEnd}
                selectedCtvId={selectedCtvId}
                onSelectCtv={selectCtv}
              />
            ) : (
              <div className="border border-[var(--border)] rounded-xl p-6 text-center text-sm text-[var(--text-muted)] min-h-[200px] flex items-center justify-center">
                Select a site to see its contracts.
              </div>
            )}
          </div>

          <div className="space-y-1 xl:sticky xl:top-4 xl:self-start">
            <p className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] font-semibold xl:hidden">
              3 · Details
            </p>
            {selectedCtvId && allCtvIds.has(selectedCtvId) && ctx ? (
              <CtvDetailPanel
                key={`${selectedCtvId}-${periodStart}-${periodEnd}`}
                ctvId={selectedCtvId}
                periodStart={periodStart}
                periodEnd={periodEnd}
                context={ctx}
                site={selectedSite}
                onUpdated={() => {
                  pendingCtvRef.current = selectedCtvRef.current;
                  pendingBucketRef.current = selectedBucketRef.current;
                  void loadTree();
                }}
                onSelectSibling={(id) => {
                  pendingCtvRef.current = id;
                  const bucket = findBucketForCtv(tree, id);
                  if (bucket) pendingBucketRef.current = bucket;
                  setSelectedCtvId(id);
                  if (bucket) setSelectedBucketId(bucket);
                }}
              />
            ) : (
              <div className="border border-[var(--border)] rounded-xl p-6 text-center text-sm text-[var(--text-muted)] min-h-[200px] flex items-center justify-center">
                Select a contract to edit rates and dates.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
